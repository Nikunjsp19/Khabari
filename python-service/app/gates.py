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
