"""Historical backtester — does the deterministic engine actually make money?

This replays the SAME logic the live desk uses (``signals.score_ticker`` for
entries, the ``exits.py`` stop/target/time rules for exits, and the SPY+VIX
market-regime gate) over daily history, so the results reflect the real
strategy rather than an idealized proxy. It is a swing/daily backtest by design:
yfinance only serves ~60 days of intraday data, and the engine's edge lives in
the daily trend/momentum layer anyway (the intraday sleeve is just timing).

Honest assumptions (stated in the output so results aren't oversold):
- Signals are computed at each day's CLOSE; entries fill at the NEXT day's OPEN
  (no look-ahead).
- Stops fill at the stop price when the day's LOW pierces it; take-profits fill
  at the target when the day's HIGH reaches it; stop is checked before target.
- Equal-weight sizing across up to ``max_positions`` slots, scaled by the regime
  size factor. Fractional shares, no commissions/slippage/taxes.
- This is a single-name long-only equity backtest on a small watchlist — far
  noisier than the diversified futures universes the source research tested.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


def _regime_from_values(
    spy_last: float | None,
    spy_sma: float | None,
    vix: float | None,
    settings: Any,
) -> dict[str, Any]:
    """Replicate ``signals.market_regime`` decision for a historical point."""
    caution = float(settings.regime_vix_caution)
    risk_off_vix = float(settings.regime_vix_risk_off)
    spy_above = None if (spy_last is None or spy_sma is None) else (spy_last >= spy_sma)

    if spy_above is None and vix is None:
        state = "unknown"
    elif (spy_above is False) or (vix is not None and vix >= risk_off_vix):
        state = "risk_off"
    elif (vix is not None and vix >= caution) or (spy_above is None):
        state = "neutral"
    else:
        state = "risk_on"

    size_factor = {"risk_on": 1.0, "neutral": 0.6, "risk_off": 0.0, "unknown": 0.85}[state]
    allow_new_buys = not (state == "risk_off" and settings.regime_block_buys_in_risk_off)
    return {"state": state, "allow_new_buys": allow_new_buys, "size_factor": size_factor}


def _prepare_frame(df: "Any") -> "Any":
    """Attach live indicator columns plus daily-context columns used by scoring."""
    from app.indicators import add_indicator_columns

    add_indicator_columns(df)
    close = df["Close"]
    vol = df.get("Volume")
    df["SMA200"] = close.rolling(200).mean()
    if vol is not None:
        df["RELVOL"] = vol / vol.rolling(20).mean()
    df["RET_3M"] = close.pct_change(63) * 100
    df["RET_6M"] = close.pct_change(126) * 100
    # Jegadeesh & Titman 12-1: 12-month return skipping the most recent month
    df["MOM_12_1"] = (close.shift(22) / close.shift(252) - 1) * 100
    return df


def _daily_dict(frame: "Any", pos: int) -> dict[str, Any]:
    import pandas as pd

    def cell(col: str) -> float | None:
        s = frame.get(col)
        if s is None:
            return None
        v = s.iloc[pos]
        return None if pd.isna(v) else float(v)

    price = cell("Close")
    sma200 = cell("SMA200")
    above = None
    dist = None
    if price is not None and sma200:
        above = price >= sma200
        dist = round((price - sma200) / sma200 * 100, 2)
    return {
        "price": price,
        "sma50": cell("SMA50"),
        "sma200": sma200,
        "ema50": cell("EMA50"),
        "ema200": cell("EMA200"),
        "rsi": cell("RSI"),
        "atr": cell("ATR"),
        "adx": cell("ADX"),
        "plus_di": cell("DMP"),
        "minus_di": cell("DMN"),
        "rel_volume": cell("RELVOL"),
        "above_sma200": above,
        "dist_sma200_pct": dist,
        "ret_3m": cell("RET_3M"),
        "ret_6m": cell("RET_6M"),
        "mom_12_1": cell("MOM_12_1"),
    }


def run_backtest(
    symbols: list[str] | None = None,
    *,
    years: float = 2.0,
    starting_cash: float = 10_000.0,
    max_positions: int = 5,
    buy_threshold: float | None = None,
    take_profit_pct: float | None = None,
    stop_loss_pct: float | None = None,
    atr_initial_mult: float | None = None,
    atr_trail_mult: float | None = None,
    time_stop_days: int | None = None,
) -> dict[str, Any]:
    """Replay the deterministic engine over daily history and report performance.

    Exit parameters can be overridden to tune the strategy. A ``take_profit_pct``
    of 0 disables the fixed take-profit entirely (pure "let winners run" mode,
    where only the ATR trailing stop closes a winner). ``time_stop_days`` of 0
    disables the time stop.
    """
    import pandas as pd
    import yfinance as yf

    from app.indicators import snapshot_from_frame
    from app.signals import cross_sectional_momentum, score_ticker

    settings = get_settings()
    symbols = [s.upper().strip() for s in (symbols or settings.watchlist_symbols) if s and s.strip()]
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        raise ValueError("No symbols to backtest")

    def _pick(override, default):
        return float(default) if override is None else float(override)

    buy_bar = float(buy_threshold if buy_threshold is not None else settings.signal_buy_threshold)
    tp_pct = _pick(take_profit_pct, settings.position_take_profit_pct)
    sl_pct = _pick(stop_loss_pct, settings.position_stop_loss_pct)
    init_mult = _pick(atr_initial_mult, settings.exit_initial_stop_atr_mult)
    trail_mult = _pick(atr_trail_mult, settings.exit_trail_atr_mult)
    time_days = int(settings.exit_time_stop_days if time_stop_days is None else time_stop_days)
    time_min_profit = float(settings.exit_time_stop_min_profit_pct)
    spy_sym = settings.regime_index_symbol
    vix_sym = settings.regime_vix_symbol

    # One batched download: watchlist + SPY + VIX, with ~1y warmup for SMA200.
    dl = list(dict.fromkeys(symbols + [spy_sym, vix_sym]))
    period = f"{int(math.ceil(years)) + 2}y"
    raw = yf.download(
        dl, period=period, interval="1d", progress=False, auto_adjust=True, threads=False, group_by="ticker"
    )
    if raw is None or raw.empty:
        raise RuntimeError("No historical data returned")
    multi = isinstance(raw.columns, pd.MultiIndex)

    def sub(sym: str):
        if multi:
            if sym not in raw.columns.get_level_values(0):
                return None
            return raw[sym].dropna(how="all")
        return raw.dropna(how="all")

    frames: dict[str, Any] = {}
    for sym in symbols:
        s = sub(sym)
        if s is not None and not s.empty and "Close" in s.columns and len(s) > 220:
            frames[sym] = _prepare_frame(s.copy())
    if not frames:
        raise RuntimeError("No tickers had enough history to backtest")

    # SPY frame for calendar + regime; VIX close for regime.
    spy = sub(spy_sym)
    if spy is None or spy.empty:
        raise RuntimeError("SPY history unavailable for regime/benchmark")
    spy = spy.copy()
    spy["SMA200"] = spy["Close"].rolling(200).mean()
    vix_df = sub(vix_sym)
    vix_close = vix_df["Close"] if vix_df is not None and "Close" in vix_df.columns else None

    # Trading calendar = SPY sessions within the requested window (post-warmup).
    calendar = list(spy.index)
    warmup = 210
    start_i = max(warmup, len(calendar) - int(round(years * 252)))
    calendar_bt = calendar[start_i:]
    if len(calendar_bt) < 30:
        raise RuntimeError("Backtest window too short after warmup")

    # Fast position lookup per ticker: date -> row position
    pos_index: dict[str, dict[Any, int]] = {
        sym: {ts: i for i, ts in enumerate(f.index)} for sym, f in frames.items()
    }

    cash = float(starting_cash)
    positions: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    pending_entries: list[str] = []

    def price_at(sym: str, date: Any, col: str) -> float | None:
        f = frames.get(sym)
        i = pos_index.get(sym, {}).get(date)
        if f is None or i is None:
            return None
        v = f[col].iloc[i]
        return None if pd.isna(v) else float(v)

    for day_idx, date in enumerate(calendar_bt):
        # --- 1) Fill pending entries at today's OPEN (signals were from yesterday)
        for sym in pending_entries:
            if sym in positions:
                continue
            entry_open = price_at(sym, date, "Open")
            if entry_open is None or entry_open <= 0:
                continue
            slots_free = max_positions - len(positions)
            if slots_free <= 0 or cash <= 0:
                break
            regime_now = _current_regime(spy, vix_close, date, settings, _regime_from_values)
            budget = min(cash, (cash + _holdings_value(positions, date, price_at)) / max_positions)
            budget *= float(regime_now["size_factor"] or 0)
            if budget <= 1:
                continue
            shares = budget / entry_open
            atr0 = price_at(sym, date, "ATR")
            positions[sym] = {
                "shares": shares,
                "entry_price": entry_open,
                "entry_date": str(date.date()) if hasattr(date, "date") else str(date),
                "entry_idx": day_idx,
                "high_water": entry_open,
                "entry_atr": atr0,
            }
            cash -= shares * entry_open
        pending_entries = []

        # --- 2) Manage open positions against the exit rules (intra-day fills)
        for sym in list(positions.keys()):
            pos = positions[sym]
            hi = price_at(sym, date, "High")
            lo = price_at(sym, date, "Low")
            cl = price_at(sym, date, "Close")
            atr = price_at(sym, date, "ATR")
            if cl is None:
                continue
            if hi is not None and hi > pos["high_water"]:
                pos["high_water"] = hi

            entry_price = pos["entry_price"]
            high_water = pos["high_water"]
            if atr and atr > 0:
                initial_stop = entry_price - init_mult * atr
                chandelier = high_water - trail_mult * atr
            else:
                initial_stop = entry_price * (1 - sl_pct / 100.0)
                chandelier = high_water * (1 - sl_pct / 100.0)
            effective_stop = max(initial_stop, chandelier)
            trailing_active = high_water > entry_price and chandelier >= initial_stop
            tp_price = entry_price * (1 + tp_pct / 100.0)
            days_held = day_idx - pos["entry_idx"]

            exit_price = None
            kind = None
            if lo is not None and lo <= effective_stop:
                exit_price = effective_stop
                kind = "trailing_stop" if trailing_active else "stop_loss"
            elif tp_pct > 0 and hi is not None and hi >= tp_price:
                exit_price = tp_price
                kind = "take_profit"
            elif time_days > 0 and days_held >= time_days:
                cl_pnl = (cl - entry_price) / entry_price * 100.0
                if cl_pnl < time_min_profit:
                    exit_price = cl
                    kind = "time_stop"

            if exit_price is not None:
                pnl = (exit_price - entry_price) * pos["shares"]
                pnl_pct = (exit_price / entry_price - 1) * 100.0
                cash += pos["shares"] * exit_price
                trades.append(
                    {
                        "ticker": sym,
                        "kind": kind,
                        "entry_date": pos["entry_date"],
                        "exit_date": str(date.date()) if hasattr(date, "date") else str(date),
                        "entry_price": round(entry_price, 4),
                        "exit_price": round(exit_price, 4),
                        "days_held": days_held,
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                    }
                )
                positions.pop(sym, None)

        # --- 3) Generate entry signals at TODAY's close (fill next session)
        slots_free = max_positions - len(positions)
        if slots_free > 0 and day_idx < len(calendar_bt) - 1:
            regime = _current_regime(spy, vix_close, date, settings, _regime_from_values)
            if regime["allow_new_buys"] and regime["size_factor"] > 0:
                daily_ctx: dict[str, Any] = {}
                snaps: dict[str, Any] = {}
                for sym, f in frames.items():
                    if sym in positions:
                        continue
                    i = pos_index[sym].get(date)
                    if i is None or i < warmup:
                        continue
                    daily_ctx[sym] = _daily_dict(f, i)
                    snaps[sym] = snapshot_from_frame(f, sym, i)
                xmom = cross_sectional_momentum(daily_ctx)
                ranked: list[tuple[str, float]] = []
                for sym, snap in snaps.items():
                    res = score_ticker(snap, daily_ctx.get(sym), xmom.get(sym))
                    if res.get("signal") == "BUY" and float(res.get("score") or 0) >= buy_bar:
                        ranked.append((sym, float(res["score"])))
                ranked.sort(key=lambda kv: kv[1], reverse=True)
                pending_entries = [s for s, _ in ranked[:slots_free]]

        # --- 4) Mark-to-market equity at close
        equity = cash + _holdings_value(positions, date, price_at)
        equity_curve.append(
            {"date": str(date.date()) if hasattr(date, "date") else str(date), "equity": round(equity, 2)}
        )

    # Close any still-open positions at the last close (for honest final equity)
    last_date = calendar_bt[-1]
    for sym in list(positions.keys()):
        cl = price_at(sym, last_date, "Close")
        if cl is None:
            continue
        pos = positions[sym]
        pnl = (cl - pos["entry_price"]) * pos["shares"]
        cash += pos["shares"] * cl
        trades.append(
            {
                "ticker": sym,
                "kind": "open_at_end",
                "entry_date": pos["entry_date"],
                "exit_date": str(last_date.date()) if hasattr(last_date, "date") else str(last_date),
                "entry_price": round(pos["entry_price"], 4),
                "exit_price": round(cl, 4),
                "days_held": len(calendar_bt) - 1 - pos["entry_idx"],
                "pnl": round(pnl, 2),
                "pnl_pct": round((cl / pos["entry_price"] - 1) * 100.0, 2),
            }
        )
        positions.pop(sym, None)

    final_equity = cash
    metrics = _metrics(starting_cash, final_equity, equity_curve, trades, years)
    benchmark = _benchmark(spy, calendar_bt)

    return {
        "params": {
            "symbols": list(frames.keys()),
            "years": years,
            "sessions": len(calendar_bt),
            "start": equity_curve[0]["date"] if equity_curve else None,
            "end": equity_curve[-1]["date"] if equity_curve else None,
            "starting_cash": starting_cash,
            "max_positions": max_positions,
            "buy_threshold": buy_bar,
            "take_profit_pct": tp_pct,
            "stop_loss_pct": sl_pct,
            "atr_initial_mult": init_mult,
            "atr_trail_mult": trail_mult,
            "time_stop_days": time_days,
        },
        "metrics": metrics,
        "benchmark_spy": benchmark,
        "trades": trades,
        "equity_curve": equity_curve,
        "assumptions": [
            "Signals at close; entries fill next-day open (no look-ahead).",
            "Stops fill at stop when day low pierces it; take-profit at target when day high reaches it.",
            "Equal-weight sizing across slots, scaled by regime size factor. Fractional shares.",
            "No commissions, slippage, or taxes. Long-only. Daily timeframe.",
        ],
    }


def _holdings_value(positions: dict[str, Any], date: Any, price_at) -> float:
    total = 0.0
    for sym, pos in positions.items():
        cl = price_at(sym, date, "Close")
        if cl is not None:
            total += pos["shares"] * cl
    return total


def _current_regime(spy, vix_close, date, settings, regime_fn) -> dict[str, Any]:
    import pandas as pd

    try:
        i = spy.index.get_loc(date)
    except KeyError:
        return {"state": "unknown", "allow_new_buys": True, "size_factor": 0.85}
    spy_last = float(spy["Close"].iloc[i]) if pd.notna(spy["Close"].iloc[i]) else None
    spy_sma = float(spy["SMA200"].iloc[i]) if pd.notna(spy["SMA200"].iloc[i]) else None
    vix = None
    if vix_close is not None and date in vix_close.index:
        v = vix_close.loc[date]
        vix = float(v) if pd.notna(v) else None
    return regime_fn(spy_last, spy_sma, vix, settings)


def _metrics(start: float, end: float, curve: list[dict], trades: list[dict], years: float) -> dict[str, Any]:
    import statistics

    total_return = (end / start - 1) * 100.0 if start else 0.0
    yrs = max(years, len(curve) / 252.0) if curve else years
    cagr = ((end / start) ** (1.0 / yrs) - 1) * 100.0 if start > 0 and yrs > 0 else 0.0

    # Max drawdown + daily returns for Sharpe
    peak = start
    max_dd = 0.0
    eq = [start] + [c["equity"] for c in curve]
    daily_rets: list[float] = []
    for i in range(1, len(eq)):
        peak = max(peak, eq[i])
        if peak > 0:
            max_dd = min(max_dd, eq[i] / peak - 1.0)
        if eq[i - 1] > 0:
            daily_rets.append(eq[i] / eq[i - 1] - 1.0)
    sharpe = 0.0
    if len(daily_rets) > 2:
        sd = statistics.pstdev(daily_rets)
        if sd > 0:
            sharpe = (statistics.fmean(daily_rets) / sd) * math.sqrt(252)

    closed = [t for t in trades if t["kind"] != "open_at_end"] + [
        t for t in trades if t["kind"] == "open_at_end"
    ]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    win_rate = (len(wins) / len(closed) * 100.0) if closed else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    by_kind: dict[str, int] = {}
    for t in closed:
        by_kind[t["kind"]] = by_kind.get(t["kind"], 0) + 1

    return {
        "total_return_pct": round(total_return, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(max_dd * 100.0, 2),
        "sharpe": round(sharpe, 2),
        "final_equity": round(end, 2),
        "trades": len(closed),
        "win_rate_pct": round(win_rate, 1),
        "wins": len(wins),
        "losses": len(losses),
        "avg_win_pct": round(statistics.fmean([t["pnl_pct"] for t in wins]), 2) if wins else 0.0,
        "avg_loss_pct": round(statistics.fmean([t["pnl_pct"] for t in losses]), 2) if losses else 0.0,
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
        "avg_days_held": round(statistics.fmean([t["days_held"] for t in closed]), 1) if closed else 0.0,
        "exits_by_kind": by_kind,
    }


def _benchmark(spy, calendar_bt: list) -> dict[str, Any]:
    import pandas as pd

    try:
        first = float(spy["Close"].loc[calendar_bt[0]])
        last = float(spy["Close"].loc[calendar_bt[-1]])
    except (KeyError, ValueError):
        return {}
    if not first or pd.isna(first) or pd.isna(last):
        return {}
    return {"buy_hold_return_pct": round((last / first - 1) * 100.0, 2)}
