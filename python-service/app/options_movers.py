"""Discover high-movement underlyings and refresh the options watchlist.

Uses free Yahoo/yfinance daily bars — no API key.
Universe = stock watchlist ∪ liquid options names ∪ held underlyings.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

import yfinance as yf

from app.config import get_settings
from app.db import (
    get_active_watchlist,
    get_latest_options_portfolio,
    set_options_watchlist,
)

logger = logging.getLogger(__name__)

# Always consider these for options liquidity / index proxies
_LIQUID_CORE = (
    "SPY,QQQ,IWM,TSLA,NVDA,AAPL,MSFT,AMZN,META,GOOGL,AMD,COIN,HOOD,ORCL,PLTR,NFLX,BA,JPM,GS"
)


def _universe(*, extra: list[str] | None = None) -> list[str]:
    settings = get_settings()
    seen: set[str] = set()
    out: list[str] = []

    def add(raw: str) -> None:
        t = str(raw).strip().upper()
        if not t or t in seen:
            return
        seen.add(t)
        out.append(t)

    for t in settings.options_watchlist_symbols:
        add(t)
    for t in (settings.options_mover_universe_extra or "").split(","):
        add(t)
    for t in _LIQUID_CORE.split(","):
        add(t)
    try:
        for t in get_active_watchlist():
            add(t)
    except Exception:  # noqa: BLE001
        logger.debug("Could not load stock watchlist for movers universe", exc_info=True)
    for t in extra or []:
        add(t)

    # Always keep open options underlyings in the pool
    try:
        book = get_latest_options_portfolio()
        for pos in (book.get("positions") or {}).values():
            add(str(pos.get("underlying") or ""))
    except Exception:  # noqa: BLE001
        pass

    return out


def _held_underlyings() -> list[str]:
    try:
        book = get_latest_options_portfolio()
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for pos in (book.get("positions") or {}).values():
        u = str(pos.get("underlying") or "").upper()
        if u and u not in out:
            out.append(u)
    return out


def rank_high_movers(
    symbols: list[str] | None = None,
    *,
    top_n: int | None = None,
) -> dict[str, Any]:
    """Rank underlyings by absolute daily move and volume (high-movement screen)."""
    settings = get_settings()
    top_n = max(3, int(top_n if top_n is not None else settings.options_mover_top_n))
    min_abs = float(settings.options_mover_min_abs_pct)
    universe = symbols or _universe()
    if not universe:
        return {"ok": False, "error": "empty_universe", "ranked": [], "selected": []}

    ranked: list[dict[str, Any]] = []
    errors: dict[str, str] = {}

    # Batch download is much faster than per-ticker history
    try:
        raw = yf.download(
            tickers=universe,
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Movers download failed: %s", exc)
        return {"ok": False, "error": str(exc), "ranked": [], "selected": []}

    if raw is None or getattr(raw, "empty", True):
        return {"ok": False, "error": "no_price_data", "ranked": [], "selected": []}

    multi = getattr(raw.columns, "nlevels", 1) > 1

    for ticker in universe:
        try:
            if multi:
                if ticker not in raw.columns.get_level_values(0):
                    errors[ticker] = "missing"
                    continue
                df = raw[ticker].dropna(how="all")
            else:
                # Single ticker download returns flat columns
                df = raw.dropna(how="all")
                if len(universe) > 1:
                    errors[ticker] = "unexpected_flat_frame"
                    continue

            if df is None or df.empty or "Close" not in df.columns:
                errors[ticker] = "no_bars"
                continue
            closes = df["Close"].dropna()
            if len(closes) < 2:
                errors[ticker] = "insufficient_bars"
                continue

            last = float(closes.iloc[-1])
            prev = float(closes.iloc[-2])
            if prev <= 0:
                continue
            day_pct = ((last - prev) / prev) * 100.0
            vol = 0.0
            if "Volume" in df.columns:
                v = df["Volume"].dropna()
                if len(v):
                    vol = float(v.iloc[-1])
            # ATR proxy: mean true range % over available bars
            atr_pct = 0.0
            if len(closes) >= 3:
                rets = closes.pct_change().dropna().abs()
                if len(rets):
                    atr_pct = float(rets.tail(5).mean() * 100.0)

            abs_pct = abs(day_pct)
            if abs_pct < min_abs and atr_pct < min_abs:
                # Still keep for ranking below threshold but mark soft
                pass

            score = abs_pct * (1.0 + math.log1p(vol) / 20.0) + atr_pct * 0.35
            ranked.append(
                {
                    "ticker": ticker,
                    "last": round(last, 4),
                    "day_pct": round(day_pct, 3),
                    "abs_pct": round(abs_pct, 3),
                    "volume": int(vol),
                    "atr_pct": round(atr_pct, 3),
                    "score": round(score, 4),
                    "high_move": abs_pct >= min_abs or atr_pct >= min_abs,
                }
            )
        except Exception as exc:  # noqa: BLE001
            errors[ticker] = str(exc)

    ranked.sort(key=lambda x: float(x.get("score") or 0), reverse=True)

    held = _held_underlyings()
    selected: list[str] = []
    for h in held:
        if h not in selected:
            selected.append(h)

    # Prefer true high-movers first, then fill by score
    for row in ranked:
        t = row["ticker"]
        if not row.get("high_move"):
            continue
        if t not in selected:
            selected.append(t)
        if len(selected) >= top_n:
            break

    if len(selected) < top_n:
        for row in ranked:
            t = row["ticker"]
            if t not in selected:
                selected.append(t)
            if len(selected) >= top_n:
                break

    # If still thin (quiet market), fall back to liquid core slice
    if len(selected) < max(3, top_n // 2):
        for t in _LIQUID_CORE.split(","):
            if t not in selected:
                selected.append(t)
            if len(selected) >= top_n:
                break

    return {
        "ok": True,
        "universe_size": len(universe),
        "ranked": ranked[:40],
        "selected": selected[:top_n],
        "held": held,
        "min_abs_pct": min_abs,
        "top_n": top_n,
        "errors": errors,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


def refresh_options_watchlist_from_movers(*, persist: bool = True) -> dict[str, Any]:
    """Research high movers and (optionally) write them into options_watchlist."""
    settings = get_settings()
    if not settings.options_auto_movers:
        tickers = []
        try:
            from app.db import get_active_options_watchlist

            tickers = get_active_options_watchlist()
        except Exception:  # noqa: BLE001
            tickers = settings.options_watchlist_symbols
        return {
            "ok": True,
            "skipped": True,
            "reason": "options_auto_movers_disabled",
            "selected": tickers,
        }

    result = rank_high_movers()
    selected = list(result.get("selected") or [])
    if not selected:
        return {**result, "persisted": False, "message": "No movers selected"}

    persisted = False
    if persist:
        set_options_watchlist(selected)
        persisted = True
        logger.info(
            "Options watchlist refreshed from high movers (%s): %s",
            len(selected),
            selected,
        )

    return {
        **result,
        "persisted": persisted,
        "message": f"Selected {len(selected)} high-movement underlyings for options",
    }
