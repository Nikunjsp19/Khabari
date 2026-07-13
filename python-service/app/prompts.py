"""LLM system/user prompt templates for the three AI agents."""

from __future__ import annotations

import json
from typing import Any

# ---------------------------------------------------------------------------
# News Agent
# ---------------------------------------------------------------------------

NEWS_SYSTEM = """You are a financial news analyst for a SHORT-TERM (intraday to a few days) trading assistant.
Summarize recent news for each ticker with emphasis on near-term price catalysts.
Prioritize: earnings, guidance, upgrades/downgrades, product launches, lawsuits, macro shocks,
unusual volume mentions, and anything likely to move the stock in hours or days — not long-term thesis.
Respond with ONLY valid JSON (no markdown fences) shaped as:
{"TICKER": ["short bullet (Sentiment)", ...], ...}
Keep each bullet under 20 words. Use Sentiment labels: Bullish, Bearish, or Neutral.
"""


def news_user_prompt(news_by_ticker: dict[str, list[str]]) -> str:
    lines: list[str] = []
    for ticker, headlines in news_by_ticker.items():
        lines.append(f"{ticker}:")
        for h in headlines:
            lines.append(f"  - {h}")
    return (
        "Summarize for SHORT-TERM trading impact (hours to a few days). "
        "Ignore long-term strategic fluff unless it can move price soon.\n\n"
        + "\n".join(lines)
    )


# ---------------------------------------------------------------------------
# Technical Agent
# ---------------------------------------------------------------------------

TECH_SYSTEM = """You are a stock technical analyst for SHORT-TERM (intraday to a few days) trades.
Given the latest indicators, interpret near-term momentum and entry/exit risk for each ticker.
Focus on RSI, MACD crossover, price vs EMA20/EMA50, and ATR volatility — not multi-month trends.
Respond with ONLY valid JSON (no markdown fences):
{"TICKER": "one short sentence", ...}
Be concise.
"""


def tech_user_prompt(indicators: dict[str, Any]) -> str:
    return (
        "Interpret these indicators for SHORT-TERM trades (hours to a few days). Return JSON only.\n\n"
        + json.dumps(indicators, indent=2, default=str)
    )


# ---------------------------------------------------------------------------
# Decision Agent
# ---------------------------------------------------------------------------

DECISION_SYSTEM = """You are an AI SHORT-TERM trading advisor for a small retail paper portfolio.
Goal: capture quick gains over hours to a few days — NOT long-term investing.

Scan ALL tickers in the context. Rank them, then pick the SINGLE best short-term opportunity,
or HOLD if none is compelling.

Rules:
- Output ONLY a single JSON object (no markdown, no commentary).
- First fill "ranked": score EVERY ticker 0–100 for near-term edge (include held names).
- Then set action/ticker from the top ranked idea — or HOLD if the best score is below 60.
- action must be one of: BUY, SELL, HOLD
- investment is dollars (number), not shares
- confidence is 0–100 and should track the winning ranked score
- risk is LOW, MEDIUM, or HIGH
- time_horizon must ALWAYS be "SHORT"
- expected_return is a short string like "2-4%" (near-term move, not annual)
- reasoning is an array of 2–5 short bullet strings focused on why THIS trade works soon
- Prefer diversification; do not ignore cash constraints
- BUY only when news + technicals align for a clear near-term edge
- If signals conflict, are mixed, or nothing looks exciting: action=HOLD, investment=0, confidence<=40
- SELL open positions when short-term momentum breaks, news turns bearish, or P&L hits exit logic
- Never recommend a trade for long-term fundamentals alone
- Never chase a stock that already spiked hard on the same headline unless pullback risk is low

Schema:
{
  "ranked": [{"ticker": "string", "score": 0, "bias": "BUY|SELL|HOLD", "note": "string"}, ...],
  "ticker": "string",
  "action": "BUY|SELL|HOLD",
  "investment": 0,
  "confidence": 0,
  "risk": "LOW|MEDIUM|HIGH",
  "time_horizon": "SHORT",
  "expected_return": "string",
  "reasoning": ["string", ...]
}
"""


def decision_user_prompt(context: dict[str, Any]) -> str:
    return (
        "SHORT-TERM mandate only (hours to a few days). "
        "Rank ALL tickers, then recommend the best near-term trade, "
        "or HOLD with $0 if nothing scores well.\n\n"
        + json.dumps(context, indent=2, default=str)
    )


# Example fixtures used in docs / tests
EXAMPLE_NEWS_INPUT = {
    "TSLA": [
        'Tesla unveils $25k EV model (Source: NasdaqNews)',
        "Minor recall announced (Source: Electrek)",
    ],
    "NVDA": [
        "Nvidia beats quarterly estimates (Reuters)",
        "AI chip demand grows (CNBC)",
    ],
}

EXAMPLE_NEWS_OUTPUT = {
    "TSLA": ["New cheap EV catalyst today (Bullish)", "Small recall — limited near-term hit (Neutral)"],
    "NVDA": ["Earnings beat — likely near-term pop (Bullish)", "AI demand supports momentum (Bullish)"],
}

EXAMPLE_TECH_INPUT = {
    "TSLA": {"rsi": 62.3, "macd": 1.2, "macd_signal": 0.8, "ema20": 850.4, "price": 858.9},
    "NVDA": {"rsi": 55.1, "macd": 0.5, "macd_signal": 0.4, "ema20": 183.7, "price": 187.2},
}

EXAMPLE_TECH_OUTPUT = {
    "TSLA": "Near-term: RSI 62, MACD above signal, price above EMA20 — short-term bullish momentum.",
    "NVDA": "Near-term: RSI 55, MACD slightly positive, above EMA20 — mild short-term uptrend.",
}

EXAMPLE_DECISION_INPUT = {
    "portfolio": {"cash": 1000, "positions": {"TSLA": {"shares": 1.5, "avg_cost": 820}}},
    "news": {
        "TSLA": ["Launched new EV model (Bullish)"],
        "NVDA": ["AI chip demand strong (Bullish)"],
    },
    "technical": {
        "TSLA": "Uptrend (RSI 62, MACD positive)",
        "NVDA": "Uptrend (RSI 55, above EMA20)",
    },
    "market": {"SPX": "+0.5%", "VIX": "17"},
}

EXAMPLE_DECISION_OUTPUT = {
    "ranked": [
        {"ticker": "NVDA", "score": 82, "bias": "BUY", "note": "Earnings + momentum"},
        {"ticker": "TSLA", "score": 55, "bias": "HOLD", "note": "Mixed recall noise"},
    ],
    "ticker": "NVDA",
    "action": "BUY",
    "investment": 250,
    "confidence": 85,
    "risk": "MEDIUM",
    "time_horizon": "SHORT",
    "expected_return": "2-4%",
    "reasoning": [
        "Fresh earnings beat is a near-term catalyst",
        "Short-term technicals bullish (RSI/MACD/EMA20)",
        "Highest ranked setup among scanned tickers",
    ],
}
