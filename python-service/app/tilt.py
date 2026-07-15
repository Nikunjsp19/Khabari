"""Momentum-tilt rebalance engine — the live desk's core recommendation logic.

This is the strategy validated in backtest to beat SPY across every window
(see ``app/backtest.py::run_tilt_backtest``). Unlike the LLM buy/sell-timing
engine, it does **not** trade in and out of the market. It:

- stays fully invested in the strongest uptrending names (relative momentum,
  Jegadeesh & Titman 1993),
- rebalances **monthly** to an equal-weight top-N,
- applies an absolute-momentum **trend brake** (Antonacci 2014): a held name
  that falls below its 200-day average is sold (to cash) even between rebalances.

It emits the SAME recommendation documents the manual desk already uses, so the
existing workflow is unchanged: each required BUY/SELL becomes a pending
recommendation you confirm in Hisaab after placing the trade. No LLM spend.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.db import (
    get_active_watchlist,
    get_db,
    get_latest_portfolio,
    save_recommendation,
)
from app.gates import should_notify
from app.indicators import compute_daily_context_batch
from app.market_hours import now_market
from app.notify import notify_recommendation
from app.trades import portfolio_with_marks

logger = logging.getLogger(__name__)


def _num(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def tilt_universe() -> list[str]:
    """Universe to rank. Uses TILT_UNIVERSE if set, else the active watchlist."""
    settings = get_settings()
    raw = (settings.tilt_universe or "").strip()
    if raw:
        syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
    else:
        syms = [s.upper() for s in get_active_watchlist()]
    return list(dict.fromkeys(syms))


def _momentum(d: dict[str, Any]) -> float | None:
    """Blended trailing momentum with 12-1 preferred, shorter windows as fallback."""
    for key in ("mom_12_1", "ret_6m", "ret_3m"):
        v = _num(d.get(key))
        if v is not None:
            return v
    return None


def rank_universe(
    daily: dict[str, Any],
    *,
    require_uptrend: bool,
    require_positive_momentum: bool,
) -> list[dict[str, Any]]:
    """Rank tickers by momentum after the absolute-momentum (trend) filter."""
    ranked: list[dict[str, Any]] = []
    for sym, d in (daily or {}).items():
        if not isinstance(d, dict):
            continue
        mom = _momentum(d)
        if mom is None:
            continue
        above = d.get("above_sma200")
        if require_uptrend and above is not True:
            continue
        if require_positive_momentum and mom <= 0:
            continue
        ranked.append(
            {
                "ticker": sym,
                "momentum": round(mom, 2),
                "price": _num(d.get("price")),
                "above_sma200": above,
                "dist_sma200_pct": _num(d.get("dist_sma200_pct")),
            }
        )
    ranked.sort(key=lambda r: r["momentum"], reverse=True)
    return ranked


def compute_tilt_plan(
    *,
    universe: list[str] | None = None,
    portfolio_marked: dict[str, Any] | None = None,
    rebalance: bool = True,
) -> dict[str, Any]:
    """Compute the target portfolio and the trades needed to reach it.

    When ``rebalance`` is True this is a full monthly rebalance (enter new
    leaders, exit names that left the top-N, trim/add drifted holds). When False
    it only computes the trend-brake SELLs for held names that broke their
    200-day trend (used on non-rebalance days).
    """
    settings = get_settings()
    top_n = int(settings.tilt_top_n)
    band = float(settings.tilt_rebalance_band_pct)
    min_trade = float(settings.tilt_min_trade_usd)

    universe = universe or tilt_universe()
    marked = portfolio_marked or portfolio_with_marks()
    cash = float(marked.get("cash") or 0.0)
    positions = marked.get("positions") or {}
    total_value = float(marked.get("total_value") or (cash))

    daily = compute_daily_context_batch(universe, period="2y")
    ranked = rank_universe(
        daily,
        require_uptrend=settings.tilt_require_uptrend,
        require_positive_momentum=settings.tilt_require_positive_momentum,
    )
    selected = ranked[:top_n]
    selected_syms = {r["ticker"] for r in selected}
    target_each = total_value / top_n if top_n > 0 else 0.0

    def held_value(sym: str) -> float:
        pos = positions.get(sym) or {}
        return float(pos.get("market_value") or 0.0)

    def held_shares(sym: str) -> float:
        pos = positions.get(sym) or {}
        return float(pos.get("shares") or 0.0)

    def last_price(sym: str) -> float | None:
        pos = positions.get(sym) or {}
        p = _num(pos.get("last_price"))
        if p:
            return p
        return _num((daily.get(sym) or {}).get("price"))

    trades: list[dict[str, Any]] = []

    # --- Trend-brake SELLs: held names that broke their 200d trend -----------
    broke_trend: set[str] = set()
    for sym in list(positions.keys()):
        d = daily.get(sym) or {}
        above = d.get("above_sma200")
        if above is False and held_shares(sym) > 0:
            broke_trend.add(sym)
            trades.append(
                {
                    "action": "SELL",
                    "ticker": sym,
                    "shares": held_shares(sym),
                    "value": held_value(sym),
                    "price": last_price(sym),
                    "kind": "trend_brake",
                    "reasons": [
                        f"{sym} fell below its 200-day average — trend brake: exit to cash",
                        f"Momentum rank filter no longer met (dist to 200d: "
                        f"{d.get('dist_sma200_pct')}%)",
                    ],
                }
            )

    if not rebalance:
        return _finalize_plan(
            trades, selected, ranked, cash, total_value, target_each, rebalance=False
        )

    # --- Full monthly rebalance ---------------------------------------------
    # Exits: held names that left the top-N (and weren't already trend-braked)
    for sym in list(positions.keys()):
        if sym in broke_trend or held_shares(sym) <= 0:
            continue
        if sym not in selected_syms:
            trades.append(
                {
                    "action": "SELL",
                    "ticker": sym,
                    "shares": held_shares(sym),
                    "value": held_value(sym),
                    "price": last_price(sym),
                    "kind": "exit_rank",
                    "reasons": [
                        f"{sym} dropped out of the top-{top_n} momentum names — rotate out",
                    ],
                }
            )

    # Entries / adjustments for the selected leaders
    for r in selected:
        sym = r["ticker"]
        price = last_price(sym) or r.get("price")
        cur_val = held_value(sym)
        if cur_val <= 0:
            dollars = target_each
            if dollars >= min_trade and price:
                trades.append(
                    {
                        "action": "BUY",
                        "ticker": sym,
                        "value": round(dollars, 2),
                        "price": price,
                        "kind": "entry",
                        "momentum": r["momentum"],
                        "reasons": [
                            f"{sym} is a top-{top_n} momentum leader (12-1 momentum "
                            f"{r['momentum']}%)",
                            f"Above its 200-day average — target equal weight "
                            f"(~${target_each:,.0f})",
                        ],
                    }
                )
            continue
        # Existing hold: rebalance only if drift exceeds the band
        if target_each <= 0:
            continue
        drift = (cur_val - target_each) / target_each
        if drift > band:
            dollars = cur_val - target_each
            if dollars >= min_trade and price:
                trades.append(
                    {
                        "action": "SELL",
                        "ticker": sym,
                        "shares": min(held_shares(sym), dollars / price),
                        "value": round(dollars, 2),
                        "price": price,
                        "kind": "trim",
                        "reasons": [
                            f"{sym} grew to {drift * 100:.0f}% over its equal-weight target "
                            f"— trim back to ~${target_each:,.0f}",
                        ],
                    }
                )
        elif drift < -band:
            dollars = target_each - cur_val
            if dollars >= min_trade and price:
                trades.append(
                    {
                        "action": "BUY",
                        "ticker": sym,
                        "value": round(dollars, 2),
                        "price": price,
                        "kind": "add",
                        "momentum": r["momentum"],
                        "reasons": [
                            f"{sym} is below its equal-weight target — top up to "
                            f"~${target_each:,.0f}",
                        ],
                    }
                )

    return _finalize_plan(
        trades, selected, ranked, cash, total_value, target_each, rebalance=True
    )


def _finalize_plan(
    trades: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
    cash: float,
    total_value: float,
    target_each: float,
    *,
    rebalance: bool,
) -> dict[str, Any]:
    return {
        "rebalance": rebalance,
        "trades": trades,
        "target": [{"ticker": r["ticker"], "momentum": r["momentum"]} for r in selected],
        "ranking": ranked,
        "cash": round(cash, 2),
        "total_value": round(total_value, 2),
        "target_each": round(target_each, 2),
        "top_n": get_settings().tilt_top_n,
    }


# ---------------------------------------------------------------------------
# Monthly cadence state (idempotent rebalancing)
# ---------------------------------------------------------------------------


def _tilt_state() -> dict[str, Any]:
    try:
        doc = get_db().tilt_state.find_one({"_id": "state"})
        return doc or {}
    except Exception:  # noqa: BLE001
        return {}


def _set_tilt_state(**fields: Any) -> None:
    try:
        get_db().tilt_state.update_one(
            {"_id": "state"},
            {"$set": {**fields, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
    except Exception:  # noqa: BLE001
        logger.warning("Could not persist tilt state", exc_info=True)


def _pending_sell_exists(ticker: str) -> bool:
    """Avoid re-alerting the same trend-brake SELL every run."""
    try:
        cur = get_db().recommendations.find(
            {"ticker": ticker.upper(), "action": "SELL", "strategy": "momentum_tilt"}
        ).sort("ts", -1).limit(1)
        for doc in cur:
            return (doc.get("status") or "pending") == "pending"
    except Exception:  # noqa: BLE001
        pass
    return False


def _confidence_for(kind: str) -> int:
    # All tilt trades should clear the notify gate; exits/brakes are highest.
    return {
        "trend_brake": 85,
        "exit_rank": 80,
        "trim": 70,
        "entry": 74,
        "add": 70,
    }.get(kind, 72)


def _rec_from_trade(trade: dict[str, Any], *, cash: float) -> dict[str, Any]:
    action = trade["action"]
    sym = trade["ticker"]
    kind = trade.get("kind", "")
    is_sell = action == "SELL"
    # For a full exit, inflate the dollar amount slightly so amount/price >=
    # owned shares (execute caps at owned) → clean full liquidation.
    if is_sell and kind in {"trend_brake", "exit_rank"}:
        investment = round(float(trade.get("value") or 0.0) * 1.03, 2)
    else:
        investment = round(float(trade.get("value") or 0.0), 2)
    horizon = "SWING — monthly momentum tilt"
    return {
        "ticker": sym,
        "action": action,
        "investment": investment,
        "confidence": _confidence_for(kind),
        "risk": "MEDIUM",
        "time_horizon": horizon,
        "expected_return": "—",
        "reasoning": list(trade.get("reasons") or []),
        "risk_notes": [],
        "strategy": "momentum_tilt",
        "tilt_kind": kind,
        "remaining_cash": round(cash, 2),
        "confidence_gated": False,
    }


def run_tilt_rebalance(
    *,
    force: bool = False,
    send_notification: bool = True,
) -> dict[str, Any]:
    """Run the tilt engine and emit BUY/SELL recommendations to confirm.

    - On the first run of a new month (or ``force``) it does a full rebalance.
    - Otherwise it only issues trend-brake SELLs for held names that broke their
      200-day trend. Quiet (no notification) when there is nothing to do.
    """
    settings = get_settings()
    if not settings.tilt_enabled and not force:
        return {"skipped": True, "reason": "tilt_disabled"}

    now = now_market()
    ym = now.strftime("%Y-%m")
    state = _tilt_state()
    is_rebalance = force or (state.get("last_rebalance_ym") != ym)

    try:
        marked = portfolio_with_marks()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Tilt: could not load portfolio")
        return {"ok": False, "error": f"portfolio unavailable: {exc}"}

    plan = compute_tilt_plan(portfolio_marked=marked, rebalance=is_rebalance)
    cash = float(plan.get("cash") or 0.0)

    # De-dupe trend-brake sells that are already pending
    emitted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    # Emit BUYs first then SELLs so SELLs (newer ts) surface first in the queue.
    ordered = sorted(plan["trades"], key=lambda t: 0 if t["action"] == "BUY" else 1)
    for trade in ordered:
        if trade["action"] == "SELL" and trade.get("kind") == "trend_brake":
            if _pending_sell_exists(trade["ticker"]):
                skipped.append({**trade, "skip_reason": "already_pending"})
                continue
        rec = _rec_from_trade(trade, cash=cash)
        if float(rec.get("investment") or 0) < 1 and rec["action"] == "BUY":
            skipped.append({**trade, "skip_reason": "amount_too_small"})
            continue
        rec_id = save_recommendation(
            rec,
            extras={
                "trigger": "tilt_rebalance" if is_rebalance else "tilt_trend_brake",
                "strategy": "momentum_tilt",
                "tilt_kind": trade.get("kind"),
                "tilt_target": plan.get("target"),
                "momentum": trade.get("momentum"),
            },
        )
        rec["recommendation_id"] = rec_id
        confirm_base = (settings.hisaab_base_url or settings.public_base_url).rstrip("/")
        confirm_path = "/trades" if settings.hisaab_base_url else "/desk"
        rec["desk_url"] = f"{confirm_base}{confirm_path}?id={rec_id}"

        notify_result = None
        notify_ok, notify_reason = should_notify(rec)
        if send_notification and notify_ok:
            try:
                notify_result = notify_recommendation(rec, recommendation_id=rec_id)
            except Exception:  # noqa: BLE001
                logger.warning("Tilt notify failed for %s", trade["ticker"], exc_info=True)
        emitted.append(
            {
                "recommendation_id": rec_id,
                "action": rec["action"],
                "ticker": rec["ticker"],
                "investment": rec["investment"],
                "kind": trade.get("kind"),
                "notified": bool(notify_result and notify_result.get("ok")),
                "notify_reason": notify_reason,
            }
        )

    if is_rebalance:
        _set_tilt_state(last_rebalance_ym=ym, last_rebalance_at=datetime.now(timezone.utc))

    logger.info(
        "Tilt %s: %s trade(s) emitted, %s skipped (target=%s)",
        "REBALANCE" if is_rebalance else "trend-brake check",
        len(emitted),
        len(skipped),
        [t["ticker"] for t in plan.get("target", [])],
    )
    return {
        "ok": True,
        "rebalance": is_rebalance,
        "month": ym,
        "emitted": emitted,
        "skipped": skipped,
        "target": plan.get("target"),
        "target_each": plan.get("target_each"),
        "total_value": plan.get("total_value"),
        "cash": plan.get("cash"),
        "top_n": plan.get("top_n"),
    }
