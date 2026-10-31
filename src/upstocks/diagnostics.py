"""Searchable activity and consistent session archives, adapted for candle scans."""
from datetime import datetime, time, timedelta, timezone
import json
from tempfile import SpooledTemporaryFile
import zipfile

from .database import utc_now
from .observability import clean
from .upstox import IST


def day_bounds(day):
    start = datetime.combine(day, time.min, IST)
    return tuple(value.astimezone(timezone.utc).isoformat(timespec="milliseconds") for value in (start, start + timedelta(days=1)))


def events(database, *, day, level=None, component=None, query="", include_http=False, before=None, limit=100):
    start, end = day_bounds(day)
    clauses, params = ["created_at >= ?", "created_at < ?"], [start, end]
    if not include_http:
        clauses.append("component != 'http'")
    for field, value in (("level", level), ("component", component)):
        if value:
            clauses.append(f"{field} = ?")
            params.append(value)
    if query:
        literal = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(message LIKE ? ESCAPE '\\' OR kind LIKE ? ESCAPE '\\' OR request_id LIKE ? ESCAPE '\\' OR details LIKE ? ESCAPE '\\')")
        params.extend([f"%{literal}%"] * 4)
    matching_clauses, matching_params = list(clauses), list(params)
    if before is not None:
        clauses.append("id < ?")
        params.append(before)
    with database.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM audit_events WHERE {' AND '.join(matching_clauses)}", matching_params).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM audit_events WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?", (*params, limit + 1)).fetchall()
        items = [clean({**dict(row), "details": json.loads(row["details"])}) for row in rows[:limit]]
        return {"events": items, "total": total, "next_cursor": items[-1]["id"] if len(rows) > limit else None}


def _counts(conn, day):
    start, end = day_bounds(day)
    queries = {
        "events": ("SELECT COUNT(*) FROM audit_events WHERE created_at >= ? AND created_at < ?", (start, end)),
        "evidence": ("SELECT COUNT(*) FROM decision_evidence WHERE day=?", (str(day),)),
        "journal_trades": ("SELECT COUNT(*) FROM journal_trades WHERE day=?", (str(day),)),
        "order_attempts": ("SELECT COUNT(*) FROM broker_orders WHERE substr(trade_id,instr(trade_id,'|')+1,10)=?", (str(day),)),
        "backtest_days": ("SELECT COUNT(*) FROM backtest_days WHERE day=?", (str(day),)),
    }
    return {key: conn.execute(sql, args).fetchone()[0] for key, (sql, args) in queries.items()}


def session_summary(database, day):
    with database.connect() as conn:
        start, end = day_bounds(day)
        sources = [row[0] for row in conn.execute(
            "SELECT DISTINCT component FROM audit_events WHERE created_at>=? AND created_at<? ORDER BY component",
            (start, end),
        )]
        http_events = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE created_at>=? AND created_at<? AND component='http'",
            (start, end),
        ).fetchone()[0]
        return {"day": str(day), "timezone": "Asia/Kolkata", **_counts(conn, day),
                "sources": sources, "http_events": http_events}


def build_session_export(database, day):
    """Run on a worker thread. Stream one SQLite snapshot into a spooled ZIP."""
    archive = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    try:
        with database.connect() as conn, zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            start, end = day_bounds(day)
            counts = _counts(conn, day)  # establishes the read snapshot

            def jsonl(filename, sql, params, payload_fields=()):
                with bundle.open(filename, "w") as output:
                    for row in conn.execute(sql, params):
                        record = dict(row)
                        for field in payload_fields:
                            record[field] = json.loads(record[field])
                        output.write((json.dumps(clean(record), ensure_ascii=False, allow_nan=False) + "\n").encode())

            jsonl("events.jsonl", "SELECT * FROM audit_events WHERE created_at>=? AND created_at<? ORDER BY id", (start, end), ("details",))
            jsonl("evidence.jsonl", "SELECT * FROM decision_evidence WHERE day=? ORDER BY id", (str(day),), ("payload",))
            jsonl("orders.jsonl", "SELECT * FROM broker_orders WHERE substr(trade_id,instr(trade_id,'|')+1,10)=? ORDER BY created_at,trade_id,account,side", (str(day),), ("payload",))
            jsonl("journal.jsonl", """SELECT t.day,t.preset,t.trade_id,t.payload,s.lots,s.lot_size,s.updated,n.text AS note
                  FROM journal_trades t JOIN journal_sessions s ON s.day=t.day AND s.preset=t.preset
                  LEFT JOIN notes n ON n.trade_id=t.trade_id WHERE t.day=? ORDER BY t.preset,t.position""", (str(day),), ("payload",))
            jsonl("backtests.jsonl", """SELECT j.id AS job_id,j.preset,j.status,j.settings,d.day,d.payload
                  FROM backtest_days d JOIN backtest_jobs j ON d.job_id=j.id WHERE d.day=? ORDER BY j.created_at""", (str(day),), ("settings", "payload"))
            manifest = {"format_version": 1, "application": "Upstocks", "day": str(day), "timezone": "Asia/Kolkata",
                        "exported_at": utc_now(), "counts": counts,
                        "schema_version": conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
                        "notes": ["Events are selected by their IST occurrence date; evidence and trades by strategy session date.",
                                  "Scan evidence contains saved strategy results and configuration, not streaming ticks or broker fill confirmations.",
                                  "Evidence starts with this upgrade. Missing historical evidence is not reconstructed.",
                                  "Backtest rows are simulations. Original legacy activity is dated by its import time.",
                                  "No broker requests were made to create this archive. Credentials and dashboard tokens are excluded."]}
            bundle.writestr("manifest.json", json.dumps(manifest, indent=2))
        archive.seek(0)
        return archive
    except BaseException:
        archive.close()
        raise
