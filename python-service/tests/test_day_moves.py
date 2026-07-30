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
