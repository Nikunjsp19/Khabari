"""End-of-day wrap: today's suggestions + the news that drove them + top headlines."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.db import get_active_watchlist, get_db, get_latest_portfolio
from app.market_hours import market_tz, now_market
from app.news import fetch_news_batch
from app.notify import notify_day_wrap
from app.trades import portfolio_with_marks

logger = logging.getLogger(__name__)

_META_ID = "day_wrap"


def _day_bounds_utc(day: datetime | None = None) -> tuple[datetime, datetime, str]:
    """Return [start, end) UTC for the market calendar day and YYYY-MM-DD key."""
    tz = market_tz()
    local = day.astimezone(tz) if day else now_market()
    start_local = datetime.combine(local.date(), time(0, 0), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), local.date().isoformat()


def _already_sent(day_key: str) -> bool:
    doc = get_db().meta.find_one({"_id": _META_ID}) or {}
    return doc.get("last_sent_day") == day_key


def _mark_sent(day_key: str, payload: dict[str, Any]) -> None:
    get_db().meta.update_one(
        {"_id": _META_ID},
        {
            "$set": {
                "last_sent_day": day_key,
                "last_sent_at": datetime.now(timezone.utc),
                "last_payload": payload,
                "updated_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )


def _recs_today(start_utc: datetime, end_utc: datetime) -> list[dict[str, Any]]:
    cursor = (
        get_db()
        .recommendations.find({"ts": {"$gte": start_utc, "$lt": end_utc}})
        .sort("ts", 1)
    )
    out: list[dict[str, Any]] = []
    for doc in cursor:
        doc["id"] = str(doc.pop("_id"))
        out.append(doc)
    return out


def _news_from_mongo(start_utc: datetime, end_utc: datetime, *, limit: int = 20) -> list[dict[str, Any]]:
    cursor = (
        get_db()
        .news.find({"saved_at": {"$gte": start_utc, "$lt": end_utc}})
        .sort("saved_at", -1)
        .limit(80)
    )
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for doc in cursor:
        title = str(doc.get("title") or "").strip()
        key = title.lower()
        if not title or key in seen:
            continue
        seen.add(key)
        tickers = doc.get("tickers") or []
        items.append(
            {
                "title": title,
                "source": doc.get("source") or "yahoo",
                "ticker": (tickers[0] if tickers else "") or "",
            }
        )
        if len(items) >= limit:
            break
    return items


def _top_news_live(symbols: list[str], *, limit: int = 8) -> list[dict[str, Any]]:
    batch = fetch_news_batch(symbols, limit_per_symbol=2)
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for ticker, articles in batch.items():
        for a in articles:
            title = str(a.get("title") or "").strip()
            key = title.lower()
            if not title or key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "title": title,
                    "source": a.get("source") or "yahoo",
                    "ticker": ticker,
                }
            )
            if len(items) >= limit:
                return items
    return items


def _news_bullets_for_ticker(rec: dict[str, Any], ticker: str) -> list[str]:
    extras = rec.get("extras") or {}
    summary = extras.get("news_summary") or {}
    raw = summary.get(ticker) or summary.get(ticker.upper()) or []
    if isinstance(raw, str):
        return [raw][:2]
    if isinstance(raw, list):
        return [str(x) for x in raw[:2] if x]
    return []


def build_day_wrap(*, day: datetime | None = None) -> dict[str, Any]:
    """Assemble concluding wrap content (no LLM)."""
    start_utc, end_utc, day_key = _day_bounds_utc(day)
    recs = _recs_today(start_utc, end_utc)
    symbols = get_active_watchlist()

    suggestions: list[dict[str, Any]] = []
    for rec in recs:
        action = str(rec.get("action") or "HOLD").upper()
        ticker = str(rec.get("ticker") or "").upper()
        status = str(rec.get("status") or "pending")
        conf = rec.get("confidence")
        investment = rec.get("investment")
        reasoning = rec.get("reasoning") or []
        if isinstance(reasoning, list):
            why = [str(x) for x in reasoning[:2]]
        else:
            why = [str(reasoning)] if reasoning else []
        news_bits = _news_bullets_for_ticker(rec, ticker)
        suggestions.append(
            {
                "action": action,
                "ticker": ticker,
                "investment": investment,
                "confidence": conf,
                "status": status,
                "reasoning": why,
                "news": news_bits,
                "id": rec.get("id"),
            }
        )

    mongo_news = _news_from_mongo(start_utc, end_utc, limit=12)
    live_news = _top_news_live(symbols, limit=10)
    # Prefer mongo (what agent actually saw), fill with live for freshness
    top_news: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for item in mongo_news + live_news:
        key = item["title"].lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        top_news.append(item)
        if len(top_news) >= 8:
            break

    try:
        portfolio = portfolio_with_marks()
    except Exception:  # noqa: BLE001
        p = get_latest_portfolio()
        portfolio = {"cash": p.get("cash"), "total_value": None, "positions": p.get("positions") or {}}

    actionable = [s for s in suggestions if s["action"] in {"BUY", "SELL"}]
    holds = [s for s in suggestions if s["action"] == "HOLD"]

    return {
        "day": day_key,
        "suggestions": suggestions,
        "actionable": actionable,
        "holds": holds,
        "top_news": top_news,
        "portfolio": {
            "cash": portfolio.get("cash"),
            "total_value": portfolio.get("total_value"),
            "positions": list((portfolio.get("positions") or {}).keys()),
        },
        "counts": {
            "recommendations": len(suggestions),
            "actionable": len(actionable),
            "holds": len(holds),
            "top_news": len(top_news),
        },
    }


def format_day_wrap_message(wrap: dict[str, Any]) -> str:
    day = wrap.get("day") or ""
    try:
        d = datetime.fromisoformat(day)
        day_label = f"{d.strftime('%a %b')} {d.day}"
    except ValueError:
        day_label = day or "today"

    lines: list[str] = [f"Khabari day wrap — {day_label}", ""]

    actionable = wrap.get("actionable") or []
    holds = wrap.get("holds") or []
    suggestions = wrap.get("suggestions") or []

    lines.append("SUGGESTIONS TODAY")
    if not suggestions:
        lines.append("• No analyze runs recorded today.")
    else:
        # Show actionable first, then one line summarizing HOLDs
        for s in actionable:
            inv = s.get("investment")
            conf = s.get("confidence")
            status = s.get("status") or "pending"
            head = f"• {s.get('action')} {s.get('ticker')}"
            if inv is not None:
                head += f" ${inv}"
            if conf is not None:
                head += f" · conf {conf}"
            head += f" · {status}"
            lines.append(head)
            for n in s.get("news") or []:
                lines.append(f"  News: {n}")
            for r in s.get("reasoning") or []:
                lines.append(f"  Why: {r}")
        if holds and not actionable:
            last = holds[-1]
            lines.append(
                f"• Mostly HOLD today (last: {last.get('ticker')} conf {last.get('confidence')})"
            )
        elif holds and actionable:
            lines.append(f"• Also {len(holds)} HOLD/silent run(s) with no trade alert")

    lines.append("")
    lines.append("TOP NEWS TODAY")
    top = wrap.get("top_news") or []
    if not top:
        lines.append("• No headlines captured.")
    else:
        for item in top[:8]:
            tkr = item.get("ticker") or ""
            prefix = f"[{tkr}] " if tkr else ""
            lines.append(f"• {prefix}{item.get('title')}")

    port = wrap.get("portfolio") or {}
    lines.append("")
    cash = port.get("cash")
    total = port.get("total_value")
    pos = port.get("positions") or []
    cash_s = f"${float(cash):.2f}" if cash is not None else "—"
    total_s = f"${float(total):.2f}" if total is not None else "—"
    pos_s = ", ".join(pos) if pos else "none"
    lines.append(f"Portfolio: cash {cash_s} · total {total_s} · held: {pos_s}")
    lines.append("Market closed for Khabari scans until tomorrow 9am ET.")

    return "\n".join(lines)


def run_day_wrap(*, force: bool = False) -> dict[str, Any]:
    """Build + send end-of-day wrap. Idempotent per calendar day unless force=True."""
    settings = get_settings()
    now = now_market()
    # Only send on weekdays (Mon–Fri)
    if now.weekday() > 4 and not force:
        return {"skipped": True, "reason": "weekend", "day": now.date().isoformat()}

    wrap = build_day_wrap()
    day_key = wrap["day"]
    if not force and _already_sent(day_key):
        logger.info("Day wrap already sent for %s", day_key)
        return {"skipped": True, "reason": "already_sent", "day": day_key}

    message = format_day_wrap_message(wrap)
    title = f"Khabari day wrap — {day_key}"
    notify_result = notify_day_wrap(title=title, message=message)

    if notify_result.get("ok") or force:
        _mark_sent(
            day_key,
            {
                "counts": wrap.get("counts"),
                "actionable": [
                    {
                        "action": s.get("action"),
                        "ticker": s.get("ticker"),
                        "investment": s.get("investment"),
                        "status": s.get("status"),
                    }
                    for s in (wrap.get("actionable") or [])
                ],
            },
        )

    return {
        "ok": bool(notify_result.get("ok")),
        "day": day_key,
        "force": force,
        "wrap": {
            "counts": wrap.get("counts"),
            "actionable": wrap.get("actionable"),
            "top_news": wrap.get("top_news"),
            "portfolio": wrap.get("portfolio"),
        },
        "message": message,
        "notification": notify_result,
        "tz": settings.market_timezone,
        "hour": settings.day_wrap_hour,
        "minute": settings.day_wrap_minute,
    }
