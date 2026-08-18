"""One-year, $1000 comparison of every stock engine against SPY buy-and-hold.

Downloads price history once and replays each engine over the same sessions so
the numbers are directly comparable.

    python scripts/backtest_one_year.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest import (  # noqa: E402
    _load_price_frames,
    run_mean_reversion_backtest,
    run_tilt_backtest,
)
from app.backtest_cache import CACHE_DIR, load_cached_frames  # noqa: E402
from app.config import get_settings  # noqa: E402

YEARS = 1.0
CASH = 1000.0

# Ordinary liquid equities only. The live watchlist also carries 2x leveraged and
# inverse single-stock ETFs (NVDL, AMDL, TSLS...), which are a poor fit for a
# no-stop mean-reversion rule — a 2x fund that keeps falling has no floor.
# Splitting them out shows how much of the result is the rule vs the universe.
CLEAN = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AVGO", "ORCL", "AMD",
    "TSLA", "NFLX", "JPM", "BAC", "GS", "TSM", "PLTR", "MSTR", "COIN", "HOOD",
    "RDDT", "ARM", "SMCI", "NOW", "QQQ", "SPY",
]


def line(label: str, m: dict, start: float) -> str:
    end = m["final_equity"]
    return (
        f"{label:<22} ${end:>9,.2f}   {m['total_return_pct']:>+7.2f}%   "
        f"dd {m['max_drawdown_pct']:>7.2f}%   sharpe {m['sharpe']:>5.2f}   "
        f"{m['trades']:>4} trades   win {m['win_rate_pct']:>5.1f}%   "
        f"P/L ${end - start:>+9,.2f}"
    )


def main() -> None:
    settings = get_settings()
    symbols = [s.upper().strip() for s in settings.watchlist_symbols if s.strip()]
    symbols = list(dict.fromkeys(symbols))
    print(f"Universe: {len(symbols)} tickers, {YEARS:g}y, starting ${CASH:,.0f}\n")

    # Prefer the local cache — Yahoo is unreachable from some sandboxes.
    if CACHE_DIR.exists() and any(CACHE_DIR.glob("*.json")):
        print(f"Using cached history from {CACHE_DIR}")
        bundle = load_cached_frames(symbols, YEARS, settings)
    else:
        bundle = _load_price_frames(symbols, YEARS, settings)
    frames, _spy, _vix, calendar, warmup, dropped, warns = bundle
    print(f"Loaded {len(frames)} tickers ({len(dropped)} dropped), {len(calendar)} sessions")
    for w in warns:
        print(f"  ! {w}")
    print()

    results = {}

    results["rsi2_live"] = run_mean_reversion_backtest(
        symbols,
        years=YEARS,
        starting_cash=CASH,
        rsi_entry=settings.mr_rsi_entry,
        max_positions=settings.mr_max_positions,
        max_position_pct=settings.mr_max_position_pct,
        require_progo=settings.mr_require_progo,
        bundle=bundle,
    )
    # Same rules, full allocation — isolates how much of the result is the
    # strategy vs the deliberately small position sizing.
    results["rsi2_full"] = run_mean_reversion_backtest(
        symbols,
        years=YEARS,
        starting_cash=CASH,
        rsi_entry=settings.mr_rsi_entry,
        max_positions=3,
        max_position_pct=33.0,
        require_progo=settings.mr_require_progo,
        bundle=bundle,
    )
    results["rsi2_no_progo"] = run_mean_reversion_backtest(
        symbols,
        years=YEARS,
        starting_cash=CASH,
        rsi_entry=settings.mr_rsi_entry,
        max_positions=settings.mr_max_positions,
        max_position_pct=settings.mr_max_position_pct,
        require_progo=False,
        bundle=bundle,
    )
    results["tilt"] = run_tilt_backtest(
        symbols,
        years=YEARS,
        starting_cash=CASH,
        top_n=settings.tilt_top_n,
        bundle=bundle,
    )

    clean = [s for s in CLEAN if s in frames]
    clean_bundle = load_cached_frames(clean, YEARS, settings)
    results["rsi2_clean"] = run_mean_reversion_backtest(
        clean,
        years=YEARS,
        starting_cash=CASH,
        rsi_entry=settings.mr_rsi_entry,
        max_positions=settings.mr_max_positions,
        max_position_pct=settings.mr_max_position_pct,
        require_progo=False,
        bundle=clean_bundle,
    )
    results["rsi2_clean_progo"] = run_mean_reversion_backtest(
        clean,
        years=YEARS,
        starting_cash=CASH,
        rsi_entry=settings.mr_rsi_entry,
        max_positions=settings.mr_max_positions,
        max_position_pct=settings.mr_max_position_pct,
        require_progo=True,
        bundle=clean_bundle,
    )
    results["rsi2_clean_33"] = run_mean_reversion_backtest(
        clean,
        years=YEARS,
        starting_cash=CASH,
        rsi_entry=settings.mr_rsi_entry,
        max_positions=3,
        max_position_pct=33.0,
        require_progo=False,
        bundle=clean_bundle,
    )
    results["tilt_clean"] = run_tilt_backtest(
        clean, years=YEARS, starting_cash=CASH, top_n=settings.tilt_top_n, bundle=clean_bundle
    )

    p = results["rsi2_live"]["params"]
    print(f"Window: {p['start']} → {p['end']}  ({p['sessions']} sessions)\n")

    print("-- full configured watchlist (55 tickers incl. leveraged ETFs) --")
    print(line("RSI(2) live config", results["rsi2_live"]["metrics"], CASH))
    print(line("RSI(2) full alloc", results["rsi2_full"]["metrics"], CASH))
    print(line("RSI(2) no ProGo", results["rsi2_no_progo"]["metrics"], CASH))
    print(line("Momentum Tilt", results["tilt"]["metrics"], CASH))
    print(f"\n-- plain equities only ({len(clean)} tickers) --")
    print(line("RSI(2) clean", results["rsi2_clean"]["metrics"], CASH))
    print(line("RSI(2) clean+ProGo", results["rsi2_clean_progo"]["metrics"], CASH))
    print(line("RSI(2) clean 33%", results["rsi2_clean_33"]["metrics"], CASH))
    print(line("Momentum Tilt clean", results["tilt_clean"]["metrics"], CASH))
    print()

    spy = results["rsi2_live"]["benchmark_spy"].get("buy_hold_return_pct")
    if spy is not None:
        spy_end = CASH * (1 + spy / 100.0)
        print(
            f"{'SPY buy & hold':<22} ${spy_end:>9,.2f}   {spy:>+7.2f}%"
            f"{'':>46}P/L ${spy_end - CASH:>+9,.2f}"
        )

    m = results["rsi2_live"]["metrics"]
    print(
        f"\nRSI(2) signal funnel: {m['signals_seen']} setups, "
        f"{m['signals_skipped_progo']} skipped by ProGo, "
        f"{m['signals_skipped_no_slot']} skipped (no free slot). "
        f"Slot exposure {m['slot_exposure_pct']}% of available slot-days."
    )

    for key in ("rsi2_live", "rsi2_full"):
        tr = results[key]["trades"]
        if not tr:
            continue
        by_kind: dict[str, int] = {}
        for t in tr:
            by_kind[t["kind"]] = by_kind.get(t["kind"], 0) + 1
        print(f"{key} exits: {by_kind}")

    tr = results["rsi2_live"]["trades"]
    if tr:
        best = max(tr, key=lambda t: t["pnl_pct"])
        worst = min(tr, key=lambda t: t["pnl_pct"])
        avg_hold = sum(t["days_held"] for t in tr) / len(tr)
        print(
            f"RSI(2) best {best['ticker']} {best['pnl_pct']:+.1f}% | "
            f"worst {worst['ticker']} {worst['pnl_pct']:+.1f}% | "
            f"avg hold {avg_hold:.1f} sessions"
        )


if __name__ == "__main__":
    main()
