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


def is_options_chase(
    right: str | None,
    day_pct: float | None,
    *,
    max_chase_pct: float | None = None,
) -> bool:
    """True when buying this right would chase an already-large same-day move."""
    if day_pct is None or right is None:
        return False
    settings = get_settings()
    threshold = float(
        max_chase_pct
        if max_chase_pct is not None
        else settings.options_max_intraday_chase_pct
    )
    if threshold <= 0:
        return False
    side = str(right).lower()
    move = float(day_pct)
    return (side == "call" and move >= threshold) or (
        side == "put" and move <= -threshold
    )


def filter_chase_candidates(
    candidates: list[dict[str, Any]],
    day_moves: dict[str, float] | None = None,
    *,
    max_chase_pct: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Drop call candidates on big green days and put candidates on big red days.

    Returns (kept, dropped). Kept list is what the LLM should see.
    """
    day_moves = day_moves or {}
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for c in candidates:
        ticker = str(c.get("underlying") or c.get("ticker") or "").upper()
        right = c.get("right")
        day_pct = day_moves.get(ticker)
        if is_options_chase(right, day_pct, max_chase_pct=max_chase_pct):
            dropped.append(c)
        else:
            kept.append(c)
    return kept, dropped


def _block_chase_buy(
    rec: dict[str, Any],
    *,
    action: str,
    notes: list[str],
    reasoning: list[str],
    warn: str,
) -> dict[str, Any]:
    if warn not in notes:
        notes.append(warn)
    if warn not in reasoning:
        reasoning.insert(0, warn)
    rec["action"] = "HOLD"
    rec["investment"] = 0
    rec["contracts"] = 0
    rec["max_loss"] = 0
    rec["chase_warned"] = True
    rec["chase_blocked"] = True
    rec["gate_original_action"] = action
    if str(rec.get("risk") or "").upper() != "HIGH":
        rec["risk"] = "HIGH"
        notes.append("Chase blocked: risk bumped to HIGH")
    rec["risk_notes"] = notes
    rec["reasoning"] = reasoning
    return rec


def apply_options_chase_gate(
    recommendation: dict[str, Any],
    day_moves: dict[str, float] | None = None,
    *,
    max_chase_pct: float | None = None,
) -> dict[str, Any]:
    """
    Hard-block BUY_TO_OPEN that chase an already-large same-day move.

    Buying calls after a big green day (or puts after a dump) is a classic FOMO
    bet — premium already prices much of the move. Convert to HOLD so we never
    ping those extensions as actionable trades.

    Fail-closed: if day % is unknown we still block (do not suggest blind chases).
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
    day_moves = day_moves or {}

    rec["chase_warned"] = False
    rec["chase_blocked"] = False

    if action != "BUY_TO_OPEN" or threshold <= 0:
        rec["risk_notes"] = notes
        return rec

    ticker = str(rec.get("ticker") or "").upper()
    right = str(rec.get("right") or "").lower()
    if not ticker:
        rec["risk_notes"] = notes
        return rec

    day_pct = day_moves.get(ticker)
    if day_pct is None:
        warn = (
            f"Chase blocked: live day move unavailable for {ticker} — refusing "
            f"BUY_TO_OPEN {right} (fail-closed; will not suggest without knowing "
            f"today's extension)."
        )
        return _block_chase_buy(
            rec, action=action, notes=notes, reasoning=reasoning, warn=warn
        )

    rec["day_pct"] = round(float(day_pct), 3)
    if not is_options_chase(right, day_pct, max_chase_pct=threshold):
        rec["risk_notes"] = notes
        return rec

    warn = (
        f"Chase blocked: {ticker} already {day_pct:+.2f}% today — a ~{abs(day_pct):.1f}% "
        f"day is significant; buying a {right} now is chasing an extension "
        f"(premium already prices much of today's move). Converted BUY_TO_OPEN → HOLD."
    )
    return _block_chase_buy(
        rec, action=action, notes=notes, reasoning=reasoning, warn=warn
    )


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
