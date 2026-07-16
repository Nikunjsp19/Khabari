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
- Tests: from `python-service/`, `pytest -q`. Note: `tests/test_gates.py::test_fingerprint_stable`
  fails on a **pre-existing** bug unrelated to any current change (it expects two
  different tickers sharing the same article `uuid` to hash differently, but
  `fingerprint_article` intentionally keys off `uuid` when present).
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
  often too optimistic because premium already prices much of the move.
- The pipeline still **may suggest** the trade: `apply_options_chase_gate` adds a
  chase caution, haircuts confidence, and bumps risk to HIGH — it does **not**
  force HOLD. When advising the user, surface that caution clearly (live day % +
  moneyness/DTE); don’t sell it as free leftover upside.
