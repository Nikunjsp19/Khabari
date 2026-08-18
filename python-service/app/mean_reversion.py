"""Connors short-term swing engine — the counterweight to the momentum tilt.

Larry Connors / Cesar Alvarez, *Short Term Trading Strategies That Work* (2009).
Daily bars, hold 1–7 sessions — swing, not day-trade.

Two setups, same book:

* **Index ETFs (SPY/QQQ/IWM)** — Double 7s: above 200d SMA, close at a 7-day
  low, exit at a 7-day high. Connors' published ETF rules.
* **Individual stocks** — RSI(2): above 200d SMA, below 5d SMA, RSI(2) < 10,
  at least two down closes in a row. Exit on 5d SMA reclaim, RSI(2) > 70, or
  a 7-session time stop.

VIX overlay (Connors Rule 4): skip *new* longs when the VIX is stretched
below its 10-day MA (complacency). Elevated VIX is a green light, not a block.

ProGo is off by default here — last year's replay showed it throwing out the
majority of valid dips. Leveraged / inverse single-stock ETFs are stripped
from the universe: a 2x product grinding lower is not a Connors dip.

Both engines draw on the SAME cash pool. `strategy_book` decides who owns a
ticker so neither exits the other's position.

Signals are evaluated on the close; Connors fills at the next open. The
scheduler therefore runs this late in the session so the alert lands while you
can still act on it.

Module-level rule functions are stdlib-only and take plain dicts, so they can be
tested without pulling pandas. Heavy imports are deferred into the runners.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

STRATEGY = "mean_reversion"


def _num(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def evaluate_entry(
    row: dict[str, Any] | None,
    *,
    setup: str = "rsi2",
    rsi_entry: float = 10.0,
    min_down_days: int = 2,
    require_progo: bool = False,
    vix_stretch_pct: float | None = None,
    vix_complacency_pct: float = -5.0,
) -> tuple[bool, list[str]]:
    """Connors swing long. Returns (is_signal, reasons).

    Fails closed on missing price/trend inputs. Missing VIX or down-day count
    fails *open* so a data hole does not silence the engine.
    """
    row = row or {}
    price = _num(row.get("price"))
    sma200 = _num(row.get("sma200"))
    if price is None or sma200 is None:
        return False, []
    if price < sma200:
        return False, []

    # Connors Rule 4: VIX stretched *below* its 10-day MA = complacency, skip.
    # Stretched above (fear) is a green light. Missing VIX does not block.
    if (
        vix_stretch_pct is not None
        and vix_stretch_pct < float(vix_complacency_pct)
    ):
        return False, []

    progo = row.get("progo") if isinstance(row.get("progo"), dict) else None
    regime = (progo or {}).get("regime") or row.get("progo_regime")
    if require_progo and regime == "distribution":
        return False, []

    kind = (setup or "rsi2").strip().lower()
    reasons: list[str] = []

    if kind == "double7s":
        at_low = row.get("at_7d_low")
        if at_low is not True:
            return False, []
        reasons = [
            f"${price:.2f} closed at a 7-day low — Double 7s pullback on an index ETF",
            f"Still above the 200-day ${sma200:.2f} — buying a dip inside an uptrend",
        ]
    else:
        sma5 = _num(row.get("sma5"))
        rsi2 = _num(row.get("rsi2"))
        if sma5 is None or rsi2 is None:
            return False, []
        if price >= sma5:
            return False, []
        if rsi2 >= float(rsi_entry):
            return False, []
        down = row.get("consecutive_down_days")
        down_n = int(down) if isinstance(down, (int, float)) and down == down else None
        if down_n is not None and down_n < int(min_down_days):
            return False, []
        reasons = [
            f"RSI(2) {rsi2:.1f} below {float(rsi_entry):.0f} — short, sharp oversold flush",
            f"Price ${price:.2f} under its 5-day average ${sma5:.2f} (pullback, not a drift)",
            f"Still above the 200-day ${sma200:.2f} — buying a dip inside an uptrend",
        ]
        if down_n is not None and down_n >= 2:
            reasons.append(
                f"{down_n} consecutive down closes — Connors' edge grows with the flush"
            )

    if vix_stretch_pct is not None and vix_stretch_pct >= 5.0:
        reasons.append(
            f"VIX {vix_stretch_pct:+.1f}% vs its 10-day average — fear stretched, bounce odds up"
        )
    if regime == "accumulation":
        reasons.append("ProGo accumulation — desks bought the close through the dip")
    return True, reasons


def evaluate_exit(
    row: dict[str, Any] | None,
    *,
    setup: str = "rsi2",
    rsi_exit: float = 70.0,
    max_hold_days: int = 7,
    days_held: float | None = None,
) -> tuple[bool, str | None]:
    """Swing exit: regime break, mechanical reclaim, or 7-session time stop.

    Connors' horizon is 1–7 days. No hard stop — his research found stops
    cut this strategy's expectancy. The time stop is the safety valve.
    """
    row = row or {}
    price = _num(row.get("price"))
    sma200 = _num(row.get("sma200"))
    if price is None:
        return False, None

    if sma200 is not None and price < sma200:
        return True, (
            f"${price:.2f} lost the 200-day ${sma200:.2f} — the uptrend that "
            f"justified the dip buy is gone, exit"
        )

    kind = (setup or "rsi2").strip().lower()
    if kind == "double7s":
        if row.get("at_7d_high") is True:
            return True, (
                f"${price:.2f} closed at a 7-day high — Double 7s bounce is in, take it"
            )
    else:
        sma5 = _num(row.get("sma5"))
        rsi2 = _num(row.get("rsi2"))
        if sma5 is not None and price > sma5:
            return True, (
                f"${price:.2f} reclaimed its 5-day average ${sma5:.2f} — "
                f"mean reversion played out, take it"
            )
        if rsi2 is not None and rsi2 > float(rsi_exit):
            return True, (
                f"RSI(2) {rsi2:.1f} back above {float(rsi_exit):.0f} — short-term bounce done"
            )

    if (
        max_hold_days > 0
        and days_held is not None
        and days_held >= float(max_hold_days)
    ):
        return True, (
            f"Held {days_held:.0f} sessions (cap {max_hold_days}) — Connors' "
            f"1–7 day swing window is up, free the cash"
        )
    return False, None


def mean_reversion_universe() -> list[str]:
    from app.config import get_settings
    from app.universe import DEFAULT_SWING_UNIVERSE, without_levered

    settings = get_settings()
    raw = (settings.mr_universe or "").strip()
    if raw:
        syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
    else:
        syms = list(DEFAULT_SWING_UNIVERSE)
    return without_levered(syms)


def fetch_vix_stretch() -> float | None:
    """VIX vs its 10-day SMA, as a percent. None if the series is missing.

    Connors Rule 4: stretched *above* the 10-day = fear (favor longs);
    stretched *below* = complacency (skip new longs).
    """
    try:
        import yfinance as yf

        raw = yf.download(
            "^VIX",
            period="1mo",
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=False,
        )
        if raw is None or raw.empty:
            return None
        close = raw["Close"]
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        close = close.dropna()
        if len(close) < 10:
            return None
        last = float(close.iloc[-1])
        sma10 = float(close.tail(10).mean())
        if sma10 <= 0:
            return None
        return round((last / sma10 - 1.0) * 100.0, 2)
    except Exception:  # noqa: BLE001
        logger.warning("VIX stretch fetch failed", exc_info=True)
        return None


def _days_held(ticker: str) -> float | None:
    from datetime import datetime, timezone

    from app.strategy_book import opened_at

    ts = opened_at(ticker)
    if ts is None:
        return None
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return max(0.0, (now - ts).total_seconds() / 86400.0)


def compute_mean_reversion_plan(
    *,
    universe: list[str] | None = None,
    portfolio_marked: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the BUY/SELL list for this strategy against the shared portfolio."""
    from app.config import get_settings
    from app.indicators import compute_daily_context_batch
    from app.strategy_book import claimed_by_others, owned_by, sleeve_state
    from app.trades import portfolio_with_marks
    from app.universe import setup_kind

    settings = get_settings()
    max_positions = max(1, int(settings.mr_max_positions))
    min_trade = float(settings.mr_min_trade_usd)
    vix_stretch = fetch_vix_stretch()

    universe = universe or mean_reversion_universe()
    marked = portfolio_marked or portfolio_with_marks()
    cash = float(marked.get("cash") or 0.0)
    positions = marked.get("positions") or {}
    total_value = float(marked.get("total_value") or cash)

    mine = owned_by(STRATEGY)
    foreign = claimed_by_others(STRATEGY)
    sleeve = sleeve_state(
        STRATEGY,
        cash=cash,
        total_value=total_value,
        positions=positions,
        owned=mine,
    )

    # Price our own open names plus anything we might enter.
    to_price = sorted(set(universe) | mine)
    daily = compute_daily_context_batch(to_price, period="1y")

    trades: list[dict[str, Any]] = []

    # --- Exits first: they free cash the entries below can use ---------------
    open_now = 0
    for sym in sorted(mine):
        pos = positions.get(sym) or {}
        shares = _num(pos.get("shares")) or 0.0
        if shares <= 0:
            continue
        row = daily.get(sym)
        should_exit, reason = evaluate_exit(
            row,
            setup=setup_kind(sym),
            rsi_exit=float(settings.mr_rsi_exit),
            max_hold_days=int(settings.mr_max_hold_days),
            days_held=_days_held(sym),
        )
        if should_exit:
            value = _num(pos.get("market_value")) or 0.0
            trades.append(
                {
                    "action": "SELL",
                    "ticker": sym,
                    "shares": shares,
                    "value": round(value, 2),
                    "price": _num(pos.get("last_price")) or _num((row or {}).get("price")),
                    "kind": "mr_exit",
                    "reasons": [reason] if reason else [],
                }
            )
        else:
            open_now += 1

    # --- Entries -------------------------------------------------------------
    free_slots = max(0, max_positions - open_now)
    exit_proceeds = sum(
        float(t.get("value") or 0.0) for t in trades if t.get("action") == "SELL"
    )
    remaining_cash = min(
        cash + exit_proceeds,
        float(sleeve.get("room") or 0.0) + exit_proceeds,
    )
    if free_slots > 0 and remaining_cash >= min_trade:
        slot_size = float(sleeve["budget"]) * (float(settings.mr_max_position_pct) / 100.0)
        candidates: list[dict[str, Any]] = []
        for sym in universe:
            if sym in mine or sym in foreign:
                continue
            # Don't stack a dip buy on top of an existing holding.
            if float((positions.get(sym) or {}).get("shares") or 0) > 0:
                continue
            row = daily.get(sym)
            kind = setup_kind(sym)
            ok, reasons = evaluate_entry(
                row,
                setup=kind,
                rsi_entry=float(settings.mr_rsi_entry),
                min_down_days=int(settings.mr_min_down_days),
                require_progo=bool(settings.progo_enabled and settings.mr_require_progo),
                vix_stretch_pct=vix_stretch,
                vix_complacency_pct=float(settings.mr_vix_complacency_pct),
            )
            if ok:
                candidates.append(
                    {
                        "ticker": sym,
                        "setup": kind,
                        "rsi2": _num((row or {}).get("rsi2")),
                        "price": _num((row or {}).get("price")),
                        "reasons": reasons,
                    }
                )
        # Deepest flush first: lowest RSI(2), then 7-day-low ETFs.
        candidates.sort(
            key=lambda c: (
                0 if c["setup"] == "rsi2" else 1,
                c["rsi2"] if c["rsi2"] is not None else 99,
            )
        )

        for c in candidates[:free_slots]:
            dollars = min(slot_size, remaining_cash)
            if dollars < min_trade or not c["price"]:
                continue
            trades.append(
                {
                    "action": "BUY",
                    "ticker": c["ticker"],
                    "value": round(dollars, 2),
                    "price": c["price"],
                    "kind": "mr_double7s" if c["setup"] == "double7s" else "mr_entry",
                    "rsi2": c["rsi2"],
                    "reasons": c["reasons"],
                }
            )
            remaining_cash = round(remaining_cash - dollars, 2)

    return {
        "strategy": STRATEGY,
        "trades": trades,
        "open_positions": sorted(mine),
        "free_slots": free_slots,
        "cash": round(cash, 2),
        "total_value": round(total_value, 2),
        "max_positions": max_positions,
        "scanned": len(universe),
        "vix_stretch_pct": vix_stretch,
        "universe": universe,
        "sleeve": sleeve,
    }


def _confidence_for(kind: str) -> int:
    return {"mr_exit": 88, "mr_entry": 74, "mr_double7s": 76}.get(kind, 70)


def _rec_from_trade(trade: dict[str, Any], *, cash: float) -> dict[str, Any]:
    kind = trade.get("kind", "")
    is_sell = trade["action"] == "SELL"
    # Pad a full exit so amount/price covers every share (execute caps at owned).
    value = float(trade.get("value") or 0.0)
    investment = round(value * 1.03, 2) if is_sell else round(value, 2)
    horizon = (
        "SWING — Double 7s index ETF (days)"
        if kind == "mr_double7s"
        else "SWING — RSI(2) mean reversion (1–7 days)"
    )
    return {
        "ticker": trade["ticker"],
        "action": trade["action"],
        "investment": investment,
        "confidence": _confidence_for(kind),
        "risk": "MEDIUM",
        "time_horizon": horizon,
        "expected_return": "—",
        "reasoning": list(trade.get("reasons") or []),
        "risk_notes": [],
        "strategy": STRATEGY,
        "mr_kind": kind,
        "remaining_cash": round(cash, 2),
        "confidence_gated": False,
    }


def _pending_exists(ticker: str, action: str) -> bool:
    """Don't re-alert the same signal on every run."""
    from app.db import get_db

    try:
        cur = (
            get_db()
            .recommendations.find(
                {"ticker": ticker.upper(), "action": action, "strategy": STRATEGY}
            )
            .sort("ts", -1)
            .limit(1)
        )
        for doc in cur:
            return (doc.get("status") or "pending") == "pending"
    except Exception:  # noqa: BLE001
        pass
    return False


def run_mean_reversion(*, send_notification: bool = True) -> dict[str, Any]:
    """Scan, emit BUY/SELL recommendations, notify. Quiet when there is nothing."""
    from app.config import get_settings
    from app.db import save_recommendation
    from app.gates import should_notify
    from app.notify import notify_recommendation
    from app.strategy_book import sync_with_positions
    from app.trades import portfolio_with_marks

    settings = get_settings()
    if not settings.stocks_trading_enabled:
        logger.info("Mean reversion skipped — STOCKS_TRADING_ENABLED=false")
        return {"skipped": True, "reason": "stocks_trading_paused"}
    if not settings.mean_reversion_enabled:
        return {"skipped": True, "reason": "mean_reversion_disabled"}

    try:
        marked = portfolio_with_marks()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Mean reversion: could not load portfolio")
        return {"ok": False, "error": f"portfolio unavailable: {exc}"}

    # Manual sells outside the app can leave stale claims behind.
    sync_with_positions(marked.get("positions"))

    plan = compute_mean_reversion_plan(portfolio_marked=marked)
    cash = float((plan.get("sleeve") or {}).get("available_cash") or plan.get("cash") or 0.0)

    emitted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    # SELLs first so freed cash is visible before the BUY alerts land.
    ordered = sorted(plan["trades"], key=lambda t: 0 if t["action"] == "SELL" else 1)
    for trade in ordered:
        if _pending_exists(trade["ticker"], trade["action"]):
            skipped.append({**trade, "skip_reason": "already_pending"})
            continue
        rec = _rec_from_trade(trade, cash=cash)
        if rec["action"] == "BUY" and float(rec.get("investment") or 0) < 1:
            skipped.append({**trade, "skip_reason": "amount_too_small"})
            continue

        rec_id = save_recommendation(
            rec,
            extras={
                "trigger": "mean_reversion",
                "strategy": STRATEGY,
                "mr_kind": trade.get("kind"),
                "rsi2": trade.get("rsi2"),
                "free_slots": plan.get("free_slots"),
                "sleeve": plan.get("sleeve"),
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
                logger.warning(
                    "Mean reversion notify failed for %s", trade["ticker"], exc_info=True
                )
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

    logger.info(
        "Mean reversion: %s emitted, %s skipped (open=%s, free slots=%s)",
        len(emitted),
        len(skipped),
        plan.get("open_positions"),
        plan.get("free_slots"),
    )
    return {
        "ok": True,
        "emitted": emitted,
        "skipped": skipped,
        "open_positions": plan.get("open_positions"),
        "free_slots": plan.get("free_slots"),
        "cash": plan.get("cash"),
        "total_value": plan.get("total_value"),
        "scanned": plan.get("scanned"),
        "sleeve": plan.get("sleeve"),
    }
