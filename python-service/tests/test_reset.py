"""Fresh-start reset must clear every piece of book state, not just positions."""

import pytest

pytest.importorskip("mongomock")

import mongomock  # noqa: E402


@pytest.fixture
def fake_db(monkeypatch):
    client = mongomock.MongoClient()
    db = client["Khabari_test"]

    import app.db as db_mod
    import app.reset as reset_mod

    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    monkeypatch.setattr(reset_mod, "get_db", lambda: db)

    monkeypatch.setenv("INITIAL_CASH", "1000")
    monkeypatch.setenv("OPTIONS_INITIAL_CASH", "1000")
    from app.config import get_settings

    get_settings.cache_clear()
    yield db
    get_settings.cache_clear()


def _seed_positions(db):
    db.portfolio.insert_one(
        {"ts": 1, "cash": 500.0, "positions": {"MSFT": {"shares": 5, "avg_cost": 400}}}
    )
    db.options_portfolio.insert_one(
        {
            "ts": 1,
            "cash": 200.0,
            "positions": {"ORCL_2026-08-21_C_300": {"underlying": "ORCL", "contracts": 1}},
        }
    )
    db.recommendations.insert_one({"ts": 1, "ticker": "AAPL", "status": "pending"})
    db.options_recommendations.insert_one({"ts": 1, "ticker": "ORCL", "status": "pending"})
    db.meta.insert_one({"_id": "position_state", "positions": {"MSFT": {"high_water": 500}}})
    db.tilt_state.insert_one({"_id": "state", "last_rebalance_ym": "2026-07"})
    db.trades.insert_one({"ts": 1, "ticker": "MSFT"})


def test_reset_clears_positions_and_state(fake_db):
    from app.reset import reset_book

    _seed_positions(fake_db)
    out = reset_book()

    assert out["ok"] is True
    assert out["cash"] == 1000.0
    assert out["options_cash"] == 1000.0

    from app.db import get_latest_options_portfolio, get_latest_portfolio

    assert get_latest_portfolio()["positions"] == {}
    assert get_latest_portfolio()["cash"] == 1000.0
    assert get_latest_options_portfolio()["positions"] == {}

    # Stale confirms must not remain actionable
    assert fake_db.recommendations.find_one({"status": "pending"}) is None
    assert fake_db.options_recommendations.find_one({"status": "pending"}) is None
    assert out["pending_stock_cancelled"] == 1
    assert out["pending_options_cancelled"] == 1

    # Exit high-water marks and tilt cadence must not survive
    assert fake_db.meta.find_one({"_id": "position_state"}) is None
    assert fake_db.tilt_state.find_one({"_id": "state"}) is None

    # History kept by default
    assert fake_db.trades.count_documents({}) == 1


def test_reset_with_custom_cash(fake_db):
    from app.reset import reset_book

    _seed_positions(fake_db)
    reset_book(cash=2500.0, options_cash=750.0)

    from app.db import get_latest_options_portfolio, get_latest_portfolio

    assert get_latest_portfolio()["cash"] == 2500.0
    assert get_latest_options_portfolio()["cash"] == 750.0


def test_reset_clear_history_wipes_ledgers(fake_db):
    from app.reset import reset_book

    _seed_positions(fake_db)
    out = reset_book(clear_history=True)

    assert fake_db.trades.count_documents({}) == 0
    assert fake_db.recommendations.count_documents({}) == 0
    assert fake_db.options_recommendations.count_documents({}) == 0
    assert out["cleared_history"] is True

    # The fresh snapshot must survive the history wipe
    from app.db import get_latest_portfolio

    assert get_latest_portfolio()["cash"] == 1000.0
    assert get_latest_portfolio()["positions"] == {}
    assert fake_db.portfolio.count_documents({}) == 1


def test_reset_rejects_negative_cash(fake_db):
    from app.reset import reset_book

    with pytest.raises(ValueError):
        reset_book(cash=-1)
