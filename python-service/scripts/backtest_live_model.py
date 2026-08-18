"""Backtest the live dual-engine stock model: $1,000, one year.

Replays Momentum Tilt, Connors Swing, and a shared-cash combined book using
the same universes and config as production.

    python scripts/backtest_live_model.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest import (  # noqa: E402
    run_combined_stock_backtest,
    run_mean_reversion_backtest,
    run_tilt_backtest,
)
from app.backtest_cache import CACHE_DIR, load_cached_frames  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.universe import DEFAULT_SWING_UNIVERSE, without_levered  # noqa: E402

YEARS = 1.0
CASH = 1000.0


def _tilt_symbols(settings) -> list[str]:
    raw = (settings.tilt_universe or "").strip()
    if raw:
        syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
    else:
        syms = [s.strip().upper() for s in settings.watchlist.split(",") if s.strip()]
    if settings.tilt_allow_levered:
        return list(dict.fromkeys(syms))
    return without_levered(syms)


def _swing_symbols(settings) -> list[str]:
    raw = (settings.mr_universe or "").strip()
    if raw:
        syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
    else:
        syms = list(DEFAULT_SWING_UNIVERSE)
    return without_levered(syms)


def _load_bundle(symbols: list[str], settings):
    syms = list(dict.fromkeys(symbols))
    if CACHE_DIR.exists() and any(CACHE_DIR.glob("*.json")):
        return load_cached_frames(syms, YEARS, settings)
    from app.backtest import _load_price_frames

    return _load_price_frames(syms, YEARS, settings)


def _summary(label: str, result: dict, start: float) -> str:
    m = result["metrics"]
    end = m["final_equity"]
    p = result["params"]
    window = f"{p.get('start', '?')} → {p.get('end', '?')}"
    return (
        f"{label}\n"
        f"  Window: {window} ({p.get('sessions', '?')} sessions)\n"
        f"  Starting: ${start:,.2f}  →  Ending: ${end:,.2f}  "
        f"(P/L ${end - start:+,.2f}, {m['total_return_pct']:+.1f}%)\n"
        f"  Max drawdown: {m['max_drawdown_pct']:.1f}%  |  "
        f"Sharpe: {m['sharpe']:.2f}  |  Trades: {m['trades']}  |  "
        f"Win rate: {m['win_rate_pct']:.1f}%"
    )


def main() -> None:
    settings = get_settings()
    tilt_syms = _tilt_symbols(settings)
    swing_syms = _swing_symbols(settings)
    all_syms = list(dict.fromkeys([*tilt_syms, *swing_syms]))

    print("Khabari live model backtest")
    print(f"  ${CASH:,.0f} starting cash, {YEARS:g} year lookback")
    print(f"  Tilt universe: {len(tilt_syms)} names (no leveraged ETFs)")
    print(f"  Connors universe: {len(swing_syms)} names\n")

    bundle = _load_bundle(all_syms, settings)
    frames, _spy, _vix, _cal, _warm, dropped, warns = bundle
    print(f"Loaded price history for {len(frames)} tickers")
    if dropped:
        print(f"  Dropped (no cache / stale): {', '.join(dropped[:12])}"
              + (" …" if len(dropped) > 12 else ""))
    for w in warns:
        print(f"  ! {w}")
    print()

    tilt = run_tilt_backtest(
        tilt_syms,
        years=YEARS,
        starting_cash=CASH,
        top_n=settings.tilt_top_n,
        bundle=bundle,
    )
    connors = run_mean_reversion_backtest(
        swing_syms,
        years=YEARS,
        starting_cash=CASH,
        bundle=bundle,
    )
    combined = run_combined_stock_backtest(
        tilt_syms,
        swing_syms,
        years=YEARS,
        starting_cash=CASH,
        bundle=bundle,
    )

    spy_ret = connors["benchmark_spy"].get("buy_hold_return_pct")
    spy_end = CASH * (1 + spy_ret / 100.0) if spy_ret is not None else None

    print("=" * 72)
    print(_summary("[Momentum Tilt] — if you ONLY followed Tilt alerts", tilt, CASH))
    print()
    print(_summary("[Connors Swing] — if you ONLY followed Connors alerts", connors, CASH))
    print()
    print(_summary(
        "Shared $1,000 — both engines, ownership rules, every alert acted on",
        combined,
        CASH,
    ))
    p = combined["params"]
    print(
        f"  Tilt round-trips: {p.get('tilt_round_trips', 0)}  |  "
        f"Connors round-trips: {p.get('connors_round_trips', 0)}"
    )
    print()
    if spy_end is not None:
        print(
            f"SPY buy & hold\n"
            f"  Starting: ${CASH:,.2f}  →  Ending: ${spy_end:,.2f}  "
            f"(P/L ${spy_end - CASH:+,.2f}, {spy_ret:+.1f}%)"
        )
    print("=" * 72)
    print()
    print("Notes:")
    print("  • Live app sends separate pings; you choose which to execute.")
    print("  • Combined row assumes you acted on every Tilt rebalance AND every Connors signal.")
    print("  • Same cash pool — second engine uses whatever cash the first left idle.")
    print("  • Uses cached Yahoo data; missing tickers shrink the universe (see dropped list).")
    print("  • No VIX cache → complacency gate is fail-open (same as 'unknown VIX' in live).")


if __name__ == "__main__":
    main()
