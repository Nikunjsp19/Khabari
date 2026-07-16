"""End-to-end options analysis pipeline (long calls / long puts)."""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.db import (
    get_active_options_watchlist,
    get_latest_options_portfolio,
    save_news,
    save_options_chain_snapshot,
    save_options_recommendation,
    save_prices,
)
from app.gates import (
    apply_options_chase_gate,
    apply_options_confidence_gate,
    should_notify_options,
)
from app.indicators import compute_indicators_batch
from app.llm import run_options_decision_agent, run_options_news_agent, run_options_technical_agent
from app.news import fetch_news_batch, headlines_by_ticker
from app.news_watch import mark_analyze_ran
from app.notify import format_options_recommendation_message, notify_options_recommendation
from app.options_data import CONTRACT_MULTIPLIER, deep_scan_underlyings, fetch_contract_quote
from app.options_movers import refresh_options_watchlist_from_movers
from app.options_risk import apply_options_risk_rules
from app.options_trades import options_portfolio_with_marks

logger = logging.getLogger(__name__)


def _day_moves_map(
    symbols: list[str],
    movers_meta: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Same-day % vs prior close for chase gating (movers first, Yahoo chart fallback)."""
    out: dict[str, float] = {}
    if movers_meta:
        for row in movers_meta.get("ranked") or []:
            t = str(row.get("ticker") or "").upper()
            if t and row.get("day_pct") is not None:
                try:
                    out[t] = float(row["day_pct"])
                except (TypeError, ValueError):
                    pass

    missing = [s.upper() for s in symbols if s.upper() not in out]
    if not missing:
        return out

    try:
        import httpx
    except ImportError:
        return out

    for ticker in missing:
        try:
            url = (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                f"?interval=1d&range=5d"
            )
            with httpx.Client(timeout=8.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
                resp = client.get(url)
                resp.raise_for_status()
                result = (resp.json().get("chart") or {}).get("result") or []
                if not result:
                    continue
                meta = result[0].get("meta") or {}
                px = meta.get("regularMarketPrice")
                prev = meta.get("previousClose") or meta.get("chartPreviousClose")
                if px is None or not prev:
                    continue
                out[ticker] = round((float(px) / float(prev) - 1.0) * 100.0, 3)
        except Exception:  # noqa: BLE001
            logger.debug("day_move fetch failed for %s", ticker, exc_info=True)
    return out


def _refresh_live_premium(final: dict[str, Any]) -> dict[str, Any]:
    """Re-quote the chosen contract from Yahoo right before we notify / save.

    Option premiums move fast; the mid captured during the deep scan can already
    be stale by the time Gemini finishes. For BUY_TO_OPEN we price at the live
    *ask* (what you typically pay to open), not the mid, so the suggested dollar
    amount is not optimistically low.
    """
    action = str(final.get("action") or "").upper()
    if action not in {"BUY_TO_OPEN", "SELL_TO_CLOSE"}:
        return final
    ticker = str(final.get("ticker") or "").upper()
    right = final.get("right")
    strike = final.get("strike")
    expiry = final.get("expiry")
    if not ticker or not right or strike is None or not expiry:
        return final

    contract = {
        "underlying": ticker,
        "right": right,
        "strike": float(strike),
        "expiry": str(expiry),
        "key": final.get("contract_key"),
        "osi": final.get("osi"),
    }
    live = fetch_contract_quote(str(final.get("osi") or ticker), contract=contract)
    if not live or not live.get("mid"):
        final.setdefault("risk_notes", []).append(
            "Could not refresh live Yahoo quote — premium may be stale"
        )
        return final

    bid = live.get("bid")
    ask = live.get("ask")
    mid = float(live["mid"])
    old = float(final.get("premium") or 0)
    if action == "BUY_TO_OPEN":
        # Prefer ask when available — mid underestimates what you'll pay.
        premium = float(ask) if ask and float(ask) > 0 else mid
        quote_basis = "ask" if ask and float(ask) > 0 else "mid"
    else:
        premium = float(bid) if bid and float(bid) > 0 else mid
        quote_basis = "bid" if bid and float(bid) > 0 else "mid"

    contracts = float(final.get("contracts") or 0)
    final["premium"] = round(premium, 4)
    final["bid"] = bid
    final["ask"] = ask
    final["mid"] = mid
    final["quote_basis"] = quote_basis
    final["quoted_premium_scan"] = old or None
    if contracts >= 1 and premium > 0:
        dollars = round(premium * CONTRACT_MULTIPLIER * contracts, 2)
        final["investment"] = dollars
        if action == "BUY_TO_OPEN":
            final["max_loss"] = dollars
    notes = list(final.get("risk_notes") or [])
    if old and abs(premium - old) / max(old, 1e-6) >= 0.05:
        notes.append(
            f"Live Yahoo refresh: scan mid ${old:.3f} → {quote_basis} ${premium:.3f} "
            f"(bid={bid} ask={ask})"
        )
    final["risk_notes"] = notes
    logger.info(
        "Options premium refresh %s %s %s %s: scan=%.4f live_%s=%.4f bid=%s ask=%s",
        action,
        ticker,
        strike,
        expiry,
        old,
        quote_basis,
        premium,
        bid,
        ask,
    )
    return final


def run_options_analysis(
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

    movers_meta: dict[str, Any] | None = None
    if symbols is None and settings.options_auto_movers:
        try:
            movers_meta = refresh_options_watchlist_from_movers(persist=True)
            logger.info(
                "Options movers refresh: selected=%s top=%s",
                movers_meta.get("selected"),
                [
                    f"{r.get('ticker')} {r.get('day_pct')}%"
                    for r in (movers_meta.get("ranked") or [])[:8]
                ],
            )
        except Exception:  # noqa: BLE001
            logger.exception("Options movers refresh failed; using existing watchlist")
            movers_meta = {"ok": False, "error": "refresh_failed"}

    symbols = [s.upper() for s in (symbols or get_active_options_watchlist())]
    # Prefer mover rank order (score), not alphabetical DB order
    if movers_meta and movers_meta.get("selected"):
        ranked_sel = [str(t).upper() for t in movers_meta["selected"]]
        rest = [t for t in symbols if t not in ranked_sel]
        symbols = ranked_sel + rest
    max_sym = max(3, int(settings.options_analyze_max_symbols))
    if len(symbols) > max_sym:
        logger.info(
            "Options scan capped to top %s underlyings (from %s): %s",
            max_sym,
            len(symbols),
            symbols[:max_sym],
        )
        symbols = symbols[:max_sym]
    if portfolio is None:
        stored = get_latest_options_portfolio()
        portfolio = {"cash": stored["cash"], "positions": stored.get("positions") or {}}

    try:
        marked = options_portfolio_with_marks()
    except Exception:  # noqa: BLE001
        marked = {"cash": portfolio["cash"], "positions": portfolio.get("positions") or {}}

    held_underlyings = [
        str(p.get("underlying", "")).upper()
        for p in (portfolio.get("positions") or {}).values()
        if p.get("underlying")
    ]
    for t in held_underlyings:
        if t and t not in symbols:
            symbols.append(t)

    logger.info(
        "Options pipeline start trigger=%s symbols=%s cash=%s",
        trigger,
        symbols,
        portfolio.get("cash"),
    )

    ind_result = compute_indicators_batch(symbols, period=period, interval=interval)
    indicators = ind_result["indicators"]
    if not indicators:
        raise RuntimeError(f"No indicators computed: {ind_result.get('errors')}")

    spots = {t: float(v["price"]) for t, v in indicators.items() if v.get("price") is not None}
    scan = deep_scan_underlyings(list(indicators.keys()), spots=spots)
    candidates_flat = list(scan.get("ranked") or [])

    # Slim candidates for LLM
    candidates_for_llm = [
        {
            "key": c.get("key"),
            "underlying": c.get("underlying"),
            "right": c.get("right"),
            "strike": c.get("strike"),
            "expiry": c.get("expiry"),
            "dte": c.get("dte"),
            "mid": c.get("mid"),
            "delta": c.get("delta"),
            "iv": c.get("iv"),
            "open_interest": c.get("open_interest"),
            "volume": c.get("volume"),
            "spread_pct": c.get("spread_pct"),
            "scan_score": c.get("scan_score"),
            "osi": c.get("osi"),
            "max_loss_per_contract": c.get("max_loss_per_contract"),
        }
        for c in candidates_flat[: max(1, int(settings.options_max_candidates_for_llm))]
    ]

    news_raw = fetch_news_batch(list(indicators.keys()))
    headlines = headlines_by_ticker(news_raw)
    news_summary = run_options_news_agent(headlines)

    tech_payload = {
        "indicators": {
            t: {
                k: v
                for k, v in vals.items()
                if k
                in {
                    "price",
                    "rsi",
                    "macd",
                    "macd_signal",
                    "ema20",
                    "ema50",
                    "atr",
                    "adx",
                }
            }
            for t, vals in indicators.items()
        },
        "candidates_by_ticker": scan.get("by_ticker") or {},
    }
    tech_summary = run_options_technical_agent(tech_payload)

    open_positions = []
    for key, pos in (marked.get("positions") or {}).items():
        open_positions.append(
            {
                "key": key,
                "underlying": pos.get("underlying"),
                "right": pos.get("right"),
                "strike": pos.get("strike"),
                "expiry": pos.get("expiry"),
                "contracts": pos.get("contracts"),
                "avg_premium": pos.get("avg_premium"),
                "last_premium": pos.get("last_premium"),
                "unrealized_pnl_pct": pos.get("unrealized_pnl_pct"),
            }
        )

    day_moves = _day_moves_map(list(indicators.keys()), movers_meta)
    logger.info(
        "Options day_moves (chase gate ±%.1f%%): %s",
        float(settings.options_max_intraday_chase_pct),
        {k: round(v, 2) for k, v in sorted(day_moves.items())},
    )

    context = {
        "portfolio": {
            "cash": marked.get("cash"),
            "total_value": marked.get("total_value"),
            "positions": open_positions,
        },
        "mandate": (
            "SHORT-TERM long calls/puts only. Deep-validated liquid contracts. "
            "Rank underlyings; take best when score>=60 else HOLD. "
            "After large same-day moves (see day_moves), still ok to suggest but "
            "with chase caution — lower confidence, not free upside."
        ),
        "trigger": trigger,
        "news": news_summary,
        "technical": tech_summary,
        "candidates": candidates_for_llm,
        "scan_errors": scan.get("errors") or {},
        "prices": spots,
        "day_moves": day_moves,
        "chase_limit_pct": float(settings.options_max_intraday_chase_pct),
    }
    decision = run_options_decision_agent(context)

    final = apply_options_risk_rules(
        decision,
        portfolio,
        max_premium_pct=settings.options_max_premium_pct,
        min_cash_pct=settings.options_min_cash_pct,
        candidates=candidates_flat,
    )
    final = apply_options_confidence_gate(final)
    final = apply_options_chase_gate(final, day_moves)
    # Premium moves while Gemini runs — re-quote Yahoo before we notify.
    final = _refresh_live_premium(final)
    if "ranked" in decision:
        final["ranked"] = decision.get("ranked")

    prices_saved = save_prices(indicators)
    news_saved = save_news(news_raw)
    try:
        save_options_chain_snapshot("_scan", scan)
    except Exception:  # noqa: BLE001
        logger.warning("Could not save options chain snapshot", exc_info=True)

    rec_id = save_options_recommendation(
        final,
        extras={
            "news_summary": news_summary,
            "technical_summary": tech_summary,
            "decision_raw": decision,
            "portfolio_snapshot": marked,
            "candidates": candidates_for_llm[:20],
            "scan_errors": scan.get("errors"),
            "trigger": trigger,
            "day_moves": day_moves,
        },
    )
    from app.notify import _confirm_url

    final["recommendation_id"] = rec_id
    final["desk_url"] = _confirm_url(rec_id, tab="options")

    notify_ok, notify_reason = should_notify_options(final)
    message = format_options_recommendation_message(final, markdown=False, recommendation_id=rec_id)
    notify_result = None
    if send_notification and notify_ok:
        notify_result = notify_options_recommendation(final, recommendation_id=rec_id)
        message = notify_result.get("message") or message
    elif send_notification and not notify_ok:
        notify_result = {"ok": False, "skipped": True, "reason": notify_reason}
        logger.info(
            "Options notification skipped (%s): %s %s conf=%s",
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
        "asset_class": "options",
        "indicators": indicators,
        "indicator_errors": ind_result.get("errors", {}),
        "scan": {
            "raw_counts": scan.get("raw_counts"),
            "candidate_counts": {k: len(v) for k, v in (scan.get("by_ticker") or {}).items()},
            "errors": scan.get("errors"),
            "top": candidates_for_llm[:10],
        },
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
        "movers": movers_meta,
    }
