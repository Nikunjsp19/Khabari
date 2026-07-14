"""Notification helpers — ntfy (easiest) and optional Telegram."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


def _confirm_url(recommendation_id: str | None = None, *, tab: str | None = None) -> str:
    settings = get_settings()
    # Options always confirm on Khabari desk (Hisaab has no options UI yet)
    if tab == "options":
        base = (settings.public_base_url or "http://localhost:8000").rstrip("/")
        url = f"{base}/desk?tab=options"
        if recommendation_id:
            url = f"{url}&id={recommendation_id}"
        return url

    base = (settings.hisaab_base_url or settings.public_base_url or "http://localhost:8000").rstrip("/")
    # Prefer Hisaab /trades (phone); fall back to Khabari /desk
    path = "/trades" if settings.hisaab_base_url else "/desk"
    url = f"{base}{path}"
    if recommendation_id:
        url = f"{url}?id={recommendation_id}"
    return url


def format_recommendation_message(
    rec: dict[str, Any],
    *,
    markdown: bool = False,
    recommendation_id: str | None = None,
) -> str:
    desk = _confirm_url(recommendation_id)
    reasons = rec.get("reasoning") or []
    sync_line = f"\n\nAfter you trade, confirm in Hisaab:\n{desk}"

    if markdown:
        bullets = "\n".join(f"• {r}" for r in reasons) or "• (none)"
        return (
            "🚨 *AI Recommendation*\n\n"
            f"*{rec.get('action')} {rec.get('ticker')}* – Invest ${rec.get('investment')}\n"
            f"Confidence: {rec.get('confidence')}%\n"
            f"Risk: {rec.get('risk')}\n\n"
            f"*Reasons:*\n{bullets}\n\n"
            f"Horizon: {rec.get('time_horizon')}\n"
            f"Expected: {rec.get('expected_return')}\n"
            f"Remaining cash: ${rec.get('remaining_cash', '—')}"
            f"{sync_line}"
        )

    bullets = "\n".join(f"• {r}" for r in reasons) or "• (none)"
    return (
        "AI Recommendation\n\n"
        f"{rec.get('action')} {rec.get('ticker')} – Invest ${rec.get('investment')}\n"
        f"Confidence: {rec.get('confidence')}%\n"
        f"Risk: {rec.get('risk')}\n\n"
        f"Reasons:\n{bullets}\n\n"
        f"Horizon: {rec.get('time_horizon')}\n"
        f"Expected: {rec.get('expected_return')}\n"
        f"Remaining cash: ${rec.get('remaining_cash', '—')}"
        f"{sync_line}"
    )


def format_options_recommendation_message(
    rec: dict[str, Any],
    *,
    markdown: bool = False,
    recommendation_id: str | None = None,
) -> str:
    desk = _confirm_url(recommendation_id, tab="options")
    reasons = rec.get("reasoning") or []
    right = (rec.get("right") or "").upper()
    strike = rec.get("strike")
    expiry = rec.get("expiry")
    contracts = rec.get("contracts")
    premium = rec.get("premium")
    max_loss = rec.get("max_loss")
    contract_line = (
        f"{rec.get('ticker')} {right} ${strike} exp {expiry} × {contracts} @ ${premium}"
        if right and strike is not None
        else f"{rec.get('action')} {rec.get('ticker')}"
    )
    sync_line = f"\n\nAfter you trade, confirm on Options desk:\n{desk}"

    if markdown:
        bullets = "\n".join(f"• {r}" for r in reasons) or "• (none)"
        return (
            "🚨 *Options Recommendation*\n\n"
            f"*{rec.get('action')}* — {contract_line}\n"
            f"Premium at risk / proceeds: ${rec.get('investment')} · Max loss: ${max_loss}\n"
            f"Confidence: {rec.get('confidence')}% · Risk: {rec.get('risk')}\n\n"
            f"*Reasons:*\n{bullets}\n\n"
            f"Horizon: {rec.get('time_horizon')}\n"
            f"Expected: {rec.get('expected_return')}\n"
            f"Options cash left: ${rec.get('remaining_cash', '—')}"
            f"{sync_line}"
        )

    bullets = "\n".join(f"• {r}" for r in reasons) or "• (none)"
    return (
        "Options Recommendation\n\n"
        f"{rec.get('action')} — {contract_line}\n"
        f"Premium at risk / proceeds: ${rec.get('investment')} · Max loss: ${max_loss}\n"
        f"Confidence: {rec.get('confidence')}% · Risk: {rec.get('risk')}\n\n"
        f"Reasons:\n{bullets}\n\n"
        f"Horizon: {rec.get('time_horizon')}\n"
        f"Expected: {rec.get('expected_return')}\n"
        f"Options cash left: ${rec.get('remaining_cash', '—')}"
        f"{sync_line}"
    )


def send_ntfy(
    title: str,
    message: str,
    *,
    click_url: str | None = None,
    priority: str = "default",
    tags: str = "chart_with_upwards_trend,moneybag",
) -> dict[str, Any]:
    settings = get_settings()
    topic = (settings.ntfy_topic or "").strip()
    if not topic:
        raise RuntimeError("NTFY_TOPIC is not set")

    # ntfy header values must be latin-1; keep body UTF-8
    def _hdr(s: str) -> str:
        return (s or "").replace("\n", " ").encode("ascii", "replace").decode("ascii")

    base = settings.ntfy_server.rstrip("/")
    url = f"{base}/{topic}"
    headers = {
        "Title": _hdr(title)[:180],
        "Priority": priority,
        "Tags": tags,
    }
    if click_url:
        headers["Click"] = click_url
        headers["Actions"] = f"view, Confirm in Hisaab, {click_url}, clear=true"
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, content=message.encode("utf-8"), headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"ntfy failed HTTP {resp.status_code}: {resp.text[:300]}")
        return {"ok": True, "channel": "ntfy", "url": url, "status_code": resp.status_code}


def send_telegram(text: str) -> dict[str, Any]:
    settings = get_settings()
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, json=payload)
        data = resp.json()
        if resp.status_code >= 400 or not data.get("ok"):
            raise RuntimeError(f"Telegram send failed: {data}")
        return {"ok": True, "channel": "telegram", "response": data}


def notify_spend_cap(
    *,
    spend_month_usd: float,
    month_cap_usd: float,
    kind: str = "reached",
) -> dict[str, Any]:
    """Alert when monthly Gemini spend hits (or approaches) the hard cap."""
    settings = get_settings()
    if kind == "warning":
        title = f"Khabari spend warning (${spend_month_usd:.2f}/${month_cap_usd:.2f})"
        message = (
            f"Gemini estimated spend this month is ${spend_month_usd:.2f}.\n"
            f"Hard cap is ${month_cap_usd:.2f}.\n"
            f"Model: {settings.gemini_model}\n"
            "Analyzes will stop automatically at the cap."
        )
        tags = "warning,money_with_wings"
        priority = "high"
    else:
        title = f"Khabari $10 cap reached (${spend_month_usd:.2f})"
        message = (
            f"Monthly Gemini spend cap reached: ${spend_month_usd:.2f} / ${month_cap_usd:.2f}.\n"
            f"Model: {settings.gemini_model}\n"
            "All further analyzes are paused until next month "
            "(or until you raise MAX_MONTHLY_SPEND_USD)."
        )
        tags = "no_entry,money_with_wings"
        priority = "urgent"

    results: dict[str, Any] = {"sent": [], "errors": [], "kind": kind}
    if settings.ntfy_topic:
        try:
            results["sent"].append(
                send_ntfy(title, message, priority=priority, tags=tags)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ntfy spend-cap alert failed: %s", exc)
            results["errors"].append({"channel": "ntfy", "error": str(exc)})

    if settings.telegram_bot_token and settings.telegram_chat_id:
        try:
            results["sent"].append(send_telegram(f"*{title}*\n\n{message}"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram spend-cap alert failed: %s", exc)
            results["errors"].append({"channel": "telegram", "error": str(exc)})

    results["ok"] = bool(results["sent"])
    results["message"] = message
    return results


def notify_day_wrap(*, title: str, message: str) -> dict[str, Any]:
    """Send the end-of-day concluding wrap (suggestions + top news)."""
    settings = get_settings()
    results: dict[str, Any] = {"sent": [], "errors": []}
    desk = _confirm_url()

    if settings.ntfy_topic:
        try:
            results["sent"].append(
                send_ntfy(
                    title,
                    message,
                    click_url=desk,
                    priority="default",
                    tags="newspaper,chart_with_upwards_trend",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ntfy day-wrap failed: %s", exc)
            results["errors"].append({"channel": "ntfy", "error": str(exc)})

    if settings.telegram_bot_token and settings.telegram_chat_id:
        try:
            results["sent"].append(send_telegram(f"*{title}*\n\n{message}"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram day-wrap failed: %s", exc)
            results["errors"].append({"channel": "telegram", "error": str(exc)})

    results["ok"] = bool(results["sent"])
    results["message"] = message
    if not results["sent"] and not results["errors"]:
        results["errors"].append(
            {
                "channel": "none",
                "error": "No notifier configured (set NTFY_TOPIC or Telegram creds)",
            }
        )
    return results


def notify_recommendation(
    rec: dict[str, Any],
    *,
    recommendation_id: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    results: dict[str, Any] = {"sent": [], "errors": []}
    desk = _confirm_url(recommendation_id)

    plain = format_recommendation_message(
        rec, markdown=False, recommendation_id=recommendation_id
    )
    md = format_recommendation_message(rec, markdown=True, recommendation_id=recommendation_id)
    title = f"{rec.get('action')} {rec.get('ticker')} (${rec.get('investment')})"

    if settings.ntfy_topic:
        try:
            results["sent"].append(send_ntfy(title, plain, click_url=desk))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ntfy failed: %s", exc)
            results["errors"].append({"channel": "ntfy", "error": str(exc)})

    if settings.telegram_bot_token and settings.telegram_chat_id:
        try:
            results["sent"].append(send_telegram(md))
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram failed: %s", exc)
            results["errors"].append({"channel": "telegram", "error": str(exc)})

    results["ok"] = bool(results["sent"])
    results["message"] = plain
    results["desk_url"] = desk
    if not results["sent"] and not results["errors"]:
        results["errors"].append(
            {
                "channel": "none",
                "error": "No notifier configured (set NTFY_TOPIC or Telegram creds)",
            }
        )
    return results


def notify_options_recommendation(
    rec: dict[str, Any],
    *,
    recommendation_id: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    results: dict[str, Any] = {"sent": [], "errors": []}
    desk = _confirm_url(recommendation_id, tab="options")

    plain = format_options_recommendation_message(
        rec, markdown=False, recommendation_id=recommendation_id
    )
    md = format_options_recommendation_message(
        rec, markdown=True, recommendation_id=recommendation_id
    )
    right = (rec.get("right") or "").upper()
    action = str(rec.get("action") or "HOLD").upper()
    if action == "HOLD":
        title = f"OPT HOLD - no trade ({rec.get('ticker') or 'scan'})"
    else:
        title = (
            f"OPT {action} {rec.get('ticker')} {right} "
            f"${rec.get('strike')} x{rec.get('contracts')}"
        )

    if settings.ntfy_topic:
        try:
            results["sent"].append(
                send_ntfy(
                    title,
                    plain,
                    click_url=desk,
                    tags="chart_with_upwards_trend,zap",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ntfy options failed: %s", exc)
            results["errors"].append({"channel": "ntfy", "error": str(exc)})

    if settings.telegram_bot_token and settings.telegram_chat_id:
        try:
            results["sent"].append(send_telegram(md))
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram options failed: %s", exc)
            results["errors"].append({"channel": "telegram", "error": str(exc)})

    results["ok"] = bool(results["sent"])
    results["message"] = plain
    results["desk_url"] = desk
    if not results["sent"] and not results["errors"]:
        results["errors"].append(
            {
                "channel": "none",
                "error": "No notifier configured (set NTFY_TOPIC or Telegram creds)",
            }
        )
    return results
