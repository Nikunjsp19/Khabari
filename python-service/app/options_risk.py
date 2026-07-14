"""Risk rules for long call / long put paper book (separate cash)."""

from __future__ import annotations

from typing import Any

from app.options_data import CONTRACT_MULTIPLIER, position_key


def apply_options_risk_rules(
    recommendation: dict[str, Any],
    portfolio: dict[str, Any],
    *,
    max_premium_pct: float = 0.40,
    min_cash_pct: float = 0.05,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Enforce options book constraints.

    - LONG only: BUY_TO_OPEN / SELL_TO_CLOSE / HOLD
    - Cap premium at risk per trade vs NAV
    - Keep min cash after debit
    - Size contracts from available cash and confidence
    - Validate contract against candidate list when opening
    """
    rec = dict(recommendation)
    cash = float(portfolio.get("cash", 0))
    positions: dict[str, Any] = dict(portfolio.get("positions") or {})
    candidates = candidates or []
    cand_by_key = {str(c.get("key")): c for c in candidates if c.get("key")}

    # Mark positions at avg premium (conservative) for NAV
    holdings = 0.0
    for pos in positions.values():
        contracts = float(pos.get("contracts", 0) or 0)
        avg = float(pos.get("avg_premium", 0) or 0)
        holdings += contracts * avg * CONTRACT_MULTIPLIER
    total_value = cash + holdings
    max_premium_dollars = total_value * max_premium_pct
    max_cash_spend = cash * (1.0 - min_cash_pct)

    action = str(rec.get("action", "HOLD")).upper()
    # Normalize legacy/alternate labels
    if action in {"BUY", "BTO"}:
        action = "BUY_TO_OPEN"
    if action in {"SELL", "STC"}:
        action = "SELL_TO_CLOSE"

    ticker = str(rec.get("ticker", "")).upper()
    right = rec.get("right")
    if isinstance(right, str):
        right = right.lower().strip()
        if right in {"c", "calls"}:
            right = "call"
        elif right in {"p", "puts"}:
            right = "put"
    else:
        right = None

    conf = float(rec.get("confidence", 0) or 0)
    conf_factor = 0.55 + 0.45 * max(0.0, min(conf / 100.0, 1.0))
    notes: list[str] = []
    adjusted = False

    rec["time_horizon"] = "SHORT"

    if action not in {"BUY_TO_OPEN", "SELL_TO_CLOSE", "HOLD"}:
        notes.append(f"Unknown action '{action}'; defaulting to HOLD")
        action = "HOLD"
        adjusted = True

    premium = float(rec.get("premium") or 0)
    contracts = float(rec.get("contracts") or 0)
    strike = rec.get("strike")
    expiry = rec.get("expiry")
    contract_key = rec.get("contract_key") or rec.get("key")

    if action == "BUY_TO_OPEN":
        # Resolve candidate
        cand = None
        if contract_key and contract_key in cand_by_key:
            cand = cand_by_key[str(contract_key)]
        elif ticker and right and strike is not None and expiry:
            try:
                key = position_key(ticker, str(expiry), str(right), float(strike))
                cand = cand_by_key.get(key)
                contract_key = key
            except (TypeError, ValueError):
                cand = None

        if not cand:
            notes.append("BUY_TO_OPEN rejected — contract not in deep-scan candidates")
            action = "HOLD"
            contracts = 0
            premium = 0
            adjusted = True
        else:
            ticker = str(cand["underlying"]).upper()
            right = cand["right"]
            strike = float(cand["strike"])
            expiry = str(cand["expiry"])
            contract_key = cand["key"]
            premium = float(cand.get("mid") or premium or 0)
            if premium <= 0:
                notes.append("Invalid premium on candidate; HOLD")
                action = "HOLD"
                contracts = 0
                adjusted = True
            else:
                allowed_dollars = min(max_cash_spend, max_premium_dollars) * conf_factor
                cost_per = premium * CONTRACT_MULTIPLIER
                max_contracts = int(allowed_dollars // cost_per) if cost_per > 0 else 0
                # Small-book floor: allow 1 contract if cash reserve allows and confidence is decent
                if (
                    max_contracts < 1
                    and cost_per > 0
                    and cost_per <= max_cash_spend
                    and conf >= 60
                ):
                    max_contracts = 1
                    notes.append("Small-book floor: allowing 1 contract within cash reserve")
                    adjusted = True
                if contracts <= 0:
                    contracts = float(max(1, max_contracts)) if max_contracts >= 1 else 0
                if contracts > max_contracts:
                    notes.append(f"Capped contracts from {contracts} to {max_contracts}")
                    contracts = float(max_contracts)
                    adjusted = True
                if contracts < 1:
                    notes.append("Not enough options cash after risk caps; HOLD")
                    action = "HOLD"
                    contracts = 0
                    adjusted = True
                # Existing same-contract exposure
                if action == "BUY_TO_OPEN" and contract_key in positions:
                    existing = positions[contract_key]
                    existing_cost = (
                        float(existing.get("contracts", 0))
                        * float(existing.get("avg_premium", 0))
                        * CONTRACT_MULTIPLIER
                    )
                    room = max(0.0, max_premium_dollars - existing_cost)
                    room_contracts = int(room // cost_per) if cost_per > 0 else 0
                    if contracts > room_contracts:
                        notes.append(f"Position near max premium exposure; capped to {room_contracts}")
                        contracts = float(room_contracts)
                        adjusted = True
                        if contracts < 1:
                            action = "HOLD"
                            contracts = 0

    elif action == "SELL_TO_CLOSE":
        # Find matching open long
        key = contract_key
        if not key and ticker and right and strike is not None and expiry:
            key = position_key(ticker, str(expiry), str(right), float(strike))
        pos = positions.get(str(key)) if key else None
        if not pos:
            # fallback: any position on ticker
            for k, p in positions.items():
                if str(p.get("underlying", "")).upper() == ticker:
                    pos = p
                    key = k
                    break
        if not pos or float(pos.get("contracts", 0) or 0) <= 0:
            notes.append(f"No open long option for {ticker} to close; HOLD")
            action = "HOLD"
            contracts = 0
            adjusted = True
        else:
            owned = float(pos.get("contracts", 0))
            if contracts <= 0 or contracts > owned:
                contracts = owned
                notes.append(f"Adjusted SELL_TO_CLOSE to full position {owned} contracts")
                adjusted = True
            ticker = str(pos.get("underlying", ticker)).upper()
            right = pos.get("right") or right
            strike = pos.get("strike") if pos.get("strike") is not None else strike
            expiry = pos.get("expiry") or expiry
            premium = float(rec.get("premium") or pos.get("avg_premium") or premium or 0)
            contract_key = key

    investment = 0.0
    max_loss = 0.0
    if action == "BUY_TO_OPEN" and contracts >= 1 and premium > 0:
        investment = round(premium * CONTRACT_MULTIPLIER * contracts, 2)
        max_loss = investment  # long premium risk
    elif action == "SELL_TO_CLOSE" and contracts >= 1 and premium > 0:
        investment = round(premium * CONTRACT_MULTIPLIER * contracts, 2)  # expected proceeds
        max_loss = 0.0

    remaining_cash = cash
    if action == "BUY_TO_OPEN":
        remaining_cash = cash - investment
    elif action == "SELL_TO_CLOSE":
        remaining_cash = cash + investment

    if action == "HOLD":
        right = None
        strike = None
        expiry = None
        contracts = 0
        premium = 0
        contract_key = None
        investment = 0
        max_loss = 0

    rec.update(
        {
            "ticker": ticker or rec.get("ticker"),
            "action": action,
            "right": right,
            "strike": float(strike) if strike is not None else None,
            "expiry": expiry,
            "contracts": int(contracts) if contracts else 0,
            "premium": round(float(premium), 4) if premium else 0,
            "contract_key": contract_key,
            "investment": round(investment, 2),
            "max_loss": round(max_loss, 2),
            "risk_adjusted": adjusted,
            "remaining_cash": round(remaining_cash, 2),
            "risk_notes": notes,
            "asset_class": "options",
        }
    )
    return rec
