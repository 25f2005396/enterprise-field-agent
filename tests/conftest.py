# tests/conftest.py
"""Hermetic test environment: temp SQLite file, real ERP server on a free port, scripted LLM."""
import json
import os
import socket
import tempfile
import threading
import time

import httpx
import pytest

_tmp = tempfile.mkdtemp(prefix="efa_test_")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


ERP_PORT = _free_port()
ENV = {
    "GROQ_API_KEY": "gsk_dummy_test_key_for_unit_tests",
    "AGENT_SECURITY_SECRET": "test_agent_security_secret_32_chars_minimum_entropy",
    "WEBHOOK_SIGNING_SECRET": "test_webhook_signing_secret_32_chars_minimum_entropy",
    "ERP_INTERNAL_API_KEY": "erp_internal_service_key_24_chars",
    "ADMIN_API_KEY": "admin_super_secret_key_24_chars",
    "OPERATOR_API_KEY": "operator_secret_key_24_chars_x",
    "APPROVER_API_KEY": "approver_secret_key_24_chars_x",
    "AUDITOR_API_KEY": "auditor_secret_key_24_chars_xx",
    "ENVIRONMENT": "development",
    "DB_PATH": os.path.join(_tmp, "test.db"),
    "ERP_API_BASE_URL": f"http://127.0.0.1:{ERP_PORT}",
    "RECONCILE_BACKOFF_SECONDS": "0",
    "APPROVAL_WEBHOOK_URL": "",
    "EXTRA_USERS_JSON": json.dumps([
        {"key": "acme_operator_key_24_chars_xxxx", "user_id": "usr-acme-op", "tenant_id": "tenant-acme", "role": "OPERATOR"},
        {"key": "acme_approver_key_24_chars_xxxx", "user_id": "usr-acme-ap", "tenant_id": "tenant-acme", "role": "APPROVER"},
    ]),
}
os.environ.update(ENV)
os.environ.pop("APPROVAL_WEBHOOK_URL")

from fastapi.testclient import TestClient  # noqa: E402

from api.database import reset_db_for_tests  # noqa: E402
from api.mock_erp_crm import app as erp_app  # noqa: E402
from server import app as gateway_app  # noqa: E402

OP = {"X-API-Key": ENV["OPERATOR_API_KEY"]}
AP = {"X-API-Key": ENV["APPROVER_API_KEY"]}
AU = {"X-API-Key": ENV["AUDITOR_API_KEY"]}
AD = {"X-API-Key": ENV["ADMIN_API_KEY"]}
INTERNAL = {"X-Internal-Token": ENV["ERP_INTERNAL_API_KEY"], "X-Tenant-ID": "tenant-default"}


@pytest.fixture(scope="session", autouse=True)
def erp_server():
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(erp_app, host="127.0.0.1", port=ERP_PORT, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{ERP_PORT}/health", timeout=0.5).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    yield
    server.should_exit = True


@pytest.fixture(autouse=True)
def fresh_db():
    reset_db_for_tests()


@pytest.fixture
def erp():
    with httpx.Client(base_url=ENV["ERP_API_BASE_URL"], timeout=10) as c:
        yield c


@pytest.fixture
def gw():
    return TestClient(gateway_app)


@pytest.fixture
def script_llm(monkeypatch):
    """script_llm([step_dict | raw_string, ...]) -> replaces the LLM with a deterministic planner."""
    def install(steps):
        it = iter(steps)
        calls = []

        def fake(system_prompt, user_prompt):
            calls.append(user_prompt)
            try:
                item = next(it)
            except StopIteration:
                raise RuntimeError("scripted LLM exhausted")
            return item if isinstance(item, str) else json.dumps(item)
        monkeypatch.setattr("agent.llm.invoke_llm", fake)
        return calls
    return install


def tool(name, **args):
    return {"thought": f"calling {name}", "action": {"tool_name": name, "arguments": args}}


def stock(erp, sku="SKU-992", tenant="tenant-default"):
    r = erp.get(f"/api/erp/inventory/{sku}", headers={**INTERNAL, "X-Tenant-ID": tenant})
    return r.json()["inventory"]["stock_level"]


FINAL = {"thought": "goal met", "final_response": "Completed."}


def propose_adjust(gw, script_llm, units=-5, tail=None, headers=None):
    """Runs lookup -> adjust proposal. Returns (session_id, approval_token)."""
    script_llm([tool("lookup_inventory", sku="SKU-992"),
                tool("adjust_inventory", sku="SKU-992", adjustment_units=units, reason="Dispatch for Project Helios"),
                *(tail if tail is not None else [FINAL])])
    r = gw.post("/api/v1/agent/run", json={"query": "Adjust SKU-992 for Project Helios"}, headers=headers or OP)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "AWAITING_APPROVAL", r.json()
    sid = r.json()["session_id"]
    pend = gw.get("/api/v1/approvals/pending", headers=AP).json()["pending"]
    token = next(p["approval_token"] for p in pend if p["session_id"] == sid)
    return sid, token
