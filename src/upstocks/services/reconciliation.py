"""Reviewed, account-scoped statement reconciliation with atomic application.

Only saved strategy intents (or identified legacy intents) can be reconciled.
The uploaded rows are operator-verified evidence, never presented as an API attestation.
"""

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import uuid
from typing import Any

from ..database import utc_now
from ..order_evidence import apply_evidence, combine, current_attempt
from .. import upstox
from ..stocks.domain import PortfolioError, StaleRevision, TERMINAL
from ..stocks.statements import fingerprint, parse_statement


def stamp_day(stamp):
    value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (
        value.replace(tzinfo=upstox.IST).date().isoformat()
        if value.tzinfo is None
        else value.astimezone(upstox.IST).date().isoformat()
    )


def plan_digest(plan):
    return fingerprint(
        {**plan, "changes": [{k: v for k, v in x.items() if k != "saved"} for x in plan["changes"]]}
    )


def signature(order):
    return fingerprint({k: v for k, v in order.items() if k not in ("updated_at", "error")})


class ReconciliationService:
    def __init__(self, store):
        self.store = store
        self.db = store.db

    def _plan(self, c, uid, broker, rows, client_id):
        pid = f"{uid}:{broker}"
        p = self.store._portfolio(c, pid)
        if broker == "paper" or not client_id or p["client_id"] and p["client_id"] != client_id:
            raise PortfolioError("Select your verified live broker account")
        stocks = self.store._orders(c, pid)
        intraday = [dict(r) for r in c.execute("SELECT * FROM broker_orders WHERE account=?", (pid,))]
        legacy_row = c.execute("SELECT value FROM settings WHERE key='momentum_tuned'").fetchone()
        legacy = json.loads(legacy_row[0]) if legacy_row else {}
        unclaimed = []
        for i, order in enumerate(legacy.get("orders", [])):
            if order.get("broker") != broker:
                continue
            identity = fingerprint({"index": i, "order": order})
            claim = c.execute(
                "SELECT portfolio_id FROM stock_legacy_claims WHERE identity=?", (identity,)
            ).fetchone()
            if not claim:
                unclaimed.append((identity, order))
        changes = []
        touched = set()
        for row in rows:
            if row["client_id"] != client_id:
                raise PortfolioError("Statement account does not match the verified broker login")
            matches: list[tuple[str, Any, Any]] = []
            for order in stocks:
                if (
                    row["order_id"] == order["broker_id"]
                    or row["intent_id"]
                    and row["intent_id"] == order["id"]
                ):
                    matches.append(("stock", order, None))
            for saved in intraday:
                order = json.loads(saved["payload"])
                for index, attempt in enumerate([*order.get("attempts", []), current_attempt(order)]):
                    if (
                        row["order_id"] == attempt.get("order_id")
                        or row["intent_id"]
                        and row["intent_id"] == attempt.get("client_tag")
                    ):
                        matches.append(("intraday", saved, index))
            for identity, order in unclaimed:
                if order.get("order_id") and row["order_id"] == str(order["order_id"]):
                    matches.append(("legacy", order, identity))
            if len(matches) != 1:
                raise PortfolioError(
                    "Each statement row must match exactly one saved strategy order; manual or ambiguous orders are excluded"
                )
            kind, saved, match_index = matches[0]
            target_identity: tuple[Any, ...]
            if kind == "intraday":
                order = json.loads(saved["payload"])
                attempt = [*order.get("attempts", []), current_attempt(order)][match_index]
                target_identity = (kind, saved["trade_id"], saved["side"], match_index)
                day = saved["trade_id"].split("|")[1]
                symbol = attempt.get("broker_symbol") or attempt.get("symbol")
                side, qty = saved["side"], attempt["qty"]
                owner, oid = attempt.get("client_id"), attempt.get("order_id")
            else:
                order = saved
                target_identity = (kind, order.get("id") or match_index)
                day = stamp_day(order["created_at"] if kind == "stock" else order["at"])
                symbol, side, qty = order["symbol"], order["side"], order["qty"]
                owner = order.get("client_id") if kind == "stock" else client_id
                oid = order.get("broker_id") if kind == "stock" else str(order["order_id"])
                if row["isin"] != order["isin"]:
                    raise PortfolioError("Statement ISIN does not match the strategy order")
            if target_identity in touched:
                raise PortfolioError("An order was matched twice")
            touched.add(target_identity)
            if (
                owner != client_id
                or oid
                and oid != row["order_id"]
                or day != row["trade_date"]
                or symbol != row["symbol"]
                or side != row["side"]
                or qty != row["qty"]
            ):
                raise PortfolioError(
                    "Statement account, date, order ID, instrument, side or quantity differs from the saved intent"
                )
            if kind == "stock":
                if (
                    row["filled_qty"] < order["filled_qty"]
                    or order["status"] in TERMINAL
                    and row["status"] != order["status"]
                ):
                    raise PortfolioError("Statement contradicts already confirmed fills or terminal status")
                if side == "BUY" and row["notional"] > order["limit_paise"] * row["filled_qty"]:
                    raise PortfolioError("Statement price exceeds the reserved buy limit")
            if kind == "intraday":
                apply_evidence(attempt, row)
            changes.append(
                {
                    "kind": kind,
                    "saved": saved,
                    "index": match_index,
                    "row": row,
                    "signature": signature(saved),
                }
            )
        legacy_changes = [x for x in changes if x["kind"] == "legacy"]
        if legacy_changes and (len(legacy_changes) != len(unclaimed) or stocks):
            raise PortfolioError(
                "Legacy adoption requires all identified broker orders together and an empty strategy ledger"
            )
        if p["blocked"] and p["blocked"].startswith("Legacy") and not stocks and not legacy_changes:
            raise PortfolioError(
                "Legacy holdings need their complete saved order history; they cannot be adopted from holdings alone"
            )
        # Return changes in intent order, independent of CSV row ordering.
        changes.sort(
            key=lambda x: (
                x["saved"].get("created_at") or x["saved"].get("at", ""),
                x["saved"].get("side") != "BUY",
            )
        )
        return {"portfolio_revision": p["revision"], "client_id": client_id, "changes": changes}

    def preview(self, uid, broker, reference, content, client_id, now):
        rows = parse_statement(content, now.date())
        digest = hashlib.sha256(content.encode()).hexdigest()
        with self.db.connect(write=True) as c:
            existing = c.execute(
                "SELECT id,status FROM reconciliation_documents WHERE user_id=? AND broker=? AND digest=?",
                (uid, broker, digest),
            ).fetchone()
            if existing and existing["status"] == "applied":
                return {"document_id": existing["id"], "status": "applied", "rows": []}
            plan = self._plan(c, uid, broker, rows, client_id)
            doc_id = existing["id"] if existing else uuid.uuid4().hex
            payload = {"rows": rows, "plan_fingerprint": plan_digest(plan), "client_id": client_id}
            c.execute(
                """INSERT INTO reconciliation_documents VALUES(?,?,?,?,?,?,'preview',?,?)
                ON CONFLICT(user_id,broker,digest) DO UPDATE SET payload=excluded.payload,status='preview',
                reference=excluded.reference,expires_at=excluded.expires_at""",
                (
                    doc_id,
                    uid,
                    broker,
                    reference,
                    digest,
                    json.dumps(payload),
                    utc_now(),
                    (now + timedelta(minutes=30)).isoformat(),
                ),
            )
            return {
                "document_id": doc_id,
                "status": "preview",
                "reference": reference,
                "rows": [
                    {
                        "kind": x["kind"],
                        **{
                            k: x["row"][k]
                            for k in (
                                "order_id",
                                "symbol",
                                "side",
                                "qty",
                                "filled_qty",
                                "status",
                                "notional",
                                "charges_paise",
                            )
                        },
                    }
                    for x in plan["changes"]
                ],
            }

    def apply(self, uid, doc_id, now):
        with self.db.connect(write=True) as c:
            doc = c.execute(
                "SELECT * FROM reconciliation_documents WHERE id=? AND user_id=?", (doc_id, uid)
            ).fetchone()
            if not doc:
                raise PortfolioError("Statement preview not found")
            if doc["status"] == "applied":
                return {"status": "applied", "document_id": doc_id, "broker": doc["broker"]}
            if datetime.fromisoformat(doc["expires_at"]) < now:
                raise PortfolioError("Statement preview expired; preview it again")
            payload = json.loads(doc["payload"])
            plan = self._plan(c, uid, doc["broker"], payload["rows"], payload["client_id"])
            if plan_digest(plan) != payload["plan_fingerprint"]:
                raise StaleRevision(
                    "Execution changed after preview. Preview the statement again before applying."
                )
            pid = f"{uid}:{doc['broker']}"
            for change in plan["changes"]:
                kind, order, row = change["kind"], change["saved"], change["row"]
                if kind == "legacy":
                    oid = "lg" + change["index"][:18]
                    stamp = datetime.fromisoformat(order["at"]).replace(tzinfo=upstox.IST).isoformat()
                    c.execute(
                        """INSERT INTO stock_orders(id,portfolio_id,cycle,side,symbol,isin,instrument_key,
                        qty,limit_paise,reserved,client_id,status,broker_id,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,'unknown',?,?,?)""",
                        (
                            oid,
                            pid,
                            "legacy:" + oid,
                            order["side"],
                            order["symbol"],
                            order["isin"],
                            "NSE_EQ|" + order["isin"],
                            order["qty"],
                            max(
                                1,
                                (row["notional"] + max(1, row["filled_qty"]) - 1)
                                // max(1, row["filled_qty"]),
                            ),
                            0,
                            payload["client_id"],
                            row["order_id"],
                            stamp,
                            utc_now(),
                        ),
                    )
                    c.execute("INSERT INTO stock_legacy_claims VALUES(?,?,?)", (change["index"], pid, doc_id))
                    order = dict(c.execute("SELECT * FROM stock_orders WHERE id=?", (oid,)).fetchone())
                if kind in ("stock", "legacy"):
                    self.store._apply(c, order["id"], row)
                    if row["charges_paise"] is not None:
                        c.execute(
                            "UPDATE stock_orders SET charges_paise=?,charges_source='operator_statement',charges_document=? WHERE id=?",
                            (row["charges_paise"], doc_id, order["id"]),
                        )
                    self.store.event(
                        c,
                        pid,
                        "STATEMENT_EVIDENCE",
                        {
                            "document_id": doc_id,
                            "order_id": order["id"],
                            "actor": uid,
                            "previous_charges": order.get("charges_paise"),
                            "charges_paise": row["charges_paise"],
                        },
                    )
                else:
                    # Read the current projection because a document may amend several attempts of this trade.
                    saved = c.execute(
                        "SELECT payload FROM broker_orders WHERE trade_id=? AND account=? AND side=?",
                        (order["trade_id"], pid, order["side"]),
                    ).fetchone()
                    current = json.loads(saved[0])
                    attempts = deepcopy(current.get("attempts", []))
                    attempt = current_attempt(current)
                    if change["index"] < len(attempts):
                        attempts[change["index"]] = apply_evidence(attempts[change["index"]], row)
                    else:
                        attempt = apply_evidence(attempt, row)
                        current["order_id"] = row["order_id"]
                    updated = combine(current, attempt, attempts)
                    updated.update(status="submitted", statement_document=doc_id)
                    updated.pop("error", None)
                    c.execute(
                        "UPDATE broker_orders SET payload=?,status='submitted',updated_at=? WHERE trade_id=? AND account=? AND side=?",
                        (json.dumps(updated), utc_now(), order["trade_id"], pid, order["side"]),
                    )
            result = self.store.revalue(c, pid)
            legacy_imported = any(x["kind"] == "legacy" for x in plan["changes"])
            c.execute(
                """UPDATE stock_portfolios SET client_id=?,paused=1,revision=revision+1,
                blocked=CASE WHEN ? THEN NULL ELSE blocked END WHERE id=?""",
                (payload["client_id"], int(legacy_imported), pid),
            )
            c.execute("UPDATE reconciliation_documents SET status='applied' WHERE id=?", (doc_id,))
            self.store.event(
                c,
                pid,
                "STATEMENT_APPLIED",
                {
                    "document_id": doc_id,
                    "reference": doc["reference"],
                    "actor": uid,
                    "net_realized": result["net_realized"],
                    "source": "operator_statement",
                },
            )
            return {"status": "applied", "document_id": doc_id, "broker": doc["broker"]}
