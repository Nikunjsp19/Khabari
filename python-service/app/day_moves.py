"""Live same-day % vs prior close — source of truth for options chase gating.

Daily-bar movers rankings can miss today's incomplete session bar, so a stock
that is already +8% live can look flat in ``yf.download(period=\"5d\")``. Chase
gates must prefer Yahoo quote meta (regularMarketPrice / previousClose).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def fetch_live_day_pct(ticker: str) -> float | None:
    """Return today's % change vs previous close, or None if unavailable."""
    t = str(ticker or "").strip().upper()
    if not t:
        return None

    # 1) Yahoo chart quote meta — live during the session
    try:
        import httpx

        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{t}"
            f"?interval=1d&range=5d"
        )
        with httpx.Client(timeout=8.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = client.get(url)
            resp.raise_for_status()
            result = (resp.json().get("chart") or {}).get("result") or []
            if result:
                meta = result[0].get("meta") or {}
                px = meta.get("regularMarketPrice")
                prev = meta.get("previousClose") or meta.get("chartPreviousClose")
                if px is not None and prev:
                    return round((float(px) / float(prev) - 1.0) * 100.0, 3)
    except Exception:  # noqa: BLE001
        logger.debug("live day_pct chart failed for %s", t, exc_info=True)

    # 2) yfinance fast_info fallback
    try:
        import yfinance as yf

        info = yf.Ticker(t).fast_info
        px = getattr(info, "last_price", None)
        prev = getattr(info, "previous_close", None)
        if px is not None and prev:
            return round((float(px) / float(prev) - 1.0) * 100.0, 3)
    except Exception:  # noqa: BLE001
        logger.debug("live day_pct fast_info failed for %s", t, exc_info=True)

    return None


def build_day_moves_map(
    symbols: list[str],
    movers_meta: dict[str, Any] | None = None,
    *,
    prefer_live: bool = True,
) -> dict[str, float]:
    """
    Same-day % for chase gating.

    When ``prefer_live`` is True (default), live quote % wins over movers daily
    bars so we never treat a roaring session as flat.
    """
    out: dict[str, float] = {}
    symbols_u = [str(s).strip().upper() for s in symbols if str(s).strip()]

    # Seed from movers ranking (may be stale / miss today's bar)
    if movers_meta:
        for row in movers_meta.get("ranked") or []:
            t = str(row.get("ticker") or "").upper()
            if t and row.get("day_pct") is not None:
                try:
                    out[t] = float(row["day_pct"])
                except (TypeError, ValueError):
                    pass

    if prefer_live:
        for t in symbols_u:
            live = fetch_live_day_pct(t)
            if live is not None:
                bar = out.get(t)
                if bar is not None and abs(live - bar) >= 1.0:
                    logger.info(
                        "day_moves live override %s: bar=%.2f%% → live=%.2f%%",
                        t,
                        bar,
                        live,
                    )
                out[t] = live
        return out

    missing = [t for t in symbols_u if t not in out]
    for t in missing:
        live = fetch_live_day_pct(t)
        if live is not None:
            out[t] = live
    return out
