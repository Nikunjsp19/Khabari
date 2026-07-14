from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mongodb_uri: str = ""
    mongodb_db: str = "Khabari"
    # Stock universe — famous/liquid + high-vol movers (system-managed when auto)
    watchlist: str = (
        "SPY,QQQ,AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AMD,AVGO,JPM,BAC,GS,NFLX,TSM,"
        "HOOD,PLTR,NOW,MSTR,COIN,SMCI,ARM,RDDT"
    )
    watchlist_auto_famous: bool = True
    initial_cash: float = 1000.0

    # LLM — Gemini is default
    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    # Comma-separated fallbacks used immediately on 503/overload (same analyze run)
    gemini_fallback_models: str = "gemini-3.1-flash-lite,gemini-flash-latest"
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
    analyze_cooldown_minutes: int = 25
    backup_analyze_hours: int = 2  # full backup every N hours (not every hour)
    max_analyzes_per_day: int = 16  # stocks + hourly options headroom
    # 0 = no call-count limit (prefer MAX_DAILY_SPEND_USD / monthly $)
    max_llm_calls_per_day: int = 0
    max_daily_spend_usd: float = 1.0  # hard daily $ brake (stocks + options)
    max_monthly_spend_usd: float = 25.0  # stocks + options LLM room
    quota_pause_minutes: int = 90
    news_min_new_articles: int = 2  # ignore single trivial headline churn
    min_notify_confidence: float = 58.0
    notify_only_actionable: bool = True
    position_take_profit_pct: float = 5.0
    position_stop_loss_pct: float = 3.5
    analyze_period: str = "5d"
    analyze_interval: str = "15m"
    # End-of-day wrap (Mon–Fri) — concluding news + suggestions summary
    day_wrap_enabled: bool = True
    day_wrap_hour: int = 16
    day_wrap_minute: int = 15

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
    # Moderate aggression: larger single-name room, less idle cash
    max_position_pct: float = 0.40
    min_cash_pct: float = 0.05

    # --- Options (separate paper book; Yahoo/yfinance chains, estimated delta) ---
    options_watchlist: str = "TSLA,NVDA,AAPL,MSFT,AMZN"
    options_initial_cash: float = 1000.0
    options_min_notify_confidence: float = 65.0
    options_max_premium_pct: float = 0.40  # max premium at risk per trade vs options NAV
    options_min_cash_pct: float = 0.05
    options_min_dte: int = 7
    options_max_dte: int = 45
    options_min_open_interest: int = 100
    options_min_volume: int = 10
    options_max_spread_pct: float = 12.0  # bid-ask as % of mid
    options_call_delta_min: float = 0.30
    options_call_delta_max: float = 0.60
    options_put_delta_min: float = -0.60
    options_put_delta_max: float = -0.30
    options_max_candidates_per_ticker: int = 8
    options_take_profit_pct: float = 40.0  # premium % gain
    options_stop_loss_pct: float = 35.0  # premium % loss
    options_scheduler_enabled: bool = True
    options_backup_analyze_hours: int = 1  # hourly during market window
    # Soft gap for options hourly job vs shared stock analyze cooldown
    options_analyze_min_gap_minutes: float = 20.0
    # Auto-pick high-movement underlyings into the options watchlist before each scan
    options_auto_movers: bool = True
    options_mover_top_n: int = 10
    options_mover_min_abs_pct: float = 1.5
    # Cap deep chain scans so options runs finish before Gemini timeouts
    options_analyze_max_symbols: int = 8
    options_max_candidates_for_llm: int = 15
    # Split Gemini prompts into small batches to avoid read timeouts
    llm_ticker_batch_size: int = 3
    # Extra names always considered in the movers universe (comma-separated)
    options_mover_universe_extra: str = "SPY,QQQ,IWM,AMD,COIN,HOOD,ORCL,PLTR,NFLX,BA,GS,JPM"

    @property
    def watchlist_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]

    @property
    def options_watchlist_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.options_watchlist.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
