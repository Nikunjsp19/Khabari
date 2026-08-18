"""Connors swing entry/exit rules (RSI(2) + Double 7s).

Imports only `app.mean_reversion` / `app.universe` — pandas stays out.
"""

from app.mean_reversion import evaluate_entry, evaluate_exit
from app.universe import is_levered_or_inverse, setup_kind, without_levered


def _setup(**over):
    """A textbook Connors stock long: uptrend, below the 5-day, RSI(2) flushed."""
    row = {
        "price": 100.0,
        "sma200": 90.0,
        "sma5": 103.0,
        "rsi2": 4.0,
        "consecutive_down_days": 3,
    }
    row.update(over)
    return row


# --- entries ---------------------------------------------------------------


def test_textbook_setup_fires():
    ok, reasons = evaluate_entry(_setup())
    assert ok
    assert len(reasons) >= 3


def test_below_200d_is_not_a_dip_buy():
    ok, _ = evaluate_entry(_setup(price=80.0))
    assert not ok


def test_price_above_5day_is_not_a_pullback():
    ok, _ = evaluate_entry(_setup(sma5=99.0))
    assert not ok


def test_rsi_above_threshold_does_not_fire():
    ok, _ = evaluate_entry(_setup(rsi2=25.0))
    assert not ok


def test_threshold_is_exclusive():
    assert not evaluate_entry(_setup(rsi2=10.0), rsi_entry=10.0)[0]
    assert evaluate_entry(_setup(rsi2=9.9), rsi_entry=10.0)[0]


def test_one_down_day_is_not_enough():
    assert not evaluate_entry(_setup(consecutive_down_days=1), min_down_days=2)[0]
    assert evaluate_entry(_setup(consecutive_down_days=2), min_down_days=2)[0]


def test_missing_down_days_fails_open():
    row = _setup()
    row.pop("consecutive_down_days")
    assert evaluate_entry(row, min_down_days=2)[0]


def test_vix_complacency_blocks_new_longs():
    assert not evaluate_entry(_setup(), vix_stretch_pct=-8.0)[0]
    assert evaluate_entry(_setup(), vix_stretch_pct=8.0)[0]


def test_missing_vix_does_not_block():
    assert evaluate_entry(_setup(), vix_stretch_pct=None)[0]


def test_double7s_fires_on_7day_low():
    row = {"price": 100.0, "sma200": 90.0, "at_7d_low": True}
    ok, reasons = evaluate_entry(row, setup="double7s")
    assert ok
    assert any("7-day low" in r for r in reasons)


def test_double7s_ignores_rsi():
    row = {"price": 100.0, "sma200": 90.0, "at_7d_low": False, "rsi2": 2.0, "sma5": 110.0}
    assert not evaluate_entry(row, setup="double7s")[0]


def test_progo_distribution_blocks_only_when_required():
    row = _setup(progo={"regime": "distribution"})
    assert not evaluate_entry(row, require_progo=True)[0]
    assert evaluate_entry(row, require_progo=False)[0]


def test_progo_accumulation_adds_a_reason():
    ok, reasons = evaluate_entry(_setup(progo={"regime": "accumulation"}))
    assert ok
    assert any("accumulation" in r for r in reasons)


def test_missing_inputs_fail_closed():
    assert not evaluate_entry(_setup(price=None))[0]
    assert not evaluate_entry(_setup(sma200=None))[0]
    assert not evaluate_entry(None)[0]
    assert not evaluate_entry({})[0]


# --- exits -----------------------------------------------------------------


def test_holds_while_below_5day():
    should_exit, reason = evaluate_exit(_setup())
    assert not should_exit
    assert reason is None


def test_exits_when_5day_reclaimed():
    should_exit, reason = evaluate_exit(_setup(price=104.0, sma5=103.0))
    assert should_exit
    assert "5-day" in reason


def test_exits_when_rsi2_reclaims_70():
    should_exit, reason = evaluate_exit(_setup(rsi2=72.0, sma5=110.0))
    assert should_exit
    assert "RSI(2)" in reason


def test_double7s_exits_on_7day_high():
    should_exit, reason = evaluate_exit(
        {"price": 100.0, "sma200": 90.0, "at_7d_high": True}, setup="double7s"
    )
    assert should_exit
    assert "7-day high" in reason


def test_time_stop_after_seven_sessions():
    should_exit, reason = evaluate_exit(_setup(), days_held=7, max_hold_days=7)
    assert should_exit
    assert "7" in reason


def test_losing_the_200day_exits_even_below_the_5day():
    should_exit, reason = evaluate_exit({"price": 85.0, "sma5": 95.0, "sma200": 90.0})
    assert should_exit
    assert "200-day" in reason


def test_exit_without_price_is_a_hold():
    assert evaluate_exit({"sma5": 100.0})[0] is False
    assert evaluate_exit(None)[0] is False


# --- universe --------------------------------------------------------------


def test_levered_products_are_stripped():
    assert is_levered_or_inverse("AMDL")
    assert is_levered_or_inverse("NVDD")
    assert not is_levered_or_inverse("AAPL")
    assert without_levered(["AAPL", "AMDL", "SPY", "amdl"]) == ["AAPL", "SPY"]


def test_index_etfs_use_double7s():
    assert setup_kind("SPY") == "double7s"
    assert setup_kind("QQQ") == "double7s"
    assert setup_kind("IWM") == "double7s"
    assert setup_kind("AAPL") == "rsi2"
