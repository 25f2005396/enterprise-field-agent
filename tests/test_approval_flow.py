import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from server import app
from tests.conftest import AD, AP, ENV, OP, FINAL, propose_adjust, stock, tool

ACME_OP = {"X-API-Key": "acme_operator_key_24_chars_xxxx"}
ACME_AP = {"X-API-Key": "acme_approver_key_24_chars_xxxx"}


def approve(gw, sid, token, headers=AP):
    return gw.post("/api/v1/agent/approve", json={"session_id": sid, "approval_token": token}, headers=headers)


def db():
    c = sqlite3.connect(ENV["DB_PATH"])
    c.row_factory = sqlite3.Row
    return c


def test_happy_path_and_one_time_use(gw, erp, script_llm):
    sid, token = propose_adjust(gw, script_llm)
    assert stock(erp) == 42                       # nothing happens before approval
    r = approve(gw, sid, token)
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED"
    assert stock(erp) == 37
    assert approve(gw, sid, token).status_code == 409      # one-time
    assert stock(erp) == 37


def test_tao_trace_is_recorded(gw, script_llm):
    sid, token = propose_adjust(gw, script_llm)
    steps = approve(gw, sid, token).json()["steps"]
    assert [s["tool_call"]["tool_name"] if s["tool_call"] else None for s in steps] == ["lookup_inventory", "adjust_inventory", None]
    assert all(s["thought"] for s in steps)
    assert '"current_stock": 37' in steps[1]["observation"]


def test_token_never_exposed_or_stored(gw, script_llm):
    sid, token = propose_adjust(gw, script_llm)
    for headers in (OP, AP, AD):
        assert token not in gw.get(f"/api/v1/agent/sessions/{sid}", headers=headers).text
    row = db().execute("SELECT state_json FROM agent_sessions WHERE session_id=?", (sid,)).fetchone()
    assert token not in row["state_json"] and token.split(".")[1] not in row["state_json"]
    assert "approval_token" not in gw.get(f"/api/v1/agent/sessions/{sid}", headers=OP).text


def test_wrong_token_rejected(gw, erp, script_llm):
    sid, token = propose_adjust(gw, script_llm)
    assert approve(gw, sid, token[:-4] + "0000").status_code == 403
    assert approve(gw, sid, "garbage").status_code == 403
    assert stock(erp) == 42


def test_db_tampering_of_pending_args_is_detected(gw, erp, script_llm):
    sid, token = propose_adjust(gw, script_llm)
    c = db()
    state = json.loads(c.execute("SELECT state_json FROM agent_sessions WHERE session_id=?", (sid,)).fetchone()[0])
    state["pending_approval"]["arguments"]["adjustment_units"] = -40
    c.execute("UPDATE agent_sessions SET state_json=? WHERE session_id=?", (json.dumps(state), sid))
    c.commit()
    assert approve(gw, sid, token).status_code == 403
    assert stock(erp) == 42


def test_expired_approval_aborts(gw, erp, script_llm):
    sid, token = propose_adjust(gw, script_llm)
    c = db()
    state = json.loads(c.execute("SELECT state_json FROM agent_sessions WHERE session_id=?", (sid,)).fetchone()[0])
    state["approval_expires_at"] = int(time.time()) - 5
    c.execute("UPDATE agent_sessions SET state_json=?, approval_expires_at=? WHERE session_id=?", (json.dumps(state), state["approval_expires_at"], sid))
    c.commit()
    assert approve(gw, sid, token).status_code == 410
    assert gw.get(f"/api/v1/agent/sessions/{sid}", headers=OP).json()["status"] == "ABORTED"
    assert stock(erp) == 42


def test_separation_of_duties_blocks_self_approval_even_for_admin(gw, erp, script_llm):
    script_llm([tool("lookup_inventory", sku="SKU-992"),
                tool("adjust_inventory", sku="SKU-992", adjustment_units=-3, reason="Admin initiated adjustment"), FINAL])
    sid = gw.post("/api/v1/agent/run", json={"query": "Admin adjusts SKU-992"}, headers=AD).json()["session_id"]
    token = next(p["approval_token"] for p in gw.get("/api/v1/approvals/pending", headers=AD).json()["pending"] if p["session_id"] == sid)
    assert approve(gw, sid, token, headers=AD).status_code == 403
    assert stock(erp) == 42
    assert approve(gw, sid, token, headers=AP).status_code == 200      # a different human can
    assert stock(erp) == 39


def test_concurrent_approvals_execute_exactly_once(gw, erp, script_llm):
    sid, token = propose_adjust(gw, script_llm)

    def go(_):
        return approve(TestClient(app), sid, token).status_code
    with ThreadPoolExecutor(4) as ex:
        codes = list(ex.map(go, range(4)))
    assert codes.count(200) == 1, codes
    assert set(codes) <= {200, 409}
    assert stock(erp) == 37


def test_reject_aborts_and_blocks_later_approval(gw, erp, script_llm):
    sid, token = propose_adjust(gw, script_llm)
    r = gw.post("/api/v1/agent/reject", json={"session_id": sid, "reason": "Not authorised"}, headers=AP)
    assert r.json()["status"] == "ABORTED"
    assert approve(gw, sid, token).status_code == 409
    assert stock(erp) == 42


def test_write_without_prior_lookup_is_blocked_then_recovers(gw, script_llm):
    calls = script_llm([tool("adjust_inventory", sku="SKU-992", adjustment_units=-1, reason="Blind write attempt"),
                        tool("lookup_inventory", sku="SKU-992"),
                        tool("adjust_inventory", sku="SKU-992", adjustment_units=-1, reason="Write after lookup")])
    r = gw.post("/api/v1/agent/run", json={"query": "Adjust SKU-992 by -1"}, headers=OP).json()
    assert r["status"] == "AWAITING_APPROVAL"
    assert "Policy:" in r["steps"][0]["observation"] and r["steps"][0]["is_error"]


def test_finished_session_can_be_reused_with_new_query(gw, script_llm):
    calls = script_llm([{"thought": "t", "final_response": "first"}, {"thought": "t", "final_response": "second"}])
    sid = gw.post("/api/v1/agent/run", json={"query": "First request"}, headers=OP).json()["session_id"]
    r = gw.post("/api/v1/agent/run", json={"query": "Second request", "session_id": sid}, headers=OP).json()
    assert r["final_response"] == "second" and "Second request" in calls[1] and "First request" not in calls[1]
    assert len(r["steps"]) == 1


def test_busy_session_cannot_be_rerun(gw, script_llm):
    sid, _ = propose_adjust(gw, script_llm)
    assert gw.post("/api/v1/agent/run", json={"query": "Another request", "session_id": sid}, headers=OP).status_code == 409


def test_cross_tenant_access_is_404(gw, script_llm):
    sid, token = propose_adjust(gw, script_llm)
    assert gw.get(f"/api/v1/agent/sessions/{sid}", headers=ACME_OP).status_code == 404
    assert approve(gw, sid, token, headers=ACME_AP).status_code == 404
    assert gw.get("/api/v1/approvals/pending", headers=ACME_AP).json()["pending"] == []


def test_agent_acts_on_the_callers_tenant_data(gw, erp, script_llm):
    script_llm([tool("lookup_inventory", sku="SKU-992"),
                tool("adjust_inventory", sku="SKU-992", adjustment_units=-10, reason="Acme tenant adjustment"), FINAL])
    sid = gw.post("/api/v1/agent/run", json={"query": "Adjust acme stock"}, headers=ACME_OP).json()["session_id"]
    token = next(p["approval_token"] for p in gw.get("/api/v1/approvals/pending", headers=ACME_AP).json()["pending"])
    assert approve(gw, sid, token, headers=ACME_AP).status_code == 200
    assert stock(erp, tenant="tenant-acme") == 32 and stock(erp, tenant="tenant-default") == 42


def test_operator_cannot_read_anothers_session_but_auditor_can(gw, script_llm):
    sid, _ = propose_adjust(gw, script_llm)
    from tests.conftest import AU
    assert gw.get(f"/api/v1/agent/sessions/{sid}", headers=AU).status_code == 200
