"""Confirm / edit / skip options recommendations against a separate paper book."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId

from app.db import get_db, get_latest_options_portfolio, save_options_portfolio
from app.options_data import CONTRACT_MULTIPLIER, fetch_contract_quote, position_key

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_options_recommendation(rec_id: str) -> dict[str, Any] | None:
    try:
        oid = ObjectId(rec_id)
    except InvalidId:
        return None
    doc = get_db().options_recommendations.find_one({"_id": oid})
    if not doc:
        return None
    doc["id"] = str(doc.pop("_id"))
    return doc


def get_pending_options_recommendation() -> dict[str, Any] | None:
    cursor = get_db().options_recommendations.find().sort("ts", -1).limit(20)
    for doc in cursor:
        status = doc.get("status", "pending")
        if status == "pending" or status is None:
            doc["id"] = str(doc.pop("_id"))
            doc["status"] = status or "pending"
            return doc
    return None


def mark_options_recommendation(rec_id: str, status: str, extra: dict[str, Any] | None = None) -> None:
    get_db().options_recommendations.update_one(
        {"_id": ObjectId(rec_id)},
        {"$set": {"status": status, "resolved_at": _now(), **(extra or {})}},
    )


def execute_options_recommendation(
    rec_id: str,
    *,
    fill_premium: float | None = None,
    contracts_override: float | None = None,
) -> dict[str, Any]:
    rec = get_options_recommendation(rec_id)
    if not rec:
        raise ValueError("Recommendation not found")
    if rec.get("status") == "executed":
        raise ValueError("Recommendation already executed")
    if rec.get("status") == "skipped":
        raise ValueError("Recommendation was skipped")

    action = str(rec.get("action", "HOLD")).upper()
    ticker = str(rec.get("ticker", "")).upper()
    portfolio = get_latest_options_portfolio()
    cash = float(portfolio["cash"])
    positions = dict(portfolio.get("positions") or {})

    trade: dict[str, Any] = {
        "recommendation_id": rec_id,
        "action": action,
        "ticker": ticker,
        "ts": _now().isoformat(),
    }

    if action == "HOLD":
        mark_options_recommendation(
            rec_id, "executed", {"trade": {"action": "HOLD", "note": "no position change"}}
        )
        return {
            "ok": True,
            "action": "HOLD",
            "portfolio": get_latest_options_portfolio(),
            "message": "HOLD acknowledged — options portfolio unchanged",
        }

    premium = float(fill_premium) if fill_premium is not None else float(rec.get("premium") or 0)
    if premium <= 0:
        raise ValueError("Invalid fill premium")

    right = rec.get("right")
    strike = float(rec["strike"]) if rec.get("strike") is not None else None
    expiry = rec.get("expiry")
    key = rec.get("contract_key") or (
        position_key(ticker, str(expiry), str(right), float(strike))
        if ticker and right and strike is not None and expiry
        else None
    )

    if action == "BUY_TO_OPEN":
        contracts = float(
            contracts_override if contracts_override is not None else rec.get("contracts") or 0
        )
        if contracts < 1:
            raise ValueError("Contracts must be at least 1")
        spend = round(premium * CONTRACT_MULTIPLIER * contracts, 2)
        if spend > cash + 0.01:
            raise ValueError(f"Not enough options cash: need ${spend:.2f}, have ${cash:.2f}")
        if not key:
            raise ValueError("Missing contract key for BUY_TO_OPEN")
        existing = positions.get(key) or {
            "underlying": ticker,
            "right": right,
            "strike": strike,
            "expiry": expiry,
            "contracts": 0.0,
            "avg_premium": 0.0,
            "osi": rec.get("osi"),
            "key": key,
        }
        old_c = float(existing.get("contracts", 0))
        old_avg = float(existing.get("avg_premium", 0))
        new_c = old_c + contracts
        new_avg = ((old_c * old_avg) + (contracts * premium)) / new_c if new_c else premium
        positions[key] = {
            "underlying": ticker,
            "right": right,
            "strike": strike,
            "expiry": expiry,
            "contracts": round(new_c, 4),
            "avg_premium": round(new_avg, 4),
            "osi": existing.get("osi") or rec.get("osi"),
            "key": key,
        }
        cash = round(cash - spend, 2)
        trade.update(
            {
                "contracts": contracts,
                "premium": premium,
                "dollars": spend,
                "right": right,
                "strike": strike,
                "expiry": expiry,
                "contract_key": key,
                "max_loss": spend,
            }
        )

    elif action == "SELL_TO_CLOSE":
        if not key or key not in positions:
            # try match underlying
            key = None
            for k, p in positions.items():
                if str(p.get("underlying", "")).upper() == ticker:
                    key = k
                    break
        if not key or key not in positions:
            raise ValueError(f"No open options position for {ticker} to close")
        existing = positions[key]
        owned = float(existing.get("contracts", 0))
        contracts = float(
            contracts_override if contracts_override is not None else rec.get("contracts") or owned
        )
        contracts = min(owned, contracts)
        if contracts <= 0:
            raise ValueError("Enter a valid contracts quantity greater than 0")
        proceeds = round(premium * CONTRACT_MULTIPLIER * contracts, 2)
        remaining = owned - contracts
        if remaining < 1e-8:
            positions.pop(key, None)
        else:
            positions[key] = {
                **existing,
                "contracts": round(remaining, 4),
            }
        cash = round(cash + proceeds, 2)
        trade.update(
            {
                "contracts": contracts,
                "premium": premium,
                "dollars": proceeds,
                "right": existing.get("right"),
                "strike": existing.get("strike"),
                "expiry": existing.get("expiry"),
                "contract_key": key,
            }
        )
    else:
        raise ValueError(f"Unsupported options action: {action}")

    save_options_portfolio(cash, positions, source="trade_confirm")
    get_db().options_trades.insert_one({**trade, "saved_at": _now()})
    mark_options_recommendation(rec_id, "executed", {"trade": trade, "fill_premium": premium})

    return {
        "ok": True,
        "trade": trade,
        "portfolio": {"cash": cash, "positions": positions},
        "message": f"Recorded {action} {ticker} — options paper book updated",
    }


def _reverse_options_trade(
    cash: float,
    positions: dict[str, Any],
    trade: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    action = str(trade.get("action", "")).upper()
    key = trade.get("contract_key")
    contracts = float(trade.get("contracts") or 0)
    dollars = float(trade.get("dollars") or 0)
    premium = float(trade.get("premium") or 0)
    next_positions = {k: dict(v) for k, v in (positions or {}).items()}

    if not key or contracts <= 0 or dollars <= 0:
        raise ValueError("Cannot update: previous options trade details are incomplete")

    if action == "BUY_TO_OPEN":
        cash = round(cash + dollars, 2)
        existing = next_positions.get(key)
        if not existing:
            raise ValueError(f"Cannot reverse BUY_TO_OPEN: position {key} not found")
        owned = float(existing.get("contracts", 0))
        avg = float(existing.get("avg_premium", 0))
        remaining = owned - contracts
        if remaining < -1e-6:
            raise ValueError(f"Cannot reverse: only {owned} contracts on books")
        if remaining < 1e-8:
            next_positions.pop(key, None)
        else:
            basis = owned * avg - contracts * premium
            next_positions[key] = {
                **existing,
                "contracts": round(remaining, 4),
                "avg_premium": round(basis / remaining, 4),
            }
    elif action == "SELL_TO_CLOSE":
        cash = round(cash - dollars, 2)
        if cash < -0.01:
            raise ValueError("Cannot reverse SELL_TO_CLOSE: not enough cash to undo proceeds")
        existing = next_positions.get(key) or {
            "underlying": trade.get("ticker"),
            "right": trade.get("right"),
            "strike": trade.get("strike"),
            "expiry": trade.get("expiry"),
            "contracts": 0.0,
            "avg_premium": premium,
            "key": key,
        }
        owned = float(existing.get("contracts", 0))
        next_positions[key] = {
            **existing,
            "contracts": round(owned + contracts, 4),
            "avg_premium": float(existing.get("avg_premium") or premium),
            "key": key,
        }
    else:
        raise ValueError(f"Unsupported previous trade action: {action}")

    return cash, next_positions


def update_options_recommendation_trade(
    rec_id: str,
    *,
    fill_premium: float,
    contracts: float,
) -> dict[str, Any]:
    rec = get_options_recommendation(rec_id)
    if not rec:
        raise ValueError("Recommendation not found")
    if rec.get("status") != "executed":
        raise ValueError("Only executed trades can be updated — confirm the trade first")

    prev = rec.get("trade") or {}
    if not prev or str(prev.get("action", "")).upper() == "HOLD" or prev.get("note") == "no position change":
        raise ValueError("Nothing to update — this recommendation had no position change")

    if fill_premium <= 0:
        raise ValueError("Enter a valid fill premium greater than 0")
    if contracts <= 0:
        raise ValueError("Enter a valid contracts quantity greater than 0")

    portfolio = get_latest_options_portfolio()
    cash, positions = _reverse_options_trade(
        float(portfolio["cash"]),
        dict(portfolio.get("positions") or {}),
        prev,
    )

    # Re-apply via execute-like logic without status checks
    action = str(prev.get("action") or rec.get("action")).upper()
    ticker = str(rec.get("ticker") or prev.get("ticker") or "").upper()
    key = prev.get("contract_key") or rec.get("contract_key")
    right = prev.get("right") or rec.get("right")
    strike = prev.get("strike") if prev.get("strike") is not None else rec.get("strike")
    expiry = prev.get("expiry") or rec.get("expiry")
    premium = float(fill_premium)

    trade: dict[str, Any] = {
        "recommendation_id": rec_id,
        "action": action,
        "ticker": ticker,
        "ts": _now().isoformat(),
        "corrected": True,
        "previous_trade": {
            "contracts": prev.get("contracts"),
            "premium": prev.get("premium"),
            "dollars": prev.get("dollars"),
        },
    }

    if action == "BUY_TO_OPEN":
        if not key:
            raise ValueError("Missing contract key")
        spend = round(premium * CONTRACT_MULTIPLIER * contracts, 2)
        if spend > cash + 0.01:
            raise ValueError(f"Not enough options cash: need ${spend:.2f}, have ${cash:.2f}")
        existing = positions.get(key) or {
            "underlying": ticker,
            "right": right,
            "strike": strike,
            "expiry": expiry,
            "contracts": 0.0,
            "avg_premium": 0.0,
            "key": key,
        }
        old_c = float(existing.get("contracts", 0))
        old_avg = float(existing.get("avg_premium", 0))
        new_c = old_c + contracts
        new_avg = ((old_c * old_avg) + (contracts * premium)) / new_c if new_c else premium
        positions[key] = {
            **existing,
            "contracts": round(new_c, 4),
            "avg_premium": round(new_avg, 4),
            "key": key,
        }
        cash = round(cash - spend, 2)
        trade.update(
            {
                "contracts": contracts,
                "premium": premium,
                "dollars": spend,
                "right": right,
                "strike": strike,
                "expiry": expiry,
                "contract_key": key,
                "max_loss": spend,
            }
        )
    elif action == "SELL_TO_CLOSE":
        if not key or key not in positions:
            raise ValueError("No position to re-apply SELL_TO_CLOSE")
        existing = positions[key]
        owned = float(existing.get("contracts", 0))
        contracts = min(owned, float(contracts))
        proceeds = round(premium * CONTRACT_MULTIPLIER * contracts, 2)
        remaining = owned - contracts
        if remaining < 1e-8:
            positions.pop(key, None)
        else:
            positions[key] = {**existing, "contracts": round(remaining, 4)}
        cash = round(cash + proceeds, 2)
        trade.update(
            {
                "contracts": contracts,
                "premium": premium,
                "dollars": proceeds,
                "right": existing.get("right"),
                "strike": existing.get("strike"),
                "expiry": existing.get("expiry"),
                "contract_key": key,
            }
        )
    else:
        raise ValueError(f"Unsupported action: {action}")

    save_options_portfolio(cash, positions, source="trade_correct")
    get_db().options_trades.insert_one({**trade, "saved_at": _now()})
    get_db().options_recommendations.update_one(
        {"_id": ObjectId(rec_id)},
        {
            "$set": {
                "trade": trade,
                "fill_premium": premium,
                "corrected_at": _now(),
            },
            "$push": {
                "trade_history": {
                    **{k: prev.get(k) for k in ("action", "contracts", "premium", "dollars", "contract_key")},
                    "replaced_at": _now(),
                }
            },
        },
    )

    return {
        "ok": True,
        "trade": trade,
        "portfolio": {"cash": cash, "positions": positions},
        "message": f"Updated {action} {ticker}: {contracts} contracts @ ${premium} — options book corrected",
    }


def skip_options_recommendation(rec_id: str, reason: str = "user_skipped") -> dict[str, Any]:
    rec = get_options_recommendation(rec_id)
    if not rec:
        raise ValueError("Recommendation not found")
    if rec.get("status") in {"executed", "skipped"}:
        raise ValueError(f"Recommendation already {rec.get('status')}")
    mark_options_recommendation(rec_id, "skipped", {"skip_reason": reason})
    return {"ok": True, "message": "Skipped — options portfolio unchanged", "recommendation_id": rec_id}


def options_portfolio_with_marks() -> dict[str, Any]:
    portfolio = get_latest_options_portfolio()
    cash = float(portfolio["cash"])
    positions = portfolio.get("positions") or {}
    marked: dict[str, Any] = {}
    holdings_value = 0.0

    for key, pos in positions.items():
        contracts = float(pos.get("contracts", 0) or 0)
        avg = float(pos.get("avg_premium", 0) or 0)
        mark = avg
        try:
            quote = fetch_contract_quote(str(pos.get("osi") or key), contract=pos)
            if quote and quote.get("mid"):
                mark = float(quote["mid"])
        except Exception:  # noqa: BLE001
            logger.debug("Could not mark options position %s", key, exc_info=True)
        value = contracts * mark * CONTRACT_MULTIPLIER
        cost = contracts * avg * CONTRACT_MULTIPLIER
        holdings_value += value
        marked[key] = {
            **pos,
            "contracts": contracts,
            "avg_premium": avg,
            "last_premium": round(mark, 4),
            "market_value": round(value, 2),
            "unrealized_pnl": round(value - cost, 2),
            "unrealized_pnl_pct": round(((mark - avg) / avg) * 100, 2) if avg else 0,
        }

    return {
        "cash": cash,
        "positions": marked,
        "holdings_value": round(holdings_value, 2),
        "total_value": round(cash + holdings_value, 2),
        "ts": portfolio.get("ts"),
        "source": portfolio.get("source"),
        "asset_class": "options",
    }


def options_positions_need_review() -> dict[str, Any]:
    """TP/SL on long option premium P&L."""
    from app.config import get_settings

    settings = get_settings()
    marked = options_portfolio_with_marks()
    reasons: list[str] = []
    for key, pos in (marked.get("positions") or {}).items():
        pct = float(pos.get("unrealized_pnl_pct") or 0)
        und = pos.get("underlying") or key
        if pct >= settings.options_take_profit_pct:
            reasons.append(f"{und} option +{pct:.1f}% >= TP {settings.options_take_profit_pct}%")
        elif pct <= -abs(settings.options_stop_loss_pct):
            reasons.append(f"{und} option {pct:.1f}% <= SL -{settings.options_stop_loss_pct}%")
    return {"needed": bool(reasons), "reasons": reasons, "portfolio": marked}
