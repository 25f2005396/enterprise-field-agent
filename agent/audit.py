# agent/audit.py
"""Tamper-evident audit log: append-only (DB triggers) and hash-chained (verify_audit_chain)."""
import hashlib
import json
import time
from typing import Any, Dict, List, Optional

from api.database import atomic_transaction, get_raw_connection

GENESIS = "0" * 64
_SENSITIVE = ("token", "secret", "password", "authorization", "api_key", "apikey", "binding")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if any(s in str(k).lower() for s in _SENSITIVE) else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _entry_hash(prev: str, ts: int, session_id, tenant_id, actor_id, actor_role, event_type,
                tool_name, idem, outcome, details_json) -> str:
    material = json.dumps([prev, ts, session_id, tenant_id, actor_id, actor_role, event_type,
                           tool_name, idem, outcome, details_json], sort_keys=False)
    return hashlib.sha256(material.encode()).hexdigest()


def record_audit_event(session_id: Optional[str], tenant_id: Optional[str], actor_id: str, actor_role: str,
                       event_type: str, outcome: str, tool_name: Optional[str] = None,
                       idempotency_key: Optional[str] = None, details: Optional[Dict[str, Any]] = None) -> None:
    details_json = json.dumps(redact(details or {}), sort_keys=True, default=str)
    ts = int(time.time())
    with atomic_transaction() as conn:  # BEGIN IMMEDIATE => chain head read + insert are atomic
        row = conn.execute("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev = row["entry_hash"] if row else GENESIS
        h = _entry_hash(prev, ts, session_id, tenant_id, actor_id, actor_role, event_type,
                        tool_name, idempotency_key, outcome, details_json)
        conn.execute(
            "INSERT INTO audit_log (ts, session_id, tenant_id, actor_id, actor_role, event_type, tool_name, "
            "idempotency_key, outcome, details_json, prev_hash, entry_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, session_id, tenant_id, actor_id, actor_role, event_type, tool_name, idempotency_key,
             outcome, details_json, prev, h))


def verify_audit_chain() -> Dict[str, Any]:
    """Recomputes every hash. Returns {'ok': bool, 'entries': n, 'broken_at': id|None}."""
    conn = get_raw_connection()
    try:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id ASC").fetchall()
    finally:
        conn.close()
    prev = GENESIS
    for r in rows:
        expected = _entry_hash(prev, r["ts"], r["session_id"], r["tenant_id"], r["actor_id"], r["actor_role"],
                               r["event_type"], r["tool_name"], r["idempotency_key"], r["outcome"],
                               r["details_json"])
        if r["prev_hash"] != prev or r["entry_hash"] != expected:
            return {"ok": False, "entries": len(rows), "broken_at": r["id"]}
        prev = r["entry_hash"]
    return {"ok": True, "entries": len(rows), "broken_at": None}


def read_audit(tenant_id: str, session_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    conn = get_raw_connection()
    try:
        if session_id:
            rows = conn.execute("SELECT * FROM audit_log WHERE tenant_id=? AND session_id=? ORDER BY id DESC LIMIT ?",
                                (tenant_id, session_id, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM audit_log WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
                                (tenant_id, limit)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
