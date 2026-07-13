"""LLM chat helpers — Gemini (default) with same-run model fallback on overload."""

from __future__ import annotations

import json
import logging
import re
import time
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


def _gemini_model_chain() -> list[str]:
    settings = get_settings()
    primary = (settings.gemini_model or "").strip()
    fallbacks = [
        m.strip()
        for m in (settings.gemini_fallback_models or "").split(",")
        if m.strip()
    ]
    chain: list[str] = []
    for model in [primary, *fallbacks]:
        if model and model not in chain:
            chain.append(model)
    return chain or ["gemini-3.5-flash"]


def _is_transient_gemini(status_code: int, err_text: str) -> bool:
    if status_code in {500, 502, 503, 504}:
        return True
    m = (err_text or "").lower()
    return any(
        token in m
        for token in (
            "high demand",
            "unavailable",
            "temporarily",
            "try again later",
            "overloaded",
        )
    )


def _chat_gemini_model(
    client: httpx.Client,
    *,
    model: str,
    system: str,
    user: str,
    temperature: float,
    api_key: str,
) -> tuple[dict[str, Any], str]:
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
        "x-goog-api-key": api_key,
    }
    resp = client.post(url, headers=headers, json=payload)
    if resp.status_code >= 400:
        raise LLMError(f"Gemini HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json(), model


def _chat_gemini(system: str, user: str, *, temperature: float) -> str:
    settings = get_settings()
    if not settings.gemini_api_key:
        raise LLMError("GEMINI_API_KEY is not set")

    from app.budget import can_call_llm, looks_like_quota_error, record_llm_call, trip_quota_pause

    ok, reason = can_call_llm()
    if not ok:
        raise LLMError(f"Free-tier budget blocked LLM call: {reason}")

    models = _gemini_model_chain()
    last_err = ""
    data: dict[str, Any] | None = None
    used_model = models[0]

    with httpx.Client(timeout=90.0) as client:
        for model_idx, model in enumerate(models):
            # One quick retry on the same model for transient 503, then switch models
            for attempt in range(1, 3):
                try:
                    data, used_model = _chat_gemini_model(
                        client,
                        model=model,
                        system=system,
                        user=user,
                        temperature=temperature,
                        api_key=settings.gemini_api_key,
                    )
                    if model_idx > 0 or attempt > 1:
                        logger.info("Gemini succeeded with model=%s attempt=%s", model, attempt)
                    break
                except LLMError as exc:
                    last_err = str(exc)
                    status_code = 0
                    if "HTTP " in last_err:
                        try:
                            status_code = int(last_err.split("HTTP ", 1)[1].split(":", 1)[0])
                        except (ValueError, IndexError):
                            status_code = 0

                    # Hard quota: pause and stop (don't burn fallbacks on true rate limits)
                    if status_code == 429 or (
                        looks_like_quota_error(last_err) and status_code not in {503, 500, 502, 504}
                    ):
                        trip_quota_pause(last_err)
                        raise

                    transient = _is_transient_gemini(status_code, last_err)
                    # 404 model gone → switch immediately
                    if status_code == 404:
                        logger.warning("Gemini model unavailable (%s); switching fallback", model)
                        break

                    if transient and attempt < 2:
                        wait = 3 * attempt
                        logger.warning(
                            "Gemini overloaded on %s (attempt %s); retry in %ss then fallback if needed",
                            model,
                            attempt,
                            wait,
                        )
                        time.sleep(wait)
                        continue

                    if transient or status_code in {500, 502, 503, 504, 404}:
                        next_model = models[model_idx + 1] if model_idx + 1 < len(models) else None
                        if next_model:
                            logger.warning(
                                "Gemini %s failed (%s); switching to fallback model %s in same run",
                                model,
                                status_code or "error",
                                next_model,
                            )
                            break
                    raise
            else:
                continue
            if data is not None:
                break

    if data is None:
        raise LLMError(last_err or "Gemini request failed on all models")

    usage = data.get("usageMetadata") or {}
    record_llm_call(
        input_tokens=int(usage.get("promptTokenCount") or 0),
        output_tokens=int(
            usage.get("candidatesTokenCount")
            or usage.get("outputTokenCount")
            or 0
        ),
        model=used_model,
    )

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
    usage = data.get("usage") or {}
    record_llm_call(
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        model=settings.openai_model,
    )
    return data["choices"][0]["message"]["content"]


def chat_json(system: str, user: str, *, temperature: float = 0.4) -> Any:
    settings = get_settings()
    provider = (settings.llm_provider or "gemini").lower()

    if provider == "openai":
        content = _chat_openai(system, user, temperature=temperature)
    else:
        if settings.gemini_api_key:
            try:
                content = _chat_gemini(system, user, temperature=temperature)
            except LLMError as exc:
                # Last resort in the same run: OpenAI if configured
                if settings.openai_api_key and _is_transient_gemini(0, str(exc)):
                    logger.warning("All Gemini models failed; falling back to OpenAI in same run")
                    content = _chat_openai(system, user, temperature=temperature)
                else:
                    raise
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
