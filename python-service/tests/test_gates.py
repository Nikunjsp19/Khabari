"""Unit tests for free quality gates and news fingerprints."""

from app.gates import (
    apply_confidence_gate,
    apply_options_chase_gate,
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


def test_options_chase_gate_warns_call_after_big_green_day(monkeypatch):
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    monkeypatch.setenv("OPTIONS_CHASE_CONFIDENCE_HAIRCUT", "10")
    monkeypatch.setenv("OPTIONS_MIN_NOTIFY_CONFIDENCE", "65")
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
    assert out["action"] == "BUY_TO_OPEN"  # still suggest
    assert out["chase_warned"] is True
    assert out["investment"] == 930
    assert out["contracts"] == 1
    assert out["risk"] == "HIGH"
    assert out["confidence"] == 65.0  # 75 - 10
    assert any("Chase caution" in n for n in out["risk_notes"])
    assert any("Chase caution" in r for r in out["reasoning"])
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
    assert out["chase_warned"] is False
    assert out["investment"] == 850
    assert out["confidence"] == 70
    get_settings.cache_clear()


def test_options_chase_gate_warns_put_after_big_red_day(monkeypatch):
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    monkeypatch.setenv("OPTIONS_CHASE_CONFIDENCE_HAIRCUT", "10")
    monkeypatch.setenv("OPTIONS_MIN_NOTIFY_CONFIDENCE", "65")
    from app.config import get_settings

    get_settings.cache_clear()
    rec = {
        "ticker": "NVDA",
        "action": "BUY_TO_OPEN",
        "right": "put",
        "contracts": 1,
        "premium": 5.0,
        "investment": 500,
        "confidence": 80,
        "risk": "MEDIUM",
        "risk_notes": [],
        "reasoning": [],
    }
    out = apply_options_chase_gate(rec, {"NVDA": -3.0})
    assert out["action"] == "BUY_TO_OPEN"
    assert out["chase_warned"] is True
    assert out["risk"] == "HIGH"
    assert out["confidence"] == 70.0
    get_settings.cache_clear()

