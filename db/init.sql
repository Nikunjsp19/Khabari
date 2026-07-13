-- Khabari AI Stock Analyst — initial schema
-- Applied automatically on first Postgres container start.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Historical OHLC + computed technical indicators
CREATE TABLE IF NOT EXISTS prices (
  id            BIGSERIAL PRIMARY KEY,
  ticker        VARCHAR(16) NOT NULL,
  ts            TIMESTAMPTZ NOT NULL,
  open          NUMERIC(18, 6),
  high          NUMERIC(18, 6),
  low           NUMERIC(18, 6),
  close         NUMERIC(18, 6),
  volume        BIGINT,
  rsi           NUMERIC(10, 4),
  macd          NUMERIC(18, 6),
  macd_signal   NUMERIC(18, 6),
  ema20         NUMERIC(18, 6),
  ema50         NUMERIC(18, 6),
  ema200        NUMERIC(18, 6),
  sma50         NUMERIC(18, 6),
  bb_upper      NUMERIC(18, 6),
  bb_lower      NUMERIC(18, 6),
  atr           NUMERIC(18, 6),
  vwap          NUMERIC(18, 6),
  momentum      NUMERIC(18, 6),
  adx           NUMERIC(10, 4),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (ticker, ts)
);

CREATE INDEX IF NOT EXISTS idx_prices_ticker_ts ON prices (ticker, ts DESC);

-- Financial news articles (deduped by uuid)
CREATE TABLE IF NOT EXISTS news (
  id            BIGSERIAL PRIMARY KEY,
  uuid          TEXT UNIQUE,
  title         TEXT NOT NULL,
  snippet       TEXT,
  url           TEXT,
  published     TIMESTAMPTZ,
  source        TEXT,
  tickers       TEXT[] NOT NULL DEFAULT '{}',
  sentiment     NUMERIC(6, 4),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_news_published ON news (published DESC);
CREATE INDEX IF NOT EXISTS idx_news_tickers ON news USING GIN (tickers);

-- Final AI recommendations (one per hourly run typically)
CREATE TABLE IF NOT EXISTS recommendations (
  id               BIGSERIAL PRIMARY KEY,
  ts               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ticker           VARCHAR(16) NOT NULL,
  action           VARCHAR(8) NOT NULL CHECK (action IN ('BUY', 'SELL', 'HOLD')),
  investment       NUMERIC(18, 2) NOT NULL DEFAULT 0,
  confidence       NUMERIC(5, 2) NOT NULL DEFAULT 0,
  risk             VARCHAR(10) NOT NULL CHECK (risk IN ('LOW', 'MEDIUM', 'HIGH')),
  time_horizon     VARCHAR(10) NOT NULL CHECK (time_horizon IN ('SHORT', 'MEDIUM', 'LONG')),
  expected_return  TEXT,
  reasoning        JSONB NOT NULL DEFAULT '[]'::jsonb,
  raw_ai_output    JSONB,
  risk_adjusted    BOOLEAN NOT NULL DEFAULT FALSE,
  remaining_cash   NUMERIC(18, 2),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_recommendations_ts ON recommendations (ts DESC);
CREATE INDEX IF NOT EXISTS idx_recommendations_ticker ON recommendations (ticker);

-- Portfolio snapshots (cash + positions)
CREATE TABLE IF NOT EXISTS portfolio (
  id            BIGSERIAL PRIMARY KEY,
  ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  cash          NUMERIC(18, 2) NOT NULL DEFAULT 1000.00,
  positions     JSONB NOT NULL DEFAULT '{}'::jsonb,
  source        TEXT DEFAULT 'manual',  -- manual | excel | system
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_portfolio_ts ON portfolio (ts DESC);

-- Optional watchlist config
CREATE TABLE IF NOT EXISTS watchlist (
  id            SERIAL PRIMARY KEY,
  ticker        VARCHAR(16) NOT NULL UNIQUE,
  active        BOOLEAN NOT NULL DEFAULT TRUE,
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed default watchlist
INSERT INTO watchlist (ticker) VALUES
  ('TSLA'), ('NVDA'), ('AAPL'), ('MSFT'), ('AMZN')
ON CONFLICT (ticker) DO NOTHING;

-- Seed initial portfolio: $1000 cash, no positions
INSERT INTO portfolio (cash, positions, source)
SELECT 1000.00, '{}'::jsonb, 'system'
WHERE NOT EXISTS (SELECT 1 FROM portfolio);

-- Convenience view: latest recommendation
CREATE OR REPLACE VIEW latest_recommendation AS
SELECT *
FROM recommendations
ORDER BY ts DESC
LIMIT 1;

-- Convenience view: latest portfolio
CREATE OR REPLACE VIEW latest_portfolio AS
SELECT *
FROM portfolio
ORDER BY ts DESC
LIMIT 1;
