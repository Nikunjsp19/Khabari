"""Monthly/daily spend caps to keep Gemini under a hard dollar budget."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.db import get_db
from app.market_hours import market_tz

logger = logging.getLogger(__name__)

_BUDGET_ID = "free_tier_budget"

# Conservative paid prices ($ per 1M tokens). Unknown aliases use the expensive Flash rate.
_MODEL_RATES: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-3-flash": (0.50, 3.00),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-flash-latest": (1.50, 9.00),  # alias can resolve to pricier Flash
    "gpt-4o-mini": (0.15, 0.60),
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _today_key() -> str:
    return datetime.now(market_tz()).date().isoformat()


def _month_key() -> str:
    return datetime.now(market_tz()).strftime("%Y-%m")


def _rates_for_model(model: str) -> tuple[float, float]:
    key = (model or "").strip().lower()
    if key in _MODEL_RATES:
        return _MODEL_RATES[key]
    for name, rates in _MODEL_RATES.items():
        if name in key or key in name:
            return rates
    # Unknown model: price like 3.5 Flash (safe overestimate)
    return (1.50, 9.00)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = _rates_for_model(model)
    return (input_tokens / 1_000_000.0) * in_rate + (output_tokens / 1_000_000.0) * out_rate


def _load() -> dict[str, Any]:
    doc = get_db().meta.find_one({"_id": _BUDGET_ID}) or {}
    today = _today_key()
    month = _month_key()

    # Roll daily counters on new calendar day (market TZ)
    if doc.get("day") != today:
        analyzes = 0
        llm_calls = 0
        spend_day = 0.0
    else:
        analyzes = int(doc.get("analyzes") or 0)
        llm_calls = int(doc.get("llm_calls") or 0)
        spend_day = float(doc.get("spend_day_usd") or 0.0)

    # Roll monthly spend on new calendar month
    if doc.get("month") != month:
        spend_month = 0.0
    else:
        spend_month = float(doc.get("spend_month_usd") or 0.0)

    return {
        "day": today,
        "month": month,
        "analyzes": analyzes,
        "llm_calls": llm_calls,
        "spend_day_usd": round(spend_day, 6),
        "spend_month_usd": round(spend_month, 6),
        "paused_until": doc.get("paused_until"),
        "last_quota_error_at": doc.get("last_quota_error_at"),
        "last_quota_error": doc.get("last_quota_error"),
        "month_cap_alerted_for": doc.get("month_cap_alerted_for"),
        "month_warn_alerted_for": doc.get("month_warn_alerted_for"),
    }


def _save(state: dict[str, Any]) -> None:
    get_db().meta.update_one(
        {"_id": _BUDGET_ID},
        {"$set": {**state, "updated_at": _now_utc()}},
        upsert=True,
    )


def clear_quota_pause() -> dict[str, Any]:
    """Clear a temporary pause (e.g. after enabling paid Tier 1)."""
    state = _load()
    state["paused_until"] = None
    state["last_quota_error"] = None
    _save(state)
    logger.info("Cleared quota pause")
    return budget_status()


def budget_status() -> dict[str, Any]:
    settings = get_settings()
    state = _load()
    paused, pause_reason = is_paused(state)
    month_cap = float(settings.max_monthly_spend_usd)
    return {
        **state,
        "limits": {
            "max_analyzes_per_day": settings.max_analyzes_per_day,
            "max_llm_calls_per_day": settings.max_llm_calls_per_day,
            "max_monthly_spend_usd": month_cap,
            "analyze_cooldown_minutes": settings.analyze_cooldown_minutes,
            "quota_pause_minutes": settings.quota_pause_minutes,
            "model": settings.gemini_model,
        },
        "paused": paused,
        "pause_reason": pause_reason,
        "analyzes_remaining": max(0, settings.max_analyzes_per_day - state["analyzes"]),
        "llm_calls_remaining": max(0, settings.max_llm_calls_per_day - state["llm_calls"]),
        "month_remaining_usd": round(max(0.0, month_cap - state["spend_month_usd"]), 4),
    }


def is_paused(state: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    state = state or _load()
    settings = get_settings()

    # Hard monthly dollar stop
    if float(state.get("spend_month_usd") or 0) >= float(settings.max_monthly_spend_usd):
        return True, f"monthly_spend_cap_${settings.max_monthly_spend_usd:.2f}"

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
    """Gate full analyze runs (3 LLM calls each) against daily/monthly caps."""
    settings = get_settings()
    state = _load()
    paused, reason = is_paused(state)
    if paused:
        return False, reason or "paused"

    if state["analyzes"] >= settings.max_analyzes_per_day:
        return False, f"daily_analyze_cap_{settings.max_analyzes_per_day}"

    if state["llm_calls"] + 3 > settings.max_llm_calls_per_day:
        return False, f"daily_llm_cap_{settings.max_llm_calls_per_day}"

    # Leave a little monthly headroom (~1 analyze worth)
    month_left = float(settings.max_monthly_spend_usd) - float(state["spend_month_usd"])
    if month_left <= 0.02:
        return False, f"monthly_spend_cap_${settings.max_monthly_spend_usd:.2f}"

    return True, "ok"


def can_call_llm() -> tuple[bool, str]:
    settings = get_settings()
    state = _load()
    paused, reason = is_paused(state)
    if paused:
        return False, reason or "paused"
    if state["llm_calls"] >= settings.max_llm_calls_per_day:
        return False, f"daily_llm_cap_{settings.max_llm_calls_per_day}"
    if float(state["spend_month_usd"]) >= float(settings.max_monthly_spend_usd):
        return False, f"monthly_spend_cap_${settings.max_monthly_spend_usd:.2f}"
    return True, "ok"


def record_analyze() -> None:
    state = _load()
    state["analyzes"] = int(state.get("analyzes") or 0) + 1
    _save(state)
    logger.info(
        "Budget analyzes=%s/%s month_spend=$%.4f/$%.2f",
        state["analyzes"],
        get_settings().max_analyzes_per_day,
        state["spend_month_usd"],
        get_settings().max_monthly_spend_usd,
    )


def record_llm_call(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    model: str | None = None,
) -> float:
    """Record one LLM call and estimated USD cost. Returns cost added."""
    settings = get_settings()
    model = model or settings.gemini_model
    # If API omitted usage, assume a conservative mid-size call.
    if input_tokens <= 0 and output_tokens <= 0:
        input_tokens, output_tokens = 5000, 1500
    cost = estimate_cost_usd(model, input_tokens, output_tokens)

    state = _load()
    prev_month = float(state.get("spend_month_usd") or 0)
    month_cap = float(settings.max_monthly_spend_usd)
    warn_at = month_cap * 0.8

    state["llm_calls"] = int(state.get("llm_calls") or 0) + 1
    state["spend_day_usd"] = round(float(state.get("spend_day_usd") or 0) + cost, 6)
    state["spend_month_usd"] = round(prev_month + cost, 6)
    month = state["month"]
    new_month = float(state["spend_month_usd"])

    # Alert once when crossing 80% and once when hitting the hard cap
    alert_kind: str | None = None
    if prev_month < month_cap <= new_month and state.get("month_cap_alerted_for") != month:
        alert_kind = "reached"
        state["month_cap_alerted_for"] = month
    elif prev_month < warn_at <= new_month and state.get("month_warn_alerted_for") != month:
        alert_kind = "warning"
        state["month_warn_alerted_for"] = month

    _save(state)
    logger.info(
        "LLM call #%s cost≈$%.4f day=$%.4f month=$%.4f/$%.2f model=%s in=%s out=%s",
        state["llm_calls"],
        cost,
        state["spend_day_usd"],
        state["spend_month_usd"],
        settings.max_monthly_spend_usd,
        model,
        input_tokens,
        output_tokens,
    )

    if alert_kind:
        try:
            from app.notify import notify_spend_cap

            notify_spend_cap(
                spend_month_usd=new_month,
                month_cap_usd=month_cap,
                kind=alert_kind,
            )
            logger.warning(
                "Sent monthly spend %s alert: $%.4f / $%.2f",
                alert_kind,
                new_month,
                month_cap,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send monthly spend cap alert")

    return cost

def trip_quota_pause(error_text: str = "") -> None:
    """After Gemini 429 / quota errors, pause LLM usage for a while."""
    settings = get_settings()
    state = _load()
    until = _now_utc() + timedelta(minutes=max(15, int(settings.quota_pause_minutes)))
    state["paused_until"] = until
    state["last_quota_error_at"] = _now_utc()
    state["last_quota_error"] = (error_text or "")[:300]
    _save(state)
    logger.warning("API quota pause until %s (%s)", until.isoformat(), error_text[:120])


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
