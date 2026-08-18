"""Live same-day % vs prior close — source of truth for options chase gating.

Daily-bar movers rankings can miss today's incomplete session bar, so a stock
that is already +8% live can look flat in ``yf.download(period=\"5d\")``. Chase
gates must prefer Yahoo quote meta (regularMarketPrice / previousClose).
"""

from __future__ import annotations

import logging
from typing import Any

from app.progo import progo_from_bars

logger = logging.getLogger(__name__)


def _fetch_chart(ticker: str, range_: str) -> dict[str, Any] | None:
    """Fetch one Yahoo daily chart result block, or None on any failure."""
    try:
        import httpx

        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?interval=1d&range={range_}"
        )
        with httpx.Client(timeout=8.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = client.get(url)
            resp.raise_for_status()
            result = (resp.json().get("chart") or {}).get("result") or []
            return result[0] if result else None
    except Exception:  # noqa: BLE001
        logger.debug("chart fetch failed for %s (%s)", ticker, range_, exc_info=True)
        return None


def fetch_live_day_pct(ticker: str) -> float | None:
    """Return today's % change vs previous close, or None if unavailable."""
    t = str(ticker or "").strip().upper()
    if not t:
        return None

    # 1) Yahoo chart quote meta — live during the session
    res = _fetch_chart(t, "5d")
    if res:
        meta = res.get("meta") or {}
        px = meta.get("regularMarketPrice")
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        if px is not None and prev:
            return round((float(px) / float(prev) - 1.0) * 100.0, 3)

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


def fetch_runup_pct(ticker: str, sessions: int = 3) -> float | None:
    """
    % change over the last ``sessions`` sessions (including today, live).

    A stock can be flat today yet already +10% on the week — buying calls into
    that is still an extension bet, so the chase gate needs this too.
    """
    t = str(ticker or "").strip().upper()
    if not t or sessions < 1:
        return None
    res = _fetch_chart(t, "1mo")
    if not res:
        return None
    try:
        meta = res.get("meta") or {}
        quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        closes = [c for c in (quote.get("close") or []) if c is not None]
        if not closes:
            return None
        live = meta.get("regularMarketPrice")
        last = float(live) if live is not None else float(closes[-1])
        # Base = close ``sessions`` sessions back (exclude today's own close)
        idx = len(closes) - 1 - sessions
        if idx < 0:
            idx = 0
        base = float(closes[idx])
        if base <= 0:
            return None
        return round((last / base - 1.0) * 100.0, 3)
    except (TypeError, ValueError):
        logger.debug("runup parse failed for %s", t, exc_info=True)
    return None


def fetch_day_decomposition(ticker: str) -> dict[str, float] | None:
    """Split today's move into the overnight gap and the intraday grind.

    ProGo's insight applied to a single session: ``gap_pct`` is public flow
    (price handed over between sessions, usually on news) and ``intraday_pct``
    is professional flow (what desks did once they could trade it).

    ``intraday_pct`` is derived as ``day_pct - gap_pct`` rather than measured
    from the bar close, so the parts always reconcile with the ``day_pct`` the
    chase gate is judging — the daily bar can lag the live quote mid-session.

    Returns None when the open or prior close is unavailable.
    """
    t = str(ticker or "").strip().upper()
    if not t:
        return None
    res = _fetch_chart(t, "5d")
    if not res:
        return None
    try:
        meta = res.get("meta") or {}
        px = meta.get("regularMarketPrice")
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        if px is None or not prev:
            return None
        prev_close = float(prev)
        if prev_close <= 0:
            return None

        quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        opens = [o for o in (quote.get("open") or []) if o is not None]
        if not opens:
            return None
        today_open = float(opens[-1])
        if today_open <= 0:
            return None

        day_pct = (float(px) / prev_close - 1.0) * 100.0
        gap_pct = (today_open / prev_close - 1.0) * 100.0
        return {
            "day_pct": round(day_pct, 3),
            "gap_pct": round(gap_pct, 3),
            "intraday_pct": round(day_pct - gap_pct, 3),
            "open": round(today_open, 4),
            "prev_close": round(prev_close, 4),
            "price": round(float(px), 4),
        }
    except (TypeError, ValueError):
        logger.debug("decomposition parse failed for %s", t, exc_info=True)
    return None


def fetch_progo(ticker: str, length: int = 14) -> dict[str, Any] | None:
    """14-session ProGo from the live Yahoo daily chart (best effort)."""
    t = str(ticker or "").strip().upper()
    if not t:
        return None
    res = _fetch_chart(t, "1mo")
    if not res:
        return None
    try:
        quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        opens = quote.get("open") or []
        closes = quote.get("close") or []
        if len(opens) != len(closes):
            n = min(len(opens), len(closes))
            opens, closes = opens[:n], closes[:n]
        return progo_from_bars(opens, closes, length=length)
    except (TypeError, ValueError):
        logger.debug("progo parse failed for %s", t, exc_info=True)
    return None


def build_progo_map(
    symbols: list[str], length: int = 14
) -> dict[str, dict[str, Any]]:
    """ProGo snapshot per ticker (best effort, skips failures)."""
    out: dict[str, dict[str, Any]] = {}
    for s in symbols:
        t = str(s).strip().upper()
        if not t:
            continue
        row = fetch_progo(t, length=length)
        if row:
            out[t] = row
    return out


def build_decomposition_map(symbols: list[str]) -> dict[str, dict[str, float]]:
    """Gap/intraday split per ticker (best effort, skips failures)."""
    out: dict[str, dict[str, float]] = {}
    for s in symbols:
        t = str(s).strip().upper()
        if not t:
            continue
        parts = fetch_day_decomposition(t)
        if parts:
            out[t] = parts
    return out


def build_runup_map(symbols: list[str], sessions: int = 3) -> dict[str, float]:
    """Recent multi-session run-up % per ticker (best effort)."""
    out: dict[str, float] = {}
    for s in symbols:
        t = str(s).strip().upper()
        if not t:
            continue
        pct = fetch_runup_pct(t, sessions)
        if pct is not None:
            out[t] = pct
    return out


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
