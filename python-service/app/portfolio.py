"""Excel / CSV portfolio parsing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


REQUIRED_COLUMNS = {"Ticker", "Shares", "AvgCost"}


def parse_portfolio_dataframe(df: pd.DataFrame, cash: float = 1000.0) -> dict[str, Any]:
    """Map spreadsheet rows → portfolio JSON."""
    # Normalize column names
    df = df.rename(columns={c: str(c).strip() for c in df.columns})
    colmap = {c.lower(): c for c in df.columns}

    def col(name: str) -> str | None:
        return colmap.get(name.lower())

    ticker_col = col("Ticker")
    shares_col = col("Shares")
    avg_col = col("AvgCost") or col("Avg Cost") or col("AverageCost")
    cash_col = col("Cash")

    if not ticker_col or not shares_col or not avg_col:
        missing = REQUIRED_COLUMNS - {ticker_col, shares_col, avg_col}
        raise ValueError(f"Portfolio missing required columns. Need Ticker, Shares, AvgCost. Missing: {missing}")

    positions: dict[str, dict[str, float]] = {}
    for _, row in df.iterrows():
        ticker = str(row[ticker_col]).strip().upper()
        if not ticker or ticker == "NAN":
            continue
        positions[ticker] = {
            "shares": float(row[shares_col]),
            "avg_cost": float(row[avg_col]),
        }

    # Cash may be a dedicated column (same value on every row) or passed in
    resolved_cash = cash
    if cash_col is not None:
        cash_values = df[cash_col].dropna()
        if not cash_values.empty:
            resolved_cash = float(cash_values.iloc[0])

    return {"cash": float(resolved_cash), "positions": positions}


def read_portfolio_file(path: str | Path, cash: float = 1000.0) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Portfolio file not found: {path}")

    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        df = pd.read_excel(path)
    elif suffix == ".csv":
        df = pd.read_csv(path)
    else:
        raise ValueError(f"Unsupported portfolio format: {suffix}")

    return parse_portfolio_dataframe(df, cash=cash)


def rows_to_portfolio(rows: list[dict[str, Any]], cash: float = 1000.0) -> dict[str, Any]:
    """Convert n8n Spreadsheet File output rows into portfolio JSON."""
    df = pd.DataFrame(rows)
    return parse_portfolio_dataframe(df, cash=cash)
