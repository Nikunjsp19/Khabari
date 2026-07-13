"""Fetch recent news without paid API keys (yfinance / Yahoo)."""

from __future__ import annotations

import logging
from typing import Any

import yfinance as yf

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


def fetch_news_batch(symbols: list[str], limit_per_symbol: int = 5) -> dict[str, list[dict[str, Any]]]:
    return {s.upper(): fetch_news_for_symbol(s, limit=limit_per_symbol) for s in symbols}


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
