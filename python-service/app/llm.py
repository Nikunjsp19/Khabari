"""LLM chat helpers — Gemini (default) with optional OpenAI fallback."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.config import get_settings
from app.prompts import (
    DECISION_SYSTEM,
    NEWS_SYSTEM,
    TECH_SYSTEM,
    decision_user_prompt,
    news_user_prompt,
    tech_user_prompt,
)

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _chat_gemini(system: str, user: str, *, temperature: float) -> str:
    settings = get_settings()
    if not settings.gemini_api_key:
        raise LLMError("GEMINI_API_KEY is not set")

    import time

    from app.budget import can_call_llm, looks_like_quota_error, record_llm_call, trip_quota_pause

    ok, reason = can_call_llm()
    if not ok:
        raise LLMError(f"Free-tier budget blocked LLM call: {reason}")

    model = settings.gemini_model
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
        },
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": settings.gemini_api_key,
    }

    last_err = ""
    with httpx.Client(timeout=90.0) as client:
        for attempt in range(1, 4):
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code < 400:
                data = resp.json()
                break
            last_err = f"Gemini HTTP {resp.status_code}: {resp.text[:500]}"
            # 503 = temporary overload; retry. 429 = real quota — pause.
            if resp.status_code == 429 or (
                looks_like_quota_error(last_err) and resp.status_code != 503
            ):
                trip_quota_pause(last_err)
                raise LLMError(last_err)
            if resp.status_code in {500, 502, 503, 504} and attempt < 3:
                wait = 8 * attempt
                logger.warning("Gemini %s on attempt %s; retry in %ss", resp.status_code, attempt, wait)
                time.sleep(wait)
                continue
            raise LLMError(last_err)
        else:
            raise LLMError(last_err or "Gemini request failed")

    record_llm_call()

    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Unexpected Gemini response: {str(data)[:400]}") from exc


def _chat_openai(system: str, user: str, *, temperature: float) -> str:
    settings = get_settings()
    if not settings.openai_api_key:
        raise LLMError("OPENAI_API_KEY is not set")

    from app.budget import can_call_llm, looks_like_quota_error, record_llm_call, trip_quota_pause

    ok, reason = can_call_llm()
    if not ok:
        raise LLMError(f"Free-tier budget blocked LLM call: {reason}")

    payload = {
        "model": settings.openai_model,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=90.0) as client:
        resp = client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
        if resp.status_code >= 400:
            err = f"OpenAI HTTP {resp.status_code}: {resp.text[:500]}"
            if resp.status_code == 429 or looks_like_quota_error(err):
                trip_quota_pause(err)
            raise LLMError(err)
        data = resp.json()
    record_llm_call()
    return data["choices"][0]["message"]["content"]


def chat_json(system: str, user: str, *, temperature: float = 0.4) -> Any:
    settings = get_settings()
    provider = (settings.llm_provider or "gemini").lower()

    if provider == "openai":
        content = _chat_openai(system, user, temperature=temperature)
    else:
        # Default Gemini; fall back to OpenAI only if Gemini key missing but OpenAI present
        if settings.gemini_api_key:
            content = _chat_gemini(system, user, temperature=temperature)
        elif settings.openai_api_key:
            content = _chat_openai(system, user, temperature=temperature)
        else:
            raise LLMError("No LLM key set. Add GEMINI_API_KEY (or OPENAI_API_KEY).")

    try:
        return _extract_json(content)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Model did not return valid JSON: {content[:300]}") from exc


def run_news_agent(headlines: dict[str, list[str]]) -> dict[str, Any]:
    return chat_json(NEWS_SYSTEM, news_user_prompt(headlines), temperature=0.3)


def run_technical_agent(indicators: dict[str, Any]) -> dict[str, Any]:
    slim = {
        t: {
            k: v
            for k, v in vals.items()
            if k
            in {
                "price",
                "rsi",
                "macd",
                "macd_signal",
                "ema20",
                "ema50",
                "ema200",
                "bb_upper",
                "bb_lower",
                "atr",
                "adx",
            }
        }
        for t, vals in indicators.items()
    }
    return chat_json(TECH_SYSTEM, tech_user_prompt(slim), temperature=0.3)


def run_decision_agent(context: dict[str, Any]) -> dict[str, Any]:
    return chat_json(DECISION_SYSTEM, decision_user_prompt(context), temperature=0.5)
