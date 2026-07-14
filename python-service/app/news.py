"""Fetch recent news — Yahoo/yfinance always; Marketaux when token is set."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import yfinance as yf

from app.config import get_settings

logger = logging.getLogger(__name__)


def fetch_news_for_symbol(symbol: str, limit: int = 5) -> list[dict[str, Any]]:
    """Return recent headlines for a ticker via yfinance."""
    symbol = symbol.upper().strip()
    items: list[dict[str, Any]] = []
    try:
        ticker = yf.Ticker(symbol)
        raw = ticker.news or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("News fetch failed for %s: %s", symbol, exc)
        return items

    for entry in raw[:limit]:
        # yfinance news shape varies by version
        content = entry.get("content") if isinstance(entry.get("content"), dict) else entry
        title = (
            content.get("title")
            or entry.get("title")
            or content.get("summary")
            or ""
        )
        if not title:
            continue
        publisher = (
            (content.get("provider") or {}).get("displayName")
            if isinstance(content.get("provider"), dict)
            else entry.get("publisher")
        ) or content.get("publisher") or "yahoo"
        link = ""
        click = content.get("clickThroughUrl") or content.get("canonicalUrl") or {}
        if isinstance(click, dict):
            link = click.get("url") or ""
        link = link or entry.get("link") or ""
        published = (
            content.get("pubDate")
            or content.get("displayTime")
            or entry.get("providerPublishTime")
        )
        items.append(
            {
                "uuid": str(entry.get("id") or entry.get("uuid") or f"{symbol}-{title[:40]}"),
                "title": title.strip(),
                "snippet": (content.get("summary") or title)[:400],
                "url": link,
                "published": str(published) if published else None,
                "source": publisher,
                "tickers": [symbol],
                "sentiment": None,
            }
        )
    return items


def _fetch_marketaux_for_symbol(symbol: str, limit: int = 5) -> list[dict[str, Any]]:
    settings = get_settings()
    token = (settings.marketaux_api_token or "").strip()
    if not token:
        return []
    symbol = symbol.upper().strip()
    url = "https://api.marketaux.com/v1/news/all"
    params = {
        "api_token": token,
        "symbols": symbol,
        "filter_entities": "true",
        "language": "en",
        "limit": min(limit, 10),
    }
    try:
        with httpx.Client(timeout=25.0) as client:
            resp = client.get(url, params=params)
            if resp.status_code >= 400:
                logger.warning("Marketaux HTTP %s for %s: %s", resp.status_code, symbol, resp.text[:200])
                return []
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Marketaux fetch failed for %s: %s", symbol, exc)
        return []

    items: list[dict[str, Any]] = []
    for article in data.get("data") or []:
        title = (article.get("title") or "").strip()
        if not title:
            continue
        items.append(
            {
                "uuid": str(article.get("uuid") or f"marketaux-{symbol}-{title[:40]}"),
                "title": title,
                "snippet": (article.get("description") or title)[:400],
                "url": article.get("url") or "",
                "published": article.get("published_at"),
                "source": (article.get("source") or "marketaux"),
                "tickers": [symbol],
                "sentiment": None,
            }
        )
    return items


def _merge_articles(*groups: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for group in groups:
        for a in group:
            key = (a.get("title") or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(a)
            if len(out) >= limit:
                return out
    return out


def fetch_news_batch(symbols: list[str], limit_per_symbol: int = 5) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for s in symbols:
        ticker = s.upper()
        yahoo = fetch_news_for_symbol(ticker, limit=limit_per_symbol)
        marketaux = _fetch_marketaux_for_symbol(ticker, limit=limit_per_symbol)
        out[ticker] = _merge_articles(marketaux, yahoo, limit=max(limit_per_symbol, 6))
    return out


def headlines_by_ticker(news_batch: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    """Flatten to prompt-friendly headline lists."""
    out: dict[str, list[str]] = {}
    for ticker, articles in news_batch.items():
        lines: list[str] = []
        for a in articles:
            src = a.get("source") or "unknown"
            lines.append(f'{a["title"]} (Source: {src})')
        if not lines:
            lines = ["No recent news found"]
        out[ticker] = lines
    return out
