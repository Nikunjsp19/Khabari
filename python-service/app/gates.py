"""Free quality gates: confidence threshold + notify only on actionable trades."""

from __future__ import annotations

from typing import Any

from app.config import get_settings


def apply_confidence_gate(recommendation: dict[str, Any]) -> dict[str, Any]:
    """
    Downgrade weak BUY/SELL to HOLD so the desk stays quiet unless the edge is real.
    """
    settings = get_settings()
    rec = dict(recommendation)
    action = str(rec.get("action", "HOLD")).upper()
    conf = float(rec.get("confidence", 0) or 0)
    notes = list(rec.get("risk_notes") or [])
    min_conf = float(settings.min_notify_confidence)

    if action in {"BUY", "SELL"} and conf < min_conf:
        notes.append(
            f"Confidence {conf:.0f} below gate {min_conf:.0f}; converted {action} → HOLD"
        )
        rec["action"] = "HOLD"
        rec["investment"] = 0
        rec["confidence_gated"] = True
        rec["gate_original_action"] = action
    else:
        rec["confidence_gated"] = False

    rec["risk_notes"] = notes
    return rec


def should_notify(recommendation: dict[str, Any]) -> tuple[bool, str]:
    """Notify only for actionable BUY/SELL that cleared the confidence gate."""
    settings = get_settings()
    if not settings.notify_only_actionable:
        return True, "notify_all"

    action = str(recommendation.get("action", "HOLD")).upper()
    conf = float(recommendation.get("confidence", 0) or 0)
    min_conf = float(settings.min_notify_confidence)

    if action == "HOLD":
        return False, "hold_silent"
    if conf < min_conf:
        return False, f"confidence_below_{min_conf:.0f}"
    if action == "BUY" and float(recommendation.get("investment", 0) or 0) < 1:
        return False, "buy_amount_too_small"
    return True, "actionable"


def apply_options_confidence_gate(recommendation: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    rec = dict(recommendation)
    action = str(rec.get("action", "HOLD")).upper()
    conf = float(rec.get("confidence", 0) or 0)
    notes = list(rec.get("risk_notes") or [])
    min_conf = float(settings.options_min_notify_confidence)

    if action in {"BUY_TO_OPEN", "SELL_TO_CLOSE"} and conf < min_conf:
        notes.append(
            f"Options confidence {conf:.0f} below gate {min_conf:.0f}; converted {action} → HOLD"
        )
        rec["action"] = "HOLD"
        rec["investment"] = 0
        rec["contracts"] = 0
        rec["max_loss"] = 0
        rec["confidence_gated"] = True
        rec["gate_original_action"] = action
    else:
        rec["confidence_gated"] = False

    rec["risk_notes"] = notes
    return rec


def apply_options_chase_gate(
    recommendation: dict[str, Any],
    day_moves: dict[str, float] | None = None,
    *,
    max_chase_pct: float | None = None,
    confidence_haircut: float | None = None,
) -> dict[str, Any]:
    """
    Caution on BUY_TO_OPEN that chase an already-large same-day move.

    Does **not** block the suggestion — a ~3% day is significant and buying calls
    after that is optimistic, but the user may still want the ping. Warn, haircut
    confidence, and bump risk so the caution is visible.
    """
    settings = get_settings()
    rec = dict(recommendation)
    action = str(rec.get("action", "HOLD")).upper()
    notes = list(rec.get("risk_notes") or [])
    reasoning = list(rec.get("reasoning") or [])
    threshold = float(
        max_chase_pct
        if max_chase_pct is not None
        else settings.options_max_intraday_chase_pct
    )
    haircut = float(
        confidence_haircut
        if confidence_haircut is not None
        else settings.options_chase_confidence_haircut
    )
    day_moves = day_moves or {}

    if action != "BUY_TO_OPEN" or threshold <= 0:
        rec["chase_warned"] = False
        rec["risk_notes"] = notes
        return rec

    ticker = str(rec.get("ticker") or "").upper()
    right = str(rec.get("right") or "").lower()
    day_pct = day_moves.get(ticker)
    if day_pct is None or not ticker:
        rec["chase_warned"] = False
        rec["risk_notes"] = notes
        return rec

    rec["day_pct"] = round(float(day_pct), 3)
    chasing = (right == "call" and day_pct >= threshold) or (
        right == "put" and day_pct <= -threshold
    )
    if chasing:
        warn = (
            f"Chase caution: {ticker} already {day_pct:+.2f}% today — a ~{abs(day_pct):.1f}% "
            f"day is significant; buying a {right} now needs *further* continuation from here "
            f"(premium likely already prices much of today's move)."
        )
        notes.append(warn)
        if warn not in reasoning:
            reasoning.insert(0, warn)
        conf = float(rec.get("confidence", 0) or 0)
        if haircut > 0 and conf > 0:
            new_conf = max(0.0, conf - haircut)
            # Keep suggestable: don't silence the ping with the haircut alone.
            floor = float(settings.options_min_notify_confidence)
            floored = max(new_conf, floor)
            notes.append(
                f"Chase caution: confidence {conf:.0f} → {floored:.0f} "
                f"(−{haircut:.0f}, floored at notify min {floor:.0f})"
            )
            rec["confidence"] = round(floored, 1)
        # Still suggest, but don't present as a low-risk slam dunk.
        if str(rec.get("risk") or "").upper() != "HIGH":
            rec["risk"] = "HIGH"
            notes.append("Chase caution: risk bumped to HIGH")
        rec["chase_warned"] = True
    else:
        rec["chase_warned"] = False

    rec["risk_notes"] = notes
    rec["reasoning"] = reasoning
    return rec


def should_notify_options(recommendation: dict[str, Any]) -> tuple[bool, str]:
    settings = get_settings()
    if not settings.notify_only_actionable:
        return True, "notify_all"

    action = str(recommendation.get("action", "HOLD")).upper()
    conf = float(recommendation.get("confidence", 0) or 0)
    min_conf = float(settings.options_min_notify_confidence)

    # Always ping options HOLDs so you know the hourly scan ran (stocks stay silent on HOLD)
    if action == "HOLD":
        return True, "options_hold_status"
    if conf < min_conf:
        return False, f"confidence_below_{min_conf:.0f}"
    if action == "BUY_TO_OPEN" and float(recommendation.get("investment", 0) or 0) < 1:
        return False, "premium_too_small"
    if action == "SELL_TO_CLOSE" and float(recommendation.get("contracts", 0) or 0) < 1:
        return False, "contracts_too_small"
    return True, "actionable"
