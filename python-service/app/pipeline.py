"""End-to-end analysis pipeline (short-term, free-data)."""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.db import (
    get_latest_portfolio,
    save_news,
    save_prices,
    save_recommendation,
    get_active_watchlist,
)
from app.gates import apply_confidence_gate, should_notify
from app.indicators import compute_daily_context_batch, compute_indicators_batch
from app.llm import run_decision_agent, run_news_agent, run_technical_agent
from app.news import fetch_news_batch, headlines_by_ticker
from app.news_watch import mark_analyze_ran
from app.notify import format_recommendation_message, notify_recommendation
from app.risk import apply_risk_rules
from app.signals import market_regime, score_universe, select_candidates
from app.trades import portfolio_with_marks

logger = logging.getLogger(__name__)


def run_analysis(
    symbols: list[str] | None = None,
    portfolio: dict[str, Any] | None = None,
    *,
    send_notification: bool = True,
    period: str | None = None,
    interval: str | None = None,
    trigger: str = "manual",
) -> dict[str, Any]:
    settings = get_settings()
    period = period or settings.analyze_period
    interval = interval or settings.analyze_interval
    symbols = [s.upper() for s in (symbols or get_active_watchlist())]
    if portfolio is None:
        stored = get_latest_portfolio()
        portfolio = {"cash": stored["cash"], "positions": stored.get("positions") or {}}

    # Include open positions so Decision Agent can monitor / suggest SELL
    try:
        marked = portfolio_with_marks()
    except Exception:  # noqa: BLE001
        marked = {"cash": portfolio["cash"], "positions": portfolio.get("positions") or {}}

    # Ensure held tickers are always analyzed
    held = list((portfolio.get("positions") or {}).keys())
    for t in held:
        if t not in symbols:
            symbols.append(t)

    logger.info(
        "Pipeline start trigger=%s symbols=%s period=%s interval=%s portfolio=%s",
        trigger,
        symbols,
        period,
        interval,
        portfolio,
    )

    ind_result = compute_indicators_batch(symbols, period=period, interval=interval)
    indicators = ind_result["indicators"]
    if not indicators:
        raise RuntimeError(f"No indicators computed: {ind_result.get('errors')}")

    # --- Deterministic quant layer: score, regime, and pre-screen candidates ---
    # Daily-timeframe context gives an accurate 200d trend anchor + volume
    # confirmation that the intraday window cannot; blended into each score.
    daily_ctx = compute_daily_context_batch(list(indicators.keys()))
    signals = score_universe(indicators, daily_ctx)
    regime = market_regime()
    held_present = [t for t in held if t in indicators]
    selection = select_candidates(signals, held_present)
    shortlist = [t for t in selection["symbols"] if t in indicators]

    forced_run = trigger in {"manual", "api", "api_force"}
    if not shortlist:
        if settings.signal_skip_llm_when_no_candidates and not forced_run:
            return _record_deterministic_hold(
                symbols=symbols,
                indicators=indicators,
                signals=signals,
                regime=regime,
                marked=marked,
                trigger=trigger,
                ind_result=ind_result,
                send_notification=send_notification,
            )
        # Forced runs still analyze something: fall back to top names by score.
        shortlist = [
            t
            for t, _ in sorted(
                signals.items(), key=lambda kv: float(kv[1].get("score") or 0), reverse=True
            )[: max(1, settings.signal_shortlist_size)]
        ]

    scan_indicators = {t: indicators[t] for t in shortlist if t in indicators}
    scan_signals = {t: signals[t] for t in shortlist if t in signals}

    logger.info(
        "Quant pre-screen: regime=%s candidates=%s shortlist=%s (from %s scanned)",
        regime.get("state"),
        selection.get("buy_candidates"),
        shortlist,
        len(indicators),
    )

    news_raw = fetch_news_batch(shortlist)
    headlines = headlines_by_ticker(news_raw)

    news_summary = run_news_agent(headlines)
    tech_summary = run_technical_agent(scan_indicators)

    context = {
        "portfolio": marked,
        "mandate": (
            "SHORT-TERM + moderately aggressive growth (hours to a few days). "
            "You are CONFIRMING a pre-screened short-list from a deterministic quant engine. "
            "Only BUY names in 'candidates'; pick the single best or HOLD if none is compelling."
        ),
        "open_positions_note": (
            "If positions exist, consider SELL/HOLD for them based on short-term P&L and momentum. "
            "Do not ignore current holdings."
        ),
        "trigger": trigger,
        "market_regime": regime,
        "quant_signals": scan_signals,
        "candidates": selection.get("buy_candidates"),
        "news": news_summary,
        "technical": tech_summary,
        "prices": {t: scan_indicators[t].get("price") for t in scan_indicators},
    }
    decision = run_decision_agent(context)

    prices = {t: float(v["price"]) for t, v in indicators.items() if v.get("price") is not None}
    final = apply_risk_rules(
        decision,
        portfolio,
        prices,
        max_position_pct=settings.max_position_pct,
        min_cash_pct=settings.min_cash_pct,
    )
    final = apply_confidence_gate(final)
    final = _reconcile_with_regime(final, regime, signals)
    if "ranked" in decision:
        final["ranked"] = decision.get("ranked")
    final["market_regime"] = regime
    chosen = str(final.get("ticker") or "").upper()
    if chosen in signals:
        final["quant_signal"] = signals[chosen]

    prices_saved = save_prices(indicators)
    news_saved = save_news(news_raw)
    rec_id = save_recommendation(
        final,
        extras={
            "news_summary": news_summary,
            "technical_summary": tech_summary,
            "decision_raw": decision,
            "portfolio_snapshot": marked,
            "trigger": trigger,
            "market_regime": regime,
            "quant_signals": scan_signals,
            "candidates": selection.get("buy_candidates"),
        },
    )
    final["recommendation_id"] = rec_id
    confirm_base = (settings.hisaab_base_url or settings.public_base_url).rstrip("/")
    confirm_path = "/trades" if settings.hisaab_base_url else "/desk"
    final["desk_url"] = f"{confirm_base}{confirm_path}?id={rec_id}"

    notify_ok, notify_reason = should_notify(final)
    message = format_recommendation_message(final, markdown=False, recommendation_id=rec_id)
    notify_result = None
    if send_notification and notify_ok:
        notify_result = notify_recommendation(final, recommendation_id=rec_id)
        message = notify_result.get("message") or message
    elif send_notification and not notify_ok:
        notify_result = {"ok": False, "skipped": True, "reason": notify_reason}
        logger.info(
            "Notification skipped (%s): %s %s conf=%s",
            notify_reason,
            final.get("action"),
            final.get("ticker"),
            final.get("confidence"),
        )

    try:
        mark_analyze_ran()
    except Exception:  # noqa: BLE001
        logger.warning("Could not mark analyze timestamp", exc_info=True)

    return {
        "symbols": symbols,
        "trigger": trigger,
        "indicators": indicators,
        "indicator_errors": ind_result.get("errors", {}),
        "market_regime": regime,
        "quant_signals": scan_signals,
        "candidates": selection.get("buy_candidates"),
        "shortlist": shortlist,
        "news": news_raw,
        "news_summary": news_summary,
        "technical_summary": tech_summary,
        "decision_raw": decision,
        "recommendation": final,
        "notification_message": message,
        "notification": notify_result,
        "notify_reason": notify_reason if send_notification else "disabled",
        "mongo": {
            "recommendation_id": rec_id,
            "prices_saved": prices_saved,
            "news_saved": news_saved,
        },
        "portfolio": marked,
    }


def _reconcile_with_regime(
    final: dict[str, Any],
    regime: dict[str, Any],
    signals: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Deterministic safety net: block fresh longs in a risk-off market and veto
    BUYs the quant engine flagged as downtrend (AVOID)."""
    action = str(final.get("action", "HOLD")).upper()
    if action != "BUY":
        return final

    notes = list(final.get("risk_notes") or [])
    ticker = str(final.get("ticker") or "").upper()
    sig = signals.get(ticker) or {}

    blocked = False
    if not regime.get("allow_new_buys", True):
        notes.append(f"Regime {regime.get('state')} — new BUYs blocked; converted BUY → HOLD")
        blocked = True
    elif sig and not sig.get("trend_ok", True):
        notes.append(f"{ticker} in downtrend per quant engine — converted BUY → HOLD")
        blocked = True

    if blocked:
        final["action"] = "HOLD"
        final["investment"] = 0
        final["regime_gated"] = True

    final["risk_notes"] = notes
    return final


def _record_deterministic_hold(
    *,
    symbols: list[str],
    indicators: dict[str, Any],
    signals: dict[str, dict[str, Any]],
    regime: dict[str, Any],
    marked: dict[str, Any],
    trigger: str,
    ind_result: dict[str, Any],
    send_notification: bool,
) -> dict[str, Any]:
    """No candidate cleared the quant bar and nothing is held — record a silent
    HOLD without spending any LLM budget."""
    top = sorted(signals.items(), key=lambda kv: float(kv[1].get("score") or 0), reverse=True)[:3]
    top_note = ", ".join(f"{t} {s.get('score')}" for t, s in top) or "none"
    reasoning = [
        "No ticker cleared the deterministic quant bar",
        f"Top scores: {top_note}",
        f"Market regime: {regime.get('state')}",
    ]
    final = {
        "ticker": None,
        "action": "HOLD",
        "investment": 0,
        "confidence": round(float(top[0][1].get("score")) if top else 0.0),
        "risk": "LOW",
        "time_horizon": "SHORT",
        "expected_return": "—",
        "reasoning": reasoning,
        "risk_notes": [],
        "confidence_gated": False,
        "quant_hold": True,
        "market_regime": regime,
    }

    prices_saved = save_prices(indicators)
    rec_id = save_recommendation(
        final,
        extras={
            "trigger": trigger,
            "market_regime": regime,
            "quant_signals": signals,
            "no_llm": True,
        },
    )
    final["recommendation_id"] = rec_id
    notify_ok, notify_reason = should_notify(final)
    try:
        mark_analyze_ran()
    except Exception:  # noqa: BLE001
        logger.warning("Could not mark analyze timestamp", exc_info=True)

    logger.info(
        "Deterministic HOLD (no LLM): trigger=%s regime=%s top=%s",
        trigger,
        regime.get("state"),
        top_note,
    )
    return {
        "symbols": symbols,
        "trigger": trigger,
        "indicators": indicators,
        "indicator_errors": ind_result.get("errors", {}),
        "market_regime": regime,
        "quant_signals": signals,
        "candidates": [],
        "shortlist": [],
        "news": {},
        "news_summary": {},
        "technical_summary": {},
        "decision_raw": {"skipped": "no_candidates_no_llm"},
        "recommendation": final,
        "notification_message": None,
        "notification": {"ok": False, "skipped": True, "reason": notify_reason},
        "notify_reason": notify_reason if send_notification else "disabled",
        "mongo": {"recommendation_id": rec_id, "prices_saved": prices_saved, "news_saved": 0},
        "portfolio": marked,
        "llm_used": False,
    }
