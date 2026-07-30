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


def _normalize_right(right: str | None) -> str | None:
    """Map model/vendor spellings to 'call' / 'put'; None when unrecognized."""
    if right is None:
        return None
    side = str(right).strip().lower().rstrip("s")
    if side in {"call", "c"}:
        return "call"
    if side in {"put", "p"}:
        return "put"
    return None


def is_options_chase(
    right: str | None,
    day_pct: float | None,
    *,
    max_chase_pct: float | None = None,
    runup_pct: float | None = None,
    max_runup_pct: float | None = None,
) -> bool:
    """
    True when buying this right chases an already-large move.

    Covers both the same-day extension (``day_pct``) and a multi-session run-up
    (``runup_pct``) — a name flat today but +10% on the week is still a chase.
    """
    side = _normalize_right(right)
    if side is None:
        return False
    settings = get_settings()
    threshold = float(
        max_chase_pct
        if max_chase_pct is not None
        else settings.options_max_intraday_chase_pct
    )
    runup_threshold = float(
        max_runup_pct
        if max_runup_pct is not None
        else settings.options_max_runup_chase_pct
    )

    if day_pct is not None and threshold > 0:
        move = float(day_pct)
        if (side == "call" and move >= threshold) or (
            side == "put" and move <= -threshold
        ):
            return True

    if runup_pct is not None and runup_threshold > 0:
        run = float(runup_pct)
        if (side == "call" and run >= runup_threshold) or (
            side == "put" and run <= -runup_threshold
        ):
            return True

    return False


def filter_chase_candidates(
    candidates: list[dict[str, Any]],
    day_moves: dict[str, float] | None = None,
    *,
    max_chase_pct: float | None = None,
    runups: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Drop call candidates on big green days and put candidates on big red days.

    Returns (kept, dropped). Kept list is what the LLM should see.
    """
    day_moves = day_moves or {}
    runups = runups or {}
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for c in candidates:
        ticker = str(c.get("underlying") or c.get("ticker") or "").upper()
        right = c.get("right")
        day_pct = day_moves.get(ticker)
        runup_pct = runups.get(ticker)
        if is_options_chase(
            right, day_pct, max_chase_pct=max_chase_pct, runup_pct=runup_pct
        ):
            dropped.append(c)
        else:
            kept.append(c)
    return kept, dropped


def sanitize_ranked_for_chase(
    ranked: list[dict[str, Any]] | None,
    day_moves: dict[str, float] | None = None,
    *,
    runups: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """
    Neutralize BUY_TO_OPEN bias on chase names in the LLM ranking.

    The ranked list is persisted and rendered by the confirm UI, so a
    ``bias: BUY_TO_OPEN`` on a +8% name still reads as a suggestion even when
    the final action was blocked.
    """
    day_moves = day_moves or {}
    runups = runups or {}
    out: list[dict[str, Any]] = []
    for row in ranked or []:
        r = dict(row)
        ticker = str(r.get("ticker") or "").upper()
        bias = str(r.get("bias") or "").upper()
        if bias == "BUY_TO_OPEN" and ticker:
            day_pct = day_moves.get(ticker)
            runup_pct = runups.get(ticker)
            # Bias has no right; block if either direction would be a chase.
            chasing = is_options_chase(
                "call", day_pct, runup_pct=runup_pct
            ) or is_options_chase("put", day_pct, runup_pct=runup_pct)
            if chasing:
                r["bias"] = "HOLD"
                r["chase_blocked"] = True
                moved = day_pct if day_pct is not None else runup_pct
                r["note"] = (
                    f"Chase blocked ({moved:+.2f}% recent move) — "
                    f"{str(row.get('note') or '')}".strip()
                )
        out.append(r)
    return out


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
    # Keep what was rejected for the audit trail, but strip the tradable contract
    # so no downstream renderer can show "HOLD — ORCL CALL $300" as a suggestion.
    rec["blocked_contract"] = {
        "ticker": rec.get("ticker"),
        "right": rec.get("right"),
        "strike": rec.get("strike"),
        "expiry": rec.get("expiry"),
    }
    for field in ("right", "strike", "expiry", "contract_key", "osi", "premium"):
        rec[field] = None
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
    runups: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    Hard-block BUY_TO_OPEN that chase an already-large move.

    Buying calls after a big green day (or puts after a dump) is a classic FOMO
    bet — premium already prices much of the move. Same for a multi-session
    run-up. Convert to HOLD so we never ping extensions as actionable trades.

    Fail-closed: unknown day %, unrecognized right, or missing ticker all block.
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
    runups = runups or {}

    rec["chase_warned"] = False
    rec["chase_blocked"] = False

    if action != "BUY_TO_OPEN":
        rec["risk_notes"] = notes
        return rec

    ticker = str(rec.get("ticker") or "").upper()
    side = _normalize_right(rec.get("right"))
    if not ticker or side is None:
        warn = (
            "Chase blocked: incomplete trade "
            f"(ticker={ticker or 'unknown'}, right={rec.get('right')!r}) — "
            "cannot verify today's extension, refusing BUY_TO_OPEN (fail-closed)."
        )
        return _block_chase_buy(
            rec, action=action, notes=notes, reasoning=reasoning, warn=warn
        )

    day_pct = day_moves.get(ticker)
    runup_pct = runups.get(ticker)
    if day_pct is not None:
        rec["day_pct"] = round(float(day_pct), 3)
    if runup_pct is not None:
        rec["runup_pct"] = round(float(runup_pct), 3)

    if day_pct is None:
        warn = (
            f"Chase blocked: live day move unavailable for {ticker} — refusing "
            f"BUY_TO_OPEN {side} (fail-closed; will not suggest without knowing "
            f"today's extension)."
        )
        return _block_chase_buy(
            rec, action=action, notes=notes, reasoning=reasoning, warn=warn
        )

    if not is_options_chase(
        side, day_pct, max_chase_pct=threshold, runup_pct=runup_pct
    ):
        rec["risk_notes"] = notes
        return rec

    runup_note = (
        f" and {runup_pct:+.2f}% over recent sessions" if runup_pct is not None else ""
    )
    warn = (
        f"Chase blocked: {ticker} already {day_pct:+.2f}% today{runup_note} — that move "
        f"is significant; buying a {side} now is chasing an extension "
        f"(premium already prices much of it). Converted BUY_TO_OPEN → HOLD."
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

    # Chase-blocked results are a non-trade. Pinging them hourly reads like a
    # suggestion for the very name we refused, so stay silent by default.
    if recommendation.get("chase_blocked") and not settings.options_notify_chase_blocked:
        return False, "chase_blocked_silent"

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
