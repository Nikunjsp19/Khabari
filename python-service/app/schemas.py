"""Pydantic models for API request/response payloads."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class IndicatorsQuery(BaseModel):
    symbols: str = Field(..., description="Comma-separated tickers, e.g. TSLA,NVDA")
    period: str = "3mo"
    interval: str = "1h"


class Position(BaseModel):
    shares: float
    avg_cost: float


class PortfolioState(BaseModel):
    cash: float = 1000.0
    positions: dict[str, Position] = Field(default_factory=dict)


class Recommendation(BaseModel):
    ticker: str
    action: Literal["BUY", "SELL", "HOLD"]
    investment: float = 0
    confidence: float = Field(ge=0, le=100)
    risk: Literal["LOW", "MEDIUM", "HIGH"]
    time_horizon: Literal["SHORT"] = "SHORT"
    expected_return: str = ""
    reasoning: list[str] = Field(default_factory=list)


class RiskRequest(BaseModel):
    recommendation: dict[str, Any]
    portfolio: PortfolioState
    prices: dict[str, float] = Field(default_factory=dict)
    max_position_pct: float = 0.30
    min_cash_pct: float = 0.10


class PortfolioRowsRequest(BaseModel):
    rows: list[dict[str, Any]]
    cash: float = 1000.0


class AnalyzeRequest(BaseModel):
    symbols: list[str] | None = None
    portfolio: PortfolioState | None = None
    send_telegram: bool = True
    period: str | None = None
    interval: str | None = None
    force: bool = Field(
        False,
        description="If true, run even outside Mon–Fri 9am–4pm ET",
    )


class HealthResponse(BaseModel):
    status: str
    service: str = "khabari-python-api"
