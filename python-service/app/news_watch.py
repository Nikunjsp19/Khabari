"""Free news watcher: poll Yahoo headlines and detect new articles (no LLM)."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.news import fetch_news_batch

logger = logging.getLogger(__name__)

_STATE_ID = "news_watch"


def _db():
    from app.db import get_db

    return get_db()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fingerprint_article(ticker: str, article: dict[str, Any]) -> str:
    raw = article.get("uuid") or f"{ticker}|{article.get('title', '')}|{article.get('url', '')}"
    return hashlib.sha1(str(raw).encode("utf-8")).hexdigest()


def load_watch_state() -> dict[str, Any]:
    doc = _db().meta.find_one({"_id": _STATE_ID}) or {}
    return {
        "seen": dict(doc.get("seen") or {}),
        "last_scan_at": doc.get("last_scan_at"),
        "last_trigger_at": doc.get("last_trigger_at"),
        "last_analyze_at": doc.get("last_analyze_at"),
    }


def save_watch_state(
    *,
    seen: dict[str, list[str]] | None = None,
    last_trigger_at: datetime | None = None,
    last_analyze_at: datetime | None = None,
) -> None:
    patch: dict[str, Any] = {"updated_at": _now(), "last_scan_at": _now()}
    if seen is not None:
        patch["seen"] = seen
    if last_trigger_at is not None:
        patch["last_trigger_at"] = last_trigger_at
    if last_analyze_at is not None:
        patch["last_analyze_at"] = last_analyze_at
    _db().meta.update_one({"_id": _STATE_ID}, {"$set": patch}, upsert=True)


def mark_analyze_ran() -> None:
    save_watch_state(last_analyze_at=_now())


def minutes_since(dt: datetime | None) -> float | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (_now() - dt).total_seconds() / 60.0


def scan_for_new_news(symbols: list[str] | None = None) -> dict[str, Any]:
    """
    Fetch headlines for the watchlist and compare fingerprints to prior scan.
    First scan seeds state and does not count as 'new' (avoids spam on restart).
    """
    from app.db import get_active_watchlist

    symbols = [s.upper() for s in (symbols or get_active_watchlist())]
    batch = fetch_news_batch(symbols, limit_per_symbol=3)
    state = load_watch_state()
    prev_seen: dict[str, list[str]] = state.get("seen") or {}
    is_first = not bool(prev_seen)

    new_by_ticker: dict[str, list[dict[str, Any]]] = {}
    next_seen: dict[str, list[str]] = {}

    for ticker, articles in batch.items():
        fps: list[str] = []
        fresh: list[dict[str, Any]] = []
        known = set(prev_seen.get(ticker) or [])
        for article in articles:
            fp = fingerprint_article(ticker, article)
            fps.append(fp)
            if not is_first and fp not in known:
                fresh.append(article)
        next_seen[ticker] = fps
        if fresh:
            new_by_ticker[ticker] = fresh

    save_watch_state(seen=next_seen)
    changed_tickers = sorted(new_by_ticker.keys())
    new_count = sum(len(v) for v in new_by_ticker.values())
    min_needed = max(1, int(get_settings().news_min_new_articles))
    meaningful = (not is_first) and new_count >= min_needed
    logger.info(
        "News scan: first=%s changed=%s new_count=%s min_needed=%s titles=%s",
        is_first,
        changed_tickers,
        new_count,
        min_needed,
        {t: [a.get("title") for a in arts[:2]] for t, arts in new_by_ticker.items()},
    )
    return {
        "first_scan": is_first,
        "changed": bool(changed_tickers),
        "meaningful": meaningful,
        "new_count": new_count,
        "min_needed": min_needed,
        "tickers": changed_tickers,
        "new_articles": {
            t: [{"title": a.get("title"), "source": a.get("source"), "uuid": a.get("uuid")} for a in arts]
            for t, arts in new_by_ticker.items()
        },
        "scanned": symbols,
    }


def analyze_cooldown_ok() -> tuple[bool, float | None]:
    """True if enough minutes have passed since the last full analyze."""
    settings = get_settings()
    state = load_watch_state()
    elapsed = minutes_since(state.get("last_analyze_at"))
    if elapsed is None:
        return True, None
    return elapsed >= settings.analyze_cooldown_minutes, elapsed


def positions_need_review(marked: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Cheap (no LLM) check: open positions with take-profit / stop-loss style moves.
    """
    from app.trades import portfolio_with_marks

    settings = get_settings()
    marked = marked or portfolio_with_marks()
    positions = marked.get("positions") or {}
    if not positions:
        return {"needed": False, "reasons": [], "positions": {}}

    reasons: list[str] = []
    hits: dict[str, Any] = {}
    take = float(settings.position_take_profit_pct)
    stop = float(settings.position_stop_loss_pct)

    for ticker, pos in positions.items():
        pct = float(pos.get("unrealized_pnl_pct") or 0)
        if pct >= take:
            reason = f"{ticker} +{pct:.1f}% ≥ take-profit {take:.1f}%"
            reasons.append(reason)
            hits[ticker] = {"pnl_pct": pct, "reason": "take_profit"}
        elif pct <= -abs(stop):
            reason = f"{ticker} {pct:.1f}% ≤ stop-loss -{abs(stop):.1f}%"
            reasons.append(reason)
            hits[ticker] = {"pnl_pct": pct, "reason": "stop_loss"}

    return {
        "needed": bool(reasons),
        "reasons": reasons,
        "positions": hits,
        "cash": marked.get("cash"),
        "total_value": marked.get("total_value"),
    }
