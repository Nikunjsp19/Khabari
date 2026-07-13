"""Apply user-confirmed trades to the portfolio so the agent can stay in sync."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import yfinance as yf
from bson import ObjectId
from bson.errors import InvalidId

from app.db import get_db, get_latest_portfolio, save_portfolio

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_recommendation(rec_id: str) -> dict[str, Any] | None:
    try:
        oid = ObjectId(rec_id)
    except InvalidId:
        return None
    doc = get_db().recommendations.find_one({"_id": oid})
    if not doc:
        return None
    doc["id"] = str(doc.pop("_id"))
    return doc


def get_pending_recommendation() -> dict[str, Any] | None:
    """Latest recommendation that still needs user confirm/skip."""
    cursor = get_db().recommendations.find().sort("ts", -1).limit(20)
    for doc in cursor:
        status = doc.get("status", "pending")
        if status == "pending" or status is None:
            doc["id"] = str(doc.pop("_id"))
            doc["status"] = status or "pending"
            return doc
    return None


def mark_recommendation(rec_id: str, status: str, extra: dict[str, Any] | None = None) -> None:
    get_db().recommendations.update_one(
        {"_id": ObjectId(rec_id)},
        {"$set": {"status": status, "resolved_at": _now(), **(extra or {})}},
    )


def fetch_last_price(ticker: str) -> float:
    ticker = ticker.upper()
    # Prefer latest stored price
    stored = get_db().prices.find_one({"ticker": ticker}, sort=[("saved_at", -1)])
    if stored and stored.get("price"):
        return float(stored["price"])

    t = yf.Ticker(ticker)
    hist = t.history(period="1d", interval="1m")
    if hist is not None and not hist.empty:
        return float(hist["Close"].iloc[-1])
    info = t.fast_info
    price = getattr(info, "last_price", None) or getattr(info, "lastPrice", None)
    if price:
        return float(price)
    raise RuntimeError(f"Could not fetch price for {ticker}")


def execute_recommendation(
    rec_id: str,
    *,
    fill_price: float | None = None,
    investment_override: float | None = None,
    shares_override: float | None = None,
) -> dict[str, Any]:
    """
    User confirms they placed the trade. Update cash/positions in MongoDB.
    After this, future AI runs see the real portfolio and can recommend sells.
    """
    rec = get_recommendation(rec_id)
    if not rec:
        raise ValueError("Recommendation not found")
    if rec.get("status") == "executed":
        raise ValueError("Recommendation already executed")
    if rec.get("status") == "skipped":
        raise ValueError("Recommendation was skipped")

    action = str(rec.get("action", "HOLD")).upper()
    ticker = str(rec.get("ticker", "")).upper()
    amount = float(investment_override if investment_override is not None else rec.get("investment") or 0)

    portfolio = get_latest_portfolio()
    cash = float(portfolio["cash"])
    positions = dict(portfolio.get("positions") or {})

    trade: dict[str, Any] = {
        "recommendation_id": rec_id,
        "action": action,
        "ticker": ticker,
        "ts": _now().isoformat(),
    }

    if action == "HOLD" or amount <= 0:
        mark_recommendation(rec_id, "executed", {"trade": {"action": "HOLD", "note": "no position change"}})
        return {
            "ok": True,
            "action": "HOLD",
            "portfolio": get_latest_portfolio(),
            "message": "HOLD acknowledged — portfolio unchanged",
        }

    price = float(fill_price) if fill_price is not None else fetch_last_price(ticker)
    if price <= 0:
        raise ValueError(f"Invalid price for {ticker}")

    if action == "BUY":
        if shares_override is not None and shares_override > 0:
            shares = float(shares_override)
            spend = round(shares * price, 2)
        else:
            spend = min(amount, cash)
            if spend < 1:
                raise ValueError("Not enough cash to execute BUY")
            shares = spend / price
            spend = round(shares * price, 2)
        if spend < 1:
            raise ValueError("Trade amount must be at least $1")
        if spend > cash + 0.01:
            raise ValueError(f"Not enough cash: need ${spend:.2f}, have ${cash:.2f}")
        existing = positions.get(ticker) or {"shares": 0.0, "avg_cost": 0.0}
        old_shares = float(existing.get("shares", 0))
        old_cost = float(existing.get("avg_cost", 0))
        new_shares = old_shares + shares
        new_avg = ((old_shares * old_cost) + spend) / new_shares if new_shares else price
        positions[ticker] = {"shares": round(new_shares, 6), "avg_cost": round(new_avg, 4)}
        cash = round(cash - spend, 2)
        trade.update({"shares": round(shares, 6), "price": price, "dollars": round(spend, 2)})

    elif action == "SELL":
        existing = positions.get(ticker)
        if not existing or float(existing.get("shares", 0)) <= 0:
            raise ValueError(f"No shares of {ticker} to sell")
        owned = float(existing["shares"])
        if shares_override is not None and shares_override > 0:
            shares_to_sell = min(owned, float(shares_override))
        else:
            shares_to_sell = min(owned, amount / price)
        if shares_to_sell <= 0:
            raise ValueError("Enter a valid quantity greater than 0")
        proceeds = round(shares_to_sell * price, 2)
        remaining = owned - shares_to_sell
        if remaining < 1e-8:
            positions.pop(ticker, None)
        else:
            positions[ticker] = {
                "shares": round(remaining, 6),
                "avg_cost": float(existing.get("avg_cost", price)),
            }
        cash = round(cash + proceeds, 2)
        trade.update({"shares": round(shares_to_sell, 6), "price": price, "dollars": proceeds})

    else:
        raise ValueError(f"Unsupported action: {action}")

    save_portfolio(cash, positions, source="trade_confirm")
    get_db().trades.insert_one({**trade, "saved_at": _now()})
    mark_recommendation(rec_id, "executed", {"trade": trade, "fill_price": price})

    return {
        "ok": True,
        "trade": trade,
        "portfolio": {"cash": cash, "positions": positions},
        "message": f"Recorded {action} {ticker} — agent portfolio updated",
    }


def _reverse_trade(
    cash: float,
    positions: dict[str, Any],
    trade: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    action = str(trade.get("action", "")).upper()
    ticker = str(trade.get("ticker", "")).upper()
    shares = float(trade.get("shares") or 0)
    dollars = float(trade.get("dollars") or 0)
    price = float(trade.get("price") or 0)
    next_positions = {k: dict(v) for k, v in (positions or {}).items()}

    if not ticker or shares <= 0 or dollars <= 0:
        raise ValueError("Cannot update: previous trade details are incomplete")

    if action == "BUY":
        cash = round(cash + dollars, 2)
        existing = next_positions.get(ticker)
        if not existing:
            raise ValueError(f"Cannot reverse BUY {ticker}: position not found")
        owned = float(existing.get("shares", 0))
        avg = float(existing.get("avg_cost", 0))
        remaining = owned - shares
        if remaining < -1e-6:
            raise ValueError(f"Cannot reverse BUY {ticker}: only {owned} shares on books")
        if remaining < 1e-8:
            next_positions.pop(ticker, None)
        else:
            basis = owned * avg - dollars
            next_positions[ticker] = {
                "shares": round(remaining, 6),
                "avg_cost": round(basis / remaining, 4),
            }
    elif action == "SELL":
        cash = round(cash - dollars, 2)
        if cash < -0.01:
            raise ValueError(f"Cannot reverse SELL {ticker}: not enough cash to undo proceeds")
        existing = next_positions.get(ticker) or {"shares": 0.0, "avg_cost": price}
        owned = float(existing.get("shares", 0))
        avg = float(existing.get("avg_cost") or price)
        next_positions[ticker] = {
            "shares": round(owned + shares, 6),
            "avg_cost": avg if owned > 0 else round(price or avg, 4),
        }
    else:
        raise ValueError(f"Unsupported previous trade action: {action}")

    return cash, next_positions


def update_recommendation_trade(
    rec_id: str,
    *,
    fill_price: float,
    shares: float,
) -> dict[str, Any]:
    """Correct an already-executed trade (wrong price/qty)."""
    rec = get_recommendation(rec_id)
    if not rec:
        raise ValueError("Recommendation not found")
    if rec.get("status") != "executed":
        raise ValueError("Only executed trades can be updated — confirm the trade first")

    prev = rec.get("trade") or {}
    if not prev or str(prev.get("action", "")).upper() == "HOLD" or prev.get("note") == "no position change":
        raise ValueError("Nothing to update — this recommendation had no position change")

    if fill_price <= 0:
        raise ValueError("Enter a valid fill price greater than 0")
    if shares <= 0:
        raise ValueError("Enter a valid quantity greater than 0")

    action = str(rec.get("action") or prev.get("action") or "HOLD").upper()
    ticker = str(rec.get("ticker") or prev.get("ticker") or "").upper()
    if action not in {"BUY", "SELL"}:
        raise ValueError("Only BUY/SELL trades can be updated")

    portfolio = get_latest_portfolio()
    cash, positions = _reverse_trade(
        float(portfolio["cash"]),
        dict(portfolio.get("positions") or {}),
        prev,
    )

    # Re-apply with corrected fill via temporary reset of status is awkward;
    # inline BUY/SELL with shares_override using reversed portfolio.
    price = float(fill_price)
    trade: dict[str, Any] = {
        "recommendation_id": rec_id,
        "action": action,
        "ticker": ticker,
        "ts": _now().isoformat(),
        "corrected": True,
        "previous_trade": {
            "shares": prev.get("shares"),
            "price": prev.get("price"),
            "dollars": prev.get("dollars"),
        },
    }

    if action == "BUY":
        spend = round(shares * price, 2)
        if spend < 1:
            raise ValueError("Trade amount must be at least $1")
        if spend > cash + 0.01:
            raise ValueError(f"Not enough cash: need ${spend:.2f}, have ${cash:.2f}")
        existing = positions.get(ticker) or {"shares": 0.0, "avg_cost": 0.0}
        old_shares = float(existing.get("shares", 0))
        old_cost = float(existing.get("avg_cost", 0))
        new_shares = old_shares + shares
        new_avg = ((old_shares * old_cost) + spend) / new_shares if new_shares else price
        positions[ticker] = {"shares": round(new_shares, 6), "avg_cost": round(new_avg, 4)}
        cash = round(cash - spend, 2)
        trade.update({"shares": round(shares, 6), "price": price, "dollars": spend})
    else:
        existing = positions.get(ticker)
        if not existing or float(existing.get("shares", 0)) <= 0:
            raise ValueError(f"No shares of {ticker} to sell")
        owned = float(existing["shares"])
        shares_to_sell = min(owned, float(shares))
        proceeds = round(shares_to_sell * price, 2)
        remaining = owned - shares_to_sell
        if remaining < 1e-8:
            positions.pop(ticker, None)
        else:
            positions[ticker] = {
                "shares": round(remaining, 6),
                "avg_cost": float(existing.get("avg_cost", price)),
            }
        cash = round(cash + proceeds, 2)
        trade.update({"shares": round(shares_to_sell, 6), "price": price, "dollars": proceeds})

    save_portfolio(cash, positions, source="trade_correct")
    get_db().trades.insert_one({**trade, "saved_at": _now()})
    get_db().recommendations.update_one(
        {"_id": ObjectId(rec_id)},
        {
            "$set": {
                "trade": trade,
                "fill_price": price,
                "corrected_at": _now(),
            },
            "$push": {
                "trade_history": {
                    "action": prev.get("action"),
                    "ticker": prev.get("ticker"),
                    "shares": prev.get("shares"),
                    "price": prev.get("price"),
                    "dollars": prev.get("dollars"),
                    "replaced_at": _now(),
                }
            },
        },
    )

    return {
        "ok": True,
        "trade": trade,
        "portfolio": {"cash": cash, "positions": positions},
        "message": f"Updated {action} {ticker}: {shares} shares @ ${price} — portfolio corrected",
    }


def skip_recommendation(rec_id: str, reason: str = "user_skipped") -> dict[str, Any]:
    rec = get_recommendation(rec_id)
    if not rec:
        raise ValueError("Recommendation not found")
    if rec.get("status") in {"executed", "skipped"}:
        raise ValueError(f"Recommendation already {rec.get('status')}")
    mark_recommendation(rec_id, "skipped", {"skip_reason": reason})
    return {"ok": True, "message": "Skipped — portfolio unchanged", "recommendation_id": rec_id}


def portfolio_with_marks() -> dict[str, Any]:
    """Latest portfolio + live marks / P&L so the agent (and you) can monitor."""
    portfolio = get_latest_portfolio()
    cash = float(portfolio["cash"])
    positions = portfolio.get("positions") or {}
    marked = {}
    holdings_value = 0.0
    for ticker, pos in positions.items():
        shares = float(pos.get("shares", 0))
        avg = float(pos.get("avg_cost", 0))
        try:
            price = fetch_last_price(ticker)
        except Exception:  # noqa: BLE001
            price = avg
        value = shares * price
        holdings_value += value
        marked[ticker] = {
            "shares": shares,
            "avg_cost": avg,
            "last_price": round(price, 4),
            "market_value": round(value, 2),
            "unrealized_pnl": round(value - shares * avg, 2),
            "unrealized_pnl_pct": round(((price - avg) / avg) * 100, 2) if avg else 0,
        }
    return {
        "cash": cash,
        "positions": marked,
        "holdings_value": round(holdings_value, 2),
        "total_value": round(cash + holdings_value, 2),
        "ts": portfolio.get("ts"),
        "source": portfolio.get("source"),
    }
