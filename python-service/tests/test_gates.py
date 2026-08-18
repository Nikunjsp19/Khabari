"""Unit tests for free quality gates and news fingerprints."""

from app.gates import (
    apply_confidence_gate,
    apply_options_chase_gate,
    classify_chase_character,
    filter_chase_candidates,
    is_options_chase,
    sanitize_ranked_for_chase,
    should_notify,
    should_notify_options,
)
from app.news_watch import fingerprint_article


def _chase_rec(ticker: str = "AMZN", right: str = "call") -> dict:
    """A clean BUY_TO_OPEN for chase-gate tests."""
    return {
        "ticker": ticker,
        "action": "BUY_TO_OPEN",
        "right": right,
        "strike": 250,
        "expiry": "2026-09-18",
        "contracts": 1,
        "premium": 5.0,
        "investment": 500,
        "max_loss": 500,
        "confidence": 75,
        "risk": "MEDIUM",
        "risk_notes": [],
        "reasoning": ["momentum"],
    }


def _decomp(day: float, gap: float) -> dict[str, float]:
    return {"day_pct": day, "gap_pct": gap, "intraday_pct": round(day - gap, 3)}


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


def test_options_chase_gate_fail_closed_when_day_pct_missing(monkeypatch):
    """Missing day % must not silently allow BUY_TO_OPEN (ORCL-style slip)."""
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    from app.config import get_settings

    get_settings.cache_clear()
    rec = {
        "ticker": "ORCL",
        "action": "BUY_TO_OPEN",
        "right": "call",
        "contracts": 1,
        "premium": 6.0,
        "investment": 600,
        "max_loss": 600,
        "confidence": 80,
        "risk": "MEDIUM",
        "risk_notes": [],
        "reasoning": [],
    }
    out = apply_options_chase_gate(rec, {})  # empty day_moves
    assert out["action"] == "HOLD"
    assert out["chase_blocked"] is True
    assert out["investment"] == 0
    assert any("unavailable" in n.lower() or "fail-closed" in n.lower() for n in out["risk_notes"])
    get_settings.cache_clear()


def test_options_chase_gate_blocks_orcl_style_extension(monkeypatch):
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    from app.config import get_settings

    get_settings.cache_clear()
    rec = {
        "ticker": "ORCL",
        "action": "BUY_TO_OPEN",
        "right": "call",
        "strike": 300,
        "expiry": "2026-08-21",
        "contracts": 1,
        "premium": 7.0,
        "investment": 700,
        "max_loss": 700,
        "confidence": 85,
        "risk": "MEDIUM",
        "risk_notes": [],
        "reasoning": ["momentum"],
    }
    out = apply_options_chase_gate(rec, {"ORCL": 8.0})
    assert out["action"] == "HOLD"
    assert out["chase_blocked"] is True
    assert out["day_pct"] == 8.0
    # Contract must be stripped so no renderer shows "HOLD — ORCL CALL $300"
    assert out["right"] is None
    assert out["strike"] is None
    assert out["expiry"] is None
    assert out["blocked_contract"]["right"] == "call"
    get_settings.cache_clear()


def test_options_chase_gate_blocks_multiday_runup(monkeypatch):
    """Flat today but +10% on the week is still an extension bet."""
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    monkeypatch.setenv("OPTIONS_MAX_RUNUP_CHASE_PCT", "8")
    from app.config import get_settings

    get_settings.cache_clear()
    rec = {
        "ticker": "PLTR",
        "action": "BUY_TO_OPEN",
        "right": "call",
        "strike": 90,
        "expiry": "2026-08-21",
        "contracts": 1,
        "premium": 4.0,
        "investment": 400,
        "confidence": 80,
        "risk": "MEDIUM",
        "risk_notes": [],
        "reasoning": [],
    }
    out = apply_options_chase_gate(rec, {"PLTR": 0.4}, runups={"PLTR": 11.0})
    assert out["action"] == "HOLD"
    assert out["chase_blocked"] is True
    assert out["runup_pct"] == 11.0
    get_settings.cache_clear()


def test_options_chase_gate_fail_closed_on_unknown_right(monkeypatch):
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    from app.config import get_settings

    get_settings.cache_clear()
    rec = {
        "ticker": "AAPL",
        "action": "BUY_TO_OPEN",
        "right": "spread",
        "contracts": 1,
        "investment": 300,
        "confidence": 80,
        "risk_notes": [],
    }
    out = apply_options_chase_gate(rec, {"AAPL": 0.2})
    assert out["action"] == "HOLD"
    assert out["chase_blocked"] is True
    get_settings.cache_clear()


def test_options_chase_gate_normalizes_right_spelling(monkeypatch):
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    from app.config import get_settings

    get_settings.cache_clear()
    rec = {
        "ticker": "NVDA",
        "action": "BUY_TO_OPEN",
        "right": "CALLS",
        "contracts": 1,
        "investment": 500,
        "confidence": 80,
        "risk_notes": [],
    }
    out = apply_options_chase_gate(rec, {"NVDA": 6.0})
    assert out["action"] == "HOLD"
    assert out["chase_blocked"] is True
    get_settings.cache_clear()


def test_chase_blocked_hold_is_silent(monkeypatch):
    """Hourly pings naming the blocked contract are what felt like spam."""
    monkeypatch.setenv("NOTIFY_ONLY_ACTIONABLE", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    ok, reason = should_notify_options(
        {"action": "HOLD", "confidence": 70, "chase_blocked": True}
    )
    assert ok is False
    assert reason == "chase_blocked_silent"

    ok2, reason2 = should_notify_options({"action": "HOLD", "confidence": 70})
    assert ok2 is True
    assert reason2 == "options_hold_status"
    get_settings.cache_clear()


def test_sanitize_ranked_neutralizes_chase_bias(monkeypatch):
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    from app.config import get_settings

    get_settings.cache_clear()
    ranked = [
        {"ticker": "ORCL", "score": 88, "bias": "BUY_TO_OPEN", "note": "breakout"},
        {"ticker": "AAPL", "score": 70, "bias": "BUY_TO_OPEN", "note": "steady"},
    ]
    out = sanitize_ranked_for_chase(ranked, {"ORCL": 8.0, "AAPL": 0.3})
    by_ticker = {r["ticker"]: r for r in out}
    assert by_ticker["ORCL"]["bias"] == "HOLD"
    assert by_ticker["ORCL"]["chase_blocked"] is True
    assert by_ticker["AAPL"]["bias"] == "BUY_TO_OPEN"
    get_settings.cache_clear()


# --- ProGo: gap (public) vs intraday (professional) chase character ----------


def test_classify_chase_character_gap_vs_intraday(monkeypatch):
    monkeypatch.setenv("OPTIONS_PROGO_GAP_SHARE", "0.6")
    from app.config import get_settings

    get_settings.cache_clear()

    # +3% day that gapped +2.8% — the public paid up on the open.
    gap_driven = classify_chase_character(3.0, _decomp(3.0, 2.8))
    assert gap_driven["character"] == "gap_driven"
    assert gap_driven["gap_share"] == round(2.8 / 3.0, 3)

    # +3% day that opened flat and ground up all session.
    intraday = classify_chase_character(3.0, _decomp(3.0, 0.2))
    assert intraday["character"] == "intraday_driven"
    assert intraday["intraday_pct"] == 2.8

    # Half and half is neither.
    assert classify_chase_character(3.0, _decomp(3.0, 1.5))["character"] == "mixed"

    # Sign-agnostic: a dumped name that gapped down reads the same way.
    assert (
        classify_chase_character(-3.0, _decomp(-3.0, -2.8))["character"] == "gap_driven"
    )
    assert (
        classify_chase_character(-3.0, _decomp(-3.0, -0.1))["character"]
        == "intraday_driven"
    )

    # Gap went the other way — whole move was intraday, share is negative.
    opened_down = classify_chase_character(3.0, _decomp(3.0, -0.5))
    assert opened_down["character"] == "intraday_driven"
    assert opened_down["gap_share"] < 0
    get_settings.cache_clear()


def test_classify_chase_character_unknown_without_data(monkeypatch):
    from app.config import get_settings

    get_settings.cache_clear()
    assert classify_chase_character(3.0, None)["character"] == "unknown"
    assert classify_chase_character(None, _decomp(3.0, 1.0))["character"] == "unknown"
    # Near-flat day makes the ratio meaningless rather than "intraday".
    assert classify_chase_character(0.02, _decomp(0.02, 0.01))["character"] == "unknown"
    get_settings.cache_clear()


def test_progo_shadow_mode_blocks_but_records_disagreement(monkeypatch):
    """Shadow mode must not change any action — only mark what live would do."""
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    monkeypatch.setenv("OPTIONS_PROGO_CHASE_MODE", "shadow")
    from app.config import get_settings

    get_settings.cache_clear()
    out = apply_options_chase_gate(
        _chase_rec(), {"AMZN": 3.0}, decompositions={"AMZN": _decomp(3.0, 0.2)}
    )
    assert out["action"] == "HOLD"
    assert out["chase_blocked"] is True
    assert out["chase_shadow_relax_candidate"] is True
    assert out["chase_character"] == "intraday_driven"
    assert any("ProGo shadow" in n for n in out["risk_notes"])
    get_settings.cache_clear()


def test_progo_live_mode_allows_intraday_driven_move(monkeypatch):
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    monkeypatch.setenv("OPTIONS_PROGO_CHASE_MODE", "live")
    from app.config import get_settings

    get_settings.cache_clear()
    out = apply_options_chase_gate(
        _chase_rec(), {"AMZN": 3.0}, decompositions={"AMZN": _decomp(3.0, 0.2)}
    )
    assert out["action"] == "BUY_TO_OPEN"
    assert out["chase_blocked"] is False
    assert out["chase_relaxed"] is True
    assert out["right"] == "call"  # contract survives, not stripped
    assert any("Chase allowed" in n for n in out["risk_notes"])
    get_settings.cache_clear()


def test_progo_live_mode_still_blocks_gap_driven_move(monkeypatch):
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    monkeypatch.setenv("OPTIONS_PROGO_CHASE_MODE", "live")
    from app.config import get_settings

    get_settings.cache_clear()
    out = apply_options_chase_gate(
        _chase_rec(), {"AMZN": 3.0}, decompositions={"AMZN": _decomp(3.0, 2.8)}
    )
    assert out["action"] == "HOLD"
    assert out["chase_blocked"] is True
    assert out["chase_character"] == "gap_driven"
    assert any("opening gap" in n for n in out["risk_notes"])
    get_settings.cache_clear()


def test_progo_live_mode_respects_absolute_ceiling(monkeypatch):
    """However healthy the shape, a +9% day is still an extension."""
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    monkeypatch.setenv("OPTIONS_PROGO_CHASE_MODE", "live")
    monkeypatch.setenv("OPTIONS_PROGO_RELAX_MAX_DAY_PCT", "5.0")
    from app.config import get_settings

    get_settings.cache_clear()
    out = apply_options_chase_gate(
        _chase_rec(), {"AMZN": 9.0}, decompositions={"AMZN": _decomp(9.0, 0.3)}
    )
    assert out["action"] == "HOLD"
    assert out["chase_blocked"] is True
    get_settings.cache_clear()


def test_progo_live_mode_still_blocks_multiday_runup(monkeypatch):
    """Flat-shaped today but +11% on the week is an extension regardless."""
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    monkeypatch.setenv("OPTIONS_MAX_RUNUP_CHASE_PCT", "8.0")
    monkeypatch.setenv("OPTIONS_PROGO_CHASE_MODE", "live")
    from app.config import get_settings

    get_settings.cache_clear()
    out = apply_options_chase_gate(
        _chase_rec(),
        {"AMZN": 3.0},
        runups={"AMZN": 11.0},
        decompositions={"AMZN": _decomp(3.0, 0.2)},
    )
    assert out["action"] == "HOLD"
    assert out["chase_blocked"] is True
    get_settings.cache_clear()


def test_progo_off_mode_leaves_gate_untouched(monkeypatch):
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    monkeypatch.setenv("OPTIONS_PROGO_CHASE_MODE", "off")
    from app.config import get_settings

    get_settings.cache_clear()
    out = apply_options_chase_gate(
        _chase_rec(), {"AMZN": 3.0}, decompositions={"AMZN": _decomp(3.0, 0.2)}
    )
    assert out["action"] == "HOLD"
    assert out["chase_blocked"] is True
    assert "chase_character" not in out
    get_settings.cache_clear()


def test_progo_attaches_14d_regime_on_chase(monkeypatch):
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    monkeypatch.setenv("OPTIONS_PROGO_CHASE_MODE", "shadow")
    from app.config import get_settings

    get_settings.cache_clear()
    out = apply_options_chase_gate(
        _chase_rec(),
        {"AMZN": 3.0},
        decompositions={"AMZN": _decomp(3.0, 2.8)},
        progo={"AMZN": {"regime": "distribution", "pro": -0.2, "public": 0.8}},
    )
    assert out["progo_regime"] == "distribution"
    assert out["chase_character"] == "gap_driven"
    get_settings.cache_clear()


def test_progo_does_not_affect_non_chase_trades(monkeypatch):
    """A small move was never a chase; ProGo must not invent a block."""
    monkeypatch.setenv("OPTIONS_MAX_INTRADAY_CHASE_PCT", "2.5")
    monkeypatch.setenv("OPTIONS_PROGO_CHASE_MODE", "live")
    from app.config import get_settings

    get_settings.cache_clear()
    out = apply_options_chase_gate(
        _chase_rec(), {"AMZN": 1.0}, decompositions={"AMZN": _decomp(1.0, 0.95)}
    )
    assert out["action"] == "BUY_TO_OPEN"
    assert out["chase_blocked"] is False
    assert out["chase_character"] == "gap_driven"  # recorded, not acted on
    get_settings.cache_clear()
