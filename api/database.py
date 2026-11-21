# api/database.py
"""SQLite (WAL) storage. Every write path uses BEGIN IMMEDIATE so check-then-write is atomic.

Single-node only. For multi-instance deployments migrate to PostgreSQL (SELECT ... FOR UPDATE,
INSERT ... ON CONFLICT) - the SQL here is deliberately plain so the port is mechanical.
"""
import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Optional, Tuple

from api.config import settings

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS leads (
        tenant_id TEXT NOT NULL, lead_id TEXT NOT NULL, company TEXT NOT NULL,
        contact_email TEXT NOT NULL, status TEXT NOT NULL,
        deal_value REAL NOT NULL CHECK(deal_value >= 0), notes TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (tenant_id, lead_id))""",
    """CREATE TABLE IF NOT EXISTS customers (
        tenant_id TEXT NOT NULL, customer_id TEXT NOT NULL, name TEXT NOT NULL,
        PRIMARY KEY (tenant_id, customer_id))""",
    """CREATE TABLE IF NOT EXISTS inventory (
        tenant_id TEXT NOT NULL, sku TEXT NOT NULL, name TEXT NOT NULL,
        stock_level INTEGER NOT NULL CHECK(stock_level >= 0), warehouse_zone TEXT NOT NULL,
        unit_price REAL NOT NULL CHECK(unit_price >= 0),
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (tenant_id, sku))""",
    """CREATE TABLE IF NOT EXISTS support_tickets (
        tenant_id TEXT NOT NULL, ticket_id TEXT NOT NULL, customer_id TEXT NOT NULL,
        subject TEXT NOT NULL, priority TEXT NOT NULL, details TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'OPEN', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (tenant_id, ticket_id))""",
    # Idempotency scope = tenant + key; the stored method/endpoint/hash must all match on replay.
    """CREATE TABLE IF NOT EXISTS idempotency_ledger (
        tenant_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, http_method TEXT NOT NULL,
        endpoint TEXT NOT NULL, request_hash TEXT NOT NULL, response_code INTEGER NOT NULL,
        response_body TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (tenant_id, idempotency_key))""",
    # Sessions: optimistic-lock version + queryable columns; the full state lives in state_json.
    """CREATE TABLE IF NOT EXISTS agent_sessions (
        session_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        state_version INTEGER NOT NULL, status TEXT NOT NULL,
        approval_expires_at INTEGER, state_json TEXT NOT NULL, updated_epoch INTEGER NOT NULL)""",
    # Append-only, hash-chained audit ledger.
    """CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, session_id TEXT,
        tenant_id TEXT, actor_id TEXT, actor_role TEXT, event_type TEXT NOT NULL,
        tool_name TEXT, idempotency_key TEXT, outcome TEXT NOT NULL, details_json TEXT,
        prev_hash TEXT NOT NULL, entry_hash TEXT NOT NULL)""",
    """CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END""",
    # Webhook outbox with retry bookkeeping. Payloads never contain secrets or approval tokens.
    """CREATE TABLE IF NOT EXISTS webhook_outbox (
        event_id TEXT PRIMARY KEY, target_url TEXT NOT NULL, payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at INTEGER NOT NULL, last_error TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
]

SEED_TENANTS = [settings.DEFAULT_TENANT_ID, "tenant-acme"]


def get_raw_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.DB_PATH, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def atomic_transaction():
    """BEGIN IMMEDIATE takes the write lock up front, so check-then-write cannot interleave."""
    conn = get_raw_connection()
    try:
        conn.execute("BEGIN IMMEDIATE;")
        yield conn
        conn.execute("COMMIT;")
    except BaseException:
        try:
            conn.execute("ROLLBACK;")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _seed(conn: sqlite3.Connection) -> None:
    for t in SEED_TENANTS:
        conn.execute("INSERT OR IGNORE INTO inventory VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                     (t, "SKU-992", "Industrial IoT Gateway Module", 42, "Zone-B-Rack-4", 450.0))
        conn.execute("INSERT OR IGNORE INTO inventory VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                     (t, "SKU-104", "Optical Fiber Transceiver 10G", 5, "Zone-A-Rack-1", 120.0))
        conn.execute("INSERT OR IGNORE INTO inventory VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                     (t, "SKU-550", "Cat6A Shielded Patch Cable 100m", 150, "Zone-C-Rack-2", 45.0))
        conn.execute("INSERT OR IGNORE INTO leads VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                     (t, "LEAD-401", "Nexus Logistics", "ops@nexuslogistics.com", "CONTACTED", 75000.0, "Enterprise inquiry."))
        conn.execute("INSERT OR IGNORE INTO leads VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                     (t, "LEAD-402", "Apex BioTech", "dr_smith@apexbio.com", "NEW", 15000.0, "API access."))
        conn.execute("INSERT OR IGNORE INTO customers VALUES (?,?,?)", (t, "CUST-001", "Globex Corporation"))


def init_db() -> None:
    with atomic_transaction() as conn:
        for stmt in SCHEMA:
            conn.execute(stmt)
        _seed(conn)


def reset_db_for_tests() -> None:
    """Drops everything and re-creates it. Refuses to run in production."""
    if settings.ENVIRONMENT == "production":
        raise RuntimeError("reset_db_for_tests is disabled in production")
    conn = get_raw_connection()
    try:
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall():
            conn.execute(f"DROP TABLE IF EXISTS {name}")
    finally:
        conn.close()
    init_db()


# ---------------- Idempotency helpers (call inside atomic_transaction) ----------------

def check_idempotency(conn, key: str, tenant_id: str, method: str, endpoint: str,
                      payload_hash: str) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
    row = conn.execute(
        "SELECT http_method, endpoint, request_hash, response_code, response_body "
        "FROM idempotency_ledger WHERE tenant_id=? AND idempotency_key=?", (tenant_id, key)
    ).fetchone()
    if not row:
        return None, None
    if (row["http_method"] != method.upper() or row["endpoint"] != endpoint
            or row["request_hash"] != payload_hash):
        raise ValueError("Idempotency conflict: key was already used with a different method, endpoint or payload.")
    return json.loads(row["response_body"]), row["response_code"]


def record_idempotency(conn, key: str, tenant_id: str, method: str, endpoint: str,
                       payload_hash: str, code: int, body: Dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO idempotency_ledger (tenant_id, idempotency_key, http_method, endpoint, "
        "request_hash, response_code, response_body) VALUES (?,?,?,?,?,?,?)",
        (tenant_id, key, method.upper(), endpoint, payload_hash, code, json.dumps(body)),
    )


init_db()
