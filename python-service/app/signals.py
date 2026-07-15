"""Deterministic entry-signal engine + market-regime filter.

The LLM is expensive and inconsistent as a primary decision-maker. Best practice
(see hybrid LLM-in-trading research) is to generate candidate BUY signals from
hard technical rules first, gate them by the broad-market regime, and only then
let the LLM confirm/veto the short-list. This module is the deterministic layer:

- ``score_ticker`` turns one indicator snapshot into a 0-100 long-side score
  plus a trend read and human-readable reasons.
- ``cross_sectional_momentum`` ranks the watchlist against itself (Jegadeesh &
  Titman relative strength) so the strongest names float to the top.
- ``score_universe`` scores every ticker we already computed indicators for.
- ``market_regime`` classifies risk_on / neutral / risk_off from SPY vs its
  200-day SMA and the VIX level (cached, fail-open).
- ``select_candidates`` builds the pre-screened short-list handed to the LLM.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def cross_sectional_momentum(daily_ctx: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Rank the watchlist against itself (Jegadeesh & Titman, 1993 relative strength).

    Blends the available trailing-return measures (12-1, 6m, 3m) into one raw
    momentum number per ticker, then converts to a 0-100 percentile rank across
    the universe. Unlike per-ticker scoring this is *relative*: it rewards the
    strongest names and flags laggards regardless of their absolute score.
    """
    daily_ctx = daily_ctx or {}
    raw: dict[str, float] = {}
    for ticker, d in daily_ctx.items():
        if not isinstance(d, dict):
            continue
        # Prefer the classic 12-1; fall back to shorter windows for young tickers.
        parts = [
            _num(d.get("mom_12_1")),
            _num(d.get("ret_6m")),
            _num(d.get("ret_3m")),
        ]
        parts = [p for p in parts if p is not None]
        if parts:
            raw[ticker] = sum(parts) / len(parts)

    if not raw:
        return {}

    ordered = sorted(raw.items(), key=lambda kv: kv[1])
    n = len(ordered)
    ranks: dict[str, dict[str, Any]] = {}
    for i, (ticker, value) in enumerate(ordered):
        pct = round((i / (n - 1)) * 100, 1) if n > 1 else 100.0
        ranks[ticker] = {"momentum": round(value, 2), "percentile": pct}
    return ranks


def score_ticker(
    ind: dict[str, Any],
    daily: dict[str, Any] | None = None,
    xmom: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic long-side score (0-100) for a single ticker.

    Multi-timeframe: the DAILY context (``daily``) provides the accurate trend
    anchor (200-day SMA), trend strength (daily ADX) and volume confirmation,
    while the intraday snapshot (``ind``) provides short-term timing (MACD, RSI,
    Bollinger). Combines trend, momentum, mean-reversion and volume sleeves, and
    hard-blocks fresh longs below the 200-day SMA ("don't catch a falling knife").
    """
    daily = daily or {}
    price = _num(ind.get("price")) or _num(daily.get("price"))
    if price is None:
        return {
            "score": 0.0,
            "signal": "AVOID",
            "trend": "unknown",
            "trend_ok": False,
            "reasons": ["No price data"],
            "price": None,
            "atr": _num(ind.get("atr")),
        }

    ema20 = _num(ind.get("ema20"))
    ema50 = _num(ind.get("ema50"))
    ema200 = _num(ind.get("ema200"))
    sma50 = _num(ind.get("sma50"))
    rsi = _num(ind.get("rsi"))
    macd = _num(ind.get("macd"))
    macd_sig = _num(ind.get("macd_signal"))
    macd_hist = _num(ind.get("macd_hist"))
    adx = _num(ind.get("adx"))
    plus_di = _num(ind.get("plus_di"))
    minus_di = _num(ind.get("minus_di"))
    bb_up = _num(ind.get("bb_upper"))
    bb_low = _num(ind.get("bb_lower"))

    # Daily context (accurate long-term trend anchor + volume)
    d_sma200 = _num(daily.get("sma200"))
    d_sma50 = _num(daily.get("sma50"))
    d_ema50 = _num(daily.get("ema50"))
    d_adx = _num(daily.get("adx"))
    d_plus_di = _num(daily.get("plus_di"))
    d_minus_di = _num(daily.get("minus_di"))
    d_rsi = _num(daily.get("rsi"))
    rel_volume = _num(daily.get("rel_volume"))

    score = 50.0
    reasons: list[str] = []
    trend = "flat"
    trend_ok = True
    used_daily_trend = False

    # --- Trend sleeve (prefer accurate daily 200d SMA) -----------------------
    if d_sma200 is not None:
        used_daily_trend = True
        if price >= d_sma200:
            score += 14
            trend = "up"
            reasons.append("Above 200-day SMA (daily uptrend)")
            anchor50 = d_ema50 if d_ema50 is not None else d_sma50
            if anchor50 is not None and price > anchor50 > d_sma200:
                score += 6
                reasons.append("Daily 50>200 stack aligned")
        else:
            score -= 22
            trend = "down"
            trend_ok = False
            reasons.append("Below 200-day SMA (daily downtrend) — avoid new longs")
    else:
        # Fallback: intraday MAs when daily context is unavailable
        long_ma = ema200 if ema200 is not None else (sma50 if sma50 is not None else ema50)
        mid_ma = ema50 if ema50 is not None else sma50
        if mid_ma is not None:
            if price > mid_ma:
                score += 12
                trend = "up"
                if long_ma is not None and mid_ma > long_ma:
                    score += 8
                    reasons.append("Uptrend: price > EMA50 > long MA")
                else:
                    reasons.append("Price above EMA50")
            else:
                score -= 12
                trend = "down"
                reasons.append("Price below EMA50")
        if long_ma is not None and price < long_ma:
            score -= 18
            trend = "down"
            trend_ok = False
            reasons.append("Below long-term MA — downtrend, avoid new longs")

    # Short-term alignment (intraday EMAs) helps timing
    if ema20 is not None and price > ema20:
        score += 3
    if used_daily_trend and ema50 is not None and price > ema50:
        score += 4
        if trend != "down":
            reasons.append("Above intraday EMA50 (short-term aligned)")

    # --- Momentum sleeve -----------------------------------------------------
    if macd is not None and macd_sig is not None:
        if macd > macd_sig:
            score += 10
            reasons.append("MACD above signal (bullish)")
        else:
            score -= 8
            reasons.append("MACD below signal (bearish)")
    if macd_hist is not None and macd_hist > 0:
        score += 4

    if rsi is not None:
        if rsi >= 72:
            score -= 6
            reasons.append(f"RSI {rsi:.0f} overbought")
        elif rsi >= 55:
            score += 8
            reasons.append(f"RSI {rsi:.0f} bullish")
        elif rsi >= 48:
            score += 3
        elif rsi >= 35:
            score -= 2
        else:  # oversold
            if trend != "down":
                score += 6
                reasons.append(f"RSI {rsi:.0f} oversold — bounce setup")
            else:
                score -= 4
                reasons.append(f"RSI {rsi:.0f} oversold in downtrend")

    # Trend strength: prefer accurate daily ADX, fall back to intraday
    use_adx, use_pdi, use_mdi, adx_tf = (
        (d_adx, d_plus_di, d_minus_di, "daily")
        if d_adx is not None
        else (adx, plus_di, minus_di, "intraday")
    )
    if use_adx is not None and use_adx >= 25 and use_pdi is not None and use_mdi is not None:
        if use_pdi > use_mdi:
            score += 8
            reasons.append(f"ADX {use_adx:.0f} ({adx_tf}) — strong uptrend")
        else:
            score -= 8
            reasons.append(f"ADX {use_adx:.0f} ({adx_tf}) — strong downtrend")

    # Daily overbought guard (swing timeframe)
    if d_rsi is not None and d_rsi >= 78:
        score -= 5
        reasons.append(f"Daily RSI {d_rsi:.0f} very overbought")

    # --- Mean-reversion sleeve ----------------------------------------------
    if bb_low is not None and price <= bb_low and trend != "down":
        score += 5
        reasons.append("At/below lower Bollinger (pullback entry)")
    if bb_up is not None and price >= bb_up:
        score -= 5
        reasons.append("At/above upper Bollinger (extended)")

    # --- Volume confirmation sleeve (avoid thin, unconfirmed moves) ----------
    if rel_volume is not None:
        if rel_volume >= 1.5:
            score += 6
            reasons.append(f"Volume {rel_volume:.1f}× avg — strong confirmation")
        elif rel_volume >= 1.2:
            score += 3
            reasons.append(f"Volume {rel_volume:.1f}× avg — confirmed")
        elif rel_volume < 0.6:
            score -= 4
            reasons.append(f"Volume {rel_volume:.1f}× avg — thin/unconfirmed")

    # --- Cross-sectional relative strength (Jegadeesh & Titman) --------------
    xmom = xmom or {}
    mom_pct = _num(xmom.get("percentile"))
    if mom_pct is not None:
        if mom_pct >= 80:
            score += 8
            reasons.append(f"Top-{100 - int(mom_pct)}% relative strength (momentum leader)")
        elif mom_pct >= 60:
            score += 4
            reasons.append("Above-median relative strength")
        elif mom_pct <= 20:
            score -= 6
            reasons.append(f"Bottom-{int(mom_pct) or 1}% relative strength (laggard)")

    score = round(_clamp(score), 1)
    settings = get_settings()
    buy_bar = float(settings.signal_buy_threshold)

    if not trend_ok:
        signal = "AVOID"
    elif score >= buy_bar:
        signal = "BUY"
    else:
        signal = "HOLD"

    return {
        "score": score,
        "signal": signal,
        "trend": trend,
        "trend_ok": trend_ok,
        "reasons": reasons,
        "price": price,
        "atr": _num(ind.get("atr")),
        "rsi": rsi,
        "daily_trend": bool(used_daily_trend),
        "above_sma200": daily.get("above_sma200"),
        "dist_sma200_pct": daily.get("dist_sma200_pct"),
        "rel_volume": rel_volume,
        "daily_adx": d_adx,
        "momentum_pct": mom_pct,
        "momentum_return": _num(xmom.get("momentum")),
    }


def score_universe(
    indicators: dict[str, Any],
    daily_ctx: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Score every ticker for which we have an indicator snapshot."""
    daily_ctx = daily_ctx or {}
    xmom = cross_sectional_momentum(daily_ctx)
    out: dict[str, dict[str, Any]] = {}
    for ticker, ind in (indicators or {}).items():
        try:
            out[ticker] = score_ticker(
                ind if isinstance(ind, dict) else {},
                daily_ctx.get(ticker),
                xmom.get(ticker),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Signal scoring failed for %s", ticker)
            out[ticker] = {
                "score": 0.0,
                "signal": "HOLD",
                "trend": "unknown",
                "trend_ok": False,
                "reasons": ["scoring error"],
            }
    return out


# ---------------------------------------------------------------------------
# Market regime (SPY trend + VIX volatility)
# ---------------------------------------------------------------------------

_regime_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_REGIME_TTL_SECONDS = 30 * 60


def _daily_close_and_sma(symbol: str, length: int = 200) -> tuple[float | None, float | None]:
    import yfinance as yf

    df = yf.download(
        symbol,
        period="1y",
        interval="1d",
        progress=False,
        auto_adjust=True,
        threads=False,
    )
    if df is None or df.empty:
        return None, None
    try:
        import pandas as pd

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        close = df["Close"].dropna()
        if close.empty:
            return None, None
        last = float(close.iloc[-1])
        sma = float(close.tail(length).mean()) if len(close) >= 20 else None
        return last, sma
    except Exception:  # noqa: BLE001
        return None, None


def _latest_close(symbol: str) -> float | None:
    import yfinance as yf

    df = yf.download(
        symbol,
        period="1mo",
        interval="1d",
        progress=False,
        auto_adjust=True,
        threads=False,
    )
    if df is None or df.empty:
        return None
    try:
        import pandas as pd

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        close = df["Close"].dropna()
        return float(close.iloc[-1]) if not close.empty else None
    except Exception:  # noqa: BLE001
        return None


def market_regime(force: bool = False) -> dict[str, Any]:
    """Classify the broad-market regime. Cached ~30m and fail-open.

    - risk_on:  SPY above its 200d SMA and VIX below the caution level
    - neutral:  transitional (mild VIX elevation or SPY near its 200d SMA)
    - risk_off: SPY below its 200d SMA or VIX at/above the risk-off level
    """
    settings = get_settings()
    now = time.time()
    if (
        not force
        and _regime_cache.get("data") is not None
        and (now - float(_regime_cache.get("ts") or 0)) < _REGIME_TTL_SECONDS
    ):
        return _regime_cache["data"]

    spy_symbol = settings.regime_index_symbol
    vix_symbol = settings.regime_vix_symbol
    caution = float(settings.regime_vix_caution)
    risk_off_vix = float(settings.regime_vix_risk_off)

    spy_last: float | None = None
    spy_sma: float | None = None
    vix: float | None = None
    try:
        spy_last, spy_sma = _daily_close_and_sma(spy_symbol, length=200)
    except Exception:  # noqa: BLE001
        logger.warning("Regime SPY fetch failed", exc_info=True)
    try:
        vix = _latest_close(vix_symbol)
    except Exception:  # noqa: BLE001
        logger.warning("Regime VIX fetch failed", exc_info=True)

    reasons: list[str] = []
    spy_above = None
    if spy_last is not None and spy_sma is not None:
        spy_above = spy_last >= spy_sma
        reasons.append(
            f"{spy_symbol} {spy_last:.2f} {'above' if spy_above else 'below'} 200d SMA {spy_sma:.2f}"
        )
    if vix is not None:
        reasons.append(f"VIX {vix:.1f}")

    # Decide state (fail-open to neutral when data is missing)
    if spy_above is None and vix is None:
        state = "unknown"
        reasons.append("Regime data unavailable — defaulting to allow trades")
    elif (spy_above is False) or (vix is not None and vix >= risk_off_vix):
        state = "risk_off"
    elif (vix is not None and vix >= caution) or (spy_above is None):
        state = "neutral"
    else:
        state = "risk_on"

    size_factor = {"risk_on": 1.0, "neutral": 0.6, "risk_off": 0.0, "unknown": 0.85}[state]
    allow_new_buys = True
    if state == "risk_off" and settings.regime_block_buys_in_risk_off:
        allow_new_buys = False

    data = {
        "state": state,
        "allow_new_buys": allow_new_buys,
        "size_factor": size_factor,
        "spy_last": round(spy_last, 2) if spy_last is not None else None,
        "spy_sma200": round(spy_sma, 2) if spy_sma is not None else None,
        "spy_above_sma200": spy_above,
        "vix": round(vix, 2) if vix is not None else None,
        "vix_caution": caution,
        "vix_risk_off": risk_off_vix,
        "reasons": reasons,
    }
    _regime_cache["ts"] = now
    _regime_cache["data"] = data
    logger.info("Market regime: %s (%s)", state, "; ".join(reasons) or "no data")
    return data


def select_candidates(
    scores: dict[str, dict[str, Any]],
    held: list[str] | None = None,
    *,
    size: int | None = None,
    min_score: float | None = None,
) -> dict[str, Any]:
    """Pick the top BUY candidates by deterministic score, always keeping held names.

    Held tickers are included even if weak so the LLM can still recommend a SELL.
    Returns both the merged symbol list handed to the LLM and the buy short-list.
    """
    settings = get_settings()
    held = [h.upper() for h in (held or [])]
    size = int(size if size is not None else settings.signal_shortlist_size)
    min_score = float(min_score if min_score is not None else settings.signal_candidate_min_score)

    ranked = sorted(
        scores.items(),
        key=lambda kv: float(kv[1].get("score") or 0),
        reverse=True,
    )
    buy_candidates: list[str] = []
    for ticker, s in ranked:
        if not s.get("trend_ok"):
            continue
        if float(s.get("score") or 0) < min_score:
            continue
        buy_candidates.append(ticker)
        if len(buy_candidates) >= size:
            break

    # Union with held (dedupe, preserve order: candidates first, then held)
    symbols = list(dict.fromkeys(buy_candidates + held))
    return {
        "symbols": symbols,
        "buy_candidates": buy_candidates,
        "held": held,
        "min_score": min_score,
        "size": size,
    }
