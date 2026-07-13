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
_analyze_lock = threading.Lock()


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
    _maybe_analyze("backup", force_cooldown=True)


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
    """If open positions hit take-profit / stop-loss bands, trigger an analyze."""
    global _last_position_check
    status = market_hours_status()
    if not is_market_hours():
        _last_position_check = {"skipped": True, "reason": "outside_market_hours", "status": status}
        return

    try:
        review = positions_need_review()
        _last_position_check = {"ok": True, "status": status, **review}
        if not review.get("needed"):
            logger.info("Position monitor: no exit bands hit")
            return
        logger.info("Position monitor trigger: %s", review.get("reasons"))
        analyze_result = _maybe_analyze("position_monitor")
        _last_position_check["analyze"] = analyze_result
    except Exception as exc:  # noqa: BLE001
        logger.exception("Position monitor failed")
        _last_position_check = {"ok": False, "error": str(exc), "status": status}


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

    sched = BackgroundScheduler(timezone=settings.market_timezone)
    hour_window = f"{settings.market_start_hour}-{settings.market_end_hour}"
    backup_every = max(1, int(settings.backup_analyze_hours))

    # Backup at market open, then every N hours within the window
    backup_hours = list(range(settings.market_start_hour, settings.market_end_hour + 1, backup_every))
    if settings.market_start_hour not in backup_hours:
        backup_hours.insert(0, settings.market_start_hour)
    backup_hour_expr = ",".join(str(h) for h in sorted(set(backup_hours)))

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
    sched.start()
    # Drop legacy job id from older deployments if it somehow exists
    try:
        if sched.get_job("khabari_hourly_analyze"):
            sched.remove_job("khabari_hourly_analyze")
    except Exception:  # noqa: BLE001
        pass

    _scheduler = sched
    logger.info(
        "Scheduler started (free-tier safe): backup@%s + news/%sm + positions/%sm; "
        "max %s analyzes / %s LLM calls per day",
        backup_hour_expr,
        settings.news_scan_minutes,
        settings.position_monitor_minutes,
        settings.max_analyzes_per_day,
        settings.max_llm_calls_per_day,
    )
    return sched


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
        "last_news_scan": _last_news_scan,
        "last_position_check": _last_position_check,
        "settings": {
            "news_scan_minutes": settings.news_scan_minutes,
            "position_monitor_minutes": settings.position_monitor_minutes,
            "analyze_cooldown_minutes": settings.analyze_cooldown_minutes,
            "backup_analyze_hours": settings.backup_analyze_hours,
            "max_analyzes_per_day": settings.max_analyzes_per_day,
            "max_llm_calls_per_day": settings.max_llm_calls_per_day,
            "news_min_new_articles": settings.news_min_new_articles,
            "min_notify_confidence": settings.min_notify_confidence,
            "notify_only_actionable": settings.notify_only_actionable,
            "analyze_period": settings.analyze_period,
            "analyze_interval": settings.analyze_interval,
        },
        "budget": budget_status(),
    }
