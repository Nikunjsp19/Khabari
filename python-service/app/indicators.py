"""Technical indicator computation via yfinance + pandas_ta."""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import pandas_ta as ta
import yfinance as yf

logger = logging.getLogger(__name__)


def _latest_scalar(series: pd.Series | None) -> float | None:
    if series is None or series.empty:
        return None
    value = series.iloc[-1]
    if pd.isna(value):
        return None
    return float(value)


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def compute_indicators(
    symbol: str,
    period: str = "3mo",
    interval: str = "1h",
) -> dict[str, Any]:
    """Fetch OHLCV for *symbol* and return the latest indicator snapshot."""
    symbol = symbol.upper().strip()
    logger.info("Computing indicators for %s (%s / %s)", symbol, period, interval)

    df = yf.download(
        symbol,
        period=period,
        interval=interval,
        progress=False,
        auto_adjust=True,
        threads=False,
    )

    if df is None or df.empty:
        raise ValueError(f"No price data returned for {symbol}")

    # yfinance may return MultiIndex columns for single tickers in newer versions
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.dropna(how="all")
    if df.empty:
        raise ValueError(f"Empty OHLCV after dropna for {symbol}")

    add_indicator_columns(df)
    return snapshot_from_frame(df, symbol, -1)


def add_indicator_columns(df: "pd.DataFrame") -> "pd.DataFrame":
    """Attach the full indicator column set in place (RSI/MACD/EMA/BB/ATR/ADX/…).

    Shared by the live path and the backtester so both score identical inputs.
    """
    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df.get("Volume")

    df["RSI"] = ta.rsi(close, length=14)

    macd = ta.macd(close, fast=12, slow=26, signal=9)
    if macd is not None and not macd.empty:
        df["MACD"] = macd.get("MACD_12_26_9")
        df["MACD_signal"] = macd.get("MACDs_12_26_9")
        df["MACD_hist"] = macd.get("MACDh_12_26_9")

    df["EMA20"] = ta.ema(close, length=20)
    df["EMA50"] = ta.ema(close, length=50)
    df["EMA200"] = ta.ema(close, length=200)
    df["SMA50"] = ta.sma(close, length=50)

    bb = ta.bbands(close, length=20, std=2)
    if bb is not None and not bb.empty:
        # Column names vary slightly across pandas_ta versions
        upper_col = next((c for c in bb.columns if c.startswith("BBU_")), None)
        lower_col = next((c for c in bb.columns if c.startswith("BBL_")), None)
        mid_col = next((c for c in bb.columns if c.startswith("BBM_")), None)
        if upper_col:
            df["BB_upper"] = bb[upper_col]
        if lower_col:
            df["BB_lower"] = bb[lower_col]
        if mid_col:
            df["BB_mid"] = bb[mid_col]

    df["ATR"] = ta.atr(high, low, close, length=14)
    df["MOM"] = ta.mom(close, length=12)

    try:
        df["VWAP"] = ta.vwap(high, low, close, volume)
    except Exception as exc:  # noqa: BLE001 — VWAP needs timezone-aware index sometimes
        logger.warning("VWAP unavailable: %s", exc)
        df["VWAP"] = None

    adx = ta.adx(high, low, close, length=14)
    if adx is not None and not adx.empty:
        adx_col = next((c for c in adx.columns if c.startswith("ADX_")), None)
        dmp_col = next((c for c in adx.columns if c.startswith("DMP_")), None)
        dmn_col = next((c for c in adx.columns if c.startswith("DMN_")), None)
        if adx_col:
            df["ADX"] = adx[adx_col]
        if dmp_col:
            df["DMP"] = adx[dmp_col]
        if dmn_col:
            df["DMN"] = adx[dmn_col]
    return df


def snapshot_from_frame(df: "pd.DataFrame", symbol: str, i: int = -1) -> dict[str, Any]:
    """Build an indicator snapshot from row ``i`` (uses only data up to that row)."""
    row = df.iloc[i]
    ts = df.index[i]

    def _cell(col: str, digits: int = 2) -> float | None:
        series = df.get(col)
        if series is None:
            return None
        val = series.iloc[i]
        return None if pd.isna(val) else round(float(val), digits)

    def _ohlc(col: str) -> float | None:
        val = row.get(col) if hasattr(row, "get") else row[col]
        return None if pd.isna(val) else float(val)

    return {
        "ticker": symbol.upper().strip(),
        "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        "price": _round(_ohlc("Close")),
        "open": _round(_ohlc("Open")),
        "high": _round(_ohlc("High")),
        "low": _round(_ohlc("Low")),
        "close": _round(_ohlc("Close")),
        "volume": int(row["Volume"]) if pd.notna(row.get("Volume")) else None,
        "rsi": _cell("RSI", 2),
        "macd": _cell("MACD", 4),
        "macd_signal": _cell("MACD_signal", 4),
        "macd_hist": _cell("MACD_hist", 4),
        "ema20": _cell("EMA20"),
        "ema50": _cell("EMA50"),
        "ema200": _cell("EMA200"),
        "sma50": _cell("SMA50"),
        "bb_upper": _cell("BB_upper"),
        "bb_lower": _cell("BB_lower"),
        "bb_mid": _cell("BB_mid"),
        "atr": _cell("ATR", 4),
        "vwap": _cell("VWAP"),
        "momentum": _cell("MOM", 4),
        "adx": _cell("ADX", 2),
        "plus_di": _cell("DMP", 2),
        "minus_di": _cell("DMN", 2),
    }


def compute_indicators_batch(
    symbols: list[str],
    period: str = "3mo",
    interval: str = "1h",
) -> dict[str, Any]:
    """Compute indicators for multiple symbols. Failures are returned per ticker."""
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}

    for symbol in symbols:
        try:
            results[symbol.upper()] = compute_indicators(symbol, period=period, interval=interval)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed indicators for %s", symbol)
            errors[symbol.upper()] = str(exc)

    return {"indicators": results, "errors": errors}


def _daily_indicator_row(sub: "pd.DataFrame") -> dict[str, Any] | None:
    """Compute a daily-timeframe context row from one ticker's OHLCV frame."""
    if sub is None or sub.empty:
        return None
    sub = sub.dropna(how="all")
    if "Close" not in sub.columns:
        return None
    close = sub["Close"].dropna()
    if close.empty:
        return None
    high, low, vol = sub["High"], sub["Low"], sub.get("Volume")
    n = len(close)
    price = _latest_scalar(close)

    def _tail_mean(series: pd.Series, length: int) -> float | None:
        s = series.dropna()
        return float(s.tail(length).mean()) if len(s) >= max(20, length // 2) else None

    sma50 = _tail_mean(close, 50)
    sma200 = _tail_mean(close, 200)
    ema50 = _latest_scalar(ta.ema(close, length=50)) if n >= 50 else None
    ema200 = _latest_scalar(ta.ema(close, length=200)) if n >= 200 else None
    rsi = _latest_scalar(ta.rsi(close, length=14)) if n >= 15 else None
    atr = _latest_scalar(ta.atr(high, low, close, length=14)) if n >= 15 else None

    adx = plus_di = minus_di = None
    if n >= 20:
        adx_df = ta.adx(high, low, close, length=14)
        if adx_df is not None and not adx_df.empty:
            adx = _latest_scalar(adx_df.get(next((c for c in adx_df.columns if c.startswith("ADX_")), ""), None))
            plus_di = _latest_scalar(adx_df.get(next((c for c in adx_df.columns if c.startswith("DMP_")), ""), None))
            minus_di = _latest_scalar(adx_df.get(next((c for c in adx_df.columns if c.startswith("DMN_")), ""), None))

    rel_volume = None
    last_vol = None
    if vol is not None and not vol.dropna().empty:
        v = vol.dropna()
        last_vol = float(v.iloc[-1])
        avg20 = float(v.tail(20).mean()) if len(v) >= 5 else None
        if avg20 and avg20 > 0:
            rel_volume = round(last_vol / avg20, 2)

    above_sma200 = None
    dist_sma200_pct = None
    if price is not None and sma200:
        above_sma200 = price >= sma200
        dist_sma200_pct = round((price - sma200) / sma200 * 100, 2)

    # Trailing total returns (~21 trading days/month) for cross-sectional momentum.
    def _ret(lookback: int) -> float | None:
        s = close.dropna()
        if len(s) <= lookback:
            return None
        past = float(s.iloc[-1 - lookback])
        if past <= 0:
            return None
        return round((float(s.iloc[-1]) / past - 1) * 100, 2)

    # Jegadeesh & Titman (1993) convention: 12-month return skipping the most
    # recent month to avoid short-term reversal noise ("12-1 momentum").
    mom_12_1 = None
    s_close = close.dropna()
    if len(s_close) >= 252:
        start = float(s_close.iloc[-252])
        end = float(s_close.iloc[-22])
        if start > 0:
            mom_12_1 = round((end / start - 1) * 100, 2)

    return {
        "price": _round(price),
        "sma50": _round(sma50),
        "sma200": _round(sma200),
        "ema50": _round(ema50),
        "ema200": _round(ema200),
        "rsi": _round(rsi, 2),
        "atr": _round(atr, 4),
        "adx": _round(adx, 2),
        "plus_di": _round(plus_di, 2),
        "minus_di": _round(minus_di, 2),
        "rel_volume": rel_volume,
        "above_sma200": above_sma200,
        "dist_sma200_pct": dist_sma200_pct,
        "ret_1m": _ret(21),
        "ret_3m": _ret(63),
        "ret_6m": _ret(126),
        "ret_12m": _ret(252),
        "mom_12_1": mom_12_1,
        "bars": n,
    }


def compute_daily_context_batch(symbols: list[str], period: str = "1y") -> dict[str, Any]:
    """Daily-timeframe context (SMA200/EMA/ADX/ATR/relative-volume) for the trend filter.

    One batched yfinance download for the whole watchlist keeps this cheap. Daily
    bars give an accurate 200-day trend anchor that the intraday 15m/5d window
    cannot (EMA200 needs 200 bars). Fails open per ticker.
    """
    symbols = [s.upper().strip() for s in symbols if s and s.strip()]
    out: dict[str, Any] = {}
    if not symbols:
        return out
    try:
        df = yf.download(
            symbols,
            period=period,
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=False,
            group_by="ticker",
        )
    except Exception:  # noqa: BLE001
        logger.warning("Daily context batch download failed", exc_info=True)
        return out
    if df is None or df.empty:
        return out

    multi = isinstance(df.columns, pd.MultiIndex)
    for sym in symbols:
        try:
            if multi:
                if sym not in df.columns.get_level_values(0):
                    continue
                sub = df[sym]
            else:
                sub = df  # single-ticker download → flat columns
            row = _daily_indicator_row(sub)
            if row:
                out[sym] = row
        except Exception:  # noqa: BLE001
            logger.warning("Daily context failed for %s", sym, exc_info=True)
    return out
