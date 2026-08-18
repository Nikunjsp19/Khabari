"""Load price history from cached Yahoo chart JSON instead of live yfinance.

Same output shape as ``backtest._load_price_frames``, so any backtest can be
handed a cached bundle via ``bundle=``. Used when the runtime has no direct
network path to Yahoo (sandbox/CI) and the JSON was fetched out of band into
``python-service/.cache/yahoo/<TICKER>.json``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache" / "yahoo"


def load_chart_frame(path: Path) -> Any | None:
    """Parse one Yahoo chart JSON into an OHLCV DataFrame indexed by date."""
    import pandas as pd

    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None

    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return None
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    if not stamps or not quote:
        return None

    # Prefer split/dividend-adjusted closes so long windows aren't distorted.
    adj = ((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose")
    close = quote.get("close") or []
    open_ = quote.get("open") or []
    high = quote.get("high") or []
    low = quote.get("low") or []
    volume = quote.get("volume") or []

    df = pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": volume,
        },
        index=pd.to_datetime(pd.Series(stamps), unit="s").dt.normalize(),
    )
    if adj and len(adj) == len(close):
        adj_s = pd.Series(adj, index=df.index)
        # Scale the whole bar by the close's adjustment so OHLC stay consistent.
        ratio = (adj_s / df["Close"]).replace([float("inf")], float("nan")).fillna(1.0)
        for col in ("Open", "High", "Low", "Close"):
            df[col] = df[col] * ratio
    df = df.dropna(subset=["Close"])
    df.index.name = "Date"
    return df if not df.empty else None


def load_cached_frames(
    symbols: list[str],
    years: float,
    settings: Any,
    *,
    cache_dir: Path | None = None,
    min_sessions: int = 220,
    light: bool = True,
):
    """Cached twin of ``backtest._load_price_frames``.

    Returns (frames, spy, vix_close, calendar, warmup, dropped, data_warnings).

    ``light`` skips the pandas_ta indicator block, which the tilt and RSI(2)
    replays don't use. Pass ``light=False`` to feed ``run_backtest``.
    """
    from app.backtest import _prepare_frame

    cache_dir = cache_dir or CACHE_DIR
    spy_sym = settings.regime_index_symbol
    data_warnings: list[str] = []

    spy_df = load_chart_frame(cache_dir / f"{spy_sym}.json")
    if spy_df is None or spy_df.empty:
        raise RuntimeError(f"No cached history for benchmark {spy_sym} in {cache_dir}")
    spy = spy_df.copy()
    spy["SMA200"] = spy["Close"].rolling(200).mean()
    latest_session = spy.index.max()

    frames: dict[str, Any] = {}
    dropped: list[str] = []
    for sym in symbols:
        df = load_chart_frame(cache_dir / f"{sym}.json")
        if (
            df is None
            or df.empty
            or len(df) <= min_sessions
            or (latest_session - df.index.max()).days > 7
        ):
            dropped.append(sym)
            continue
        frames[sym] = _prepare_frame(df.copy(), light=light)
    if not frames:
        raise RuntimeError("No cached tickers had complete-enough history")
    if dropped:
        data_warnings.append(f"dropped for short/stale history: {dropped}")

    vix_df = load_chart_frame(cache_dir / f"{settings.regime_vix_symbol}.json")
    vix_close = vix_df["Close"] if vix_df is not None else None
    if vix_close is None:
        data_warnings.append("no cached VIX — regime gate treats volatility as unknown")

    return frames, spy, vix_close, list(spy.index), 210, dropped, data_warnings
