"""Unit tests for free quality gates and news fingerprints."""

from app.gates import apply_confidence_gate, should_notify
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
