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
from app.indicators import compute_indicators_batch
from app.llm import run_decision_agent, run_news_agent, run_technical_agent
from app.news import fetch_news_batch, headlines_by_ticker
from app.news_watch import mark_analyze_ran
from app.notify import format_recommendation_message, notify_recommendation
from app.risk import apply_risk_rules
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

    news_raw = fetch_news_batch(list(indicators.keys()))
    headlines = headlines_by_ticker(news_raw)

    news_summary = run_news_agent(headlines)
    tech_summary = run_technical_agent(indicators)

    context = {
        "portfolio": marked,
        "mandate": "SHORT-TERM only (hours to a few days). Rank all tickers; pick best or HOLD.",
        "open_positions_note": (
            "If positions exist, consider SELL/HOLD for them based on short-term P&L and momentum. "
            "Do not ignore current holdings."
        ),
        "trigger": trigger,
        "news": news_summary,
        "technical": tech_summary,
        "prices": {t: v.get("price") for t, v in indicators.items()},
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
    if "ranked" in decision:
        final["ranked"] = decision.get("ranked")

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
