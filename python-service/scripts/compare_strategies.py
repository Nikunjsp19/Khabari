"""Honest head-to-head: momentum tilt vs the timing engine vs SPY buy-and-hold.

Downloads the price history ONCE (the slow part) and reuses it across every
window and both strategies.

Run from python-service/ with the venv active:
    python -m scripts.compare_strategies
"""

from __future__ import annotations

import sys

# A broad, diversified large-cap universe across sectors (not the tiny hindsight
# watchlist). Still survivorship-biased toward today's survivors — noted in output.
UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AVGO", "ORCL", "ADBE", "CRM",
    "AMD", "QCOM", "TXN", "INTC", "MU",
    "COST", "WMT", "HD", "MCD", "NKE", "SBUX", "PG", "KO", "PEP",
    "JPM", "BAC", "V", "MA", "GS",
    "UNH", "JNJ", "LLY", "ABBV", "MRK",
    "CAT", "GE", "XOM", "CVX",
    "NFLX", "DIS", "TSLA", "LIN", "HON",
]


def _fmt(x):
    return "n/a".rjust(9) if x is None else f"{x:>9.2f}"


def main() -> int:
    import time

    from app.backtest import _load_price_frames, run_backtest, run_tilt_backtest
    from app.config import get_settings

    settings = get_settings()
    max_years = 5.0

    print(f"Universe: {len(UNIVERSE)} diversified large caps")
    print(f"Loading {int(max_years) + 2}y of daily data once (slow)...", flush=True)
    t = time.time()
    bundle = _load_price_frames(UNIVERSE, max_years, settings)
    frames = bundle[0]
    print(f"  loaded {len(frames)} tickers in {time.time() - t:.0f}s; "
          f"dropped={bundle[5]}, warnings={bundle[6]}", flush=True)

    windows = [1.0, 2.0, 3.0, 5.0]
    for years in windows:
        print(f"\n### {years:g}-year window " + "-" * 46, flush=True)
        try:
            tilt = run_tilt_backtest(UNIVERSE, years=years, top_n=10, bundle=bundle)
        except Exception as exc:  # noqa: BLE001
            print(f"  tilt failed: {exc}")
            tilt = None
        try:
            eng = run_backtest(UNIVERSE, years=years, max_positions=10, bundle=bundle)
        except Exception as exc:  # noqa: BLE001
            print(f"  engine failed: {exc}")
            eng = None

        spy = None
        for r in (tilt, eng):
            if r and r.get("benchmark_spy", {}).get("buy_hold_return_pct") is not None:
                spy = r["benchmark_spy"]["buy_hold_return_pct"]
                break

        hdr = f"  {'strategy':<22}{'total%':>9}{'CAGR%':>9}{'maxDD%':>9}{'Sharpe':>9}{'trades':>8}{'win%':>8}"
        print(hdr)
        if tilt:
            m = tilt["metrics"]
            print(f"  {'momentum tilt':<22}{_fmt(m['total_return_pct'])}{_fmt(m['cagr_pct'])}"
                  f"{_fmt(m['max_drawdown_pct'])}{_fmt(m['sharpe'])}{m['trades']:>8}{_fmt(m['win_rate_pct'])}")
        if eng:
            m = eng["metrics"]
            print(f"  {'timing engine':<22}{_fmt(m['total_return_pct'])}{_fmt(m['cagr_pct'])}"
                  f"{_fmt(m['max_drawdown_pct'])}{_fmt(m['sharpe'])}{m['trades']:>8}{_fmt(m['win_rate_pct'])}")
        print(f"  {'SPY buy & hold':<22}{_fmt(spy)}")
        sys.stdout.flush()

    print("\n" + "=" * 72)
    print("Survivorship-biased universe: trust the SPY-relative gap, not absolutes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
