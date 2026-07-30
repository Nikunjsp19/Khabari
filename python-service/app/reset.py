"""Fresh-start reset for the paper books.

Clearing positions by hand is easy to get half-right: stale pending
recommendations stay confirmable, the exit engine keeps high-water marks for
tickers you no longer own, and the tilt engine still thinks it rebalanced this
month. This resets all of that together.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.db import get_db, save_options_portfolio, save_portfolio

logger = logging.getLogger(__name__)

_EXIT_STATE_ID = "position_state"
_TILT_STATE_ID = "state"


def reset_book(
    *,
    cash: float | None = None,
    options_cash: float | None = None,
    clear_history: bool = False,
) -> dict[str, Any]:
    """
    Reset both paper books to zero positions.

    ``cash`` / ``options_cash`` default to the configured starting balances.
    ``clear_history`` also wipes the trade ledgers and past recommendations.
    """
    settings = get_settings()
    db = get_db()
    now = datetime.now(timezone.utc)

    stock_cash = float(settings.initial_cash if cash is None else cash)
    opt_cash = float(settings.options_initial_cash if options_cash is None else options_cash)
    if stock_cash < 0 or opt_cash < 0:
        raise ValueError("cash amounts cannot be negative")

    summary: dict[str, Any] = {}

    # Stale pendings are confirmable against a book that no longer holds the
    # shares, and a pending SELL also mutes the next tilt alert for that name.
    cancelled = {"status": "skipped", "skip_reason": "book_reset", "resolved_at": now}
    summary["pending_stock_cancelled"] = db.recommendations.update_many(
        {"status": "pending"}, {"$set": cancelled}
    ).modified_count
    summary["pending_options_cancelled"] = db.options_recommendations.update_many(
        {"status": "pending"}, {"$set": cancelled}
    ).modified_count

    if clear_history:
        summary["trades_deleted"] = db.trades.delete_many({}).deleted_count
        summary["options_trades_deleted"] = db.options_trades.delete_many({}).deleted_count
        summary["recommendations_deleted"] = db.recommendations.delete_many({}).deleted_count
        summary["options_recommendations_deleted"] = (
            db.options_recommendations.delete_many({}).deleted_count
        )
        # Portfolio snapshots are append-only; drop history before writing the
        # fresh one so the reset snapshot is the only thing left.
        summary["portfolio_snapshots_deleted"] = db.portfolio.delete_many({}).deleted_count
        summary["options_portfolio_snapshots_deleted"] = (
            db.options_portfolio.delete_many({}).deleted_count
        )

    save_portfolio(stock_cash, {}, source="reset")
    save_options_portfolio(opt_cash, {}, source="reset")

    # Exit-engine high-water marks only self-prune when the engine runs, which
    # it does not while stocks are paused.
    summary["exit_state_cleared"] = bool(
        db.meta.delete_one({"_id": _EXIT_STATE_ID}).deleted_count
    )
    # Without this the tilt engine skips its full rebalance until next month,
    # so a re-enabled empty book would never get rebuilt.
    summary["tilt_state_cleared"] = bool(
        db.tilt_state.delete_one({"_id": _TILT_STATE_ID}).deleted_count
    )

    summary["cash"] = stock_cash
    summary["options_cash"] = opt_cash
    summary["cleared_history"] = clear_history
    summary["reset_at"] = now.isoformat()
    summary["ok"] = True

    logger.info(
        "Book reset: cash=%s options_cash=%s history=%s pendings=%s/%s",
        stock_cash,
        opt_cash,
        clear_history,
        summary["pending_stock_cancelled"],
        summary["pending_options_cancelled"],
    )
    return summary
