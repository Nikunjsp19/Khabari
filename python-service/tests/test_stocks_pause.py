"""Stock trading pause must hold at the engine level, not just the callers."""

import pytest


@pytest.fixture(autouse=True)
def _paused(monkeypatch):
    monkeypatch.setenv("STOCKS_TRADING_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_exit_monitor_skipped_while_paused():
    from app.exits import run_exit_monitor

    out = run_exit_monitor(send_notification=True)
    assert out["skipped"] is True
    assert out["reason"] == "stocks_trading_paused"
    assert out["alerted"] == []


def test_day_wrap_skipped_while_paused():
    from app.day_wrap import run_day_wrap

    out = run_day_wrap(force=True)
    assert out["skipped"] is True
    assert out["reason"] == "stocks_trading_paused"


def test_tilt_rebalance_skipped_while_paused():
    from app.tilt import run_tilt_rebalance

    out = run_tilt_rebalance(force=True, send_notification=True)
    assert out["skipped"] is True
    assert out["reason"] == "stocks_trading_paused"


def test_trigger_tilt_now_skipped_while_paused():
    from app.scheduler import trigger_tilt_now

    out = trigger_tilt_now(force=True)
    assert out["skipped"] is True
    assert out["reason"] == "stocks_trading_paused"


def test_scheduler_jobs_exclude_stock_jobs_while_paused(monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    import app.scheduler as sched_mod

    sched_mod.stop_scheduler()
    sched = sched_mod.start_scheduler()
    try:
        job_ids = {j.id for j in sched.get_jobs()}
        assert "khabari_tilt" not in job_ids
        assert "khabari_backup_analyze" not in job_ids
        assert "khabari_position_monitor" not in job_ids
        assert "khabari_day_wrap" not in job_ids
        # Options must keep running
        assert "khabari_options_backup_analyze" in job_ids
    finally:
        sched_mod.stop_scheduler()
