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

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]

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
        logger.warning("VWAP unavailable for %s: %s", symbol, exc)
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

    latest = df.iloc[-1]
    ts = df.index[-1]

    return {
        "ticker": symbol,
        "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        "price": _round(_latest_scalar(close)),
        "open": _round(float(latest["Open"]) if pd.notna(latest["Open"]) else None),
        "high": _round(float(latest["High"]) if pd.notna(latest["High"]) else None),
        "low": _round(float(latest["Low"]) if pd.notna(latest["Low"]) else None),
        "close": _round(_latest_scalar(close)),
        "volume": int(latest["Volume"]) if pd.notna(latest["Volume"]) else None,
        "rsi": _round(_latest_scalar(df.get("RSI")), 2),
        "macd": _round(_latest_scalar(df.get("MACD")), 4),
        "macd_signal": _round(_latest_scalar(df.get("MACD_signal")), 4),
        "macd_hist": _round(_latest_scalar(df.get("MACD_hist")), 4),
        "ema20": _round(_latest_scalar(df.get("EMA20"))),
        "ema50": _round(_latest_scalar(df.get("EMA50"))),
        "ema200": _round(_latest_scalar(df.get("EMA200"))),
        "sma50": _round(_latest_scalar(df.get("SMA50"))),
        "bb_upper": _round(_latest_scalar(df.get("BB_upper"))),
        "bb_lower": _round(_latest_scalar(df.get("BB_lower"))),
        "bb_mid": _round(_latest_scalar(df.get("BB_mid"))),
        "atr": _round(_latest_scalar(df.get("ATR")), 4),
        "vwap": _round(_latest_scalar(df.get("VWAP"))),
        "momentum": _round(_latest_scalar(df.get("MOM")), 4),
        "adx": _round(_latest_scalar(df.get("ADX")), 2),
        "plus_di": _round(_latest_scalar(df.get("DMP")), 2),
        "minus_di": _round(_latest_scalar(df.get("DMN")), 2),
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
