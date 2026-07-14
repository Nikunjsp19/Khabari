"""Yahoo/yfinance options chain client + deep liquidity/Greeks scan filters.

No API key / signup. Delayed market data. Delta estimated via Black-Scholes from Yahoo IV.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from typing import Any

import yfinance as yf

from app.config import get_settings

logger = logging.getLogger(__name__)

CONTRACT_MULTIPLIER = 100
_MAX_EXPIRIES_PER_SCAN = 4
_RISK_FREE = 0.045


def _f(v: Any, default: float | None = None) -> float | None:
    if v is None or v == "":
        return default
    try:
        val = float(v)
        if math.isnan(val):
            return default
        return val
    except (TypeError, ValueError):
        return default


def _i(v: Any, default: int = 0) -> int:
    try:
        val = float(v)
        if math.isnan(val):
            return default
        return int(val)
    except (TypeError, ValueError):
        return default


def _parse_expiry(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if "T" in s:
        s = s.split("T", 1)[0]
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return s


def _dte(expiry: str, today: date | None = None) -> int | None:
    today = today or datetime.now(timezone.utc).date()
    try:
        exp = date.fromisoformat(expiry[:10])
    except ValueError:
        return None
    return (exp - today).days


def _normalize_right(raw: Any) -> str | None:
    s = str(raw or "").strip().lower()
    if s in {"c", "call", "calls"}:
        return "call"
    if s in {"p", "put", "puts"}:
        return "put"
    return None


def _mid(bid: float | None, ask: float | None, last: float | None = None) -> float | None:
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return round((bid + ask) / 2.0, 4)
    if last is not None and last > 0:
        return round(last, 4)
    if ask is not None and ask > 0:
        return round(ask, 4)
    if bid is not None and bid > 0:
        return round(bid, 4)
    return None


def _spread_pct(bid: float | None, ask: float | None, mid: float | None) -> float | None:
    if bid is None or ask is None or mid is None or mid <= 0:
        return None
    return round(((ask - bid) / mid) * 100.0, 2)


def position_key(
    underlying: str,
    expiry: str,
    right: str,
    strike: float,
) -> str:
    r = "C" if right.lower().startswith("c") else "P"
    strike_s = f"{float(strike):g}"
    return f"{underlying.upper()}_{expiry}_{r}_{strike_s}"


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def estimate_delta(
    *,
    spot: float | None,
    strike: float,
    dte: int | None,
    iv_pct: float | None,
    right: str,
) -> float | None:
    """Black-Scholes delta from Yahoo impliedVolatility (percent)."""
    if spot is None or spot <= 0 or strike <= 0:
        return None
    if dte is None or dte < 0:
        return None
    if iv_pct is None or iv_pct <= 0:
        return None
    t = max(dte, 1) / 365.0
    # Yahoo IV is often already annualized fraction (0.25) or percent (25)
    iv = iv_pct / 100.0 if iv_pct > 1.5 else iv_pct
    if iv <= 0:
        return None
    try:
        d1 = (math.log(spot / strike) + (_RISK_FREE + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    except (ValueError, ZeroDivisionError):
        return None
    if right == "call":
        return round(_norm_cdf(d1), 4)
    if right == "put":
        return round(_norm_cdf(d1) - 1.0, 4)
    return None


def normalize_contract(
    raw: dict[str, Any],
    *,
    underlying: str,
    expiry: str,
    right: str,
    spot: float | None = None,
) -> dict[str, Any] | None:
    """Normalize yfinance option row into stable internal shape."""
    right_n = _normalize_right(right)
    strike = _f(raw.get("strike"))
    expiry_n = _parse_expiry(expiry)
    if not right_n or strike is None or not expiry_n:
        return None

    bid = _f(raw.get("bid"))
    ask = _f(raw.get("ask"))
    last = _f(raw.get("lastPrice") or raw.get("last") or raw.get("price"))
    mid = _mid(bid, ask, last)

    # Yahoo impliedVolatility is typically a fraction (e.g. 0.32)
    iv_raw = _f(raw.get("impliedVolatility") or raw.get("iv"))
    iv = None
    if iv_raw is not None:
        iv = round(iv_raw * 100.0, 2) if iv_raw <= 1.5 else round(iv_raw, 2)

    dte = _dte(expiry_n)
    delta = estimate_delta(
        spot=spot,
        strike=strike,
        dte=dte,
        iv_pct=iv,
        right=right_n,
    )
    oi = _i(raw.get("openInterest") or raw.get("open_interest"), 0)
    volume = _i(raw.get("volume"), 0)
    osi = str(raw.get("contractSymbol") or raw.get("symbol") or "").strip() or None
    spread = _spread_pct(bid, ask, mid)
    und = underlying.upper()
    key = position_key(und, expiry_n, right_n, strike)

    return {
        "key": key,
        "underlying": und,
        "right": right_n,
        "strike": strike,
        "expiry": expiry_n,
        "dte": dte,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "delta": delta,
        "gamma": None,
        "theta": None,
        "vega": None,
        "iv": iv,
        "open_interest": oi,
        "volume": volume,
        "spread_pct": spread,
        "osi": osi,
        "max_loss_per_contract": round((mid or 0) * CONTRACT_MULTIPLIER, 2) if mid else None,
        "greeks_source": "estimated_bs",
    }


def _spot_price(ticker: yf.Ticker, symbol: str) -> float | None:
    try:
        info = ticker.fast_info
        px = getattr(info, "last_price", None) or getattr(info, "lastPrice", None)
        if px:
            return float(px)
    except Exception:  # noqa: BLE001
        pass
    try:
        hist = ticker.history(period="1d", interval="1m")
        if hist is not None and not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:  # noqa: BLE001
        logger.debug("Could not fetch spot for %s", symbol, exc_info=True)
    return None


def fetch_expirations(ticker: str) -> list[str]:
    settings = get_settings()
    t = yf.Ticker(ticker.upper())
    try:
        raw = list(t.options or [])
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"yfinance expirations failed for {ticker}: {exc}") from exc

    out: list[str] = []
    for d in raw:
        parsed = _parse_expiry(d)
        if not parsed:
            continue
        dte = _dte(parsed)
        if dte is None:
            continue
        if settings.options_min_dte <= dte <= settings.options_max_dte:
            out.append(parsed)
    return out


def fetch_chain_for_expiration(
    ticker: str,
    expiration: str,
    *,
    spot: float | None = None,
) -> list[dict[str, Any]]:
    symbol = ticker.upper().strip()
    t = yf.Ticker(symbol)
    if spot is None:
        spot = _spot_price(t, symbol)
    try:
        chain = t.option_chain(expiration)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"yfinance chain failed for {symbol} {expiration}: {exc}") from exc

    contracts: list[dict[str, Any]] = []
    for right, frame in (("call", chain.calls), ("put", chain.puts)):
        if frame is None or frame.empty:
            continue
        for row in frame.to_dict(orient="records"):
            norm = normalize_contract(
                row,
                underlying=symbol,
                expiry=expiration,
                right=right,
                spot=spot,
            )
            if norm:
                contracts.append(norm)
    return contracts


def fetch_chain(ticker: str, *, spot: float | None = None) -> dict[str, Any]:
    """Fetch near-term Yahoo options chains with estimated delta."""
    ticker = ticker.upper().strip()
    t = yf.Ticker(ticker)
    if spot is None:
        spot = _spot_price(t, ticker)

    expirations = fetch_expirations(ticker)[:_MAX_EXPIRIES_PER_SCAN]
    contracts: list[dict[str, Any]] = []
    errors: list[str] = []

    for exp in expirations:
        try:
            contracts.extend(fetch_chain_for_expiration(ticker, exp, spot=spot))
        except Exception as exc:  # noqa: BLE001
            logger.warning("yfinance chain fetch failed %s %s: %s", ticker, exp, exc)
            errors.append(f"{exp}: {exc}")

    return {
        "ticker": ticker,
        "count": len(contracts),
        "contracts": contracts,
        "expirations": expirations,
        "spot": spot,
        "errors": errors,
        "source": "yfinance",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_contract_quote(
    osi_or_underlying: str,
    *,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Re-quote a contract by reloading its expiry chain from Yahoo."""
    if not contract:
        return None
    und = str(contract.get("underlying") or "").upper()
    expiry = str(contract.get("expiry") or "")
    if not und or not expiry:
        return None
    try:
        rows = fetch_chain_for_expiration(und, expiry)
    except Exception as exc:  # noqa: BLE001
        logger.warning("yfinance quote failed for %s: %s", und, exc)
        return None

    key = contract.get("key") or position_key(
        und,
        expiry,
        str(contract.get("right")),
        float(contract.get("strike")),
    )
    osi = contract.get("osi") or (osi_or_underlying if len(str(osi_or_underlying)) > 8 else None)
    for c in rows:
        if c.get("key") == key or (osi and c.get("osi") == osi):
            return c
    return None


def score_candidate(c: dict[str, Any]) -> float:
    """Higher is better — liquidity + sane spread + delta in preferred band."""
    mid = float(c.get("mid") or 0)
    oi = float(c.get("open_interest") or 0)
    vol = float(c.get("volume") or 0)
    spread = float(c.get("spread_pct") or 99)
    delta = abs(float(c.get("delta") or 0))
    delta_score = 1.0 - abs(delta - 0.45) * 2.0
    liq = min(oi / 500.0, 1.5) + min(vol / 200.0, 1.0)
    spread_score = max(0.0, 1.2 - spread / 10.0)
    premium_ok = 0.3 if 0.15 <= mid <= 15 else 0.0
    return round(delta_score + liq + spread_score + premium_ok, 4)


def filter_candidates(
    contracts: list[dict[str, Any]],
    *,
    spot: float | None = None,
) -> list[dict[str, Any]]:
    """Hard filters + rank; return compact tradable long-option candidates."""
    settings = get_settings()
    out: list[dict[str, Any]] = []
    for c in contracts:
        dte = c.get("dte")
        if dte is None:
            continue
        if dte < settings.options_min_dte or dte > settings.options_max_dte:
            continue
        mid = c.get("mid")
        if mid is None or mid <= 0.05:
            continue
        if int(c.get("open_interest") or 0) < settings.options_min_open_interest:
            continue
        if int(c.get("volume") or 0) < settings.options_min_volume:
            continue
        spread = c.get("spread_pct")
        if spread is None or spread > settings.options_max_spread_pct:
            continue
        delta = c.get("delta")
        if delta is None:
            continue
        right = c.get("right")
        if right == "call":
            if not (settings.options_call_delta_min <= delta <= settings.options_call_delta_max):
                continue
        elif right == "put":
            if not (settings.options_put_delta_min <= delta <= settings.options_put_delta_max):
                continue
        else:
            continue
        if spot and spot > 0:
            moneyness = abs(float(c["strike"]) - spot) / spot
            if moneyness > 0.18:
                continue
        scored = dict(c)
        scored["scan_score"] = score_candidate(c)
        out.append(scored)

    out.sort(key=lambda x: float(x.get("scan_score") or 0), reverse=True)
    limit = max(1, int(settings.options_max_candidates_per_ticker))
    return out[:limit]


def deep_scan_underlyings(
    symbols: list[str],
    *,
    spots: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Fetch + filter candidates for each underlying via yfinance."""
    spots = spots or {}
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    raw_counts: dict[str, int] = {}

    for sym in symbols:
        ticker = sym.upper()
        try:
            chain = fetch_chain(ticker, spot=spots.get(ticker))
            raw_counts[ticker] = int(chain.get("count") or 0)
            candidates = filter_candidates(
                chain.get("contracts") or [],
                spot=spots.get(ticker) or chain.get("spot"),
            )
            by_ticker[ticker] = candidates
        except Exception as exc:  # noqa: BLE001
            logger.warning("Options deep scan failed for %s: %s", ticker, exc)
            errors[ticker] = str(exc)
            by_ticker[ticker] = []

    flat: list[dict[str, Any]] = []
    for rows in by_ticker.values():
        flat.extend(rows)
    flat.sort(key=lambda x: float(x.get("scan_score") or 0), reverse=True)

    return {
        "by_ticker": by_ticker,
        "ranked": flat[:40],
        "raw_counts": raw_counts,
        "errors": errors,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "source": "yfinance",
    }
