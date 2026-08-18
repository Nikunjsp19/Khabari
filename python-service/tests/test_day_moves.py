"""Tests for live same-day % used by options chase gating."""

from app.day_moves import build_day_moves_map


def test_build_day_moves_prefers_live_over_stale_bar(monkeypatch):
    """Movers bars can understate today's move — live quote must win."""
    import app.day_moves as dm

    monkeypatch.setattr(
        dm,
        "fetch_live_day_pct",
        lambda t: {"ORCL": 8.2, "AAPL": 0.4}.get(t),
    )
    movers = {
        "ranked": [
            {"ticker": "ORCL", "day_pct": 0.3},  # stale / missed session bar
            {"ticker": "AAPL", "day_pct": 0.5},
        ]
    }
    out = build_day_moves_map(["ORCL", "AAPL"], movers, prefer_live=True)
    assert out["ORCL"] == 8.2
    assert out["AAPL"] == 0.4


def test_build_day_moves_fills_missing_from_live(monkeypatch):
    import app.day_moves as dm

    monkeypatch.setattr(dm, "fetch_live_day_pct", lambda t: 3.5 if t == "NVDA" else None)
    out = build_day_moves_map(["NVDA"], {"ranked": []}, prefer_live=True)
    assert out["NVDA"] == 3.5


def _chart(prev_close: float, today_open: float, price: float) -> dict:
    return {
        "meta": {"regularMarketPrice": price, "previousClose": prev_close},
        "indicators": {"quote": [{"open": [prev_close, today_open]}]},
    }


def test_decomposition_parts_reconcile_with_day_pct(monkeypatch):
    """gap + intraday must equal the day % the chase gate judges."""
    import app.day_moves as dm

    # Opened at 100 → 103 close, prior close 100: all 3% earned intraday.
    monkeypatch.setattr(dm, "_fetch_chart", lambda t, r: _chart(100.0, 100.0, 103.0))
    out = dm.fetch_day_decomposition("AMZN")
    assert out["day_pct"] == 3.0
    assert out["gap_pct"] == 0.0
    assert out["intraday_pct"] == 3.0

    # Gapped to 102.8 then drifted to 103: almost all of it was the gap.
    monkeypatch.setattr(dm, "_fetch_chart", lambda t, r: _chart(100.0, 102.8, 103.0))
    out2 = dm.fetch_day_decomposition("AMZN")
    assert out2["day_pct"] == 3.0
    assert out2["gap_pct"] == 2.8
    assert round(out2["gap_pct"] + out2["intraday_pct"], 3) == out2["day_pct"]


def test_decomposition_none_when_chart_unavailable(monkeypatch):
    import app.day_moves as dm

    monkeypatch.setattr(dm, "_fetch_chart", lambda t, r: None)
    assert dm.fetch_day_decomposition("AMZN") is None

    # Missing prior close must not be treated as a zero base.
    monkeypatch.setattr(
        dm,
        "_fetch_chart",
        lambda t, r: {"meta": {"regularMarketPrice": 10.0}, "indicators": {}},
    )
    assert dm.fetch_day_decomposition("AMZN") is None


def test_build_decomposition_map_skips_failures(monkeypatch):
    import app.day_moves as dm

    monkeypatch.setattr(
        dm,
        "fetch_day_decomposition",
        lambda t: {"day_pct": 3.0, "gap_pct": 0.1, "intraday_pct": 2.9}
        if t == "AMZN"
        else None,
    )
    out = dm.build_decomposition_map(["AMZN", "GOOGL"])
    assert set(out) == {"AMZN"}


def test_fetch_progo_from_chart_opens_closes(monkeypatch):
    import app.day_moves as dm

    opens = [100.0]
    closes = [100.0]
    px = 100.0
    for _ in range(20):
        opens.append(px)
        px *= 1.01
        closes.append(px)

    monkeypatch.setattr(
        dm,
        "_fetch_chart",
        lambda t, r: {"indicators": {"quote": [{"open": opens, "close": closes}]}},
    )
    out = dm.fetch_progo("AMZN", length=14)
    assert out is not None
    assert out["regime"] == "accumulation"
    assert out["pro"] > 0
