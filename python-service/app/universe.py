"""Shared ticker universes for the stock engines.

The live watchlist still carries 2x/inverse single-stock ETFs (TSLL, AMDL,
NVDD…). Those belong in a leveraged-product book, not in Connors-style swing
mean reversion or a monthly momentum tilt: they trend without a floor, so a
"dip" is often just the product grinding lower. Both engines strip them unless
explicitly allowed.
"""

from __future__ import annotations

# Direxion / GraniteShares / REX 1x–2x single-stock and index products currently
# (or recently) on the Khabari watchlist. Keep this explicit — suffix heuristics
# collide with ordinary tickers (GS, HON, UNH).
LEVERED_OR_INVERSE: frozenset[str] = frozenset(
    {
        "TSLL",
        "METU",
        "NVDU",
        "NVDL",
        "AAPU",
        "AMZU",
        "MSFU",
        "GGLL",
        "AMUU",
        "AMDL",
        "AVL",
        "NFXL",
        "ORCU",
        "PLTU",
        "HODU",
        "CONX",
        "CONL",
        "MSTU",
        "QQQU",
        "FNGG",
        "TSLS",
        "TSDD",
        "METD",
        "NVDD",
        "NVD",
        "AAPD",
        "AMZD",
        "MSFD",
        "GGLS",
        "AMDD",
        "AVS",
        "NFXS",
        "ORCS",
        "PLTD",
    }
)

# Connors: Double 7s on index ETFs, RSI(2) on individual names.
INDEX_ETFS: frozenset[str] = frozenset({"SPY", "QQQ", "IWM", "DIA"})

# Monthly momentum tilt: liquid US large-caps across sectors (~45 names).
# No 2x/inverse — they dominate short-window momentum then draw down hard.
DEFAULT_TILT_UNIVERSE: tuple[str, ...] = (
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "META",
    "NVDA",
    "AVGO",
    "ORCL",
    "ADBE",
    "CRM",
    "NOW",
    "AMD",
    "QCOM",
    "TXN",
    "INTC",
    "MU",
    "COST",
    "WMT",
    "HD",
    "MCD",
    "NKE",
    "SBUX",
    "PG",
    "KO",
    "PEP",
    "JPM",
    "BAC",
    "V",
    "MA",
    "GS",
    "UNH",
    "JNJ",
    "LLY",
    "ABBV",
    "MRK",
    "CAT",
    "GE",
    "HON",
    "LIN",
    "XOM",
    "CVX",
    "NFLX",
    "DIS",
    "TSLA",
    "UBER",
    "PLTR",
    "TSM",
    "APP",
)

# Connors swing: index ETFs for Double 7s + stable large-caps for RSI(2).
# Skips hyper-volatile single names (COIN, MSTR, SMCI, HOOD…) — Connors tested
# liquid equities; 2x products and meme beta don't mean-revert cleanly.
DEFAULT_SWING_UNIVERSE: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "IWM",
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "META",
    "NVDA",
    "AVGO",
    "ORCL",
    "AMD",
    "QCOM",
    "JPM",
    "BAC",
    "V",
    "MA",
    "GS",
    "NFLX",
    "COST",
    "WMT",
    "HD",
    "UNH",
    "LLY",
    "XOM",
    "CVX",
    "PG",
    "KO",
    "DIS",
    "CAT",
    "GE",
    "TSM",
)


def monitoring_universe() -> list[str]:
    """Union watchlist for indicators, desk, and legacy paths — no levered ETFs."""
    return without_levered(list(dict.fromkeys([*DEFAULT_TILT_UNIVERSE, *DEFAULT_SWING_UNIVERSE])))


def is_levered_or_inverse(ticker: str) -> bool:
    return str(ticker or "").strip().upper() in LEVERED_OR_INVERSE


def setup_kind(ticker: str) -> str:
    """Which Connors swing setup this name uses."""
    t = str(ticker or "").strip().upper()
    if t in INDEX_ETFS:
        return "double7s"
    return "rsi2"


def without_levered(symbols: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        t = str(s or "").strip().upper()
        if not t or t in seen or is_levered_or_inverse(t):
            continue
        seen.add(t)
        out.append(t)
    return out
