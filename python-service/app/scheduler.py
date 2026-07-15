"""Background scheduler: news polls, position checks, hourly backup analyze."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.budget import budget_status, can_start_analyze, record_analyze
from app.llm import LLMError
from app.market_hours import is_market_hours, market_hours_status
from app.news_watch import (
    analyze_cooldown_ok,
    positions_need_review,
    save_watch_state,
    scan_for_new_news,
)
from app.pipeline import run_analysis

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_last_run: dict[str, Any] | None = None
_last_news_scan: dict[str, Any] | None = None
_last_position_check: dict[str, Any] | None = None
_last_day_wrap: dict[str, Any] | None = None
_last_options_run: dict[str, Any] | None = None
_last_options_position_check: dict[str, Any] | None = None
_analyze_lock = threading.Lock()
_options_analyze_lock = threading.Lock()


def _record_analyze(result: dict[str, Any], *, trigger: str, status: dict[str, Any]) -> dict[str, Any]:
    rec = result.get("recommendation", {})
    return {
        "skipped": False,
        "ok": True,
        "trigger": trigger,
        "status": status,
        "recommendation": {
            "action": rec.get("action"),
            "ticker": rec.get("ticker"),
            "investment": rec.get("investment"),
            "confidence": rec.get("confidence"),
        },
        "notify_reason": result.get("notify_reason"),
        "notification_ok": bool((result.get("notification") or {}).get("ok")),
        "mongo": result.get("mongo"),
    }


def _maybe_analyze(trigger: str, *, force_cooldown: bool = False) -> dict[str, Any]:
    global _last_run
    status = market_hours_status()
    if not is_market_hours():
        out = {"skipped": True, "reason": "outside_market_hours", "status": status, "trigger": trigger}
        _last_run = out
        return out

    if not _analyze_lock.acquire(blocking=False):
        out = {"skipped": True, "reason": "analyze_in_progress", "status": status, "trigger": trigger}
        logger.info("Skipping analyze (%s) — another run in progress", trigger)
        return out

    try:
        budget_ok, budget_reason = can_start_analyze()
        if not budget_ok:
            out = {
                "skipped": True,
                "reason": budget_reason,
                "status": status,
                "trigger": trigger,
                "budget": budget_status(),
            }
            logger.info("Skipping analyze (%s) — free-tier budget: %s", trigger, budget_reason)
            if "monthly_spend_cap" in (budget_reason or ""):
                try:
                    from app.notify import notify_spend_cap

                    b = budget_status()
                    # notify_spend_cap is idempotent via month_cap_alerted_for in record_llm_call;
                    # still send once from scheduler if somehow missed.
                    if b.get("month_cap_alerted_for") != b.get("month"):
                        notify_spend_cap(
                            spend_month_usd=float(b.get("spend_month_usd") or 0),
                            month_cap_usd=float(
                                (b.get("limits") or {}).get("max_monthly_spend_usd") or 10
                            ),
                            kind="reached",
                        )
                        from app.budget import _load, _save

                        st = _load()
                        st["month_cap_alerted_for"] = st.get("month")
                        _save(st)
                except Exception:  # noqa: BLE001
                    logger.exception("Failed monthly-cap scheduler alert")
            _last_run = out
            return out

        ok, elapsed = analyze_cooldown_ok()
        # Even backup runs respect cooldown unless nothing ran yet today-ish;
        # force_cooldown only skips the soft wait when budget still allows.
        if not ok and not force_cooldown:
            out = {
                "skipped": True,
                "reason": "cooldown",
                "elapsed_minutes": elapsed,
                "status": status,
                "trigger": trigger,
            }
            logger.info("Skipping analyze (%s) — cooldown %.1fm", trigger, elapsed or 0)
            _last_run = out
            return out
        if force_cooldown and not ok and (elapsed or 0) < (get_settings().analyze_cooldown_minutes * 0.5):
            # Soft protect: backup won't fire if we just analyzed < half cooldown ago
            out = {
                "skipped": True,
                "reason": "recent_run",
                "elapsed_minutes": elapsed,
                "status": status,
                "trigger": trigger,
            }
            logger.info("Skipping backup analyze — ran %.1fm ago", elapsed or 0)
            _last_run = out
            return out

        logger.info("Analyze starting trigger=%s status=%s", trigger, status)
        try:
            result = run_analysis(send_notification=True, trigger=trigger)
            try:
                record_analyze()
            except Exception:  # noqa: BLE001
                logger.warning("Could not record analyze budget", exc_info=True)
            _last_run = _record_analyze(result, trigger=trigger, status=status)
            _last_run["budget"] = budget_status()
            logger.info("Analyze done (%s): %s", trigger, _last_run["recommendation"])
            return _last_run
        except LLMError as exc:
            logger.error("Analyze LLM error (%s): %s", trigger, exc)
            _last_run = {
                "skipped": False,
                "ok": False,
                "error": str(exc),
                "status": status,
                "trigger": trigger,
                "budget": budget_status(),
            }
            return _last_run
        except Exception as exc:  # noqa: BLE001
            logger.exception("Analyze failed (%s)", trigger)
            _last_run = {
                "skipped": False,
                "ok": False,
                "error": str(exc),
                "status": status,
                "trigger": trigger,
            }
            return _last_run
    finally:
        _analyze_lock.release()


def _backup_job() -> None:
    """Sparse backup full scan if news was quiet (still budget-capped)."""
    _sync_famous_stock_watchlist()
    _maybe_analyze("backup", force_cooldown=True)


def _sync_famous_stock_watchlist() -> None:
    """Keep stock watchlist on the configured famous/liquid universe."""
    settings = get_settings()
    if not settings.watchlist_auto_famous:
        return
    try:
        from app.db import set_watchlist

        set_watchlist(settings.watchlist_symbols)
    except Exception:  # noqa: BLE001
        logger.warning("Could not sync famous stock watchlist", exc_info=True)


def _record_options_analyze(
    result: dict[str, Any], *, trigger: str, status: dict[str, Any]
) -> dict[str, Any]:
    rec = result.get("recommendation", {})
    return {
        "skipped": False,
        "ok": True,
        "trigger": trigger,
        "status": status,
        "asset_class": "options",
        "recommendation": {
            "action": rec.get("action"),
            "ticker": rec.get("ticker"),
            "right": rec.get("right"),
            "strike": rec.get("strike"),
            "expiry": rec.get("expiry"),
            "contracts": rec.get("contracts"),
            "investment": rec.get("investment"),
            "confidence": rec.get("confidence"),
        },
        "notify_reason": result.get("notify_reason"),
        "notification_ok": bool((result.get("notification") or {}).get("ok")),
        "mongo": result.get("mongo"),
    }


def _maybe_options_analyze(trigger: str, *, force_cooldown: bool = False) -> dict[str, Any]:
    global _last_options_run
    settings = get_settings()
    status = market_hours_status()
    if not settings.options_scheduler_enabled:
        out = {"skipped": True, "reason": "options_scheduler_disabled", "trigger": trigger}
        _last_options_run = out
        return out
    if not is_market_hours():
        out = {
            "skipped": True,
            "reason": "outside_market_hours",
            "status": status,
            "trigger": trigger,
        }
        _last_options_run = out
        return out

    if not _options_analyze_lock.acquire(blocking=False):
        out = {
            "skipped": True,
            "reason": "options_analyze_in_progress",
            "status": status,
            "trigger": trigger,
        }
        return out

    try:
        budget_ok, budget_reason = can_start_analyze()
        if not budget_ok:
            out = {
                "skipped": True,
                "reason": budget_reason,
                "status": status,
                "trigger": trigger,
                "budget": budget_status(),
            }
            logger.info("Skipping options analyze (%s) — budget: %s", trigger, budget_reason)
            _last_options_run = out
            return out

        ok, elapsed = analyze_cooldown_ok()
        if not ok and not force_cooldown:
            out = {
                "skipped": True,
                "reason": "cooldown",
                "elapsed_minutes": elapsed,
                "status": status,
                "trigger": trigger,
            }
            _last_options_run = out
            return out
        # Hourly options: only soft-block if another analyze ran in the last ~20m
        min_gap = float(settings.options_analyze_min_gap_minutes)
        if force_cooldown and not ok and (elapsed or 0) < min_gap:
            out = {
                "skipped": True,
                "reason": "recent_run",
                "elapsed_minutes": elapsed,
                "status": status,
                "trigger": trigger,
            }
            _last_options_run = out
            return out

        from app.options_pipeline import run_options_analysis

        logger.info("Options analyze starting trigger=%s", trigger)
        try:
            result = run_options_analysis(send_notification=True, trigger=f"options_{trigger}")
            try:
                record_analyze()
            except Exception:  # noqa: BLE001
                logger.warning("Could not record options analyze budget", exc_info=True)
            _last_options_run = _record_options_analyze(result, trigger=trigger, status=status)
            _last_options_run["budget"] = budget_status()
            logger.info("Options analyze done (%s): %s", trigger, _last_options_run["recommendation"])
            return _last_options_run
        except LLMError as exc:
            logger.error("Options analyze LLM error (%s): %s", trigger, exc)
            _last_options_run = {
                "skipped": False,
                "ok": False,
                "error": str(exc),
                "status": status,
                "trigger": trigger,
                "budget": budget_status(),
            }
            return _last_options_run
        except Exception as exc:  # noqa: BLE001
            logger.exception("Options analyze failed (%s)", trigger)
            _last_options_run = {
                "skipped": False,
                "ok": False,
                "error": str(exc),
                "status": status,
                "trigger": trigger,
            }
            return _last_options_run
    finally:
        _options_analyze_lock.release()


def _options_backup_job() -> None:
    _maybe_options_analyze("backup", force_cooldown=True)


def _options_position_monitor_job() -> None:
    global _last_options_position_check
    status = market_hours_status()
    if not is_market_hours():
        _last_options_position_check = {
            "skipped": True,
            "reason": "outside_market_hours",
            "status": status,
        }
        return
    try:
        from app.options_trades import options_positions_need_review

        review = options_positions_need_review()
        _last_options_position_check = {"ok": True, "status": status, **review}
        if not review.get("needed"):
            logger.info("Options position monitor: no exit bands hit")
            return
        logger.info("Options position monitor trigger: %s", review.get("reasons"))
        analyze_result = _maybe_options_analyze("position_monitor")
        _last_options_position_check["analyze"] = analyze_result
    except Exception as exc:  # noqa: BLE001
        logger.exception("Options position monitor failed")
        _last_options_position_check = {"ok": False, "error": str(exc), "status": status}


def _news_scan_job() -> None:
    """Poll Yahoo news; run full analyze only on meaningful new headlines."""
    global _last_news_scan
    status = market_hours_status()
    if not is_market_hours():
        _last_news_scan = {"skipped": True, "reason": "outside_market_hours", "status": status}
        return

    try:
        scan = scan_for_new_news()
        _last_news_scan = {"ok": True, "status": status, **scan}
        if scan.get("first_scan"):
            logger.info("News watcher seeded fingerprints; waiting for next new headline")
            return
        if not scan.get("meaningful"):
            logger.info(
                "News scan: no meaningful change (new=%s need=%s)",
                scan.get("new_count"),
                scan.get("min_needed"),
            )
            return

        save_watch_state(last_trigger_at=datetime.now(timezone.utc))
        analyze_result = _maybe_analyze("news_change")
        _last_news_scan["analyze"] = analyze_result
    except Exception as exc:  # noqa: BLE001
        logger.exception("News scan failed")
        _last_news_scan = {"ok": False, "error": str(exc), "status": status}


def _position_monitor_job() -> None:
    """Deterministic exit engine: fire decisive SELL alerts on stop/target/time hits.

    Unlike the old band check (which only woke the LLM), the exit engine sends a
    concrete SELL recommendation the user can confirm — no LLM spend required.
    """
    global _last_position_check
    status = market_hours_status()
    if not is_market_hours():
        _last_position_check = {"skipped": True, "reason": "outside_market_hours", "status": status}
        return

    settings = get_settings()
    if not settings.exit_engine_enabled:
        try:
            review = positions_need_review()
            _last_position_check = {"ok": True, "status": status, **review}
            if not review.get("needed"):
                logger.info("Position monitor: no exit bands hit")
                return
            logger.info("Position monitor trigger: %s", review.get("reasons"))
            _last_position_check["analyze"] = _maybe_analyze("position_monitor")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Position monitor failed")
            _last_position_check = {"ok": False, "error": str(exc), "status": status}
        return

    try:
        from app.exits import run_exit_monitor

        result = run_exit_monitor(send_notification=True)
        _last_position_check = {"ok": True, "status": status, **result}
        if not result.get("needed"):
            logger.info("Exit engine: no stops/targets hit (%s positions)", result.get("positions"))
            return
        logger.info(
            "Exit engine fired %s SELL alert(s): %s",
            len(result.get("alerted") or []),
            [a.get("ticker") for a in (result.get("alerted") or [])],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Exit engine failed")
        _last_position_check = {"ok": False, "error": str(exc), "status": status}


_last_tilt_run: dict[str, Any] | None = None


def _tilt_job() -> None:
    """Momentum-tilt engine: monthly rebalance + daily 200d trend-brake SELLs.

    Runs during market hours, no LLM spend. Idempotent per month (the engine
    tracks the last rebalance month), so extra ticks just do the trend-brake
    check. Emits standard BUY/SELL recommendations you confirm in Hisaab.
    """
    global _last_tilt_run
    status = market_hours_status()
    if not is_market_hours():
        _last_tilt_run = {"skipped": True, "reason": "outside_market_hours", "status": status}
        return
    try:
        from app.tilt import run_tilt_rebalance

        result = run_tilt_rebalance(send_notification=True)
        _last_tilt_run = {"status": status, **result}
        if result.get("emitted"):
            logger.info(
                "Tilt job: %s (%s trades) — %s",
                "REBALANCE" if result.get("rebalance") else "trend-brake",
                len(result.get("emitted") or []),
                [e.get("ticker") for e in (result.get("emitted") or [])],
            )
        else:
            logger.info("Tilt job: nothing to do (rebalance=%s)", result.get("rebalance"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Tilt job failed")
        _last_tilt_run = {"ok": False, "error": str(exc), "status": status}


def trigger_tilt_now(force: bool = True) -> dict[str, Any]:
    """Run the tilt engine immediately (API/ops)."""
    from app.tilt import run_tilt_rebalance

    return run_tilt_rebalance(force=force, send_notification=True)


def _day_wrap_job() -> None:
    """Mon–Fri after the close: push concluding news + today's suggestions."""
    global _last_day_wrap
    try:
        from app.day_wrap import run_day_wrap

        _last_day_wrap = run_day_wrap(force=False)
        if _last_day_wrap.get("skipped"):
            logger.info("Day wrap skipped: %s", _last_day_wrap.get("reason"))
        else:
            logger.info(
                "Day wrap sent day=%s actionable=%s news=%s ok=%s",
                _last_day_wrap.get("day"),
                (_last_day_wrap.get("wrap") or {}).get("counts", {}).get("actionable"),
                (_last_day_wrap.get("wrap") or {}).get("counts", {}).get("top_news"),
                _last_day_wrap.get("ok"),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Day wrap failed")
        _last_day_wrap = {"ok": False, "error": str(exc)}


def _cron_minute_expr(every_minutes: int, *, minimum: int = 1) -> str:
    """APScheduler minute field: */N is invalid when N >= 60."""
    every = max(minimum, int(every_minutes))
    if every >= 60:
        return "0"
    return f"*/{every}"


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    settings = get_settings()
    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled (SCHEDULER_ENABLED=false)")
        return None

    if _scheduler and _scheduler.running:
        return _scheduler

    job_defaults = {
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 900,  # still run if woken ≤15 min late (Docker sleep)
    }
    sched = BackgroundScheduler(timezone=settings.market_timezone, job_defaults=job_defaults)
    hour_window = f"{settings.market_start_hour}-{settings.market_end_hour}"
    backup_every = max(1, int(settings.backup_analyze_hours))

    # Backup at market open, then every N hours within the window
    backup_hours = list(range(settings.market_start_hour, settings.market_end_hour + 1, backup_every))
    if settings.market_start_hour not in backup_hours:
        backup_hours.insert(0, settings.market_start_hour)
    backup_hour_expr = ",".join(str(h) for h in sorted(set(backup_hours)))

    if settings.tilt_enabled:
        # Momentum tilt is the primary stock engine: it replaces the LLM
        # buy/sell-timing analyze (backup + news-triggered) AND the ATR exit
        # engine, because the tilt has its own monthly rebalance + trend brake.
        # Run a few times across the session so a mid-session start / VM wake
        # still triggers the monthly rebalance and the daily trend-brake check.
        tilt_hours = sorted(
            {settings.market_start_hour, (settings.market_start_hour + settings.market_end_hour) // 2}
        )
        sched.add_job(
            _tilt_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour=",".join(str(h) for h in tilt_hours),
                minute=5,
                timezone=settings.market_timezone,
            ),
            id="khabari_tilt",
            replace_existing=True,
        )
    else:
        sched.add_job(
            _backup_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour=backup_hour_expr,
                minute=0,
                timezone=settings.market_timezone,
            ),
            id="khabari_backup_analyze",
            replace_existing=True,
        )

        sched.add_job(
            _news_scan_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour=hour_window,
                minute=_cron_minute_expr(settings.news_scan_minutes, minimum=5),
                timezone=settings.market_timezone,
            ),
            id="khabari_news_scan",
            replace_existing=True,
        )
        sched.add_job(
            _position_monitor_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour=hour_window,
                minute=_cron_minute_expr(settings.position_monitor_minutes, minimum=15),
                timezone=settings.market_timezone,
            ),
            id="khabari_position_monitor",
            replace_existing=True,
        )

    if settings.day_wrap_enabled:
        sched.add_job(
            _day_wrap_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour=settings.day_wrap_hour,
                minute=settings.day_wrap_minute,
                timezone=settings.market_timezone,
            ),
            id="khabari_day_wrap",
            replace_existing=True,
        )

    if settings.options_scheduler_enabled:
        options_backup_every = max(1, int(settings.options_backup_analyze_hours))
        options_backup_hours = list(
            range(settings.market_start_hour, settings.market_end_hour + 1, options_backup_every)
        )
        if settings.market_start_hour not in options_backup_hours:
            options_backup_hours.insert(0, settings.market_start_hour)
        options_backup_hour_expr = ",".join(str(h) for h in sorted(set(options_backup_hours)))
        # Stagger to :30 so we don't collide with stock backup at :00
        # With OPTIONS_BACKUP_ANALYZE_HOURS=1 this is every hour Mon–Fri 9:30–16:30 ET
        sched.add_job(
            _options_backup_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour=options_backup_hour_expr,
                minute=30,
                timezone=settings.market_timezone,
            ),
            id="khabari_options_backup_analyze",
            replace_existing=True,
        )
        sched.add_job(
            _options_position_monitor_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour=hour_window,
                minute=_cron_minute_expr(settings.position_monitor_minutes, minimum=15),
                timezone=settings.market_timezone,
            ),
            id="khabari_options_position_monitor",
            replace_existing=True,
        )

    sched.start()
    # Drop legacy job id from older deployments if it somehow exists
    try:
        if sched.get_job("khabari_hourly_analyze"):
            sched.remove_job("khabari_hourly_analyze")
    except Exception:  # noqa: BLE001
        pass

    _scheduler = sched
    logger.info(
        "Scheduler started: backup@%s + news/%sm + positions/%sm "
        "+ day_wrap@%02d:%02d; max %s analyzes / $%.2f daily / $%.2f monthly",
        backup_hour_expr,
        settings.news_scan_minutes,
        settings.position_monitor_minutes,
        settings.day_wrap_hour if settings.day_wrap_enabled else -1,
        settings.day_wrap_minute if settings.day_wrap_enabled else -1,
        settings.max_analyzes_per_day,
        settings.max_daily_spend_usd,
        settings.max_monthly_spend_usd,
    )

    # If we start mid-session (or wake from sleep), don't wait for the next cron tick
    if is_market_hours():
        sched.add_job(
            _tilt_job if settings.tilt_enabled else _backup_job,
            id="khabari_startup_catchup",
            replace_existing=True,
            misfire_grace_time=900,
        )
        logger.info(
            "Queued startup catch-up %s (market is open)",
            "tilt rebalance" if settings.tilt_enabled else "analyze",
        )

    # If we start after the wrap time on a weekday, still send today's wrap once
    if settings.day_wrap_enabled:
        from app.market_hours import now_market

        now = now_market()
        wrap_passed = (now.hour, now.minute) >= (settings.day_wrap_hour, settings.day_wrap_minute)
        if now.weekday() <= 4 and wrap_passed:
            sched.add_job(
                _day_wrap_job,
                id="khabari_day_wrap_catchup",
                replace_existing=True,
                misfire_grace_time=3600,
            )
            logger.info("Queued day-wrap catch-up (past %02d:%02d ET)", settings.day_wrap_hour, settings.day_wrap_minute)

    return sched


def trigger_analyze_now(trigger: str = "manual") -> dict[str, Any]:
    """Run analyze immediately (used by API / ops)."""
    return _maybe_analyze(trigger, force_cooldown=True)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
    _scheduler = None


def scheduler_status() -> dict[str, Any]:
    settings = get_settings()
    jobs = []
    if _scheduler:
        for job in _scheduler.get_jobs():
            jobs.append(
                {
                    "id": job.id,
                    "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                }
            )
    return {
        "enabled": settings.scheduler_enabled,
        "running": bool(_scheduler and _scheduler.running),
        "window": market_hours_status(),
        "jobs": jobs,
        "last_run": _last_run,
        "last_tilt_run": _last_tilt_run,
        "last_news_scan": _last_news_scan,
        "last_position_check": _last_position_check,
        "last_day_wrap": _last_day_wrap,
        "last_options_run": _last_options_run,
        "last_options_position_check": _last_options_position_check,
        "settings": {
            "news_scan_minutes": settings.news_scan_minutes,
            "position_monitor_minutes": settings.position_monitor_minutes,
            "analyze_cooldown_minutes": settings.analyze_cooldown_minutes,
            "backup_analyze_hours": settings.backup_analyze_hours,
            "max_analyzes_per_day": settings.max_analyzes_per_day,
            "max_llm_calls_per_day": settings.max_llm_calls_per_day,
            "max_daily_spend_usd": settings.max_daily_spend_usd,
            "max_monthly_spend_usd": settings.max_monthly_spend_usd,
            "news_min_new_articles": settings.news_min_new_articles,
            "min_notify_confidence": settings.min_notify_confidence,
            "notify_only_actionable": settings.notify_only_actionable,
            "analyze_period": settings.analyze_period,
            "analyze_interval": settings.analyze_interval,
            "day_wrap_enabled": settings.day_wrap_enabled,
            "day_wrap_hour": settings.day_wrap_hour,
            "day_wrap_minute": settings.day_wrap_minute,
            "options_scheduler_enabled": settings.options_scheduler_enabled,
            "options_min_notify_confidence": settings.options_min_notify_confidence,
            "options_backup_analyze_hours": settings.options_backup_analyze_hours,
            "options_analyze_min_gap_minutes": settings.options_analyze_min_gap_minutes,
            "options_auto_movers": settings.options_auto_movers,
            "options_mover_top_n": settings.options_mover_top_n,
            "options_mover_min_abs_pct": settings.options_mover_min_abs_pct,
            "options_data_source": "yfinance",
            "signal_buy_threshold": settings.signal_buy_threshold,
            "signal_shortlist_size": settings.signal_shortlist_size,
            "regime_block_buys_in_risk_off": settings.regime_block_buys_in_risk_off,
            "exit_engine_enabled": settings.exit_engine_enabled,
            "exit_trail_atr_mult": settings.exit_trail_atr_mult,
            "exit_initial_stop_atr_mult": settings.exit_initial_stop_atr_mult,
            "exit_time_stop_days": settings.exit_time_stop_days,
            "tilt_enabled": settings.tilt_enabled,
            "tilt_top_n": settings.tilt_top_n,
            "tilt_rebalance_band_pct": settings.tilt_rebalance_band_pct,
            "tilt_require_uptrend": settings.tilt_require_uptrend,
        },
        "budget": budget_status(),
    }
