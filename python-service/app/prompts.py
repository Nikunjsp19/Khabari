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
Given the latest indicators, interpret near-term momentum and whether a trade setup is actionable soon.
Focus on RSI, MACD crossover, price vs EMA20/EMA50, and ATR volatility — not multi-month trends.
Call out bullish continuation, pullback entries, and breakdown risk clearly.
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
Style: MODERATELY AGGRESSIVE growth — hunt momentum and near-term catalysts.
Goal: capture quick gains over hours to a few days — NOT long-term investing.
Bias: prefer taking a reasonable trade over sitting in cash when an edge exists.

Scan ALL tickers in the context. Rank them, then pick the SINGLE best short-term opportunity,
or HOLD only if nothing clears the bar.

Rules:
- Output ONLY a single JSON object (no markdown, no commentary).
- First fill "ranked": score EVERY ticker 0–100 for near-term edge (include held names).
- Then set action/ticker from the top ranked idea — BUY/SELL if best score >= 52; HOLD only if best < 52.
- action must be one of: BUY, SELL, HOLD
- investment is dollars (number), not shares
- For BUY with score 52–69: size ~15–25% of available cash
- For BUY with score 70+: size ~25–40% of available cash (still respect cash constraints)
- confidence is 0–100 and should track the winning ranked score (do not sandbag confidence)
- risk is LOW, MEDIUM, or HIGH
- time_horizon must ALWAYS be "SHORT"
- expected_return is a short string like "2-4%" (near-term move, not annual)
- reasoning is an array of 2–5 short bullet strings focused on why THIS trade works soon
- Prefer growth/momentum names with volume, MACD/EMA turn, RSI recovery, or fresh catalysts
- BUY when news OR technicals give a clear near-term edge (both preferred, one strong signal is enough)
- HOLD only when the top idea is weak/noisy or risks are clearly one-sided against you — not just because signals are imperfect
- SELL open positions when short-term momentum breaks, news turns bearish, or P&L hits exit logic
- Never recommend a trade for long-term fundamentals alone
- Avoid chasing parabolic same-day spikes; mild post-catalyst continuation is OK if momentum still supports it

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
        "SHORT-TERM + moderately aggressive growth mandate (hours to a few days). "
        "Rank ALL tickers, then take the best near-term trade when score >= 52. "
        "HOLD with $0 only if nothing clears that bar.\n\n"
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
