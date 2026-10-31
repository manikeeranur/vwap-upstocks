"""Transactional portfolio ledger. No transaction spans a broker call."""

import json
import uuid

from ..database import utc_now
from ..fees import valid_estimate, stock_attempt
from .accounting import balances
from .domain import PortfolioError, StaleRevision, TERMINAL, fee_provision


class StockStore:
    def __init__(self, database):
        self.db = database

    @staticmethod
    def event(c, pid, kind, details):
        c.execute(
            "INSERT INTO stock_events(portfolio_id,kind,details,created_at) VALUES(?,?,?,?)",
            (pid, kind, json.dumps(details), utc_now()),
        )

    def ensure(self, users, legacy=None):
        with self.db.connect(write=True) as c:
            for user in users:
                for broker in ("paper", "upstox", "kite", "indmoney"):
                    pid = f"{user['id']}:{broker}"
                    inserted = c.execute(
                        """INSERT OR IGNORE INTO stock_portfolios
                        (id,user_id,broker,enabled,paused,usable_capital,cash,entry_source) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            pid,
                            user["id"],
                            broker,
                            int(broker == "paper"),
                            int(broker != "paper"),
                            5_000_000,
                            5_000_000,
                            "monthly" if broker == "paper" else "paper",
                        ),
                    ).rowcount
                    if inserted:
                        self.event(c, pid, "CREATED", {"actor": user["id"], "cash": 5_000_000})
                    if (
                        legacy
                        and broker != "paper"
                        and (
                            broker in legacy.get("positions", {})
                            or any(o.get("broker") == broker for o in legacy.get("orders", []))
                        )
                    ):
                        marker = c.execute(
                            "SELECT 1 FROM stock_events WHERE portfolio_id=? AND kind='LEGACY_BLOCK'", (pid,)
                        ).fetchone()
                        if not marker:
                            c.execute(
                                "UPDATE stock_portfolios SET enabled=0,paused=1,blocked=? WHERE id=?",
                                (
                                    "Legacy live records have no verified owner/fills. Resolve the legacy ledger before enabling this portfolio.",
                                    pid,
                                ),
                            )
                            self.event(
                                c,
                                pid,
                                "LEGACY_BLOCK",
                                {"source": "settings.momentum_tuned", "preserved": True},
                            )

    @staticmethod
    def _portfolio(c, pid):
        row = c.execute("SELECT * FROM stock_portfolios WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise PortfolioError("Portfolio not found")
        return dict(row)

    @staticmethod
    def _position(c, pid):
        row = c.execute("SELECT * FROM stock_positions WHERE portfolio_id=?", (pid,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _orders(c, pid):
        orders = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM stock_orders WHERE portfolio_id=? ORDER BY julianday(created_at),rowid", (pid,)
            )
        ]
        latest: dict[str, dict] = {}
        for row in c.execute("SELECT details FROM stock_events WHERE portfolio_id=? AND kind='BROKER_FEE_ESTIMATE' ORDER BY id DESC", (pid,)):
            event = json.loads(row[0])
            latest.setdefault(event["order_id"], event["estimate"])
        for order in orders:
            estimate = latest.get(order["id"])
            if estimate and valid_estimate({**stock_attempt(order), "fee_estimate": estimate}):
                order["fee_estimate"] = estimate
        return orders

    def snapshot(self, user_id=None):
        with self.db.connect() as c:
            rows = c.execute(
                "SELECT * FROM stock_portfolios" + (" WHERE user_id=?" if user_id is not None else ""),
                (user_id,) if user_id is not None else (),
            ).fetchall()
            out = []
            for row in rows:
                p = dict(row)
                p["position"] = self._position(c, p["id"])
                p["orders"] = self._orders(c, p["id"])
                totals = balances(p["usable_capital"], p["orders"])
                p.update({key: totals[key] for key in ("missing_charges", "net_realized")})
                estimated_orders = [{**o, "charges_paise": o.get("charges_paise") if o.get("charges_paise") is not None
                                     else (o.get("fee_estimate") or {}).get("total_paise")} for o in p["orders"]]
                # Reporting estimates never release the conservative cash provision.
                estimated = balances(p["usable_capital"], estimated_orders)
                p["estimated_net_realized"] = estimated["net_realized"]
                p["fees_pending"] = estimated["missing_charges"]
                p["recoveries"] = [
                    dict(r)
                    for r in c.execute(
                        "SELECT * FROM stock_recoveries WHERE portfolio_id=? ORDER BY created_at", (p["id"],)
                    )
                ]
                p["plan"] = json.loads(p["plan"]) if p["plan"] else None
                p["events"] = [
                    dict(r)
                    for r in c.execute(
                        "SELECT kind,details,created_at FROM stock_events WHERE portfolio_id=? ORDER BY id DESC LIMIT 50",
                        (p["id"],),
                    )
                ]
                out.append(p)
            return out

    def configure(
        self, pid, revision, *, enabled, paused, usable_capital, client_id=None, entry_source="monthly"
    ):
        with self.db.connect(write=True) as c:
            p = self._portfolio(c, pid)
            if revision != p["revision"]:
                raise StaleRevision("Settings changed in another session. Reload before saving.")
            if enabled and p["blocked"]:
                raise PortfolioError(p["blocked"])
            if not 100_000 <= usable_capital <= 10_000_000_000:
                raise PortfolioError("Usable capital must be between ₹1,000 and ₹10 crore")
            pending = any(o["status"] not in TERMINAL for o in self._orders(c, pid))
            if usable_capital != p["usable_capital"] and (self._position(c, pid) or pending):
                raise PortfolioError("Change capital only when flat with no unresolved orders")
            if p["client_id"] and client_id and p["client_id"] != client_id:
                raise PortfolioError("This portfolio belongs to a different broker login")
            cash = p["cash"] + usable_capital - p["usable_capital"]
            if cash < 0:
                raise PortfolioError("Capital reduction exceeds remaining strategy cash")
            c.execute(
                """UPDATE stock_portfolios SET enabled=?,paused=?,usable_capital=?,cash=?,
                         client_id=?,entry_source=?,alignment_pending=?,revision=revision+1 WHERE id=?""",
                (
                    int(enabled),
                    int(paused),
                    usable_capital,
                    cash,
                    p["client_id"] or client_id or "",
                    entry_source,
                    int(
                        bool(
                            enabled
                            and entry_source != "monthly"
                            and not self._position(c, pid)
                            and not pending
                            and (
                                not p["enabled"]
                                or p["entry_source"] != entry_source
                                or p["alignment_pending"]
                            )
                        )
                    ),
                    pid,
                ),
            )
            self.revalue(c, pid)
            self.event(
                c,
                pid,
                "SETTINGS",
                {
                    "actor": p["user_id"],
                    "enabled": enabled,
                    "paused": paused,
                    "usable_capital": usable_capital,
                    "entry_source": entry_source,
                },
            )

    def plan(self, pid, plan):
        with self.db.connect(write=True) as c:
            c.execute("UPDATE stock_portfolios SET plan=? WHERE id=?", (json.dumps(plan), pid))
            self.event(c, pid, "DECISION", plan)

    def block(self, pid, reason):
        with self.db.connect(write=True) as c:
            p = self._portfolio(c, pid)
            if p["blocked"] == reason:
                return
            c.execute(
                "UPDATE stock_portfolios SET blocked=?,paused=1,revision=revision+1 WHERE id=?", (reason, pid)
            )
            self.event(c, pid, "BLOCKED", {"reason": reason})

    def recovered(self, pid):
        with self.db.connect(write=True) as c:
            p = self._portfolio(c, pid)
            if p["blocked"] and p["blocked"].startswith("Legacy"):
                raise PortfolioError(p["blocked"])
            if any(o["status"] not in TERMINAL for o in self._orders(c, pid)):
                raise PortfolioError("An unresolved order still needs broker confirmation")
            c.execute(
                "UPDATE stock_portfolios SET blocked=NULL,paused=1,last_error=NULL,revision=revision+1 WHERE id=?",
                (pid,),
            )
            self.event(c, pid, "RECONCILED_PAUSED", {"actor": p["user_id"]})

    def health(self, pid, error=None):
        with self.db.connect(write=True) as c:
            c.execute(
                "UPDATE stock_portfolios SET last_error=?,last_checked=? WHERE id=?", (error, utc_now(), pid)
            )

    def reserve(self, pid, cycle, side, instrument, qty, limit, client_id, parent_id=None):
        with self.db.connect(write=True) as c:
            p = self._portfolio(c, pid)
            if p["blocked"] or not p["enabled"] or (side == "BUY" and p["paused"]):
                raise PortfolioError("Portfolio disabled, paused or blocked")
            user = c.execute("SELECT entries_paused FROM users WHERE id=?", (p["user_id"],)).fetchone()
            if side == "BUY" and p["broker"] != "paper" and user[0]:
                raise PortfolioError("All live entries are paused for this user")
            orders = self._orders(c, pid)
            if any(o["status"] not in TERMINAL for o in orders):
                raise PortfolioError("Reconcile the existing order first")
            if any(o["cycle"] == cycle and o["side"] == side for o in orders):
                return None
            pos = self._position(c, pid)
            parent = next((o for o in orders if o["id"] == parent_id), None) if parent_id else None
            if parent_id and (
                not parent
                or parent["side"] != "SELL"
                or parent["status"] not in ("cancelled", "rejected")
                or side != "SELL"
                or not pos
                or parent["isin"] != pos["isin"]
                or qty != pos["qty"]
                or any(o["parent_id"] == parent_id for o in orders)
            ):
                raise PortfolioError("Replacement exit is not eligible")
            if qty <= 0 or limit <= 0:
                raise PortfolioError("Quantity and limit must be positive")
            reserve = qty * limit + fee_provision(qty * limit) if side == "BUY" else 0
            if side == "BUY" and (
                pos or p["cash_shortfall"] or reserve > min(p["cash"], p["usable_capital"])
            ):
                raise PortfolioError("Insufficient allocated cash or an existing holding")
            if side == "SELL" and (not pos or pos["isin"] != instrument["isin"] or qty > pos["qty"]):
                raise PortfolioError("Sell exceeds the strategy-owned holding")
            if p["broker"] != "paper" and (not client_id or client_id != p["client_id"]):
                raise PortfolioError("Broker account identity changed")
            oid, stamp = "mt" + uuid.uuid4().hex[:18], utc_now()
            c.execute(
                """INSERT INTO stock_orders(id,portfolio_id,cycle,side,symbol,isin,instrument_key,qty,
                         limit_paise,reserved,client_id,status,created_at,updated_at,parent_id,origin_cycle)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)""",
                (
                    oid,
                    pid,
                    cycle,
                    side,
                    instrument["symbol"],
                    instrument["isin"],
                    instrument["key"],
                    qty,
                    limit,
                    reserve,
                    client_id,
                    stamp,
                    stamp,
                    parent_id,
                    (parent["origin_cycle"] or parent["cycle"]) if parent else cycle,
                ),
            )
            self.event(c, pid, "ORDER_INTENT", {"id": oid, "side": side, "qty": qty, "reserved": reserve})
            return dict(c.execute("SELECT * FROM stock_orders WHERE id=?", (oid,)).fetchone())

    def submission(self, oid, broker_id=None, error=None):
        with self.db.connect(write=True) as c:
            row = c.execute("SELECT * FROM stock_orders WHERE id=?", (oid,)).fetchone()
            status = "submitted" if broker_id else "unknown"
            c.execute(
                "UPDATE stock_orders SET broker_id=?,status=?,error=?,updated_at=? WHERE id=?",
                (broker_id, status, error, utc_now(), oid),
            )
            self.event(
                c,
                row["portfolio_id"],
                "ORDER_SUBMISSION",
                {"id": oid, "status": status, "broker_id": broker_id, "error": error},
            )

    def apply(self, oid, update):
        """Apply cumulative fills exactly once, including after restart."""
        with self.db.connect(write=True) as c:
            self._apply(c, oid, update)
            pid = c.execute("SELECT portfolio_id FROM stock_orders WHERE id=?", (oid,)).fetchone()[0]
            self.revalue(c, pid)

    def _apply(self, c, oid, update):
        order = dict(c.execute("SELECT * FROM stock_orders WHERE id=?", (oid,)).fetchone())
        p = self._portfolio(c, order["portfolio_id"])
        filled, notional, status = update["filled_qty"], update["notional"], update["status"]
        if filled < order["filled_qty"]:
            return
        if filled > order["qty"] or filled < 0 or notional < 0 or (filled and not notional):
            raise PortfolioError("Invalid cumulative broker fill")
        if status == "complete" and filled != order["qty"]:
            raise PortfolioError("Completed order has an inconsistent filled quantity")
        if order["status"] in TERMINAL and status != order["status"]:
            raise PortfolioError("Broker terminal status changed; review required")
        if order["side"] == "BUY" and notional > order["limit_paise"] * filled:
            raise PortfolioError("Broker fill exceeds the reserved limit price")
        delta, value = filled - order["filled_qty"], notional - order["notional"]
        provision = fee_provision(notional)
        fee_delta = provision - order["fee_provision"]
        if delta or value or fee_delta:
            c.execute(
                "INSERT INTO stock_fills(order_id,cumulative_qty,qty,notional,fee_provision,created_at) VALUES(?,?,?,?,?,?)",
                (oid, filled, delta, value, fee_delta, utc_now()),
            )
            self.event(
                c,
                p["id"],
                "FILL_APPLIED",
                {"order_id": oid, "qty": delta, "notional": value, "fee_provision": fee_delta},
            )
        c.execute(
            """UPDATE stock_orders SET broker_id=?,status=?,filled_qty=?,notional=?,fee_provision=?,
                     error=NULL,updated_at=? WHERE id=?""",
            (update["order_id"], status, filled, notional, provision, utc_now(), oid),
        )
        if status != order["status"]:
            self.event(c, p["id"], "ORDER_STATUS", {"order_id": oid, "status": status})

    def recover(self):
        with self.db.connect(write=True) as c:
            for row in c.execute(
                "SELECT id,portfolio_id FROM stock_orders WHERE status='pending'"
            ).fetchall():
                c.execute(
                    "UPDATE stock_orders SET status='unknown',error='Restarted during submission; reconcile before trading' WHERE id=?",
                    (row["id"],),
                )
                c.execute(
                    "UPDATE stock_portfolios SET blocked='Uncertain order requires reconciliation',paused=1 WHERE id=?",
                    (row["portfolio_id"],),
                )
                self.event(c, row["portfolio_id"], "RECOVERED_UNKNOWN", {"order_id": row["id"]})

    def save_fee_estimate(self, order_id, estimate):
        with self.db.connect(write=True) as c:
            row = c.execute("SELECT * FROM stock_orders WHERE id=?", (order_id,)).fetchone()
            if not row or not valid_estimate({**stock_attempt(dict(row)), "fee_estimate": estimate}):
                raise PortfolioError("Order fill changed before fee evidence was saved")
            if row["charges_paise"] is not None:
                return
            self.event(c, row["portfolio_id"], "BROKER_FEE_ESTIMATE",
                       {"order_id": order_id, "estimate": estimate})

    def revalue(self, c, pid):
        p = self._portfolio(c, pid)
        result = balances(p["usable_capital"], self._orders(c, pid))
        c.execute(
            "UPDATE stock_portfolios SET cash=?,cash_shortfall=?,realized=? WHERE id=?",
            (result["cash"], result["cash_shortfall"], result["realized"], pid),
        )
        c.execute("DELETE FROM stock_positions WHERE portfolio_id=?", (pid,))
        if result["position"]:
            h = result["position"]
            c.execute(
                "INSERT INTO stock_positions VALUES(?,?,?,?,?,?,?)",
                (pid, h["symbol"], h["isin"], h["instrument_key"], h["qty"], h["cost"], h["opened_at"]),
            )
        return result

    def queue_recovery(self, pid, revision, parent_id, expires_at):
        with self.db.connect(write=True) as c:
            p = self._portfolio(c, pid)
            if p["revision"] != revision:
                raise StaleRevision("Portfolio changed; refresh before approving recovery")
            orders = self._orders(c, pid)
            parent = next((o for o in orders if o["id"] == parent_id), None)
            if not p["enabled"] or not self._position(c, pid):
                raise PortfolioError("Enable the portfolio with entries paused before recovering its exit")
            if (
                not parent
                or parent["side"] != "SELL"
                or parent["status"] not in ("cancelled", "rejected")
                or parent["isin"] != self._position(c, pid)["isin"]
                or any(o["parent_id"] == parent_id for o in orders)
            ):
                raise PortfolioError("Only a confirmed cancelled/rejected exit can be replaced")
            if any(o["status"] not in TERMINAL for o in orders):
                raise PortfolioError("An unresolved order must be reconciled first")
            if p["blocked"] and p["blocked"].startswith("Legacy"):
                raise PortfolioError(p["blocked"])
            rid = "rx" + uuid.uuid4().hex[:18]
            inserted = c.execute(
                """INSERT INTO stock_recoveries VALUES(?,?,?,'queued',?,?)
                ON CONFLICT(parent_id) DO UPDATE SET status='queued',expires_at=excluded.expires_at
                WHERE stock_recoveries.status IN ('cancelled','expired')""",
                (rid, pid, parent_id, expires_at, utc_now()),
            ).rowcount
            if inserted:
                c.execute(
                    "UPDATE stock_portfolios SET blocked=NULL,paused=1,revision=revision+1 WHERE id=?", (pid,)
                )
                self.event(
                    c,
                    pid,
                    "EXIT_RECOVERY_QUEUED",
                    {"parent_id": parent_id, "actor": p["user_id"], "expires_at": expires_at},
                )

    def recovery_status(self, rid, status):
        with self.db.connect(write=True) as c:
            row = c.execute("SELECT * FROM stock_recoveries WHERE id=?", (rid,)).fetchone()
            c.execute("UPDATE stock_recoveries SET status=? WHERE id=?", (status, rid))
            self.event(c, row["portfolio_id"], "EXIT_RECOVERY_STATUS", {"id": rid, "status": status})

    def alignment_done(self, pid):
        with self.db.connect(write=True) as c:
            c.execute("UPDATE stock_portfolios SET alignment_pending=0 WHERE id=?", (pid,))

    def cancel_recovery(self, pid, revision, recovery_id):
        with self.db.connect(write=True) as c:
            p = self._portfolio(c, pid)
            if p["revision"] != revision:
                raise StaleRevision("Portfolio changed; refresh before cancelling recovery")
            changed = c.execute(
                "UPDATE stock_recoveries SET status='cancelled' WHERE id=? AND portfolio_id=? AND status='queued'",
                (recovery_id, pid),
            ).rowcount
            if not changed:
                raise PortfolioError("Recovery is no longer queued; submitted orders must be reconciled")
            c.execute("UPDATE stock_portfolios SET revision=revision+1 WHERE id=?", (pid,))
            self.event(c, pid, "EXIT_RECOVERY_CANCELLED", {"id": recovery_id, "actor": p["user_id"]})
