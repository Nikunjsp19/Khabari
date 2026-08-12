"""Deterministic options exit engine — SELL_TO_CLOSE alerts without LLM.

Watches open long calls/puts vs premium TP/SL (and near-expiry time stop),
then pushes a decisive ntfy you can confirm in Hisaab. User still places the
broker trade; Khabari owns the "when to sell" call.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from app.config import get_settings
from app.db import get_db
from app.options_data import CONTRACT_MULTIPLIER
from app.options_trades import options_portfolio_with_marks

logger = logging.getLogger(__name__)

_STATE_ID = "options_position_state"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _minutes_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (_now() - ts.astimezone(timezone.utc)).total_seconds() / 60.0
    except Exception:  # noqa: BLE001
        return None


def load_options_exit_state() -> dict[str, Any]:
    doc = get_db().meta.find_one({"_id": _STATE_ID}) or {}
    return dict(doc.get("state") or {})


def save_options_exit_state(state: dict[str, Any]) -> None:
    get_db().meta.update_one(
        {"_id": _STATE_ID},
        {"$set": {"state": state, "updated_at": _now()}},
        upsert=True,
    )


def _dte(expiry: str | None) -> int | None:
    if not expiry:
        return None
    try:
        exp = date.fromisoformat(str(expiry)[:10])
        return (exp - date.today()).days
    except Exception:  # noqa: BLE001
        return None


def evaluate_options_exits() -> dict[str, Any]:
    """Mark book and return exit rows that hit TP / SL / near-expiry."""
    settings = get_settings()
    tp = float(settings.options_take_profit_pct)
    sl = abs(float(settings.options_stop_loss_pct))
    time_dte = int(settings.options_exit_time_stop_dte)

    marked = options_portfolio_with_marks()
    positions = marked.get("positions") or {}
    exits: list[dict[str, Any]] = []
    checked = 0

    for key, pos in positions.items():
        contracts = float(pos.get("contracts") or 0)
        avg = float(pos.get("avg_premium") or 0)
        last = float(pos.get("last_premium") or 0)
        if contracts < 1 or avg <= 0 or last <= 0:
            continue
        checked += 1
        pct = float(pos.get("unrealized_pnl_pct") or ((last - avg) / avg) * 100)
        dte = _dte(str(pos.get("expiry") or ""))
        und = str(pos.get("underlying") or key).upper()
        right = str(pos.get("right") or "").lower()
        strike = pos.get("strike")
        expiry = pos.get("expiry")
        dollars = round(contracts * last * CONTRACT_MULTIPLIER, 2)

        kind: str | None = None
        reason = ""
        if pct >= tp:
            kind = "take_profit"
            reason = f"{und} {right} +{pct:.1f}% >= TP {tp:.0f}% — take profit / SELL NOW"
        elif pct <= -sl:
            kind = "stop_loss"
            reason = f"{und} {right} {pct:.1f}% <= SL -{sl:.0f}% — cut loss / SELL NOW"
        elif dte is not None and dte <= time_dte:
            kind = "time_stop"
            reason = (
                f"{und} {right} only {dte} DTE left — time stop / SELL NOW "
                f"(theta; do not hold into expiry)"
            )

        if not kind:
            continue

        exits.append(
            {
                "key": key,
                "underlying": und,
                "right": right,
                "strike": strike,
                "expiry": expiry,
                "contracts": contracts,
                "avg_premium": avg,
                "last_premium": last,
                "market_value": dollars,
                "pnl_pct": round(pct, 2),
                "dte": dte,
                "kind": kind,
                "reason": reason,
                "osi": pos.get("osi"),
                "contract_key": key,
            }
        )

    return {
        "needed": bool(exits),
        "exits": exits,
        "checked": checked,
        "positions": len(positions),
        "portfolio": marked,
        "tp_pct": tp,
        "sl_pct": sl,
    }


def _build_sell_recommendation(exit_row: dict[str, Any]) -> dict[str, Any]:
    kind = exit_row["kind"]
    label = {
        "take_profit": "Take-profit hit",
        "stop_loss": "Stop-loss hit",
        "time_stop": "Time stop (near expiry)",
    }.get(kind, "Exit signal")
    premium = float(exit_row["last_premium"])
    contracts = float(exit_row["contracts"])
    dollars = float(exit_row["market_value"])
    return {
        "action": "SELL_TO_CLOSE",
        "ticker": exit_row["underlying"],
        "right": exit_row["right"],
        "strike": exit_row["strike"],
        "expiry": exit_row["expiry"],
        "contracts": int(contracts) if contracts == int(contracts) else contracts,
        "premium": round(premium, 4),
        "contract_key": exit_row["contract_key"],
        "osi": exit_row.get("osi"),
        "investment": dollars,
        "max_loss": 0,
        "confidence": 95,
        "risk": "LOW" if kind == "take_profit" else "HIGH",
        "time_horizon": "SHORT",
        "expected_return": "lock gains" if kind == "take_profit" else "cut loss / avoid theta",
        "reasoning": [
            f"SELL NOW — {label}",
            exit_row["reason"],
            f"Mark ~${premium:.2f} (entry ${float(exit_row['avg_premium']):.2f}) · "
            f"P&L {exit_row['pnl_pct']:+.1f}% · ~${dollars:.0f} proceeds",
            "Deterministic exit — no LLM. Confirm after you sell in the broker.",
        ],
        "asset_class": "options",
        "signal_source": "options_exit_engine",
        "exit_kind": kind,
        "bid": None,
        "ask": None,
        "mid": premium,
        "quote_basis": "mid",
    }


def run_options_exit_monitor(*, send_notification: bool = True) -> dict[str, Any]:
    """Evaluate options exits and fire SELL_TO_CLOSE ntfy (user confirms)."""
    from app.db import save_options_recommendation
    from app.notify import notify_options_recommendation

    settings = get_settings()
    result = evaluate_options_exits()
    if not result.get("needed"):
        return {"ok": True, "alerted": [], **result}

    state = load_options_exit_state()
    cooldown = float(settings.options_exit_alert_cooldown_minutes)
    alerted: list[dict[str, Any]] = []

    for exit_row in result["exits"]:
        key = str(exit_row["key"])
        entry = dict(state.get(key) or {})
        last_alert_min = _minutes_since(entry.get("exit_alerted_at"))
        same_kind = entry.get("last_exit_kind") == exit_row["kind"]
        if last_alert_min is not None and same_kind and last_alert_min < cooldown:
            logger.info(
                "Options exit alert for %s suppressed (%.0fm < %.0fm cooldown)",
                key,
                last_alert_min,
                cooldown,
            )
            continue

        rec = _build_sell_recommendation(exit_row)
        rec_id = save_options_recommendation(
            rec,
            extras={
                "trigger": "options_exit_engine",
                "exit_kind": exit_row["kind"],
                "exit_detail": exit_row,
            },
        )
        rec["recommendation_id"] = rec_id

        notify_result = None
        if send_notification:
            try:
                notify_result = notify_options_recommendation(rec, recommendation_id=rec_id)
            except Exception:  # noqa: BLE001
                logger.exception("Options exit notification failed for %s", key)

        entry["exit_alerted_at"] = _now().isoformat()
        entry["last_exit_kind"] = exit_row["kind"]
        entry["last_exit_rec_id"] = rec_id
        state[key] = entry
        alerted.append(
            {
                "key": key,
                "ticker": exit_row["underlying"],
                "kind": exit_row["kind"],
                "pnl_pct": exit_row["pnl_pct"],
                "recommendation_id": rec_id,
                "notified": bool(notify_result and notify_result.get("ok")),
            }
        )
        logger.info(
            "Options SELL NOW alert: %s %s pnl=%s%% rec=%s",
            exit_row["kind"],
            key,
            exit_row["pnl_pct"],
            rec_id,
        )

    save_options_exit_state(state)
    return {"ok": True, "alerted": alerted, **result}
