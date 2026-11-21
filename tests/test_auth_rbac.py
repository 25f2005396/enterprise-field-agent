import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.auth import authenticate
from api.config import Settings
from tests.conftest import AD, AP, AU, OP, ENV


def test_missing_key_401(gw):
    assert gw.post("/api/v1/agent/run", json={"query": "Check SKU-992"}).status_code == 401


def test_invalid_key_403(gw):
    assert gw.post("/api/v1/agent/run", json={"query": "Check SKU-992"}, headers={"X-API-Key": "nope"}).status_code == 403


def test_non_ascii_key_does_not_500(gw):
    with pytest.raises(HTTPException) as e:
        authenticate("клÜч-ключ-ключ-ключ-ключ")
    assert e.value.status_code == 403
    assert gw.get("/api/v1/agent/sessions/x", headers={"X-API-Key": "caf\u00e9-key-key-key-key-key".encode("latin-1")}).status_code == 403


def test_role_matrix(gw):
    run = lambda h: gw.post("/api/v1/agent/run", json={"query": "Check SKU-992"}, headers=h).status_code
    assert run(AU) == 403
    assert run(AP) == 403
    assert gw.get("/api/v1/approvals/pending", headers=OP).status_code == 403
    assert gw.post("/api/v1/agent/approve", json={"session_id": "s", "approval_token": "t"}, headers=OP).status_code == 403
    assert gw.get("/api/v1/audit", headers=OP).status_code == 403
    assert gw.get("/api/v1/audit", headers=AU).status_code == 200
    assert gw.get("/api/v1/audit/verify", headers=AU).json()["ok"] is True


def test_session_ids_are_unguessable_and_server_generated(gw, script_llm):
    script_llm([{"thought": "n/a", "final_response": "ok"}])
    r = gw.post("/api/v1/agent/run", json={"query": "Say done"}, headers=OP)
    assert r.json()["session_id"].startswith("sess_") and len(r.json()["session_id"]) > 30
    # a client cannot pick its own id: unknown ids are rejected rather than created
    assert gw.post("/api/v1/agent/run", json={"query": "Say done", "session_id": "sess_attacker"}, headers=OP).status_code == 404


@pytest.mark.parametrize("field,value", [("AGENT_SECURITY_SECRET", "a" * 40), ("WEBHOOK_SIGNING_SECRET", "changeme" * 5),
                                          ("ERP_INTERNAL_API_KEY", "short")])
def test_weak_secrets_rejected_at_startup(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})
