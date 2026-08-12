"""Tests for deterministic options exit engine."""

from app.options_exits import _build_sell_recommendation, evaluate_options_exits


def test_build_sell_recommendation_is_sell_to_close():
    rec = _build_sell_recommendation(
        {
            "key": "SMCI_2026-07-31_P_25",
            "underlying": "SMCI",
            "right": "put",
            "strike": 25.0,
            "expiry": "2026-07-31",
            "contracts": 2,
            "avg_premium": 1.59,
            "last_premium": 2.1,
            "market_value": 420.0,
            "pnl_pct": 32.1,
            "dte": 15,
            "kind": "take_profit",
            "reason": "SMCI put +32.1% >= TP 25%",
            "contract_key": "SMCI_2026-07-31_P_25",
        }
    )
    assert rec["action"] == "SELL_TO_CLOSE"
    assert rec["ticker"] == "SMCI"
    assert rec["confidence"] == 95
    assert "SELL NOW" in rec["reasoning"][0]


def test_evaluate_options_exits_tp(monkeypatch):
    monkeypatch.setenv("OPTIONS_TAKE_PROFIT_PCT", "25")
    monkeypatch.setenv("OPTIONS_STOP_LOSS_PCT", "25")
    monkeypatch.setenv("OPTIONS_EXIT_TIME_STOP_DTE", "1")
    from app.config import get_settings

    get_settings.cache_clear()

    marked = {
        "cash": 500,
        "positions": {
            "SMCI_2026-07-31_P_25": {
                "underlying": "SMCI",
                "right": "put",
                "strike": 25,
                "expiry": "2026-07-31",
                "contracts": 2,
                "avg_premium": 1.0,
                "last_premium": 1.30,
                "unrealized_pnl_pct": 30.0,
            }
        },
    }
    monkeypatch.setattr(
        "app.options_exits.options_portfolio_with_marks",
        lambda: marked,
    )
    out = evaluate_options_exits()
    assert out["needed"] is True
    assert out["exits"][0]["kind"] == "take_profit"
    get_settings.cache_clear()
