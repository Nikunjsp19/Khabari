"""30/70 sleeves: Tilt and Connors each get a reserved slice of NAV."""

from app.config import get_settings
from app.strategy_book import cap_buys_to_sleeve, sleeve_state


def setup_function():
    get_settings.cache_clear()


def teardown_function():
    get_settings.cache_clear()


def test_empty_book_reserves_300_tilt_700_connors():
    tilt = sleeve_state(
        "momentum_tilt",
        cash=1000,
        total_value=1000,
        positions={},
        owned=set(),
    )
    mr = sleeve_state(
        "mean_reversion",
        cash=1000,
        total_value=1000,
        positions={},
        owned=set(),
    )
    assert tilt["pct"] == 30.0
    assert tilt["budget"] == 300.0
    assert tilt["available_cash"] == 300.0
    assert mr["pct"] == 70.0
    assert mr["budget"] == 700.0
    assert mr["available_cash"] == 700.0


def test_holdings_shrink_connors_room():
    state = sleeve_state(
        "mean_reversion",
        cash=500,
        total_value=1000,
        positions={"AAPL": {"market_value": 400}},
        owned={"AAPL"},
    )
    assert state["invested"] == 400.0
    assert state["room"] == 300.0
    assert state["available_cash"] == 300.0


def test_real_cash_is_the_other_cap():
    state = sleeve_state(
        "momentum_tilt",
        cash=50,
        total_value=1000,
        positions={},
        owned=set(),
    )
    assert state["budget"] == 300.0
    assert state["available_cash"] == 50.0


def test_tilt_buy_cap_does_not_spend_connors_cash():
    sleeve = {"room": 300.0}
    trades = [
        {"action": "BUY", "ticker": "NVDA", "value": 200.0},
        {"action": "BUY", "ticker": "AAPL", "value": 200.0},
        {"action": "SELL", "ticker": "MSFT", "value": 0.0},
    ]
    out = cap_buys_to_sleeve(trades, cash=1000.0, sleeve=sleeve, min_trade=20.0)
    buys = [t for t in out if t["action"] == "BUY"]
    assert len(buys) == 2
    assert buys[0]["value"] == 200.0
    assert buys[1]["value"] == 100.0
    assert sum(t["value"] for t in buys) == 300.0
