"""FastAPI entrypoint for Khabari stock analyst helpers."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.db import (
    get_active_options_watchlist,
    get_active_watchlist,
    get_latest_options_portfolio,
    get_latest_options_recommendation,
    get_latest_portfolio,
    get_latest_recommendation,
    health_status,
    init_db,
    save_options_portfolio,
    save_portfolio,
    serialize_mongo,
    set_options_watchlist,
    set_watchlist,
)
from app.desk import DESK_HTML
from app.indicators import compute_daily_context_batch, compute_indicators, compute_indicators_batch
from app.llm import LLMError
from app.market_hours import is_market_hours, market_hours_status
from app.options_pipeline import run_options_analysis
from app.pipeline import run_analysis
from app.portfolio import read_portfolio_file, rows_to_portfolio
from app.prompts import (
    DECISION_SYSTEM,
    EXAMPLE_DECISION_INPUT,
    EXAMPLE_DECISION_OUTPUT,
    EXAMPLE_NEWS_INPUT,
    EXAMPLE_NEWS_OUTPUT,
    EXAMPLE_TECH_INPUT,
    EXAMPLE_TECH_OUTPUT,
    NEWS_SYSTEM,
    TECH_SYSTEM,
    decision_user_prompt,
    news_user_prompt,
    tech_user_prompt,
)
from app.risk import apply_risk_rules
from app.scheduler import scheduler_status, start_scheduler, stop_scheduler
from app.schemas import (
    AnalyzeRequest,
    HealthResponse,
    OptionsAnalyzeRequest,
    OptionsPortfolioState,
    PortfolioRowsRequest,
    PortfolioState,
    RiskRequest,
)
from app.trades import (
    execute_recommendation,
    get_pending_recommendation,
    get_recommendation,
    portfolio_with_marks,
    skip_recommendation,
    update_recommendation_trade,
)
from app.options_trades import (
    execute_options_recommendation,
    get_options_recommendation,
    get_pending_options_recommendation,
    options_portfolio_with_marks,
    skip_options_recommendation,
    update_options_recommendation_trade,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("khabari")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        init_db()
        logger.info("MongoDB ready: %s", health_status())
    except Exception as exc:  # noqa: BLE001
        logger.error("MongoDB init failed: %s", exc)
    try:
        start_scheduler()
    except Exception as exc:  # noqa: BLE001
        logger.error("Scheduler failed to start: %s", exc)
    yield
    stop_scheduler()


app = FastAPI(
    title="Khabari Stock Analyst API",
    description="Hourly AI stock + options analyst — Mon–Fri 9am–4pm ET.",
    version="0.4.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    mongo = health_status()
    if not mongo.get("ok"):
        raise HTTPException(status_code=503, detail={"status": "degraded", "mongo": mongo})
    return HealthResponse(status="ok")


@app.get("/")
def root() -> dict[str, Any]:
    settings = get_settings()
    return {
        "service": "khabari-python-api",
        "storage": "mongodb",
        "watchlist": get_active_watchlist(),
        "market_hours": market_hours_status(),
        "schedule": scheduler_status(),
        "endpoints": [
            "GET /desk",
            "GET /watchlist",
            "PUT /watchlist",
            "GET /health",
            "GET /schedule",
            "POST /day-wrap",
            "GET /indicators?symbols=TSLA,NVDA",
            "GET /regime",
            "GET /signals",
            "GET /backtest",
            "GET /tilt/plan",
            "POST /tilt/rebalance",
            "GET /exits/check",
            "POST /exits/run",
            "POST /analyze",
            "GET /portfolio",
            "GET /portfolio/marked",
            "POST /trades/{id}/execute",
            "POST /trades/{id}/skip",
            "GET /recommendations/latest",
            "GET /recommendations/pending",
            "POST /risk/apply",
            "GET /prompts",
            "POST /options/analyze",
            "POST /options/movers/refresh",
            "GET /options/watchlist",
            "PUT /options/watchlist",
            "GET /options/portfolio",
            "GET /options/portfolio/marked",
            "GET /options/recommendations/pending",
            "POST /options/trades/{id}/execute",
        ],
        "mongo": health_status(),
        "desk": f"{settings.public_base_url.rstrip('/')}/desk",
        "options_desk": f"{settings.public_base_url.rstrip('/')}/desk?tab=options",
    }


@app.get("/watchlist")
def watchlist_get() -> dict[str, Any]:
    return {"tickers": get_active_watchlist()}


@app.put("/watchlist")
def watchlist_put(body: dict[str, Any]) -> dict[str, Any]:
    """
    Set your stocks of interest.
    Body: {"tickers": ["AAPL", "NVDA", "MSFT"]}
    or {"tickers": "AAPL,NVDA,MSFT"}
    """
    raw = body.get("tickers", [])
    if isinstance(raw, str):
        tickers = [t.strip() for t in raw.split(",") if t.strip()]
    elif isinstance(raw, list):
        tickers = raw
    else:
        raise HTTPException(status_code=400, detail="Provide tickers as a list or comma-separated string")
    try:
        saved = set_watchlist(tickers)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"tickers": saved, "message": "Watchlist updated — next analyze uses these tickers"}


@app.get("/desk", response_class=HTMLResponse)
def desk() -> str:
    """Confirm trades here after you place them manually."""
    return DESK_HTML


@app.get("/schedule")
def schedule() -> dict[str, Any]:
    return scheduler_status()


@app.post("/day-wrap")
def day_wrap(force: bool = True) -> dict[str, Any]:
    """
    Send (or preview-build) the end-of-day wrap: today's suggestions + top news.
    Defaults to force=true so manual calls can re-send.
    """
    from app.day_wrap import run_day_wrap

    return run_day_wrap(force=force)


@app.get("/budget")
def budget() -> dict[str, Any]:
    from app.budget import budget_status

    return budget_status()


@app.post("/budget/clear-pause")
def budget_clear_pause() -> dict[str, Any]:
    """Clear temporary API pause after enabling paid billing / Tier 1."""
    from app.budget import clear_quota_pause

    return clear_quota_pause()


@app.get("/portfolio/marked")
def portfolio_marked() -> dict[str, Any]:
    return serialize_mongo(portfolio_with_marks())


@app.get("/regime")
def regime() -> dict[str, Any]:
    """Current broad-market regime (SPY vs 200d SMA + VIX)."""
    from app.signals import market_regime

    return market_regime(force=True)


@app.get("/signals")
def signals(
    symbols: str = Query("", description="Comma-separated tickers (default: watchlist)"),
    period: str = Query(""),
    interval: str = Query(""),
) -> dict[str, Any]:
    """Deterministic quant signals + short-list preview — no LLM, no spend."""
    from app.signals import market_regime, score_universe, select_candidates

    settings = get_settings()
    tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()] or get_active_watchlist()
    ind = compute_indicators_batch(
        tickers,
        period=period or settings.analyze_period,
        interval=interval or settings.analyze_interval,
    )
    daily_ctx = compute_daily_context_batch(list(ind["indicators"].keys()))
    scores = score_universe(ind["indicators"], daily_ctx)
    held = list((get_latest_portfolio().get("positions") or {}).keys())
    selection = select_candidates(scores, held)
    ranked = sorted(scores.items(), key=lambda kv: float(kv[1].get("score") or 0), reverse=True)
    return {
        "regime": market_regime(),
        "candidates": selection.get("buy_candidates"),
        "shortlist": selection.get("symbols"),
        "signals": {t: s for t, s in ranked},
        "daily_context": daily_ctx,
        "errors": ind.get("errors", {}),
    }


@app.get("/backtest")
def backtest(
    symbols: str = Query(""),
    years: float = Query(2.0, ge=0.25, le=10.0),
    starting_cash: float = Query(10000.0, gt=0),
    max_positions: int = Query(5, ge=1, le=25),
    buy_threshold: float | None = Query(None, ge=0.0, le=100.0),
    take_profit_pct: float | None = Query(None, ge=0.0, le=200.0),
    stop_loss_pct: float | None = Query(None, ge=0.0, le=100.0),
    atr_initial_mult: float | None = Query(None, ge=0.0, le=20.0),
    atr_trail_mult: float | None = Query(None, ge=0.0, le=20.0),
    time_stop_days: int | None = Query(None, ge=0, le=365),
    include_curve: bool = Query(False),
    include_trades: bool = Query(True),
) -> dict[str, Any]:
    """Backtest the deterministic engine over daily history — no LLM, no spend.

    Replays the live entry scoring + exit rules over the last ``years`` of daily
    data and reports win rate, drawdown, Sharpe, CAGR vs SPY buy-and-hold. Exit
    parameters default to live settings; override to tune. take_profit_pct=0
    disables the fixed target (pure trailing-stop / let-winners-run mode).
    """
    from app.backtest import run_backtest

    tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()] or None
    result = run_backtest(
        tickers,
        years=years,
        starting_cash=starting_cash,
        max_positions=max_positions,
        buy_threshold=buy_threshold,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        atr_initial_mult=atr_initial_mult,
        atr_trail_mult=atr_trail_mult,
        time_stop_days=time_stop_days,
    )
    if not include_curve:
        result.pop("equity_curve", None)
    if not include_trades:
        result.pop("trades", None)
    return result


@app.get("/tilt/plan")
def tilt_plan(rebalance: bool = Query(True)) -> dict[str, Any]:
    """Preview the momentum-tilt target portfolio + trades — no writes, no spend."""
    from app.tilt import compute_tilt_plan

    return serialize_mongo(compute_tilt_plan(rebalance=rebalance))


@app.post("/tilt/rebalance")
def tilt_rebalance(force: bool = True, send: bool = True) -> dict[str, Any]:
    """Run the momentum-tilt engine now; emits BUY/SELL recs to confirm in Hisaab.

    force=true does a full rebalance regardless of the monthly cadence.
    """
    from app.tilt import run_tilt_rebalance

    return serialize_mongo(run_tilt_rebalance(force=force, send_notification=send))


@app.get("/exits/check")
def exits_check() -> dict[str, Any]:
    """Preview the deterministic exit engine against open positions (no alerts sent)."""
    from app.exits import evaluate_exits

    return serialize_mongo(evaluate_exits())


@app.post("/exits/run")
def exits_run(send: bool = True) -> dict[str, Any]:
    """Run the exit engine now; fires decisive SELL alerts on stop/target/time hits."""
    from app.exits import run_exit_monitor

    return serialize_mongo(run_exit_monitor(send_notification=send))


@app.get("/recommendations/pending")
def recommendations_pending() -> dict[str, Any]:
    doc = get_pending_recommendation()
    if not doc:
        raise HTTPException(status_code=404, detail="No pending recommendation")
    return serialize_mongo(doc)


@app.get("/recommendations/{rec_id}")
def recommendations_one(rec_id: str) -> dict[str, Any]:
    doc = get_recommendation(rec_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return serialize_mongo(doc)


@app.post("/trades/{rec_id}/execute")
def trades_execute(
    rec_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    fill = body.get("fill_price", body.get("fillPrice"))
    fill_price = float(fill) if fill is not None and fill != "" else None
    shares_raw = body.get("shares", body.get("quantity", body.get("fill_shares", body.get("fillShares"))))
    shares_override = float(shares_raw) if shares_raw is not None and shares_raw != "" else None
    try:
        return execute_recommendation(
            rec_id,
            fill_price=fill_price,
            shares_override=shares_override,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("trade execute failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/trades/{rec_id}/update")
def trades_update(
    rec_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    """Correct an already-executed trade (wrong fill price or quantity)."""
    fill = body.get("fill_price", body.get("fillPrice"))
    shares_raw = body.get("shares", body.get("quantity", body.get("fill_shares", body.get("fillShares"))))
    if fill is None or fill == "" or shares_raw is None or shares_raw == "":
        raise HTTPException(status_code=400, detail="fill_price and shares are required")
    try:
        return update_recommendation_trade(
            rec_id,
            fill_price=float(fill),
            shares=float(shares_raw),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("trade update failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/trades/{rec_id}/skip")
def trades_skip(rec_id: str) -> dict[str, Any]:
    try:
        return skip_recommendation(rec_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/analyze")
def analyze(body: AnalyzeRequest | None = None) -> dict[str, Any]:
    """
    Full pipeline: indicators → news → Gemini agents → risk → notify → MongoDB.
    Blocked outside Mon–Fri 9am–4pm ET unless force=true.
    Also respects free-tier daily Gemini budget unless force=true.
    """
    body = body or AnalyzeRequest()
    if not body.force and not is_market_hours():
        raise HTTPException(
            status_code=403,
            detail={
                "error": "outside_market_hours",
                "message": "Analysis only runs Mon–Fri 9am–4pm ET. Pass force=true to override.",
                "market_hours": market_hours_status(),
            },
        )

    from app.budget import budget_status, can_start_analyze, record_analyze

    if not body.force:
        ok, reason = can_start_analyze()
        if not ok:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "free_tier_budget",
                    "message": f"Skipping analyze to protect free limits: {reason}",
                    "budget": budget_status(),
                },
            )

    symbols = body.symbols or get_active_watchlist()
    portfolio = body.portfolio.model_dump() if body.portfolio else None
    try:
        result = run_analysis(
            symbols=symbols,
            portfolio=portfolio,
            send_notification=body.send_telegram,
            period=body.period,
            interval=body.interval,
            trigger="api" if not body.force else "api_force",
        )
        try:
            record_analyze()
        except Exception:  # noqa: BLE001
            logger.warning("Could not record analyze budget", exc_info=True)
        result["budget"] = budget_status()
        return result
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("analyze failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/indicators/{symbol}")
def indicators_one(
    symbol: str,
    period: str = Query("3mo"),
    interval: str = Query("1h"),
) -> dict[str, Any]:
    try:
        return compute_indicators(symbol, period=period, interval=interval)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("indicators_one failed for %s", symbol)
        raise HTTPException(status_code=502, detail=f"Upstream data error: {exc}") from exc


@app.get("/indicators")
def indicators_many(
    symbols: str = Query(..., description="Comma-separated tickers"),
    period: str = Query("3mo"),
    interval: str = Query("1h"),
) -> dict[str, Any]:
    tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not tickers:
        raise HTTPException(status_code=400, detail="Provide at least one symbol")
    if len(tickers) > 20:
        raise HTTPException(status_code=400, detail="Max 20 symbols per request")

    result = compute_indicators_batch(tickers, period=period, interval=interval)
    if not result["indicators"] and result["errors"]:
        raise HTTPException(status_code=502, detail=result["errors"])
    return result


@app.get("/portfolio")
def portfolio_latest() -> dict[str, Any]:
    return serialize_mongo(get_latest_portfolio())


@app.post("/portfolio")
def portfolio_save(body: PortfolioState) -> dict[str, Any]:
    doc_id = save_portfolio(body.cash, {k: v.model_dump() for k, v in body.positions.items()})
    return {"id": doc_id, "portfolio": body.model_dump()}


@app.get("/recommendations/latest")
def recommendations_latest() -> dict[str, Any]:
    doc = get_latest_recommendation()
    if not doc:
        raise HTTPException(status_code=404, detail="No recommendations yet")
    return serialize_mongo(doc)


@app.post("/risk/apply")
def risk_apply(body: RiskRequest) -> dict[str, Any]:
    portfolio = body.portfolio.model_dump()
    return apply_risk_rules(
        body.recommendation,
        portfolio,
        body.prices,
        max_position_pct=body.max_position_pct,
        min_cash_pct=body.min_cash_pct,
    )


@app.post("/portfolio/from-rows")
def portfolio_from_rows(body: PortfolioRowsRequest) -> dict[str, Any]:
    try:
        parsed = rows_to_portfolio(body.rows, cash=body.cash)
        save_portfolio(parsed["cash"], parsed["positions"], source="excel")
        return parsed
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/portfolio/upload")
async def portfolio_upload(
    file: UploadFile = File(...),
    cash: float = Query(1000.0),
) -> dict[str, Any]:
    data_dir = Path("/app/data/uploads")
    data_dir.mkdir(parents=True, exist_ok=True)
    dest = data_dir / (file.filename or "portfolio.xlsx")
    content = await file.read()
    dest.write_bytes(content)
    try:
        parsed = read_portfolio_file(dest, cash=cash)
        save_portfolio(parsed["cash"], parsed["positions"], source="upload")
        return parsed
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/portfolio/example")
def portfolio_example() -> PortfolioState:
    settings = get_settings()
    return PortfolioState(cash=settings.initial_cash, positions={})


@app.get("/prompts")
def prompts_catalog() -> dict[str, Any]:
    return {
        "news_agent": {
            "system": NEWS_SYSTEM,
            "user_example": news_user_prompt(EXAMPLE_NEWS_INPUT),
            "output_example": EXAMPLE_NEWS_OUTPUT,
        },
        "technical_agent": {
            "system": TECH_SYSTEM,
            "user_example": tech_user_prompt(EXAMPLE_TECH_INPUT),
            "output_example": EXAMPLE_TECH_OUTPUT,
        },
        "decision_agent": {
            "system": DECISION_SYSTEM,
            "user_example": decision_user_prompt(EXAMPLE_DECISION_INPUT),
            "output_example": EXAMPLE_DECISION_OUTPUT,
            "schema": EXAMPLE_DECISION_OUTPUT,
        },
    }


@app.post("/prompts/decision")
def build_decision_prompt(context: dict[str, Any]) -> dict[str, str]:
    return {"system": DECISION_SYSTEM, "user": decision_user_prompt(context)}


@app.post("/prompts/news")
def build_news_prompt(news_by_ticker: dict[str, list[str]]) -> dict[str, str]:
    return {"system": NEWS_SYSTEM, "user": news_user_prompt(news_by_ticker)}


@app.post("/prompts/technical")
def build_tech_prompt(indicators: dict[str, Any]) -> dict[str, str]:
    return {"system": TECH_SYSTEM, "user": tech_user_prompt(indicators)}


# ---------------------------------------------------------------------------
# Options routes (separate paper book)
# ---------------------------------------------------------------------------


@app.get("/options/watchlist")
def options_watchlist_get() -> dict[str, Any]:
    return {"tickers": get_active_options_watchlist()}


@app.post("/options/movers/refresh")
def options_movers_refresh() -> dict[str, Any]:
    """Scan high-movement names and rewrite the options watchlist (no LLM)."""
    from app.options_movers import refresh_options_watchlist_from_movers

    return refresh_options_watchlist_from_movers(persist=True)


@app.put("/options/watchlist")
def options_watchlist_put(body: dict[str, Any]) -> dict[str, Any]:
    raw = body.get("tickers", [])
    if isinstance(raw, str):
        tickers = [t.strip() for t in raw.split(",") if t.strip()]
    elif isinstance(raw, list):
        tickers = raw
    else:
        raise HTTPException(status_code=400, detail="Provide tickers as a list or comma-separated string")
    try:
        saved = set_options_watchlist(tickers)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"tickers": saved, "message": "Options watchlist updated"}


@app.get("/options/portfolio")
def options_portfolio_latest() -> dict[str, Any]:
    return serialize_mongo(get_latest_options_portfolio())


@app.post("/options/portfolio")
def options_portfolio_save(body: OptionsPortfolioState) -> dict[str, Any]:
    positions = {k: v.model_dump() for k, v in body.positions.items()}
    doc_id = save_options_portfolio(body.cash, positions)
    return {"id": doc_id, "portfolio": body.model_dump()}


@app.get("/options/portfolio/marked")
def options_portfolio_marked() -> dict[str, Any]:
    return serialize_mongo(options_portfolio_with_marks())


@app.get("/options/recommendations/pending")
def options_recommendations_pending() -> dict[str, Any]:
    doc = get_pending_options_recommendation()
    if not doc:
        raise HTTPException(status_code=404, detail="No pending options recommendation")
    return serialize_mongo(doc)


@app.get("/options/recommendations/latest")
def options_recommendations_latest() -> dict[str, Any]:
    doc = get_latest_options_recommendation()
    if not doc:
        raise HTTPException(status_code=404, detail="No options recommendations yet")
    return serialize_mongo(doc)


@app.get("/options/recommendations/{rec_id}")
def options_recommendations_one(rec_id: str) -> dict[str, Any]:
    doc = get_options_recommendation(rec_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Options recommendation not found")
    return serialize_mongo(doc)


@app.post("/options/trades/{rec_id}/execute")
def options_trades_execute(
    rec_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    fill = body.get("fill_premium", body.get("fillPremium", body.get("premium")))
    fill_premium = float(fill) if fill is not None and fill != "" else None
    contracts_raw = body.get("contracts", body.get("quantity"))
    contracts_override = (
        float(contracts_raw) if contracts_raw is not None and contracts_raw != "" else None
    )
    try:
        return execute_options_recommendation(
            rec_id,
            fill_premium=fill_premium,
            contracts_override=contracts_override,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("options trade execute failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/options/trades/{rec_id}/update")
def options_trades_update(
    rec_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    fill = body.get("fill_premium", body.get("fillPremium", body.get("premium")))
    contracts_raw = body.get("contracts", body.get("quantity"))
    if fill is None or fill == "" or contracts_raw is None or contracts_raw == "":
        raise HTTPException(status_code=400, detail="fill_premium and contracts are required")
    try:
        return update_options_recommendation_trade(
            rec_id,
            fill_premium=float(fill),
            contracts=float(contracts_raw),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("options trade update failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/options/trades/{rec_id}/skip")
def options_trades_skip(rec_id: str) -> dict[str, Any]:
    try:
        return skip_options_recommendation(rec_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/options/analyze")
def options_analyze(body: OptionsAnalyzeRequest | None = None) -> dict[str, Any]:
    """
    Options pipeline: spot indicators → news → Yahoo/yfinance deep scan →
    Gemini agents → options risk → notify → MongoDB.
    """
    body = body or OptionsAnalyzeRequest()
    if not body.force and not is_market_hours():
        raise HTTPException(
            status_code=403,
            detail={
                "error": "outside_market_hours",
                "message": "Options analysis only runs Mon–Fri 9am–4pm ET. Pass force=true to override.",
                "market_hours": market_hours_status(),
            },
        )

    from app.budget import budget_status, can_start_analyze, record_analyze

    if not body.force:
        ok, reason = can_start_analyze()
        if not ok:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "free_tier_budget",
                    "message": f"Skipping options analyze to protect free limits: {reason}",
                    "budget": budget_status(),
                },
            )

    symbols = body.symbols  # None → pipeline auto-picks high movers into watchlist
    portfolio = None
    if body.portfolio:
        portfolio = {
            "cash": body.portfolio.cash,
            "positions": {k: v.model_dump() for k, v in body.portfolio.positions.items()},
        }
    try:
        result = run_options_analysis(
            symbols=symbols,
            portfolio=portfolio,
            send_notification=body.send_telegram,
            period=body.period,
            interval=body.interval,
            trigger="api" if not body.force else "api_force",
        )
        try:
            record_analyze()
        except Exception:  # noqa: BLE001
            logger.warning("Could not record options analyze budget", exc_info=True)
        result["budget"] = budget_status()
        return result
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("options analyze failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
