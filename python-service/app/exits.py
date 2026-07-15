"""Deterministic exit engine — the part that decides *when to SELL*.

Exits determine the majority of a strategy's profitability, yet the old flow only
used fixed +5%/-3.5% bands to *wake the LLM*, which could still say HOLD. This
module makes exits decisive and volatility-aware:

- **ATR trailing stop (Chandelier)**: high-water-mark − N×ATR, ratchets up only.
- **Initial hard stop**: entry − M×ATR (or a % floor) to cap the first loss.
- **Take-profit**: lock gains at a target %.
- **Time stop**: drop dead trades that stagnate, freeing capital.

When a position breaches a stop we create a decisive SELL recommendation and push
an alert (the user still confirms in Hisaab). Per-position entry price, entry time
and high-water mark are persisted in Mongo so trailing stops survive restarts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_STATE_ID = "position_state"


def _db():
    from app.db import get_db

    return get_db()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def load_position_state() -> dict[str, Any]:
    doc = _db().meta.find_one({"_id": _STATE_ID}) or {}
    return dict(doc.get("positions") or {})


def save_position_state(state: dict[str, Any]) -> None:
    _db().meta.update_one(
        {"_id": _STATE_ID},
        {"$set": {"positions": state, "updated_at": _now()}},
        upsert=True,
    )


def _minutes_since(dt: Any) -> float | None:
    if not dt:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (_now() - dt).total_seconds() / 60.0


def _daily_atr_and_high(ticker: str, *, lookback: int = 22) -> tuple[float | None, float | None]:
    """Daily ATR(14) and the highest high over ``lookback`` sessions (Chandelier ref)."""
    try:
        import pandas as pd
        import pandas_ta as ta
        import yfinance as yf

        df = yf.download(
            ticker,
            period="3mo",
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=False,
        )
        if df is None or df.empty:
            return None, None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(how="all")
        if df.empty:
            return None, None
        atr_series = ta.atr(df["High"], df["Low"], df["Close"], length=14)
        atr = None
        if atr_series is not None and not atr_series.empty:
            val = atr_series.iloc[-1]
            atr = float(val) if pd.notna(val) else None
        recent_high = float(df["High"].tail(lookback).max())
        return atr, recent_high
    except Exception:  # noqa: BLE001
        logger.warning("Daily ATR/high fetch failed for %s", ticker, exc_info=True)
        return None, None


def _seed_or_update(
    state: dict[str, Any],
    ticker: str,
    *,
    avg_cost: float,
    last_price: float,
) -> dict[str, Any]:
    entry = state.get(ticker) or {}
    changed = False
    if not entry.get("entry_price"):
        entry["entry_price"] = round(float(avg_cost or last_price), 4)
        entry["entry_ts"] = _now().isoformat()
        entry["high_water"] = round(max(float(avg_cost or 0), float(last_price)), 4)
        entry["high_water_ts"] = _now().isoformat()
        changed = True
    # Ratchet the high-water mark up only
    hw = float(entry.get("high_water") or 0)
    if last_price > hw:
        entry["high_water"] = round(float(last_price), 4)
        entry["high_water_ts"] = _now().isoformat()
        changed = True
    entry["_changed"] = changed
    state[ticker] = entry
    return entry


def evaluate_exits(marked: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate every open stock position against the deterministic exit rules."""
    from app.trades import portfolio_with_marks

    settings = get_settings()
    marked = marked or portfolio_with_marks()
    positions = marked.get("positions") or {}
    state = load_position_state()

    # Drop state for tickers no longer held
    for stale in [t for t in state if t not in positions]:
        state.pop(stale, None)

    exits: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []

    tp_pct = float(settings.position_take_profit_pct)
    sl_pct = float(settings.position_stop_loss_pct)
    init_mult = float(settings.exit_initial_stop_atr_mult)
    trail_mult = float(settings.exit_trail_atr_mult)
    time_days = int(settings.exit_time_stop_days)
    time_min_profit = float(settings.exit_time_stop_min_profit_pct)

    for ticker, pos in positions.items():
        last_price = _num(pos.get("last_price"))
        avg_cost = _num(pos.get("avg_cost")) or last_price
        pnl_pct = _num(pos.get("unrealized_pnl_pct")) or 0.0
        if last_price is None or last_price <= 0:
            continue

        entry = _seed_or_update(state, ticker, avg_cost=avg_cost or last_price, last_price=last_price)
        entry_price = float(entry.get("entry_price") or avg_cost or last_price)
        high_water = float(entry.get("high_water") or last_price)

        atr, _recent_high = _daily_atr_and_high(ticker)

        if atr and atr > 0:
            initial_stop = entry_price - init_mult * atr
            chandelier = high_water - trail_mult * atr
            stop_basis = "atr"
        else:
            initial_stop = entry_price * (1 - sl_pct / 100.0)
            chandelier = high_water * (1 - sl_pct / 100.0)
            stop_basis = "pct"
        effective_stop = max(initial_stop, chandelier)
        trailing_active = high_water > entry_price and chandelier >= initial_stop

        days_held = None
        mins = _minutes_since(entry.get("entry_ts"))
        if mins is not None:
            days_held = mins / (60.0 * 24.0)

        decision: dict[str, Any] | None = None

        # 1) Stop breach (protect capital / lock trailing gains) — highest priority
        if last_price <= effective_stop:
            kind = "trailing_stop" if trailing_active else "stop_loss"
            if kind == "trailing_stop":
                reason = (
                    f"{ticker} ${last_price:.2f} hit trailing stop ${effective_stop:.2f} "
                    f"(high ${high_water:.2f}, {trail_mult:g}×ATR {atr:.2f})"
                    if stop_basis == "atr"
                    else f"{ticker} ${last_price:.2f} hit trailing stop ${effective_stop:.2f} (high ${high_water:.2f})"
                )
            else:
                reason = (
                    f"{ticker} ${last_price:.2f} hit stop ${effective_stop:.2f} "
                    f"({init_mult:g}×ATR below entry ${entry_price:.2f})"
                    if stop_basis == "atr"
                    else f"{ticker} ${last_price:.2f} ≤ stop-loss ${effective_stop:.2f} ({sl_pct:.1f}% below entry)"
                )
            decision = {"kind": kind, "reason": reason}

        # 2) Take-profit target
        elif pnl_pct >= tp_pct:
            decision = {
                "kind": "take_profit",
                "reason": f"{ticker} +{pnl_pct:.1f}% ≥ take-profit {tp_pct:.1f}% — lock the gain",
            }

        # 3) Time stop for dead trades
        elif time_days > 0 and days_held is not None and days_held >= time_days and pnl_pct < time_min_profit:
            decision = {
                "kind": "time_stop",
                "reason": (
                    f"{ticker} held {days_held:.1f}d with {pnl_pct:+.1f}% (< {time_min_profit:.1f}%) — "
                    "dead trade, free the capital"
                ),
            }

        check = {
            "ticker": ticker,
            "last_price": round(last_price, 4),
            "entry_price": round(entry_price, 4),
            "high_water": round(high_water, 4),
            "atr": round(atr, 4) if atr else None,
            "stop_basis": stop_basis,
            "initial_stop": round(initial_stop, 4),
            "trailing_stop": round(chandelier, 4),
            "effective_stop": round(effective_stop, 4),
            "trailing_active": trailing_active,
            "pnl_pct": round(pnl_pct, 2),
            "days_held": round(days_held, 2) if days_held is not None else None,
            "exit": bool(decision),
        }
        checked.append(check)

        if decision:
            shares = _num(pos.get("shares")) or 0.0
            market_value = _num(pos.get("market_value")) or round(shares * last_price, 2)
            exits.append(
                {
                    "ticker": ticker,
                    "kind": decision["kind"],
                    "reason": decision["reason"],
                    "last_price": round(last_price, 4),
                    "shares": shares,
                    "market_value": round(market_value, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "effective_stop": round(effective_stop, 4),
                    "high_water": round(high_water, 4),
                }
            )

    # Persist high-water/entry updates
    for entry in state.values():
        entry.pop("_changed", None)
    save_position_state(state)

    return {
        "needed": bool(exits),
        "exits": exits,
        "checked": checked,
        "positions": len(positions),
        "cash": marked.get("cash"),
        "total_value": marked.get("total_value"),
    }


def _build_exit_recommendation(exit_row: dict[str, Any], regime: dict[str, Any] | None = None) -> dict[str, Any]:
    kind = exit_row["kind"]
    label = {
        "trailing_stop": "Trailing stop hit",
        "stop_loss": "Stop-loss hit",
        "take_profit": "Take-profit target",
        "time_stop": "Time stop (stagnant)",
    }.get(kind, "Exit signal")
    reasoning = [exit_row["reason"], f"Deterministic exit: {label}"]
    if regime and regime.get("state") == "risk_off":
        reasoning.append("Broad market risk-off — protecting capital")
    return {
        "ticker": exit_row["ticker"],
        "action": "SELL",
        "investment": exit_row["market_value"],
        "confidence": 90,
        "risk": "LOW" if kind == "take_profit" else "MEDIUM",
        "time_horizon": "SHORT",
        "expected_return": "protect gains" if kind == "take_profit" else "limit loss",
        "reasoning": reasoning,
        "signal_source": "exit_engine",
        "exit_kind": kind,
        "effective_stop": exit_row.get("effective_stop"),
        "high_water": exit_row.get("high_water"),
        "pnl_pct": exit_row.get("pnl_pct"),
    }


def run_exit_monitor(*, send_notification: bool = True) -> dict[str, Any]:
    """Evaluate exits, fire decisive SELL alerts (user still confirms), dedupe alerts."""
    from app.db import save_recommendation
    from app.notify import notify_recommendation

    settings = get_settings()
    try:
        from app.signals import market_regime

        regime = market_regime()
    except Exception:  # noqa: BLE001
        regime = None

    result = evaluate_exits()
    if not result.get("needed"):
        return {"ok": True, "alerted": [], **result}

    state = load_position_state()
    cooldown = float(settings.exit_alert_cooldown_minutes)
    alerted: list[dict[str, Any]] = []

    for exit_row in result["exits"]:
        ticker = exit_row["ticker"]
        entry = state.get(ticker) or {}
        last_alert_min = _minutes_since(entry.get("exit_alerted_at"))
        same_kind = entry.get("last_exit_kind") == exit_row["kind"]
        if last_alert_min is not None and same_kind and last_alert_min < cooldown:
            logger.info(
                "Exit alert for %s suppressed (%.0fm < %.0fm cooldown)",
                ticker,
                last_alert_min,
                cooldown,
            )
            continue

        rec = _build_exit_recommendation(exit_row, regime)
        rec_id = save_recommendation(
            rec,
            extras={
                "trigger": "exit_engine",
                "exit_kind": exit_row["kind"],
                "exit_detail": exit_row,
                "regime": regime,
            },
        )
        rec["recommendation_id"] = rec_id
        confirm_base = (settings.hisaab_base_url or settings.public_base_url).rstrip("/")
        confirm_path = "/trades" if settings.hisaab_base_url else "/desk"
        rec["desk_url"] = f"{confirm_base}{confirm_path}?id={rec_id}"

        notify_result = None
        if send_notification:
            try:
                notify_result = notify_recommendation(rec, recommendation_id=rec_id)
            except Exception:  # noqa: BLE001
                logger.exception("Exit notification failed for %s", ticker)

        entry["exit_alerted_at"] = _now().isoformat()
        entry["last_exit_kind"] = exit_row["kind"]
        entry["last_exit_rec_id"] = rec_id
        state[ticker] = entry
        alerted.append(
            {
                "ticker": ticker,
                "kind": exit_row["kind"],
                "recommendation_id": rec_id,
                "notified": bool(notify_result and notify_result.get("ok")),
            }
        )
        logger.info("Exit alert sent: %s %s rec=%s", exit_row["kind"], ticker, rec_id)

    save_position_state(state)
    return {"ok": True, "alerted": alerted, **result}
