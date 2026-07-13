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
    get_active_watchlist,
    get_latest_portfolio,
    get_latest_recommendation,
    health_status,
    init_db,
    save_portfolio,
    serialize_mongo,
    set_watchlist,
)
from app.desk import DESK_HTML
from app.indicators import compute_indicators, compute_indicators_batch
from app.llm import LLMError
from app.market_hours import is_market_hours, market_hours_status
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
    description="Hourly AI stock analyst — Mon–Fri 9am–4pm ET.",
    version="0.3.0",
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
            "POST /analyze",
            "GET /portfolio",
            "GET /portfolio/marked",
            "POST /trades/{id}/execute",
            "POST /trades/{id}/skip",
            "GET /recommendations/latest",
            "GET /recommendations/pending",
            "POST /risk/apply",
            "GET /prompts",
        ],
        "mongo": health_status(),
        "desk": f"{settings.public_base_url.rstrip('/')}/desk",
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
