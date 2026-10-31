# Upstocks

A local NIFTY trading workspace with paper/live strategy scanning, historical backtests, a trade journal, and CSV/PDF exports. Python 3.12+, FastAPI, Jinja, and SQLite. The interface includes a live IST clock.

## Run

```bash
uv sync --locked
cp .env.example .env  # first setup only; keep an existing .env
# Set UPSTOX_ACCESS_TOKEN in .env.
uv run --locked upstocks
```

Open http://127.0.0.1:3400/paper. With an existing environment, `.venv/bin/python -m upstocks.app` also works. Configuration is read from `.env`, with environment variables taking precedence. See [.env.example](.env.example) for all options.

Use **one process / one worker** and a database on local disk. An OS file lock prevents two instances from scanning against the same database. In WSL, keep it under the Linux home directory; do not place the database on a network share. The application remains a local, single-user tool: internet-facing or multi-user deployment requires authentication, authorization, TLS, and a deployment-specific security review.

## Architecture

```text
Browser / Jinja templates
        |
api/                 HTTP routes and response contracts
schemas.py           Validated command inputs
        |
services/trading.py   Scanner, live-order decisions, lifecycle
services/backtests.py Bounded background jobs
services/quotes.py    Shared one-second LTP reads for open trade display
        |                         |
repository.py        Transactional state, journal, jobs, audit
database.py          SQLite connections, migrations, ownership, backup
        |
data/upstocks.sqlite3

live.py              Stateless strategy helpers
vwap.py / smc.py / ema_pullback.py   Strategy calculations
upstox.py            Broker gateway, throttling, reusable market cache
app.py               Composition root, startup/shutdown, error handling
observability.py     JSON log formatting, redaction, rotation
middleware.py        Request IDs, timing, cache headers
security.py          Local host, same-origin, and command-token checks
diagnostics.py       Activity queries and consistent session archives
```

The strategy calculations are preserved. Routers resolve services from application state; trading state and historical results no longer depend on module-level dictionaries. Request schemas reject invalid quantities, nonfinite numbers, malformed dates/times, and string booleans. Enabling real orders still requires `confirm: "LIVE"`.

Open paper/live strategy trades display fresh LTP and running P&L once per second while the Overview is visible and the market is open. `GET /api/paper/quotes` batches and deduplicates open instruments, shares in-flight requests across tabs, and caches each second's response. It uses [Upstox LTP V3](https://upstox.com/developer/api-documentation/ltp-v3/) with a separate one-request-per-second limiter, consistent with Upstox's [per-API rate limits](https://upstox.com/developer/api-documentation/rate-limiting/), so historical jobs cannot consume the quote budget. Network latency or broker limits can delay updates; failures retain the last price with a delayed marker and retry backoff.

Entry, stop-loss, target, closed-trade exits, and saved strategy results remain unchanged by quotes. Running P&L is calculated from the strategy entry, not a confirmed broker fill. Only numeric price/P&L fields are patched each second, so typing into settings and scrolling tables are uninterrupted. Full state refreshes every ten seconds or when a new scan is detected. Historical replays and closed trades keep their recorded values. No broker quote requests are made without current open trades, outside market hours, or when no client requests updates. The price tooltip reports receipt time because LTP V3 does not include an exchange timestamp. These display updates do not run strategy scans, trigger orders, or write tick data to the journal/evidence store.

## Momentum rotation (NSE equities)

`/momentum` backtests a monthly rotation over NSE EQ stocks (`momentum.py`, run by `services/momentum.py`). At each month's last trading day it takes the 500 stocks with the highest 6-month median turnover (with at least 1 year of history), scores each by the average of the z-scored 6- and 12-month return ÷ 1-year annualised volatility, sells holdings ranked worse than 20 or closing below their 100-day average, and fills the free slots from the top of the ranking (skipping held stocks and stocks below their average). Orders fill at the next open, cash ÷ free slots per buy, whole shares, 0.1% per side. All rule numbers are editable on the page.

Daily prices come from Upstox (split/bonus-adjusted) and are cached per stock in `.cache/equity/*.npz`. The first run downloads all ~2,350 company shares (ETFs, ISIN INF…, are excluded) (one request each; 30–45 minutes under Upstox's 2,000/30-min budget). Later runs extend only the top 1,000 by turnover plus files more than 45 days old; *Update prices for every stock* forces all. When an update's overlap day disagrees with the cached close (a later split/bonus), that stock is re-downloaded in full. During market hours bulk downloads stay under 200/min and 900/30 min so the live scanner keeps its budget. The stock list is today's listings, so delisted companies are missing (survivorship bias). The last result is kept in `data/momentum_last.json`; one run at a time.

## Controls and session diagnostics

Patterns reviewed in the local `rams-strategy` app were adapted to this Upstox candle-scanning workflow: searchable activity, session archives, append-only decision records, revision-checked configuration, and local dashboard boundaries. Its Kite adapter, streaming execution engine, strategy rules, credentials, and historical data were not copied.

**Pause entries** on Overview persists across restarts and blocks new live BUY reservations. Paper scans and exits for existing submitted buys continue. It does not cancel orders already reserved or sent. Resuming preserves each strategy's Live setting and requires explicit confirmation when a strategy is live.

Every configuration command supplies the `revision` returned by `/api/paper`. A stale revision receives HTTP 409 with refreshed state; the UI asks the user to review and retry. Database comparison and update occur in one transaction. Resuming entries through the API also requires `confirm: "LIVE"`.

**Activity & logs** filters events by IST date, severity, source, message, or request ID. HTTP requests are hidden by default. Pagination uses event IDs; session totals and ZIP exports include all records for the date, regardless of the visible filters. The screen reads saved data and refreshes on demand.

**Export session** creates a ZIP from one read-only SQLite snapshot on a worker thread. It contains `manifest.json` plus JSON Lines files for events, decision evidence, order attempts, journal trades/notes, and backtest results. Events use their actual occurrence date in IST; trades and evidence use the strategy session date. No broker requests are made by export. Credentials and the dashboard token are excluded. The manifest explains these boundaries.

Schema migration 2 preserves existing records and adds event severity/source, configuration revisions, the entry gate, and decision evidence. Scan snapshots capture results and current configuration; order intents capture the trade and configuration before broker submission. Outcomes and restart recovery append separate records. Evidence includes a process run ID and hashes of the strategy/execution source files. SQLite triggers prevent ordinary updates/deletes of evidence, but this is not a tamper-proof audit store against a database administrator. Evidence begins with this upgrade; old signals are not reconstructed. Saved candle-scan evidence is not a streaming tick log or a broker fill confirmation.

The browser adds a dashboard command token to every JSON mutation. The server verifies it, rejects cross-origin actions, restricts hostnames to `UPSTOCKS_ALLOWED_HOSTS`, and limits command bodies to 1 MiB, including chunked bodies. The default hostnames are loopback only. The token persists with this local database so a restart does not invalidate open tabs. These checks protect local browser actions; they are not authentication. Keep the server bound to loopback unless adding a properly authenticated deployment boundary.

## Storage and migration

The default database is `data/upstocks.sqlite3`. WAL mode supports readers during writes; foreign keys, a busy timeout, `synchronous=FULL`, and short transactions protect committed state. Each operation owns and closes its connection. Blocking database work runs outside the async event loop.

The schema separates strategy configuration, order attempts, journal sessions/trades, notes, backtest jobs/day results, audit events, and version metadata. Flexible strategy result details are JSON payloads within indexed relational records. Business changes and their audit entries commit in the same transaction. Audit records are retained until deliberately archived; file-log retention is separate.

On first startup:

1. Apply versioned schema migrations.
2. Import `live_state.json` (or `paper_state.json` if live state is absent) and `journal.json`.
3. Preserve the original files and make checksum-named copies in `data/legacy/`.
4. Record a one-time migration marker. Later startups read SQLite only.

Malformed legacy state fails startup instead of resetting trading settings. Repair the original JSON and restart; an incomplete import rolls back. Existing credentials stay in `.env`. Historical candle cache files remain under `.cache/` and can be rebuilt. New databases, logs, and legacy backups are excluded from Git. Previously tracked JSON files are left untouched.

For a controlled migration without starting the scanner, stop the old server and run:

```bash
.venv/bin/python -m upstocks.maintenance migrate
```

Do not run the old JSON-based server beside the new app. Migration preserves existing live-mode choices; it does not enable live trading for a fresh installation.

## Order and job recovery

An order attempt is committed **before** contacting the broker. A unique `(trade_id, side)` constraint prevents duplicate submissions, including concurrent requests and restarts. A broker order ID means **submitted**, not filled. Exits use the original buy quantity even if settings change.

If a request times out, is interrupted, or lacks an order ID, its outcome is `unknown`; it is never automatically resent. Pending attempts become unknown on restart. Check the broker's order book and positions before taking further action. Broker status and fill reconciliation is implemented; unresolved historical outcomes require broker evidence. See docs/execution-safety.md. Local SQLite cannot provide an exactly-once guarantee across the broker's network boundary.

Backtests save every completed day. Completed jobs and exports survive restart and are accessible from **Saved runs** on the strategy backtest screen. Interrupted jobs retain partial results and show an error; rerun explicitly when appropriate. The default limit is two concurrent jobs, each processing up to three days concurrently under the shared broker rate limiter. Extra jobs receive HTTP 429.

Shutdown stops the scanner, cancels outstanding jobs, records interrupted work, and closes the HTTP client. `UPSTOCKS_SCANNER_ENABLED=false` disables automatic scanning; explicit scan and order controls remain available.

## Logs and health

Files in `logs/` are JSON Lines, with UTC timestamps:

- `application.jsonl`: lifecycle, strategy, broker, and job events.
- `access.jsonl`: method, route template, status, duration, request ID.
- `error.jsonl`: errors and redacted exception messages/stack locations, without source lines or local variables.

Each rotates at 10 MiB with five retained backups by default. `X-Request-ID` connects an HTTP response to its logs and database audit records. Access logs omit query strings, request headers, and bodies. Known token values, bearer tokens, and named credential fields are redacted. Logs and backups still contain trading information; protect them with local file permissions.

`GET /health/live` checks the HTTP process. `GET /health/ready` checks database access and scanner lifecycle, and reports active jobs, entry pause state, orders needing review, and HTTP audit-write failures since startup. The Activity screen highlights these failures while the file logs retain details. A healthy response does not certify broker availability, order fills, or data freshness. Upstox failures remain visible in the trading UI.

The legacy `upstocks.log` file is no longer the application log destination. External process supervisors may separately capture console output.

## Backup, check, and restore

```bash
.venv/bin/python -m upstocks.maintenance check
.venv/bin/python -m upstocks.maintenance backup data/backups/upstocks-2026-10-12.sqlite3
```

The backup command uses SQLite's online backup API, includes committed WAL data, validates the copy, and refuses to overwrite a destination. It works while the app is running. Use a new filename each time, and copy backups to protected storage on another device if disaster recovery is required. Backups are manual; no scheduler is installed.

To restore, stop the application, retain the current database and its `-wal`/`-shm` files together as a recovery copy, and restore a verified backup to a **new path**. Point `UPSTOCKS_DATABASE_PATH` at it and run `check` before starting. Review broker positions and turn live trading off before resuming from an older backup: the broker may have accepted orders after that backup was made. Never copy only a running database's main file or discard its WAL.

## Validation

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary databases and mocked broker calls. They cover migration preservation and rollback, WAL backups, concurrent order reservation, interrupted orders/jobs, restart persistence, entry-pause and exit behavior, original exit quantities, concurrent configuration revisions, input validation, page rendering/JavaScript parsing, complete snapshot exports, IST date boundaries, log redaction/rotation, request correlation, host/origin/token checks, and chunked-body limits. No live orders are placed by the tests.


## Stock workspace and execution safety

Stocks now has a dedicated /stocks workspace with account-scoped paper and live
portfolios, usable-capital limits, explicit save/revision controls, and durable
order/fill history. A broker acknowledgement is not a fill. Pending outcomes are
reconciled before further orders, and the global entry pause also covers stocks.

Overview reports confirmed realized P&L separately from estimated open-position
valuation. Net values require complete charge evidence. Runtime JSON from the old
momentum implementation is retained for migration review, never silently adopted.

Read [Execution architecture, upgrade and recovery](docs/execution-safety.md)
before deploying this branch. It documents data ownership, cash provisions,
migration guards, limitations and repeatable validation commands.

The entry-source selector can align a newly enabled live portfolio with its current
paper holding or a fresh saved tuned backtest. The Exit recovery panel replaces
only confirmed cancelled/rejected exits for the verified remaining quantity.
Historical fills and final charges use an account-scoped CSV preview/apply flow;
broker fee estimates never count as exact net P&L. These additions use schema 8.

## Exchange calendar and login reminders

The P&L calendar marks weekends, NSE/BSE trading holidays, and special sessions.
Selecting a day shows its holiday name and equity/F&O hours in IST. Strategy
filters select the relevant exchange. Recorded trades and P&L remain visible
regardless of a date's calendar classification.

The shared calendar service reads the public
[Upstox holiday feed](https://upstox.com/developer/api-documentation/get-market-holidays/)
without broker credentials. A verified 2026 snapshot ships with the app.
Successful refreshes are saved under exchange_calendar in SQLite settings;
the current year refreshes on calendar requests and scheduled reminder checks,
at most once every six hours. Failed refreshes retain the last good snapshot
and back off for 15 minutes. Missing years are explicitly shown as unavailable;
weekends can still be displayed. Unknown calendars suppress automated login
reminders until verified data becomes available.

Telegram login reminders and automatic Upstox approval requests run only on
trading weekdays. Weekends stay quiet even when a special session is scheduled.
Settlement-only holidays with open equity/derivative exchanges still count as
trading days. Overview and stock pages suppress expired-login nags on closed
days, while unresolved-order and other operational problems remain visible.
Explicit broker login and reconciliation controls remain available.

Calendar metadata does not enable special-session trading or change execution,
order ownership, holdings, fill accounting, or the existing market-data checks.

## Paper and Live data views

Overview, Journal, Calendar and Stocks share a Paper/Live view selector. The
choice and broker filter persist within the browser tab and travel in page links.
Changing the view never changes trading settings or enables orders. Paper scan
and replay requests are simulation-only, including scans for today; the background
worker remains responsible for explicitly enabled live strategies.

Paper intraday results use one simulated lot per trade, independent of broker
lot settings. Journal and Calendar use the quantity saved for each session.
Live Overview reads all saved, account-scoped broker orders, so earlier open
positions remain visible after a paper replay. It shows confirmed realized gross
P&L, estimated open valuation and unresolved orders separately. Missing prices or
final charges display as unavailable, never as paper fills or exact zero profit.

Live Journal and Calendar include fully settled broker trades with final charges,
even when their original paper scan is missing. Broker filters apply consistently.
Stocks keeps separate paper and broker portfolios, capital limits, holdings,
orders and activity. Switching away from unsaved stock settings requires an
explicit discard. Backtests, market data, broker accounts and system activity have
fixed context labels instead of a misleading mode switch.

Run the reporting and mode regression checks with:
`node --test tests/test_reporting.cjs tests/test_workspace_views.cjs tests/test_exchange_calendar.cjs`.
