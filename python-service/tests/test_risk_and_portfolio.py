"""Unit tests for risk rules and portfolio parsing (no network)."""

from app.portfolio import parse_portfolio_dataframe, rows_to_portfolio
from app.risk import apply_risk_rules
import pandas as pd


def test_buy_capped_at_40_percent():
    portfolio = {"cash": 1000.0, "positions": {}}
    rec = {
        "ticker": "NVDA",
        "action": "BUY",
        "investment": 500,
        "confidence": 100,
        "risk": "MEDIUM",
        "time_horizon": "SHORT",
        "expected_return": "5%",
        "reasoning": ["test"],
    }
    out = apply_risk_rules(rec, portfolio, prices={"NVDA": 100})
    assert out["action"] == "BUY"
    # max_cash_spend=950, max_per_trade=400, conf_factor=1 → allowed=400
    assert out["investment"] == 400.0
    assert out["remaining_cash"] == 600.0
    assert out["risk_adjusted"] is True


def test_buy_scaled_by_confidence():
    portfolio = {"cash": 1000.0, "positions": {}}
    rec = {
        "ticker": "TSLA",
        "action": "BUY",
        "investment": 400,
        "confidence": 50,
        "risk": "LOW",
        "time_horizon": "SHORT",
        "expected_return": "3%",
        "reasoning": [],
    }
    out = apply_risk_rules(rec, portfolio)
    # allowed = min(950, 400) * (0.55 + 0.45*0.5) = 400 * 0.775 = 310
    assert out["investment"] == 310.0


def test_sell_with_no_shares_becomes_hold():
    portfolio = {"cash": 500.0, "positions": {}}
    rec = {
        "ticker": "AAPL",
        "action": "SELL",
        "investment": 100,
        "confidence": 80,
        "risk": "LOW",
        "time_horizon": "SHORT",
        "expected_return": "2%",
        "reasoning": [],
    }
    out = apply_risk_rules(rec, portfolio)
    assert out["action"] == "HOLD"
    assert out["investment"] == 0


def test_tiny_buy_becomes_hold():
    portfolio = {"cash": 2.0, "positions": {}}
    rec = {
        "ticker": "MSFT",
        "action": "BUY",
        "investment": 50,
        "confidence": 90,
        "risk": "MEDIUM",
        "time_horizon": "SHORT",
        "expected_return": "1%",
        "reasoning": [],
    }
    out = apply_risk_rules(rec, portfolio)
    # max_cash=1.9, max_pos=0.8, conf_factor≈0.955 → allowed≈0.76 → HOLD
    assert out["action"] == "HOLD"


def test_portfolio_from_rows():
    rows = [
        {"Ticker": "TSLA", "Shares": 1.5, "AvgCost": 820},
        {"Ticker": "NVDA", "Shares": 2.0, "AvgCost": 165},
    ]
    result = rows_to_portfolio(rows, cash=1000)
    assert result["cash"] == 1000.0
    assert result["positions"]["TSLA"]["shares"] == 1.5
    assert result["positions"]["NVDA"]["avg_cost"] == 165.0


def test_portfolio_with_cash_column():
    df = pd.DataFrame(
        [
            {"Ticker": "AAPL", "Shares": 1, "AvgCost": 180, "Cash": 750},
        ]
    )
    result = parse_portfolio_dataframe(df, cash=1000)
    assert result["cash"] == 750.0
