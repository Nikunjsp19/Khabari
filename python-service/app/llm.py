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
from app.options_prompts import (
    OPTIONS_DECISION_SYSTEM,
    OPTIONS_NEWS_SYSTEM,
    OPTIONS_TECH_SYSTEM,
    options_decision_user_prompt,
    options_news_user_prompt,
    options_tech_user_prompt,
)

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Model sometimes wraps JSON in prose — take the outermost object/array
        start_obj, start_arr = text.find("{"), text.find("[")
        starts = [i for i in (start_obj, start_arr) if i >= 0]
        if not starts:
            raise
        start = min(starts)
        end_obj, end_arr = text.rfind("}"), text.rfind("]")
        end = max(end_obj, end_arr)
        if end <= start:
            raise
        return json.loads(text[start : end + 1])


def chat_json(system: str, user: str, *, temperature: float = 0.4) -> Any:
    settings = get_settings()
    provider = (settings.llm_provider or "gemini").lower()

    def _once(temp: float) -> str:
        if provider == "openai":
            return _chat_openai(system, user, temperature=temp)
        if settings.gemini_api_key:
            try:
                return _chat_gemini(system, user, temperature=temp)
            except LLMError as exc:
                if settings.openai_api_key and _is_transient_gemini(0, str(exc)):
                    logger.warning("All Gemini models failed; falling back to OpenAI in same run")
                    return _chat_openai(system, user, temperature=temp)
                raise
        if settings.openai_api_key:
            return _chat_openai(system, user, temperature=temp)
        raise LLMError("No LLM key set. Add GEMINI_API_KEY (or OPENAI_API_KEY).")

    last_content = ""
    for attempt, temp in enumerate((temperature, max(0.1, temperature - 0.2)), start=1):
        try:
            last_content = _once(temp)
            return _extract_json(last_content)
        except json.JSONDecodeError:
            if attempt == 1:
                logger.warning("LLM returned invalid JSON; retrying once")
                continue
            raise LLMError(f"Model did not return valid JSON: {last_content[:300]}")
        except LLMError:
            raise
    raise LLMError(f"Model did not return valid JSON: {last_content[:300]}")


def _ticker_batches(keys: list[str], size: int | None = None) -> list[list[str]]:
    settings = get_settings()
    n = max(1, int(size if size is not None else settings.llm_ticker_batch_size))
    return [keys[i : i + n] for i in range(0, len(keys), n)]


def _subset_dict(data: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {k: data[k] for k in keys if k in data}


def run_news_agent(headlines: dict[str, list[str]]) -> dict[str, Any]:
    keys = list(headlines.keys())
    if not keys:
        return {}
    merged: dict[str, Any] = {}
    for batch in _ticker_batches(keys):
        chunk = _subset_dict(headlines, batch)
        logger.info("News agent batch tickers=%s", batch)
        try:
            part = chat_json(NEWS_SYSTEM, news_user_prompt(chunk), temperature=0.3)
            if isinstance(part, dict):
                merged.update(part)
        except LLMError as exc:
            logger.warning("News agent batch failed %s (%s); using raw headlines", batch, exc)
            for t, h in chunk.items():
                merged[t] = h[:3] if isinstance(h, list) else [str(h)]
    return merged


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
    keys = list(slim.keys())
    if not keys:
        return {}
    merged: dict[str, Any] = {}
    for batch in _ticker_batches(keys):
        chunk = _subset_dict(slim, batch)
        logger.info("Technical agent batch tickers=%s", batch)
        try:
            part = chat_json(TECH_SYSTEM, tech_user_prompt(chunk), temperature=0.3)
            if isinstance(part, dict):
                merged.update(part)
        except LLMError as exc:
            logger.warning("Technical agent batch failed %s (%s)", batch, exc)
            for t, vals in chunk.items():
                merged[t] = f"Indicators only (LLM batch failed): RSI={vals.get('rsi')} MACD={vals.get('macd')}"
    return merged


def run_decision_agent(context: dict[str, Any]) -> dict[str, Any]:
    return chat_json(DECISION_SYSTEM, decision_user_prompt(context), temperature=0.5)


def run_options_news_agent(headlines: dict[str, list[str]]) -> dict[str, Any]:
    keys = list(headlines.keys())
    if not keys:
        return {}
    merged: dict[str, Any] = {}
    for batch in _ticker_batches(keys):
        chunk = _subset_dict(headlines, batch)
        logger.info("Options news agent batch tickers=%s", batch)
        try:
            part = chat_json(
                OPTIONS_NEWS_SYSTEM,
                options_news_user_prompt(chunk),
                temperature=0.3,
            )
            if isinstance(part, dict):
                merged.update(part)
        except LLMError as exc:
            logger.warning("Options news batch failed %s (%s); using raw headlines", batch, exc)
            for t, h in chunk.items():
                merged[t] = h[:3] if isinstance(h, list) else [str(h)]
    return merged


def run_options_technical_agent(payload: dict[str, Any]) -> dict[str, Any]:
    # Trim candidate lists per ticker for token cost
    slim_candidates: dict[str, list[dict[str, Any]]] = {}
    for ticker, rows in (payload.get("candidates_by_ticker") or {}).items():
        slim_candidates[ticker] = [
            {
                k: r.get(k)
                for k in (
                    "key",
                    "right",
                    "strike",
                    "expiry",
                    "dte",
                    "mid",
                    "delta",
                    "iv",
                    "open_interest",
                    "volume",
                    "spread_pct",
                    "scan_score",
                )
            }
            for r in (rows or [])[:4]
        ]

    indicators = payload.get("indicators") or {}
    keys = sorted(set(list(indicators.keys()) + list(slim_candidates.keys())))
    if not keys:
        return {}

    merged: dict[str, Any] = {}
    for batch in _ticker_batches(keys):
        body = {
            "indicators": _subset_dict(indicators, batch),
            "candidates_by_ticker": _subset_dict(slim_candidates, batch),
        }
        logger.info("Options technical agent batch tickers=%s", batch)
        try:
            part = chat_json(
                OPTIONS_TECH_SYSTEM,
                options_tech_user_prompt(body),
                temperature=0.3,
            )
            if isinstance(part, dict):
                merged.update(part)
        except LLMError as exc:
            logger.warning("Options technical batch failed %s (%s)", batch, exc)
            for t in batch:
                merged[t] = {
                    "spot_bias": "neutral",
                    "note": "LLM batch failed; use scan candidates only",
                    "preferred": "none",
                }
    return merged


def run_options_decision_agent(context: dict[str, Any]) -> dict[str, Any]:
    """
    Two-pass decision to keep each Gemini call small:
    1) Score underlyings in tiny batches
    2) Final BUY/HOLD pick using only the top shortlist + their candidates
    """
    candidates = list(context.get("candidates") or [])
    by_und: dict[str, list[dict[str, Any]]] = {}
    for c in candidates:
        und = str(c.get("underlying") or "").upper()
        if und:
            by_und.setdefault(und, []).append(c)

    news = context.get("news") or {}
    tech = context.get("technical") or {}
    prices = context.get("prices") or {}
    tickers = sorted(
        set(list(by_und.keys()) + list(news.keys()) + list(tech.keys()) + list(prices.keys()))
    )
    if not tickers:
        tickers = [str(context.get("ticker") or "SPY")]

    ranked_all: list[dict[str, Any]] = []
    for batch in _ticker_batches(tickers):
        mini_ctx = {
            "portfolio": context.get("portfolio"),
            "mandate": (
                "Rank ONLY these underlyings for short-term long call/put edge. "
                "Return JSON with ranked array only (no final trade yet)."
            ),
            "news": _subset_dict(news if isinstance(news, dict) else {}, batch),
            "technical": _subset_dict(tech if isinstance(tech, dict) else {}, batch),
            "candidates": [c for t in batch for c in by_und.get(t, [])][:8],
            "prices": _subset_dict(prices if isinstance(prices, dict) else {}, batch),
            "phase": "rank_only",
            "tickers": batch,
        }
        logger.info("Options decision rank batch tickers=%s", batch)
        try:
            part = chat_json(
                OPTIONS_DECISION_SYSTEM
                + "\nFor this call: fill ranked for the given tickers only. "
                "Set action=HOLD, investment=0. Do not invent contracts.",
                options_decision_user_prompt(mini_ctx),
                temperature=0.3,
            )
            rows = part.get("ranked") if isinstance(part, dict) else None
            if isinstance(rows, list):
                ranked_all.extend(rows)
        except LLMError as exc:
            logger.warning("Options rank batch failed %s (%s)", batch, exc)
            for t in batch:
                ranked_all.append(
                    {"ticker": t, "score": 40, "bias": "HOLD", "note": "rank batch failed"}
                )

    ranked_all.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
    # Shortlist top names for the final (small) decision call
    shortlist = []
    seen: set[str] = set()
    for row in ranked_all:
        t = str(row.get("ticker") or "").upper()
        if not t or t in seen:
            continue
        seen.add(t)
        shortlist.append(t)
        if len(shortlist) >= 3:
            break
    if not shortlist:
        shortlist = tickers[:3]

    final_ctx = {
        "portfolio": context.get("portfolio"),
        "mandate": context.get("mandate"),
        "trigger": context.get("trigger"),
        "news": _subset_dict(news if isinstance(news, dict) else {}, shortlist),
        "technical": _subset_dict(tech if isinstance(tech, dict) else {}, shortlist),
        "candidates": [c for t in shortlist for c in by_und.get(t, [])][:12],
        "prices": _subset_dict(prices if isinstance(prices, dict) else {}, shortlist),
        "pre_ranked": ranked_all[:12],
        "shortlist": shortlist,
        "phase": "final_pick",
    }
    logger.info("Options decision final shortlist=%s", shortlist)
    decision = chat_json(
        OPTIONS_DECISION_SYSTEM
        + "\nUse pre_ranked as prior scores. Pick the single best trade from candidates "
        "on the shortlist, or HOLD if none clears the bar.",
        options_decision_user_prompt(final_ctx),
        temperature=0.4,
    )
    if isinstance(decision, dict) and not decision.get("ranked"):
        decision["ranked"] = ranked_all[:20]
    return decision


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

    with httpx.Client(timeout=180.0) as client:
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
