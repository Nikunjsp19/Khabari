"""Portfolio risk rules applied after the Decision Agent output."""

from __future__ import annotations

from typing import Any


def apply_risk_rules(
    recommendation: dict[str, Any],
    portfolio: dict[str, Any],
    prices: dict[str, float] | None = None,
    *,
    max_position_pct: float = 0.40,
    min_cash_pct: float = 0.05,
) -> dict[str, Any]:
    """
    Enforce portfolio constraints on an AI recommendation.

    Rules:
    - Force time_horizon to SHORT (short-term mandate)
    - Max single-position exposure: max_position_pct of total portfolio value
    - Keep at least min_cash_pct of cash after BUY
    - Size scales with confidence, with a moderate floor so mid-confidence
      trades still get meaningful size (not half-sized into irrelevance)
    - SELL with zero shares → HOLD
    - Tiny BUY amounts (< $1) → HOLD
    """
    prices = prices or {}
    rec = dict(recommendation)

    cash = float(portfolio.get("cash", 0))
    positions: dict[str, Any] = portfolio.get("positions") or {}

    # Mark-to-market portfolio value
    holdings_value = 0.0
    for ticker, pos in positions.items():
        shares = float(pos.get("shares", 0))
        px = float(prices.get(ticker, pos.get("avg_cost", 0)))
        holdings_value += shares * px

    total_value = cash + holdings_value
    max_per_trade = total_value * max_position_pct
    max_cash_spend = cash * (1.0 - min_cash_pct)

    action = str(rec.get("action", "HOLD")).upper()
    ticker = str(rec.get("ticker", "")).upper()
    amt = float(rec.get("investment", 0) or 0)
    conf = float(rec.get("confidence", 0) or 0)
    # Moderate aggression: 50 conf → ~0.775 size factor, 100 → 1.0
    conf_factor = 0.55 + 0.45 * max(0.0, min(conf / 100.0, 1.0))

    adjusted = False
    notes: list[str] = []

    # Khabari is short-term only
    if str(rec.get("time_horizon", "SHORT")).upper() != "SHORT":
        notes.append(f"Forced time_horizon from {rec.get('time_horizon')} to SHORT")
        adjusted = True
    rec["time_horizon"] = "SHORT"

    if action == "BUY":
        allowed = min(max_cash_spend, max_per_trade) * conf_factor
        if amt > allowed:
            notes.append(f"Capped investment from ${amt:.2f} to ${allowed:.2f}")
            amt = allowed
            adjusted = True
        if amt < 1:
            notes.append("BUY amount too small after risk caps; converted to HOLD")
            action = "HOLD"
            amt = 0
            adjusted = True
        # Existing position exposure check
        if ticker in positions and total_value > 0:
            existing = float(positions[ticker].get("shares", 0)) * float(
                prices.get(ticker, positions[ticker].get("avg_cost", 0))
            )
            room = max(0.0, max_per_trade - existing)
            if amt > room:
                notes.append(f"Position already near max exposure; capped to ${room:.2f}")
                amt = room
                adjusted = True
                if amt < 1:
                    action = "HOLD"
                    amt = 0

    elif action == "SELL":
        shares = float(positions.get(ticker, {}).get("shares", 0))
        if shares <= 0:
            notes.append(f"No shares of {ticker} to sell; converted to HOLD")
            action = "HOLD"
            amt = 0
            adjusted = True
        else:
            px = float(prices.get(ticker, positions[ticker].get("avg_cost", 0)))
            max_sell = shares * px
            if amt <= 0 or amt > max_sell:
                notes.append(f"Adjusted SELL amount to full position ${max_sell:.2f}")
                amt = max_sell
                adjusted = True

    elif action != "HOLD":
        notes.append(f"Unknown action '{action}'; defaulting to HOLD")
        action = "HOLD"
        amt = 0
        adjusted = True

    remaining_cash = cash
    if action == "BUY":
        remaining_cash = cash - amt
    elif action == "SELL":
        remaining_cash = cash + amt

    rec["ticker"] = ticker or rec.get("ticker")
    rec["action"] = action
    rec["investment"] = round(amt, 2)
    rec["risk_adjusted"] = adjusted
    rec["remaining_cash"] = round(remaining_cash, 2)
    rec["risk_notes"] = notes
    return rec
