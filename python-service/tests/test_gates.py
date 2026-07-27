"""Unit tests for free quality gates and news fingerprints."""

from app.gates import (
    apply_confidence_gate,
    apply_options_chase_gate,
    filter_chase_candidates,
    is_options_chase,
    should_notify,
)
from app.news_watch import fingerprint_article


def test_confidence_gate_downgrades_weak_buy(monkeypatch):
    monkeypatch.setenv("MIN_NOTIFY_CONFIDENCE", "70")
    from app.config import get_settings

    get_settings.cache_clear()
    rec = {
        "ticker": "NVDA",
        "action": "BUY",
        "investment": 200,
        "confidence": 55,
        "risk_notes": [],
    }
    out = apply_confidence_gate(rec)
    assert out["action"] == "HOLD"
    assert out["investment"] == 0
    assert out["confidence_gated"] is True
    get_settings.cache_clear()


def test_should_notify_hold_silent(monkeypatch):
    monkeypatch.setenv("NOTIFY_ONLY_ACTIONABLE", "true")
    monkeypatch.setenv("MIN_NOTIFY_CONFIDENCE", "70")
    from app.config import get_settings

    get_settings.cache_clear()
    ok, reason = should_notify({"action": "HOLD", "confidence": 90, "investment": 0})
    assert ok is False
    assert reason == "hold_silent"

    ok2, _ = should_notify({"action": "BUY", "confidence": 80, "investment": 150})
    assert ok2 is True
    get_settings.cache_clear()


def test_fingerprint_stable():
    a = {"uuid": "abc", "title": "Hello", "url": "https://x"}
    assert fingerprint_article("NVDA", a) == fingerprint_article("NVDA", a)
    assert fingerprint_article("NVDA", a) != fingerprint_article("AAPL", a)


def test_options_chase_gate_blocks_call_after_big_green_day(monkeypatch):
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    from app.config import get_settings

    get_settings.cache_clear()
    rec = {
        "ticker": "GOOGL",
        "action": "BUY_TO_OPEN",
        "right": "call",
        "strike": 380,
        "expiry": "2026-07-24",
        "contracts": 1,
        "premium": 9.3,
        "investment": 930,
        "max_loss": 930,
        "confidence": 75,
        "risk": "MEDIUM",
        "risk_notes": [],
        "reasoning": ["momentum"],
    }
    out = apply_options_chase_gate(rec, {"GOOGL": 3.1})
    assert out["action"] == "HOLD"
    assert out["chase_blocked"] is True
    assert out["chase_warned"] is True
    assert out["investment"] == 0
    assert out["contracts"] == 0
    assert out["max_loss"] == 0
    assert out["risk"] == "HIGH"
    assert out["gate_original_action"] == "BUY_TO_OPEN"
    assert any("Chase blocked" in n for n in out["risk_notes"])
    assert any("Chase blocked" in r for r in out["reasoning"])
    get_settings.cache_clear()


def test_options_chase_gate_blocks_extreme_10pct_extension(monkeypatch):
    """Nobody buys calls after a +10% day — hard block must fire."""
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    from app.config import get_settings

    get_settings.cache_clear()
    rec = {
        "ticker": "TSLA",
        "action": "BUY_TO_OPEN",
        "right": "call",
        "contracts": 2,
        "premium": 12.0,
        "investment": 2400,
        "max_loss": 2400,
        "confidence": 90,
        "risk": "LOW",
        "risk_notes": [],
        "reasoning": ["huge momentum"],
    }
    out = apply_options_chase_gate(rec, {"TSLA": 10.0})
    assert out["action"] == "HOLD"
    assert out["chase_blocked"] is True
    assert out["investment"] == 0
    assert out["day_pct"] == 10.0
    get_settings.cache_clear()


def test_options_chase_gate_allows_call_when_move_small(monkeypatch):
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    from app.config import get_settings

    get_settings.cache_clear()
    rec = {
        "ticker": "AMZN",
        "action": "BUY_TO_OPEN",
        "right": "call",
        "contracts": 1,
        "premium": 8.5,
        "investment": 850,
        "confidence": 70,
        "risk": "MEDIUM",
        "risk_notes": [],
    }
    out = apply_options_chase_gate(rec, {"AMZN": 1.2})
    assert out["action"] == "BUY_TO_OPEN"
    assert out["chase_blocked"] is False
    assert out["chase_warned"] is False
    assert out["investment"] == 850
    assert out["confidence"] == 70
    get_settings.cache_clear()


def test_options_chase_gate_blocks_put_after_big_red_day(monkeypatch):
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    from app.config import get_settings

    get_settings.cache_clear()
    rec = {
        "ticker": "NVDA",
        "action": "BUY_TO_OPEN",
        "right": "put",
        "contracts": 1,
        "premium": 5.0,
        "investment": 500,
        "max_loss": 500,
        "confidence": 80,
        "risk": "MEDIUM",
        "risk_notes": [],
        "reasoning": [],
    }
    out = apply_options_chase_gate(rec, {"NVDA": -3.0})
    assert out["action"] == "HOLD"
    assert out["chase_blocked"] is True
    assert out["risk"] == "HIGH"
    assert out["investment"] == 0
    get_settings.cache_clear()


def test_options_chase_gate_allows_put_on_green_day(monkeypatch):
    """Fade/mean-reversion puts after a rally are not chase bets."""
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    from app.config import get_settings

    get_settings.cache_clear()
    rec = {
        "ticker": "META",
        "action": "BUY_TO_OPEN",
        "right": "put",
        "contracts": 1,
        "premium": 4.0,
        "investment": 400,
        "confidence": 72,
        "risk": "MEDIUM",
        "risk_notes": [],
    }
    out = apply_options_chase_gate(rec, {"META": 4.5})
    assert out["action"] == "BUY_TO_OPEN"
    assert out["chase_blocked"] is False
    assert out["investment"] == 400
    get_settings.cache_clear()


def test_is_options_chase_helpers():
    assert is_options_chase("call", 3.0, max_chase_pct=2.5) is True
    assert is_options_chase("call", 1.0, max_chase_pct=2.5) is False
    assert is_options_chase("put", -4.0, max_chase_pct=2.5) is True
    assert is_options_chase("put", 4.0, max_chase_pct=2.5) is False
    assert is_options_chase("call", -5.0, max_chase_pct=2.5) is False


def test_filter_chase_candidates_drops_extension_side(monkeypatch):
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    from app.config import get_settings

    get_settings.cache_clear()
    cands = [
        {"underlying": "TSLA", "right": "call", "key": "tsla-c"},
        {"underlying": "TSLA", "right": "put", "key": "tsla-p"},
        {"underlying": "AAPL", "right": "call", "key": "aapl-c"},
        {"underlying": "NVDA", "right": "put", "key": "nvda-p"},
    ]
    kept, dropped = filter_chase_candidates(
        cands, {"TSLA": 10.0, "AAPL": 0.5, "NVDA": -5.0}
    )
    kept_keys = {c["key"] for c in kept}
    dropped_keys = {c["key"] for c in dropped}
    assert kept_keys == {"tsla-p", "aapl-c"}  # fade put + small green call
    assert dropped_keys == {"tsla-c", "nvda-p"}  # chase call + chase put
    get_settings.cache_clear()
