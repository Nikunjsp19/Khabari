"""MongoDB Atlas persistence for Khabari."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import PyMongoError

from app.config import get_settings

logger = logging.getLogger(__name__)

_client: MongoClient | None = None


def get_client() -> MongoClient:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.mongodb_uri:
            raise RuntimeError("MONGODB_URI is not set")
        _client = MongoClient(
            settings.mongodb_uri,
            serverSelectionTimeoutMS=8000,
            connectTimeoutMS=8000,
        )
    return _client


def get_db() -> Database:
    settings = get_settings()
    return get_client()[settings.mongodb_db]


def ping() -> bool:
    get_client().admin.command("ping")
    return True


def ensure_indexes() -> None:
    db = get_db()
    db.prices.create_index([("ticker", ASCENDING), ("ts", DESCENDING)], unique=True)
    db.news.create_index("uuid", unique=True, sparse=True)
    db.news.create_index([("published", DESCENDING)])
    db.recommendations.create_index([("ts", DESCENDING)])
    db.recommendations.create_index("status")
    db.portfolio.create_index([("ts", DESCENDING)])
    db.watchlist.create_index("ticker", unique=True)
    db.trades.create_index([("ts", DESCENDING)])
    db.meta.create_index("updated_at")


def seed_defaults() -> None:
    """Seed watchlist + $1000 cash portfolio if empty."""
    settings = get_settings()
    db = get_db()

    if db.watchlist.count_documents({}) == 0:
        db.watchlist.insert_many(
            [{"ticker": t, "active": True} for t in settings.watchlist_symbols]
        )
        logger.info("Seeded watchlist: %s", settings.watchlist_symbols)

    if db.portfolio.count_documents({}) == 0:
        db.portfolio.insert_one(
            {
                "ts": datetime.now(timezone.utc),
                "cash": settings.initial_cash,
                "positions": {},
                "source": "system",
            }
        )
        logger.info("Seeded portfolio with cash=%s", settings.initial_cash)


def init_db() -> None:
    ensure_indexes()
    seed_defaults()


def get_active_watchlist() -> list[str]:
    """Active tickers from MongoDB; fall back to env WATCHLIST."""
    docs = list(get_db().watchlist.find({"active": True}).sort("ticker", ASCENDING))
    if docs:
        return [str(d["ticker"]).upper() for d in docs]
    return get_settings().watchlist_symbols


def set_watchlist(tickers: list[str]) -> list[str]:
    """Replace the active watchlist with the given tickers."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in tickers:
        t = str(raw).strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        cleaned.append(t)

    if not cleaned:
        raise ValueError("Watchlist must include at least one ticker")

    db = get_db()
    db.watchlist.delete_many({})
    db.watchlist.insert_many([{"ticker": t, "active": True, "updated_at": _now()} for t in cleaned])
    logger.info("Watchlist updated: %s", cleaned)
    return cleaned


def _now() -> datetime:
    return datetime.now(timezone.utc)


def save_prices(indicators: dict[str, dict[str, Any]]) -> int:
    col: Collection = get_db().prices
    count = 0
    for ticker, row in indicators.items():
        doc = {
            **row,
            "ticker": ticker,
            "ts": row.get("ts") or _now().isoformat(),
            "saved_at": _now(),
        }
        col.update_one(
            {"ticker": ticker, "ts": doc["ts"]},
            {"$set": doc},
            upsert=True,
        )
        count += 1
    return count


def save_news(news_batch: dict[str, list[dict[str, Any]]]) -> int:
    col: Collection = get_db().news
    count = 0
    for ticker, articles in news_batch.items():
        for article in articles:
            uuid = article.get("uuid") or f"{ticker}-{article.get('title', '')[:40]}"
            doc = {**article, "uuid": uuid, "tickers": article.get("tickers") or [ticker], "saved_at": _now()}
            col.update_one({"uuid": uuid}, {"$set": doc}, upsert=True)
            count += 1
    return count


def save_recommendation(rec: dict[str, Any], *, extras: dict[str, Any] | None = None) -> str:
    doc = {
        **rec,
        "ts": _now(),
        "status": "pending",
        "extras": extras or {},
    }
    result = get_db().recommendations.insert_one(doc)
    return str(result.inserted_id)


def get_latest_portfolio() -> dict[str, Any]:
    doc = get_db().portfolio.find_one(sort=[("ts", DESCENDING)])
    if not doc:
        settings = get_settings()
        return {"cash": settings.initial_cash, "positions": {}}
    return {
        "cash": float(doc.get("cash", 0)),
        "positions": doc.get("positions") or {},
        "ts": doc.get("ts"),
        "source": doc.get("source"),
    }


def save_portfolio(cash: float, positions: dict[str, Any], source: str = "manual") -> str:
    result = get_db().portfolio.insert_one(
        {
            "ts": _now(),
            "cash": cash,
            "positions": positions,
            "source": source,
        }
    )
    return str(result.inserted_id)


def get_latest_recommendation() -> dict[str, Any] | None:
    doc = get_db().recommendations.find_one(sort=[("ts", DESCENDING)])
    if not doc:
        return None
    doc["id"] = str(doc.pop("_id"))
    return doc


def serialize_mongo(doc: Any) -> Any:
    """Make Mongo docs JSON-safe."""
    if isinstance(doc, list):
        return [serialize_mongo(x) for x in doc]
    if isinstance(doc, dict):
        out = {}
        for k, v in doc.items():
            if k == "_id":
                out["id"] = str(v)
            else:
                out[k] = serialize_mongo(v)
        return out
    if isinstance(doc, datetime):
        return doc.isoformat()
    return doc


def health_status() -> dict[str, Any]:
    try:
        ping()
        db = get_db()
        return {
            "ok": True,
            "database": db.name,
            "collections": {
                "prices": db.prices.estimated_document_count(),
                "news": db.news.estimated_document_count(),
                "recommendations": db.recommendations.estimated_document_count(),
                "portfolio": db.portfolio.estimated_document_count(),
            },
        }
    except PyMongoError as exc:
        return {"ok": False, "error": str(exc)}
