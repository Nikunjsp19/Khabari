"""Sweep Tilt vs Connors cash splits on the same historical window.

Treats each slice as independent (you only act on that engine's alerts with
that sleeve). Sums ending equity for the combined book.

    python scripts/backtest_allocation_sweep.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest import run_mean_reversion_backtest, run_tilt_backtest  # noqa: E402
from app.backtest_cache import CACHE_DIR, load_cached_frames  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.universe import DEFAULT_SWING_UNIVERSE, without_levered  # noqa: E402

TOTAL = 1000.0
YEARS = 1.0


def _tilt_symbols(settings) -> list[str]:
    raw = (settings.tilt_universe or "").strip()
    if raw:
        syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
    else:
        syms = [s.strip().upper() for s in settings.watchlist.split(",") if s.strip()]
    return list(dict.fromkeys(syms)) if settings.tilt_allow_levered else without_levered(syms)


def _swing_symbols(settings) -> list[str]:
    raw = (settings.mr_universe or "").strip()
    if raw:
        syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
    else:
        syms = list(DEFAULT_SWING_UNIVERSE)
    return without_levered(syms)


def _load_bundle(symbols: list[str], settings, years: float):
    if CACHE_DIR.exists() and any(CACHE_DIR.glob("*.json")):
        return load_cached_frames(symbols, years, settings)
    from app.backtest import _load_price_frames

    return _load_price_frames(symbols, years, settings)


def sweep(years: float, total: float) -> list[dict]:
    settings = get_settings()
    tilt_syms = _tilt_symbols(settings)
    swing_syms = _swing_symbols(settings)
    all_syms = list(dict.fromkeys([*tilt_syms, *swing_syms]))
    bundle = _load_bundle(all_syms, settings, years)

    rows: list[dict] = []
    for tilt_pct in range(0, 101, 5):
        tilt_cash = total * tilt_pct / 100.0
        connors_cash = total - tilt_cash

        tilt_end = tilt_cash
        connors_end = connors_cash
        tilt_dd = connors_dd = 0.0
        tilt_sh = connors_sh = 0.0

        if tilt_cash >= 1:
            tr = run_tilt_backtest(
                tilt_syms,
                years=years,
                starting_cash=tilt_cash,
                top_n=settings.tilt_top_n,
                bundle=bundle,
            )
            m = tr["metrics"]
            tilt_end = m["final_equity"]
            tilt_dd = m["max_drawdown_pct"]
            tilt_sh = m["sharpe"]

        if connors_cash >= 1:
            cr = run_mean_reversion_backtest(
                swing_syms,
                years=years,
                starting_cash=connors_cash,
                bundle=bundle,
            )
            m = cr["metrics"]
            connors_end = m["final_equity"]
            connors_dd = m["max_drawdown_pct"]
            connors_sh = m["sharpe"]

        combined = tilt_end + connors_end
        ret = (combined / total - 1) * 100.0
        # Weighted drawdown proxy (not path-accurate, conservative upper-ish bound)
        w_t = tilt_cash / total if total else 0
        w_c = connors_cash / total if total else 0
        blend_dd = w_t * tilt_dd + w_c * connors_dd
        blend_sh = w_t * tilt_sh + w_c * connors_sh if total else 0

        rows.append({
            "tilt_pct": tilt_pct,
            "connors_pct": 100 - tilt_pct,
            "tilt_cash": tilt_cash,
            "connors_cash": connors_cash,
            "tilt_end": tilt_end,
            "connors_end": connors_end,
            "combined_end": combined,
            "return_pct": ret,
            "blend_dd": blend_dd,
            "blend_sh": blend_sh,
        })
    return rows


def main() -> None:
    print(f"Allocation sweep — ${TOTAL:,.0f} total, {YEARS:g}y sleeves (independent)\n")
    rows = sweep(YEARS, TOTAL)
    best = max(rows, key=lambda r: r["combined_end"])
    best_sh = max(rows, key=lambda r: r["blend_sh"])

    print(f"{'Tilt%':>6} {'Conn%':>6} {'Tilt$':>7} {'Conn$':>7} {'Combined':>10} {'Return':>8} {'~DD':>7} {'~Sh':>5}")
    print("-" * 62)
    for r in rows:
        mark = " *" if r is best else ""
        print(
            f"{r['tilt_pct']:>5}% {r['connors_pct']:>5}% "
            f"${r['tilt_cash']:>6.0f} ${r['connors_cash']:>6.0f} "
            f"${r['combined_end']:>9.2f} {r['return_pct']:>+7.1f}% "
            f"{r['blend_dd']:>6.1f}% {r['blend_sh']:>5.2f}{mark}"
        )

    spy_ret = None
    settings = get_settings()
    bundle = _load_bundle(_tilt_symbols(settings) + _swing_symbols(settings), settings, YEARS)
    if connors_cash := 1:
        cr = run_mean_reversion_backtest(_swing_symbols(settings), years=YEARS, starting_cash=1, bundle=bundle)
        spy_ret = cr["benchmark_spy"].get("buy_hold_return_pct")

    print()
    print(
        f"Max return split: {best['tilt_pct']}% Tilt (${best['tilt_cash']:.0f}) + "
        f"{best['connors_pct']}% Connors (${best['connors_cash']:.0f}) "
        f"→ ${best['combined_end']:.2f} ({best['return_pct']:+.1f}%)"
    )
    print(
        f"Max ~Sharpe split: {best_sh['tilt_pct']}% Tilt + {best_sh['connors_pct']}% Connors "
        f"→ ${best_sh['combined_end']:.2f} ({best_sh['return_pct']:+.1f}%, ~Sharpe {best_sh['blend_sh']:.2f})"
    )
    if spy_ret is not None:
        print(f"SPY buy & hold same window: ${TOTAL * (1 + spy_ret / 100):.2f} ({spy_ret:+.1f}%)")


if __name__ == "__main__":
    main()
