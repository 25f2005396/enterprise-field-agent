import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent import webhooks
from agent.security import make_stream_ticket
from agent.webhooks import (enqueue_approval_webhook, is_safe_webhook_url, process_outbox, sign_body,
                            verify_webhook_signature)
from api.auth import AuthenticatedUser, UserRole
from api.config import settings
from tests.conftest import AP, ENV, FINAL, OP, propose_adjust, tool


# ----------------------------------------------------------------- SSRF

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/hook", "http://localhost:9000/hook", "http://[::1]/hook", "http://[::ffff:127.0.0.1]/hook",
    "http://10.0.0.5/hook", "http://192.168.1.10/hook", "http://169.254.169.254/latest/meta-data",
    "http://100.64.0.1/hook", "http://0.0.0.0/hook", "ftp://93.184.216.34/hook", "http:///nohost",
    "https://user:pw@93.184.216.34/hook", "not a url", ""])
def test_ssrf_targets_are_rejected_even_in_development(url):
    assert is_safe_webhook_url(url) is False


def test_public_ip_allowed_but_http_blocked_in_production(monkeypatch):
    assert is_safe_webhook_url("https://93.184.216.34/hook") is True
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert is_safe_webhook_url("http://93.184.216.34/hook") is False
    assert is_safe_webhook_url("https://93.184.216.34/hook") is True


def test_private_override_is_ignored_in_production(monkeypatch):
    monkeypatch.setattr(settings, "WEBHOOK_ALLOW_PRIVATE_TARGETS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert is_safe_webhook_url("https://127.0.0.1/hook") is False


# ----------------------------------------------------------------- signatures

def test_signature_roundtrip_tamper_and_replay():
    ts, body = int(time.time()), '{"a":1}'
    sig = sign_body(ts, body)
    assert verify_webhook_signature(ts, body, sig)
    assert not verify_webhook_signature(ts, '{"a":2}', sig)
    old = ts - 3600
    assert not verify_webhook_signature(old, body, sign_body(old, body))


# ----------------------------------------------------------------- outbox + retry

class Receiver:
    def __init__(self, statuses):
        self.statuses, self.hits = list(statuses), []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                outer.hits.append((dict(self.headers), body))
                code = outer.statuses.pop(0) if outer.statuses else 200
                self.send_response(code); self.end_headers()

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/hook"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def receiver(monkeypatch):
    monkeypatch.setattr(settings, "WEBHOOK_ALLOW_PRIVATE_TARGETS", True)   # dev-only escape hatch for localhost
    made = []

    def make(statuses):
        r = Receiver(statuses)
        made.append(r)
        monkeypatch.setattr(settings, "APPROVAL_WEBHOOK_URL", r.url)
        return r
    yield make
    for r in made:
        r.close()


def row(event_id):
    c = sqlite3.connect(ENV["DB_PATH"]); c.row_factory = sqlite3.Row
    return c.execute("SELECT * FROM webhook_outbox WHERE event_id=?", (event_id,)).fetchone()


def test_outbox_retries_with_backoff_then_delivers_signed_payload(receiver):
    rcv = receiver([500, 200])
    ev = enqueue_approval_webhook("sess_1", "tenant-default", "adjust_inventory", {"sku": "SKU-992"}, "h" * 64, int(time.time()) + 600)
    now = int(time.time())
    assert process_outbox(now=now) == 1
    r = row(ev); assert r["status"] == "PENDING" and r["attempts"] == 1 and r["next_attempt_at"] > now
    assert process_outbox(now=now) == 0                         # backoff: not due yet
    assert process_outbox(now=now + 1000) == 1
    r = row(ev); assert r["status"] == "DELIVERED" and r["attempts"] == 2
    headers, body = rcv.hits[-1]
    assert verify_webhook_signature(int(headers["X-Event-Timestamp"]), body, headers["X-Agent-Signature"])
    assert "approval_token" not in body and '"action_hash"' in body      # no secrets on the wire


def test_outbox_goes_dead_after_max_attempts(receiver, monkeypatch):
    receiver([500] * 10)
    monkeypatch.setattr(settings, "WEBHOOK_MAX_ATTEMPTS", 3)
    ev = enqueue_approval_webhook("sess_1", "tenant-default", "adjust_inventory", {"sku": "SKU-992"}, "h" * 64, int(time.time()) + 600)
    t = int(time.time())
    for i in range(5):
        process_outbox(now=t + i * 2000)
    r = row(ev); assert r["status"] == "DEAD" and r["attempts"] == 3 and "HTTP 500" in r["last_error"]


def test_delivery_rechecks_ssrf_policy(receiver, monkeypatch):
    receiver([200])
    ev = enqueue_approval_webhook("sess_1", "tenant-default", "adjust_inventory", {}, "h" * 64, int(time.time()) + 600)
    monkeypatch.setattr(settings, "WEBHOOK_ALLOW_PRIVATE_TARGETS", False)   # policy tightened after enqueue
    process_outbox(now=int(time.time()))
    assert row(ev)["last_error"] == "blocked by SSRF policy"


def test_webhook_enqueued_only_after_state_is_saved(receiver, gw, script_llm):
    receiver([200])
    sid, _ = propose_adjust(gw, script_llm)
    c = sqlite3.connect(ENV["DB_PATH"])
    assert c.execute("SELECT COUNT(*) FROM webhook_outbox").fetchone()[0] == 1
    assert c.execute("SELECT status FROM agent_sessions WHERE session_id=?", (sid,)).fetchone()[0] == "AWAITING_APPROVAL"


def test_unsafe_webhook_url_is_never_enqueued(monkeypatch):
    monkeypatch.setattr(settings, "APPROVAL_WEBHOOK_URL", "http://169.254.169.254/x")
    assert enqueue_approval_webhook("s", "t", "tool", {}, "h", 1) is None


# ----------------------------------------------------------------- SSE

def events(text):
    out = []
    for block in text.strip().split("\n\n"):
        if block.startswith(":"):
            continue
        name = next(l for l in block.splitlines() if l.startswith("event:"))[7:]
        data = json.loads(next(l for l in block.splitlines() if l.startswith("data:"))[6:])
        out.append((name, data))
    return out


def test_sse_streams_thought_action_observation_without_token(gw, script_llm):
    sid, token = propose_adjust(gw, script_llm)
    ticket = gw.post(f"/api/v1/agent/sessions/{sid}/stream-ticket", headers=OP).json()["ticket"]
    r = gw.get(f"/api/v1/agent/sessions/{sid}/events", params={"ticket": ticket})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    names = [n for n, _ in events(r.text)]
    assert names == ["thought_action", "observation", "thought_action", "approval_required"]
    assert token not in r.text and "approval_token" not in r.text


def test_sse_terminal_status_event(gw, script_llm):
    script_llm([tool("lookup_inventory", sku="SKU-992"), FINAL])
    sid = gw.post("/api/v1/agent/run", json={"query": "Check SKU-992"}, headers=OP).json()["session_id"]
    r = gw.get(f"/api/v1/agent/sessions/{sid}/events", headers=OP)           # header auth for non-browser clients
    evs = events(r.text)
    assert evs[-1][0] == "status" and evs[-1][1]["status"] == "COMPLETED"


def test_sse_ticket_security(gw, script_llm):
    sid, _ = propose_adjust(gw, script_llm)
    user = AuthenticatedUser(user_id="usr-operator-01", tenant_id="tenant-default", role=UserRole.OPERATOR)
    url = f"/api/v1/agent/sessions/{sid}/events"
    assert gw.get(url).status_code == 401                                        # no credentials
    assert gw.get(url, params={"ticket": make_stream_ticket(user, sid, ttl=-1)}).status_code == 403   # expired
    assert gw.get(url, params={"ticket": make_stream_ticket(user, "sess_other")}).status_code == 403  # other session
    good = make_stream_ticket(user, sid)
    assert gw.get(url, params={"ticket": good[:-3] + "abc"}).status_code == 403  # tampered
    assert gw.get(url, params={"ticket": good}).status_code == 200


def test_sse_respects_tenant_boundary(gw, script_llm):
    sid, _ = propose_adjust(gw, script_llm)
    acme = {"X-API-Key": "acme_operator_key_24_chars_xxxx"}
    assert gw.post(f"/api/v1/agent/sessions/{sid}/stream-ticket", headers=acme).status_code == 404
    assert gw.get(f"/api/v1/agent/sessions/{sid}/events", headers=acme).status_code == 404
