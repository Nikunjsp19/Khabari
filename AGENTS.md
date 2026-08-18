# AGENTS.md

## Cursor Cloud specific instructions

Khabari is an hourly AI stock-analyst backend. The only runnable service is the
**Python FastAPI app** in `python-service/` (`app.main:app`). `n8n` + Postgres in
`docker-compose.yml` are optional orchestration and are **not** required to run or
test the Python service (Docker is not preinstalled on this VM).

### Environment already provisioned (do not re-run in the update script)
- System packages installed via apt: `python3.12-venv` (needed for `python -m venv`)
  and `mongodb-org-server` (the app persists to MongoDB).
- The update script (auto-run on startup) creates `python-service/.venv` and
  installs `python-service/requirements.txt`. Activate it with
  `source python-service/.venv/bin/activate`.

### MongoDB (non-obvious)
- The app reads `MONGODB_URI` (see `app/config.py`). For local dev there is no
  MongoDB Atlas; run a local server instead. There is **no systemd** on this VM,
  so start `mongod` manually, e.g.:
  `mongod --dbpath /tmp/mongo-data --bind_ip 127.0.0.1 --port 27017` (in a tmux
  session), then export `MONGODB_URI="mongodb://127.0.0.1:27017"`.
- Without Mongo the app still boots (startup errors are caught), but any DB-backed
  endpoint (`/health`, `/`, `/schedule`, `/analyze`, `/portfolio`, budget) errors.
  `market_hours_status()` / the trade-window logic works without Mongo.

### Run / test / lint
- Run app: from `python-service/`, `uvicorn app.main:app --host 127.0.0.1 --port 8000`
  (venv active, `MONGODB_URI` exported). Docs at `/docs`.
- Tests: from `python-service/`, `pytest -q`. All tests should pass.
 (`fingerprint_article` now keys on ticker + article id, so two tickers sharing
 one article no longer dedupe against each other.)
- There is no separate linter configured; rely on tests.

### Trade window / notifications gotcha (important for testing)
- Analyze/suggestion runs are gated to the trade window **Mon–Fri 09:00–16:00 ET**
  via `app/market_hours.py::is_market_hours` (minute-precise; closes exactly at
  4:00pm). Outside that window the scheduler jobs skip and `POST /analyze` returns
  403 `outside_market_hours` — pass `{"force": true}` to run it anyway when testing.
- The end-of-day "suggestions" summary (day wrap) runs on its own cron at
  `DAY_WRAP_HOUR:DAY_WRAP_MINUTE` (default 16:15) and is intentionally *not* gated
  by the trade window.
- The full analyze pipeline calls Gemini (LLM) and needs `GEMINI_API_KEY`; without
  it the pipeline fails at the LLM step. Risk/prompt/market-hours endpoints work
  without any API keys.

### Scheduler / notifications gotcha (Mac + Docker)
- Cron jobs live **inside** `khabari-python-api`. When the Mac sleeps, Docker
  freezes and APScheduler ticks are missed — health checks can still look "up"
  while no ntfy pings fire. A **watchdog** (`khabari_scheduler_watchdog`) runs
  every ~10 minutes during the trade window and forces an options scan (and a
  once-per-day tilt catch-up) if the last run is overdue. Misfire grace is ~2h.
- **Stocks:** with `TILT_ENABLED=true`, hourly LLM stock analyze is off. You get
  tilt pings only when there are rebalance/trend-brake trades (a few times a day
  at most), not every hour. **Options** still scan ~hourly and notify on HOLD or
  actionable BUY_TO_OPEN.
- Keep the laptop awake during market hours (or prevent sleep) for best results;
  the watchdog recovers after wake, but it cannot run while the VM is frozen.

### Options chase / same-day extension (important)
- A large same-day move (~**±2.5%+**, see `options_max_intraday_chase_pct`) is
  **significant**. Buying calls after a big green day (or puts after a dump) is
  a FOMO extension bet — premium already prices much of the move.
- The pipeline **hard-blocks** those trades: chase-direction contracts are
  filtered from the LLM candidate list, and `apply_options_chase_gate` converts
  any remaining BUY_TO_OPEN chase into HOLD (`chase_blocked=True`). Fade /
  non-chase setups on other names can still suggest.
- Also blocks **multi-session run-ups** (`options_max_runup_chase_pct`, default
  8% over `options_chase_runup_sessions` sessions): flat today but +10% on the
  week is still an extension.
- Day % uses **live Yahoo quote** (`regularMarketPrice` / `previousClose`), not
  movers daily bars (those can miss today's incomplete session and understate
  a +8% day).
- **Fail-closed**: missing day %, unknown `right`, or missing ticker all block.
- A blocked rec has its contract stripped (`right`/`strike`/`expiry` → None,
  original kept in `blocked_contract`) and is **silent** by default
  (`options_notify_chase_blocked=false`) — otherwise the hourly HOLD ping still
  named the contract and read like a suggestion.
- `day_moves` / `runups` are passed into **both** LLM passes; the `ranked` list
  is sanitized so chase names cannot carry `bias: BUY_TO_OPEN`.
- **ProGo** (Larry Williams, used by Rosputnia on stocks/futures): daily
  professional-vs-public overlay. `accumulation` confirms a long (desks buying
  the close); `distribution` skips new tilt entries and haircuts the LLM-path
  quant score. Config: `PROGO_ENABLED`, `TILT_SKIP_PROGO_DISTRIBUTION`.
  The options chase gate also records a same-session gap/grind split in
  `shadow` mode — that is *not* her stock strategy, just a related filter.

### Multiple stock strategies, sleeved cash
- Two independent stock engines run in parallel and each sends its **own**
 notification. Both use the same `portfolio` document, but **sleeves** cap
 how much of NAV each may invest: **30% Tilt (`TILT_SLEEVE_PCT`)** and
 **70% Connors (`MR_SLEEVE_PCT`)**. Tilt equal-weights top-N against its
 $300 sleeve (~$30/name). Connors sizes 33% of its $700 sleeve per slot
 (~$231 × 3). Confirm-time `execute_recommendation` rejects a BUY that
 would breach that engine's sleeve.
 - **Momentum Tilt** (`app/tilt.py`, `TILT_ENABLED`) — buys strength, monthly
 rebalance of the top-N by momentum, 200d trend-brake SELLs. Holds months.
 Leveraged/inverse single-stock ETFs are stripped unless `TILT_ALLOW_LEVERED`.
 - **Connors Swing** (`app/mean_reversion.py`, `MEAN_REVERSION_ENABLED`) —
 short-term swing, **not** a day trade. Holds 1–7 sessions. Two setups from
 *Short Term Trading Strategies That Work*:
 - Index ETFs (SPY/QQQ/IWM): **Double 7s** — 7-day low in, 7-day high out,
 above the 200d SMA.
 - Stocks: **RSI(2)** — above 200d, below 5d, RSI(2) < 10, at least two
 down closes. Exit 5d reclaim, RSI(2) > 70, or a 7-session time stop.
 - VIX overlay: skip *new* longs when VIX is stretched below its 10-day MA
 (complacency). Elevated VIX is a green light.
 - Universe is liquid large-caps + those three ETFs; 2x products are excluded.
 Default size is 33% of the **Connors sleeve** per slot × 3 slots. ProGo is off here.
 Runs **once daily at `MR_RUN_HOUR:MR_RUN_MINUTE`** (15:45 ET).
- They are intentionally **opposite in style** (trend-following vs mean
 reversion) so their drawdowns don't line up. Connors' own guidance is that
 RSI(2) belongs in a multi-strategy book, not used alone — it sits in cash a
 lot and its edge is win rate, not compounding.
- **Ownership (`app/strategy_book.py`)**: a ticker is *claimed* by a strategy
 when one of its recommendations is executed, and released when the position
 closes. Without this the tilt would rank-exit a name RSI(2) just bought and
 the ATR engine would stop it out before the 5d-SMA exit fired. So:
 - tilt ignores foreign-claimed tickers for ranking, entries, and both SELL paths
 - the ATR exit engine (`app/exits.py`) skips foreign-claimed tickers
 - RSI(2) only manages what it claimed, and never enters a name already held
 - claims are reconciled against real positions each run, so manual sells
 outside the app don't leave a ticker stuck
- Reads **fail open**: if Mongo is unavailable, "nobody owns anything", which
 restores single-strategy behaviour rather than freezing trades.
- Sleeve math lives in `app/strategy_book.py::sleeve_state`. Each engine
 sizes against its budget; leftover cash in one sleeve is not spent by the other.
- Every alert is prefixed with the engine name (`[Connors Swing] BUY AAPL ($231)`)
 and `/desk` shows a Strategy line plus sleeve %. `GET /strategies` lists what's
 live, each sleeve, and which tickers each engine owns.
- Endpoints: `GET /mean-reversion/plan` (dry run), `POST /mean-reversion/run`.

### Stock trading pause
- `STOCKS_TRADING_ENABLED=false` (default) pauses stock trading. Options keep
  running. Set `true` to re-enable.
- Gated at the **engine** level (not just callers): `run_tilt_rebalance`,
  `run_exit_monitor`, `run_day_wrap`, plus `_maybe_analyze`, `_tilt_job`,
  `_position_monitor_job`, `_day_wrap_job`, `trigger_tilt_now`, and the
  `/analyze`, `/tilt/rebalance`, `/exits/run`, `/day-wrap` endpoints (403).
- While paused, `start_scheduler` registers only options + watchdog jobs.
