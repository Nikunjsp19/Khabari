"""Which strategy owns which ticker, and how much of the book it may use.

All strategies share ONE cash pool and ONE positions map, so without this two
engines would fight over the same shares: the tilt would rank-exit a name the
mean-reversion engine just bought, and the ATR stop would sell it from under
the RSI(2) exit rule.

Sleeves (`TILT_SLEEVE_PCT` / `MR_SLEEVE_PCT`) cap how much of NAV each engine
may invest so they stop over-subscribing the same $1,000.

A ticker is *claimed* when a recommendation carrying a ``strategy`` field is
executed, and released when the position is fully closed. Unclaimed tickers
belong to the incumbent engines (tilt + ATR exits) so existing holdings keep
behaving exactly as before.

Every read fails open — if Mongo is unavailable the caller sees "nobody owns
anything", which restores today's single-strategy behaviour rather than
silently freezing trades.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Engines that operate on anything not explicitly claimed by someone else.
INCUMBENT = "momentum_tilt"


def _collection():
    from app.db import get_db

    return get_db().strategy_positions


def claim(ticker: str, strategy: str, *, rec_id: str | None = None) -> None:
    """Record that *strategy* now owns *ticker* (no-op if already owned)."""
    t = str(ticker or "").strip().upper()
    s = str(strategy or "").strip()
    if not t or not s:
        return
    try:
        _collection().update_one(
            {"_id": t},
            {
                "$setOnInsert": {
                    "ticker": t,
                    "strategy": s,
                    "opened_at": datetime.now(timezone.utc),
                },
                "$set": {"last_rec_id": rec_id},
            },
            upsert=True,
        )
    except Exception:  # noqa: BLE001
        logger.warning("Could not claim %s for %s", t, s, exc_info=True)


def release(ticker: str) -> None:
    """Drop ownership — call when a position is fully closed."""
    t = str(ticker or "").strip().upper()
    if not t:
        return
    try:
        _collection().delete_one({"_id": t})
    except Exception:  # noqa: BLE001
        logger.warning("Could not release %s", t, exc_info=True)


def opened_at(ticker: str):
    """When this ticker was claimed, or None."""
    t = str(ticker or "").strip().upper()
    if not t:
        return None
    try:
        doc = _collection().find_one({"_id": t})
        return (doc or {}).get("opened_at")
    except Exception:  # noqa: BLE001
        logger.warning("Could not read opened_at of %s", t, exc_info=True)
        return None


def owner_of(ticker: str) -> str | None:
    t = str(ticker or "").strip().upper()
    if not t:
        return None
    try:
        doc = _collection().find_one({"_id": t})
        return (doc or {}).get("strategy")
    except Exception:  # noqa: BLE001
        logger.warning("Could not read owner of %s", t, exc_info=True)
        return None


def owned_by(strategy: str) -> set[str]:
    """Tickers explicitly claimed by *strategy*."""
    s = str(strategy or "").strip()
    if not s:
        return set()
    try:
        return {
            str(d.get("ticker") or d.get("_id")).upper()
            for d in _collection().find({"strategy": s})
        }
    except Exception:  # noqa: BLE001
        logger.warning("Could not list positions for %s", s, exc_info=True)
        return set()


def claimed_by_others(strategy: str) -> set[str]:
    """Tickers another strategy owns — *strategy* must leave these alone."""
    s = str(strategy or "").strip()
    try:
        return {
            str(d.get("ticker") or d.get("_id")).upper()
            for d in _collection().find({"strategy": {"$ne": s}})
        }
    except Exception:  # noqa: BLE001
        logger.warning("Could not list foreign positions vs %s", s, exc_info=True)
        return set()


def sleeve_pct_for(strategy: str) -> float:
    """Share of portfolio NAV this engine is allowed to invest (0–100)."""
    from app.config import get_settings

    settings = get_settings()
    s = str(strategy or "").strip()
    if s == "momentum_tilt":
        pct = float(settings.tilt_sleeve_pct)
    elif s == "mean_reversion":
        pct = float(settings.mr_sleeve_pct)
    else:
        return 100.0
    return max(0.0, min(100.0, pct))


def _position_value(pos: dict[str, Any] | None) -> float:
    p = pos or {}
    mv = p.get("market_value")
    if mv is not None:
        try:
            return max(0.0, float(mv))
        except (TypeError, ValueError):
            return 0.0
    try:
        shares = float(p.get("shares") or 0.0)
        px = float(p.get("last_price") or p.get("avg_cost") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, shares * px)


def sleeve_state(
    strategy: str,
    *,
    cash: float,
    total_value: float,
    positions: dict[str, Any] | None,
    owned: set[str],
) -> dict[str, Any]:
    """How much of the book this engine already uses, and cash it may still spend.

    ``available_cash`` is ``min(real cash, sleeve room)`` so a BUY cannot steal
    the other engine's reserved capital.
    """
    pct = sleeve_pct_for(strategy)
    budget = max(0.0, float(total_value or 0.0) * pct / 100.0)
    invested = 0.0
    pos_map = positions or {}
    for raw in owned:
        key = str(raw or "").strip().upper()
        invested += _position_value(pos_map.get(key) or pos_map.get(raw))
    room = max(0.0, budget - invested)
    available = min(max(0.0, float(cash or 0.0)), room)
    return {
        "strategy": str(strategy or "").strip(),
        "pct": pct,
        "budget": round(budget, 2),
        "invested": round(invested, 2),
        "room": round(room, 2),
        "available_cash": round(available, 2),
    }


def owned_holdings(
    strategy: str,
    positions: dict[str, Any] | None,
) -> set[str]:
    """Tickers this engine currently holds (tilt owns anything not claimed elsewhere)."""
    held = {
        str(t).upper()
        for t, p in (positions or {}).items()
        if float((p or {}).get("shares") or 0) > 0
    }
    s = str(strategy or "").strip() or INCUMBENT
    if s == "mean_reversion":
        return held & owned_by(s)
    foreign = claimed_by_others(s)
    return {t for t in held if t not in foreign}


def cap_buys_to_sleeve(
    trades: list[dict[str, Any]],
    *,
    cash: float,
    sleeve: dict[str, Any],
    min_trade: float,
) -> list[dict[str, Any]]:
    """Trim BUY notionals so they don't spend another engine's reserved cash."""
    sell_val = sum(float(t.get("value") or 0.0) for t in trades if t.get("action") == "SELL")
    room = min(cash + sell_val, float(sleeve.get("room") or 0.0) + sell_val)
    out: list[dict[str, Any]] = []
    for trade in trades:
        if trade.get("action") != "BUY":
            out.append(trade)
            continue
        dollars = min(float(trade.get("value") or 0.0), room)
        if dollars < min_trade:
            continue
        out.append({**trade, "value": round(dollars, 2)})
        room = max(0.0, room - dollars)
    return out


def sync_with_positions(positions: dict[str, Any] | None) -> None:
    """Release claims for tickers no longer held (manual sells, corrections)."""
    held = {
        str(t).upper()
        for t, p in (positions or {}).items()
        if float((p or {}).get("shares") or 0) > 0
    }
    try:
        for doc in list(_collection().find({})):
            t = str(doc.get("ticker") or doc.get("_id")).upper()
            if t not in held:
                _collection().delete_one({"_id": t})
    except Exception:  # noqa: BLE001
        logger.warning("Could not sync strategy book", exc_info=True)
