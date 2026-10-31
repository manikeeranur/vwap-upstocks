# Execution architecture and operating contract

The application is a modular monolith. FastAPI routes validate commands and bind the
signed-in user. The stock service orchestrates independent paper/broker portfolios;
pure rules live in stocks/domain.py, deterministic cash/cost-basis replay in
stocks/accounting.py, transactional persistence in stocks/store.py,
and broker evidence/limit-order adapters in execution_brokers.py. Intraday scans,
broker reconciliation, optional statistics and monthly ranking downloads have
separate task lifecycles. The application still requires one process per SQLite
database; its OS process lock enforces this.

## Ownership and capital

A stock portfolio belongs to (user, broker, strategy). Paper has an independent
ledger. A live portfolio also binds to the broker's authenticated client ID.
Changing the broker login never transfers a portfolio. Manual holdings are not
imported. Shared-symbol orders, pledged/financed holdings and quantity mismatches
stop automation for the affected portfolio.

All ledger amounts are integer paise. Buying is bounded by the configured usable
capital, remaining strategy cash and fresh broker cash. The broker receives a LIMIT
order rounded to the current instrument tick. A 1% plus ₹50 provision per executed
side is retained for costs. This is a conservative budgeting allocation, not an
actual contract-note fee or a promise of exact net returns. The UI labels it as a
provision; profits do not automatically raise usable capital. Reviewed statement
charges replace provisions through deterministic replay, including after partial
or complete exits. Exact net P&L stays unavailable while any filled order lacks
final charges. A fee adjustment that exhausts cash exposes a shortfall and blocks
new purchases instead of hiding the debt.

Paper uses the same bounded allocation rules. Consequently its results should not
be presented as the older, fully compounding historical backtest.

## Order lifecycle

Reserve an immutable unique reference in SQLite before contacting a broker.
Submission acknowledgement does not spend cash or create a position. Poll the
broker order book, match the order reference/ID, account, instrument, product, side
and quantity, then apply cumulative fills and cash movements in one transaction.
Repeated polling is idempotent. Partial fills preserve the unsold balance. A new
purchase cannot replace a remaining holding or bypass an unresolved exit.

A timeout, crash or failed acknowledgement persistence never triggers a blind
resubmission. Reconciliation continues for disabled portfolios. The dedicated
Stocks page shows pending/unknown orders and offers reconciliation that leaves
entries paused. Orders absent from the broker's current-day book remain unresolved;
absence is not proof of rejection. Exact-ID order-detail lookup is attempted;
intraday history retries have a time budget and five-minute backoff so missing
history cannot monopolize reconciliation. The statement workflow below resolves
identified historical intents without guessing from symbols or dates.

Cancelled/rejected monthly rotations stop that cycle; no automatic replacement
order is submitted. Unfilled DAY limit orders can require review at the broker.
The Exit recovery panel requires an explicit approval, terminal broker status,
fresh ownership checks and the remaining quantity. Stock recovery requests expire
after seven days, can be cancelled before submission, and leave entries paused.
The original order stays in the ledger; a linked child intent reserves a unique
replacement. A further rejection needs another explicit approval.

Intraday recovery archives each terminal attempt before reserving its remainder.
The projection aggregates confirmed quantities and weighted prices across attempts,
including after restart. Unknown/open attempts cannot be replaced. A broker
position or manual-contract mismatch prevents the replacement.

## Controls

- Pause entries: prevents new purchases; normal exits continue.
- Enable portfolio: enables that portfolio's scheduled decisions. Disabling stops
  new buys and sells; reconciliation of existing orders continues.
- Global Pause entries: applies to all of the user's live intraday and stock
  portfolios, including reservations made while another request is in progress.
- Budget edits: allowed only when flat with no unresolved orders.
- Settings writes use revision checks. A stale browser cannot overwrite newer settings. The form retains the revision from the first edit even while background status polling continues.

A flat portfolio can enter the current paper holding or a selected saved tuned
backtest when enabled. This alignment uses a verified market session between
09:30 and 15:00 IST, fresh quotes and the same usable-capital limit. A saved
backtest must match every tuned parameter and be complete through the ranking's
latest completed session. Paper alignment waits for its current monthly decision
and any due paper entry/exit. Cash-only sources stay in cash. This does not adopt
existing manual shares. The normal monthly schedule resumes after alignment.

Each enabled portfolio decides independently on the first eligible monthly
session. Missing/incomplete ranking data and a missing held-stock ranking block
decisions. Entry/exit quotes require a recent exchange trade timestamp. Ranking
downloads run separately from broker reconciliation.

## Upgrade and recovery

Back up SQLite before deployment using the existing maintenance command:

    uv run upstocks-db backup /absolute/path/to/pre-upgrade.sqlite3

Keep the backup outside the release checkout and keep the encryption key with the normal protected backups. Stop the application before an offline migration. Do not restore a pre-upgrade snapshot over newer broker activity. Schema
migration 7 adds portfolio, position, order, fill and append-only event tables.
Migration 8 adds entry sources, linked recovery intents, reviewed statement
documents, final-charge fields, legacy ownership claims and archived intraday
attempts. Neither migration overwrites manual broker holdings. Upgrade a backup
copy first, checking integrity and foreign keys, then migrate the production
database during the normal stopped-service release procedure.

Legacy global momentum JSON is retained unchanged. If it contains live orders or
positions, new live portfolios start disabled and blocked: old records cannot
establish account ownership or actual fills. Compare those records with broker
statements before using the reviewed import. Every identified legacy order for
that broker must be supplied together to an empty ledger. A global unique claim
prevents two accounts adopting the same legacy intent. A legacy record with no
unique broker order ID or incomplete intent evidence remains blocked; holdings
alone are insufficient. Ordinary reconcile cannot clear the legacy guard.

Legacy intraday orders lacking confirmed quantities/status also block new entries
until broker evidence is available. Reconnect the same broker account and inspect
the unresolved-order alert. Never infer a completed trade solely from an order ID.

Readiness reports scanner, reconciliation, momentum and reminder worker health.
Historical journal/calendar history includes fully closed confirmed quantities with recorded fill prices even when final charges are pending. When any fee evidence is missing the selection uses gross P&L consistently for daily colors, result filters and performance metrics, with net unavailable. Once all displayed trades have saved fees, the selection uses net P&L, explicitly labeled estimated until statement charges are available. Open, partial and unresolved executions are excluded, never filled using simulated prices. Broker history is scoped to the signed-in user.

Portfolio-specific failures remain visible in Stocks and the Overview. If storage
fails after submission, keep automation blocked and reconcile the durable intent
against the broker before resuming.

## Statement workflow

1. Select the live broker portfolio in Stocks and connect that same broker login.
2. Download the CSV template. Copy only the existing strategy order IDs and exact
   instrument/side/quantity/date evidence from the broker's statement or contract
   note. Use one row per order with cumulative fills; amounts are in rupees.
   Leave final charges blank when unavailable. Never substitute fee estimates.
3. Add the document reference and preview. Matching is account-scoped and requires
   an existing unique intent. Unknown/manual/ambiguous orders and inconsistent
   terminal status or decreasing fill quantity are rejected.
4. Review every row, then apply. The preview expires after 30 minutes. Any relevant
   execution/settings change requires a new preview. All evidence, charges,
   projections and audit records commit together or roll back together. Reapplying
   an identical document is idempotent. Its digest and normalized rows are retained.
5. Reconcile current broker ownership before resuming stock entries. Imported
   evidence is explicitly operator-verified; it is not a broker API attestation.

Archived intraday attempts are amendable through their own exact broker IDs;
the immutable archive remains, while the audited projection includes corrected
fills/charges. Stock cash and cost basis replay in chronological intent order,
using insertion order to break equal timestamps.

~~~mermaid
flowchart LR
    UI[Stocks / Overview] --> API[Validated user-scoped commands]
    API --> S[Portfolio execution service]
    API --> R[Statement review service]
    S --> B[Broker evidence and order adapters]
    S --> L[SQLite intent / fill / event ledger]
    R --> L
    L --> A[Deterministic accounting]
    A --> UI
    I[Intraday scanner] --> O[Attempt reservation and reconciliation]
    O --> B
    O --> L
~~~

Live credentials are never required by the automated tests. Production connection
checks are read-only and cannot validate execution at the exchange. A release
must preserve unresolved historical records until evidence is available; empty
current-day books must not be used to mark older orders cancelled or complete.

## Validation

Run:
- uv sync --locked
- uv run ruff check src tests
- uv run mypy (execution, persistence and reconciliation; stricter rules for money/fill calculations)
- uv run python -m unittest discover -s tests
- node --test tests/test_reporting.cjs

No test requires a production broker token or submits an order. Broker contract
fixtures cover acknowledgement, partial fill, rejection, unknown status, identity
and instrument mismatch, limit-price payloads, and ownership checks.

## Broker contract references

- Kite orders: https://kite.trade/docs/connect/v3/orders/
- Kite holdings/positions: https://kite.trade/docs/connect/v3/portfolio/
- Kite cash: https://kite.trade/docs/connect/v3/user/
- Upstox cash-only funds: https://upstox.com/developer/api-documentation/get-funds-and-margin-v3/
- Upstox holdings: https://upstox.com/developer/api-documentation/get-holdings/
- Upstox positions: https://upstox.com/developer/api-documentation/get-positions/
- Upstox quotes: https://upstox.com/developer/api-documentation/get-full-market-quote/
- Upstox instrument ticks: https://upstox.com/developer/api-documentation/instruments/
- INDstocks order lifecycle: https://api-docs.indstocks.com/normal_orders/
- INDstocks holdings/positions: https://api-docs.indstocks.com/portfolio_funds/
- INDstocks equity cash: https://api-docs.indstocks.com/Users/

These references document the adapter contracts. Real broker acceptance, liquidity,
depository authorization and exchange behaviour must still be verified in a staged
rollout; mock tests are not a live-trading certification.


## Review fixes and boundaries

The review changes cover account-scoped momentum configuration, confirmed fill
accounting, partial-exit preservation, global pause, usable-capital limits, durable
order intents and restart recovery, incomplete ranking guards, separate paper/live
reporting, persistent errors, session labels, the dedicated Stocks workspace,
explicit save feedback, normalized stock storage, worker readiness, independent
strategy processing, frontend modules and automated checks.

The strict type check currently covers the pure stock money/decision rules.
Repository-wide lint checks target critical syntax and undefined-name errors;
existing modules have not all been converted to strict typing.

The rollout still requires a database backup and review of any blocked legacy
orders against broker records. There is deliberately no automatic adoption of
manual holdings, no forced fill completion, and no automatic resubmission of an
uncertain or cancelled rotation. New stock portfolios are separate from the old
simulated portfolio; the original JSON is retained for audit and comparison.

## Automatic live charges

Kite virtual contract notes, Upstox brokerage calculations, and INDmoney's margin
calculator supply estimates for the actual filled quantity and weighted price.
Each saved estimate includes its broker order/account identity, cumulative quantity,
notional, request metadata, breakdown and calculation time. A changed fill invalidates
the previous estimate. Partial/cancelled attempts and replacement exits keep separate
fees. Repeated reconciliation does not charge an order twice. Missing or failed
responses remain unavailable and retry with backoff; they never become zero.

Overview, journal and calendar use the same saved fees. Net results based on these
calculators are explicitly estimated. Statement-confirmed charges override estimates.
If any selected closed trade has no fee evidence, historical totals show gross with
net pending instead of mixing gross and net. Overview realized totals cover only
closed confirmed trades and disclose excluded open/unresolved orders.

Stock fee estimates are append-only events used for reporting estimated net returns.
They never release conservative execution cash provisions or change usable capital.
Delivery-sale DP charges and other back-office adjustments may only appear on final
statements. Calculator requests have no historical tariff-date input, so backfilled
calculations retain their calculation timestamp and remain estimates.

References: Kite https://kite.trade/docs/connect/v3/margins/#virtual-contract-note ;
Upstox https://upstox.com/developer/api-documentation/get-brokerage/ ;
INDmoney https://api-docs.indstocks.com/margin_calculation/ .
