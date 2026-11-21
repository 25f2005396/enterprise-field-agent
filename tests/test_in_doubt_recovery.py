"""In-doubt writes: the request may have been applied even though the client saw a failure.
Faults are injected at agent.tools._send, the single HTTP choke point."""
import json
import sqlite3
import time

import httpx

from agent import tools
from tests.conftest import AP, ENV, FINAL, OP, propose_adjust, stock, tool


def inject(monkeypatch, behaviour, writes_only=True, times=1):
    """behaviour(real_call) -> response | raises. Applies to the first `times` matching calls."""
    real = tools._send
    box = {"n": 0}

    def fake(method, path, tenant_id, idempotency_key=None, json_body=None):
        is_write = method in ("POST", "PATCH")
        if (is_write or not writes_only) and (times is None or box["n"] < times):
            box["n"] += 1
            return behaviour(lambda: real(method, path, tenant_id, idempotency_key, json_body))
        return real(method, path, tenant_id, idempotency_key, json_body)
    monkeypatch.setattr("agent.tools._send", fake)
    return box


def applied_then_timeout(call):
    call()                                   # the ERP really applied it ...
    raise httpx.ReadTimeout("simulated")     # ... but the client never saw the response


def approve(gw, sid, token):
    return gw.post("/api/v1/agent/approve", json={"session_id": sid, "approval_token": token}, headers=AP)


def test_timeout_after_apply_reconciles_without_double_write_or_replanning(gw, erp, script_llm, monkeypatch):
    calls = script_llm  # alias for readability
    sid, token = propose_adjust(gw, script_llm)
    inject(monkeypatch, applied_then_timeout)
    r = approve(gw, sid, token).json()
    assert r["status"] == "COMPLETED"
    assert stock(erp) == 37                                     # applied exactly once
    obs = r["steps"][1]["observation"]
    assert "Reconciled" in obs and "HAD been applied" in obs


def test_timeout_before_apply_is_replayed_with_same_key(gw, erp, script_llm, monkeypatch):
    sid, token = propose_adjust(gw, script_llm)
    keys = []
    real = tools._send

    def fake(method, path, tenant_id, idempotency_key=None, json_body=None):
        if method == "POST" and "adjust" in path:
            keys.append(idempotency_key)
            if len(keys) == 1:
                raise httpx.ReadTimeout("lost before reaching server")
        return real(method, path, tenant_id, idempotency_key, json_body)
    monkeypatch.setattr("agent.tools._send", fake)
    r = approve(gw, sid, token).json()
    assert r["status"] == "COMPLETED" and stock(erp) == 37
    assert len(keys) == 2 and keys[0] == keys[1]               # identical key on the replay
    assert "NOT applied" in r["steps"][1]["observation"]


def test_llm_is_not_consulted_during_reconciliation(gw, erp, script_llm, monkeypatch):
    calls = script_llm([tool("lookup_inventory", sku="SKU-992"),
                        tool("adjust_inventory", sku="SKU-992", adjustment_units=-5, reason="Dispatch for Project Helios"), FINAL])
    sid = gw.post("/api/v1/agent/run", json={"query": "Adjust SKU-992"}, headers=OP).json()["session_id"]
    token = gw.get("/api/v1/approvals/pending", headers=AP).json()["pending"][0]["approval_token"]
    inject(monkeypatch, applied_then_timeout)
    approve(gw, sid, token)
    assert len(calls) == 3                                      # lookup, propose, final - nothing in between


def test_server_5xx_on_write_is_treated_as_in_doubt(gw, erp, script_llm, monkeypatch):
    sid, token = propose_adjust(gw, script_llm)
    inject(monkeypatch, lambda call: httpx.Response(503, json={"detail": "upstream unavailable"}))
    r = approve(gw, sid, token).json()
    assert r["status"] == "COMPLETED" and stock(erp) == 37


def test_persistent_outage_pauses_then_reconcile_resumes(gw, erp, script_llm, monkeypatch):
    calls = script_llm([tool("lookup_inventory", sku="SKU-992"),
                        tool("adjust_inventory", sku="SKU-992", adjustment_units=-5, reason="Dispatch for Project Helios"), FINAL])
    sid = gw.post("/api/v1/agent/run", json={"query": "Adjust SKU-992"}, headers=OP).json()["session_id"]
    token = gw.get("/api/v1/approvals/pending", headers=AP).json()["pending"][0]["approval_token"]

    def blackout(call):
        raise httpx.ReadTimeout("ERP unreachable")
    real_send = tools._send
    inject(monkeypatch, blackout, writes_only=False, times=None)    # every ERP call fails
    r = approve(gw, sid, token).json()
    assert r["status"] == "PAUSED_RECONCILIATION_REQUIRED" and r["in_doubt_tool"] == "adjust_inventory"
    assert r["steps"][1]["in_doubt"] is True
    assert len(calls) == 2 and stock_direct() == 42             # no replanning, nothing applied

    # the in-doubt record is durable and keeps its original idempotency key
    persisted = json.loads(sqlite3.connect(ENV["DB_PATH"]).execute("SELECT state_json FROM agent_sessions WHERE session_id=?", (sid,)).fetchone()[0])
    key = persisted["in_doubt_action"]["idempotency_key"]
    assert key == persisted["steps"][1]["tool_call"]["idempotency_key"]

    # while paused, a new run is refused and nobody can approve again
    assert gw.post("/api/v1/agent/run", json={"query": "More work", "session_id": sid}, headers=OP).status_code == 409
    assert approve(gw, sid, token).status_code == 409

    monkeypatch.setattr("agent.tools._send", real_send)         # ERP is reachable again (LLM script stays)
    r = gw.post("/api/v1/agent/reconcile", json={"session_id": sid}, headers=OP).json()
    assert r["status"] == "COMPLETED"                           # resolved, then the agent finished its plan
    assert stock(erp) == 37
    assert gw.post("/api/v1/agent/reconcile", json={"session_id": sid}, headers=OP).status_code == 409


def stock_direct():
    c = sqlite3.connect(ENV["DB_PATH"])
    return c.execute("SELECT stock_level FROM inventory WHERE sku='SKU-992' AND tenant_id='tenant-default'").fetchone()[0]


def test_crashed_worker_is_recovered_via_stale_executing_state(gw, erp, script_llm):
    sid, token = propose_adjust(gw, script_llm)
    c = sqlite3.connect(ENV["DB_PATH"])
    state = json.loads(c.execute("SELECT state_json FROM agent_sessions WHERE session_id=?", (sid,)).fetchone()[0])
    state.update(status="EXECUTING", staged_action=state["pending_approval"], pending_approval=None, approval_binding=None)
    c.execute("UPDATE agent_sessions SET state_json=?, status='EXECUTING', updated_epoch=? WHERE session_id=?",
              (json.dumps(state), int(time.time()) - 1000, sid))
    c.commit()
    r = gw.post("/api/v1/agent/reconcile", json={"session_id": sid}, headers=OP)
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED"
    assert stock_direct() == 37


def test_fresh_executing_session_is_not_stolen(gw, script_llm):
    sid, _ = propose_adjust(gw, script_llm)
    c = sqlite3.connect(ENV["DB_PATH"])
    state = json.loads(c.execute("SELECT state_json FROM agent_sessions WHERE session_id=?", (sid,)).fetchone()[0])
    state.update(status="EXECUTING", staged_action=state["pending_approval"], pending_approval=None)
    c.execute("UPDATE agent_sessions SET state_json=?, status='EXECUTING', updated_epoch=? WHERE session_id=?", (json.dumps(state), int(time.time()), sid))
    c.commit()
    assert gw.post("/api/v1/agent/reconcile", json={"session_id": sid}, headers=OP).status_code == 409


def test_connect_error_is_not_in_doubt_and_planner_may_retry(gw, erp, script_llm, monkeypatch):
    sid, token = propose_adjust(gw, script_llm, tail=[tool("adjust_inventory", sku="SKU-992", adjustment_units=-5, reason="Dispatch for Project Helios")])
    inject(monkeypatch, lambda call: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    r = approve(gw, sid, token).json()
    assert r["steps"][1]["in_doubt"] is False and "Connection failed" in r["steps"][1]["observation"]
    assert r["status"] == "AWAITING_APPROVAL" and stock(erp) == 42    # planner retried -> fresh approval needed
    assert r["pending_tool"] == "adjust_inventory"


def test_read_timeout_is_not_in_doubt(gw, script_llm, monkeypatch):
    script_llm([tool("lookup_inventory", sku="SKU-992"), tool("lookup_inventory", sku="SKU-992"), FINAL])
    inject(monkeypatch, lambda call: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), writes_only=False, times=1)
    r = gw.post("/api/v1/agent/run", json={"query": "Check SKU-992"}, headers=OP).json()
    assert r["steps"][0]["is_error"] and not r["steps"][0]["in_doubt"]
    assert r["status"] == "COMPLETED"
