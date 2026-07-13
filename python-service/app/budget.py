"""Daily free-tier budgets for Gemini / analyze runs (Mongo-backed)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.db import get_db
from app.market_hours import market_tz

logger = logging.getLogger(__name__)

_BUDGET_ID = "free_tier_budget"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _today_key() -> str:
    return datetime.now(market_tz()).date().isoformat()


def _load() -> dict[str, Any]:
    doc = get_db().meta.find_one({"_id": _BUDGET_ID}) or {}
    today = _today_key()
    if doc.get("day") != today:
        return {
            "day": today,
            "analyzes": 0,
            "llm_calls": 0,
            "paused_until": None,
            "last_quota_error_at": None,
        }
    return {
        "day": today,
        "analyzes": int(doc.get("analyzes") or 0),
        "llm_calls": int(doc.get("llm_calls") or 0),
        "paused_until": doc.get("paused_until"),
        "last_quota_error_at": doc.get("last_quota_error_at"),
    }


def _save(state: dict[str, Any]) -> None:
    get_db().meta.update_one(
        {"_id": _BUDGET_ID},
        {"$set": {**state, "updated_at": _now_utc()}},
        upsert=True,
    )


def budget_status() -> dict[str, Any]:
    settings = get_settings()
    state = _load()
    paused, pause_reason = is_paused(state)
    return {
        **state,
        "limits": {
            "max_analyzes_per_day": settings.max_analyzes_per_day,
            "max_llm_calls_per_day": settings.max_llm_calls_per_day,
            "analyze_cooldown_minutes": settings.analyze_cooldown_minutes,
            "quota_pause_minutes": settings.quota_pause_minutes,
        },
        "paused": paused,
        "pause_reason": pause_reason,
        "analyzes_remaining": max(0, settings.max_analyzes_per_day - state["analyzes"]),
        "llm_calls_remaining": max(0, settings.max_llm_calls_per_day - state["llm_calls"]),
    }


def is_paused(state: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    state = state or _load()
    paused_until = state.get("paused_until")
    if paused_until:
        if isinstance(paused_until, str):
            paused_until = datetime.fromisoformat(paused_until.replace("Z", "+00:00"))
        if paused_until.tzinfo is None:
            paused_until = paused_until.replace(tzinfo=timezone.utc)
        if _now_utc() < paused_until:
            return True, f"quota_pause_until_{paused_until.isoformat()}"
    return False, None


def can_start_analyze() -> tuple[bool, str]:
    """Gate full analyze runs (3 LLM calls each) against daily free-tier caps."""
    settings = get_settings()
    state = _load()
    paused, reason = is_paused(state)
    if paused:
        return False, reason or "paused"

    if state["analyzes"] >= settings.max_analyzes_per_day:
        return False, f"daily_analyze_cap_{settings.max_analyzes_per_day}"

    # Reserve room for the 3 agent calls in one analyze
    if state["llm_calls"] + 3 > settings.max_llm_calls_per_day:
        return False, f"daily_llm_cap_{settings.max_llm_calls_per_day}"

    return True, "ok"


def can_call_llm() -> tuple[bool, str]:
    settings = get_settings()
    state = _load()
    paused, reason = is_paused(state)
    if paused:
        return False, reason or "paused"
    if state["llm_calls"] >= settings.max_llm_calls_per_day:
        return False, f"daily_llm_cap_{settings.max_llm_calls_per_day}"
    return True, "ok"


def record_analyze() -> None:
    state = _load()
    state["analyzes"] = int(state.get("analyzes") or 0) + 1
    _save(state)
    logger.info("Budget analyzes=%s/%s", state["analyzes"], get_settings().max_analyzes_per_day)


def record_llm_call() -> None:
    state = _load()
    state["llm_calls"] = int(state.get("llm_calls") or 0) + 1
    _save(state)


def trip_quota_pause(error_text: str = "") -> None:
    """After Gemini 429 / quota errors, pause LLM usage for a while."""
    settings = get_settings()
    state = _load()
    until = _now_utc() + timedelta(minutes=max(15, int(settings.quota_pause_minutes)))
    state["paused_until"] = until
    state["last_quota_error_at"] = _now_utc()
    state["last_quota_error"] = (error_text or "")[:300]
    _save(state)
    logger.warning("Free-tier quota pause until %s (%s)", until.isoformat(), error_text[:120])


def looks_like_quota_error(message: str) -> bool:
    m = (message or "").lower()
    return any(
        token in m
        for token in (
            "429",
            "resource_exhausted",
            "quota",
            "rate limit",
            "rate_limit",
            "too many requests",
        )
    )
