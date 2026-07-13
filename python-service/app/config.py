from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mongodb_uri: str = ""
    mongodb_db: str = "Khabari"
    watchlist: str = "TSLA,NVDA,AAPL,MSFT,AMZN"
    initial_cash: float = 1000.0

    # LLM — Gemini is default (use explicit cheap Flash, not flash-latest alias)
    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # Market window + scheduler (Mon–Fri 9–4 ET)
    market_timezone: str = "America/New_York"
    market_start_hour: int = 9
    market_end_hour: int = 16  # 4pm inclusive (runs at 9,10,...,16)
    scheduler_enabled: bool = True

    # Free/paid guards (keep Gemini under ~$10/month)
    news_scan_minutes: int = 30
    position_monitor_minutes: int = 60
    analyze_cooldown_minutes: int = 45
    backup_analyze_hours: int = 2  # full backup every N hours (not every hour)
    max_analyzes_per_day: int = 8  # ~8×3 = 24 Gemini calls/day
    max_llm_calls_per_day: int = 30
    max_monthly_spend_usd: float = 10.0  # hard stop
    quota_pause_minutes: int = 90
    news_min_new_articles: int = 2  # ignore single trivial headline churn
    min_notify_confidence: float = 70.0
    notify_only_actionable: bool = True
    position_take_profit_pct: float = 4.0
    position_stop_loss_pct: float = 3.0
    analyze_period: str = "5d"
    analyze_interval: str = "15m"

    public_base_url: str = "http://localhost:8000"
    # Phone confirm UI — Hisaab /trades (preferred over local /desk)
    hisaab_base_url: str = ""

    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    alpha_vantage_api_key: str = ""
    finnhub_api_key: str = ""
    marketaux_api_token: str = ""
    newsdata_api_key: str = ""
    max_position_pct: float = 0.30
    min_cash_pct: float = 0.10

    @property
    def watchlist_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
