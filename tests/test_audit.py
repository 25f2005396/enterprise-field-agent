import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.audit import record_audit_event, verify_audit_chain
from tests.conftest import AP, AU, ENV, propose_adjust


def conn():
    return sqlite3.connect(ENV["DB_PATH"], isolation_level=None)   # autocommit: no lingering transactions


def test_audit_trail_is_written_and_chain_verifies(gw, script_llm):
    sid, token = propose_adjust(gw, script_llm)
    gw.post("/api/v1/agent/approve", json={"session_id": sid, "approval_token": token}, headers=AP)
    events = [e["event_type"] for e in gw.get("/api/v1/audit", params={"session_id": sid}, headers=AU).json()["events"]]
    for expected in ("SESSION_RUN_TRIGGERED", "APPROVAL_REQUESTED", "APPROVAL_GRANTED", "TOOL_EXECUTION"):
        assert expected in events
    assert gw.get("/api/v1/audit/verify", headers=AU).json()["ok"] is True


def test_update_and_delete_are_blocked_by_triggers():
    record_audit_event("s", "t", "u", "OPERATOR", "X", "OK")
    c = conn()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        c.execute("UPDATE audit_log SET outcome='CLEAN'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        c.execute("DELETE FROM audit_log")


def test_tampering_with_triggers_dropped_is_detected_by_hash_chain():
    for i in range(5):
        record_audit_event("s", "t", "u", "OPERATOR", f"EVT{i}", "OK")
    c = conn()
    c.execute("DROP TRIGGER audit_no_update")
    c.execute("UPDATE audit_log SET outcome='FORGED' WHERE id=3")
    c.commit()
    assert verify_audit_chain() == {"ok": False, "entries": 5, "broken_at": 3}


def test_deleting_a_middle_row_is_detected():
    for i in range(5):
        record_audit_event("s", "t", "u", "OPERATOR", f"EVT{i}", "OK")
    c = conn()
    c.execute("DROP TRIGGER audit_no_delete")
    c.execute("DELETE FROM audit_log WHERE id=2")
    c.commit()
    assert verify_audit_chain()["ok"] is False


def test_secrets_are_redacted_recursively():
    record_audit_event("s", "t", "u", "OPERATOR", "X", "OK",
                       details={"approval_token": "SECRET-A", "nested": {"api_key": "SECRET-B", "keep": "visible"},
                                "list": [{"Authorization": "SECRET-C"}]})
    row = conn().execute("SELECT details_json FROM audit_log").fetchone()[0]
    assert "SECRET" not in row and "visible" in row


def test_concurrent_audit_writes_keep_chain_valid():
    with ThreadPoolExecutor(12) as ex:
        list(ex.map(lambda i: record_audit_event("s", "t", "u", "OPERATOR", f"E{i}", "OK"), range(40)))
    assert verify_audit_chain() == {"ok": True, "entries": 40, "broken_at": None}
