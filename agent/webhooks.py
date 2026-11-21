# agent/webhooks.py
"""Signed webhooks with SSRF defence and a durable outbox + retry worker.
Webhook bodies never contain secrets or approval tokens - approvers fetch the token from the
authenticated GET /api/v1/approvals/pending endpoint."""
import hashlib
import hmac
import ipaddress
import json
import socket
import threading
import time
import uuid
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

from api.config import settings
from api.database import atomic_transaction


def _ip_blocked(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (not ip.is_global) or ip.is_multicast


def is_safe_webhook_url(url: str) -> bool:
    """Resolves the hostname and rejects any private / loopback / link-local / reserved target.
    NOTE: residual DNS-rebinding risk between check and connect; mitigate with an egress proxy in production."""
    try:
        p = urlparse(url)
        host = p.hostname
        if not host or p.username or p.password:
            return False
        prod = settings.ENVIRONMENT == "production"
        if p.scheme != "https" and (prod or p.scheme != "http"):
            return False
        if settings.WEBHOOK_ALLOW_PRIVATE_TARGETS and not prod:
            return True
        port = p.port or (443 if p.scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        return bool(infos) and not any(_ip_blocked(i[4][0]) for i in infos)
    except Exception:
        return False


def sign_body(timestamp: int, body: str) -> str:
    return hmac.new(settings.WEBHOOK_SIGNING_SECRET.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()


def verify_webhook_signature(timestamp: int, body: str, signature: str, tolerance: int = 300) -> bool:
    """Reference verifier for receivers: constant-time compare + replay window."""
    if abs(time.time() - timestamp) > tolerance:
        return False
    return hmac.compare_digest(sign_body(timestamp, body), signature)


def enqueue_approval_webhook(session_id: str, tenant_id: str, tool_name: str, arguments: Dict[str, Any],
                             action_hash: str, expires_at: int) -> Optional[str]:
    url = settings.APPROVAL_WEBHOOK_URL
    if not url or not is_safe_webhook_url(url):
        return None
    event_id = f"evt_{uuid.uuid4().hex}"
    payload = {"event_id": event_id, "event_type": "HUMAN_APPROVAL_REQUIRED", "session_id": session_id,
               "tenant_id": tenant_id, "tool_name": tool_name, "arguments": arguments,
               "action_hash": action_hash, "expires_at": expires_at,
               "fetch_token_from": "/api/v1/approvals/pending"}
    with atomic_transaction() as conn:
        conn.execute("INSERT INTO webhook_outbox (event_id, target_url, payload_json, status, next_attempt_at) "
                     "VALUES (?,?,?, 'PENDING', ?)",
                     (event_id, url, json.dumps(payload, sort_keys=True), int(time.time())))
    return event_id


def _backoff(attempts: int) -> int:
    return min(30 * 2 ** max(attempts - 1, 0), 900)


def process_outbox(now: Optional[int] = None, limit: int = 20) -> int:
    """Delivers due rows. Rows are leased (next_attempt_at pushed out) before sending so two workers
    never send the same event. Returns the number of rows attempted."""
    now = int(now if now is not None else time.time())
    with atomic_transaction() as conn:
        rows = conn.execute("SELECT * FROM webhook_outbox WHERE status='PENDING' AND next_attempt_at<=? "
                            "ORDER BY created_at LIMIT ?", (now, limit)).fetchall()
        for r in rows:
            conn.execute("UPDATE webhook_outbox SET next_attempt_at=? WHERE event_id=?", (now + 60, r["event_id"]))
    for r in rows:
        error: Optional[str] = None
        if not is_safe_webhook_url(r["target_url"]):
            error = "blocked by SSRF policy"
        else:
            ts = int(time.time())
            try:
                with httpx.Client(timeout=4.0, follow_redirects=False) as c:
                    res = c.post(r["target_url"], content=r["payload_json"], headers={
                        "Content-Type": "application/json", "X-Agent-Signature": sign_body(ts, r["payload_json"]),
                        "X-Event-ID": r["event_id"], "X-Event-Timestamp": str(ts)})
                if not res.is_success:
                    error = f"HTTP {res.status_code}"
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        with atomic_transaction() as conn:
            attempts = r["attempts"] + 1
            if error is None:
                conn.execute("UPDATE webhook_outbox SET status='DELIVERED', attempts=?, last_error=NULL "
                             "WHERE event_id=?", (attempts, r["event_id"]))
            elif attempts >= settings.WEBHOOK_MAX_ATTEMPTS:
                conn.execute("UPDATE webhook_outbox SET status='DEAD', attempts=?, last_error=? WHERE event_id=?",
                             (attempts, error, r["event_id"]))
            else:
                conn.execute("UPDATE webhook_outbox SET attempts=?, last_error=?, next_attempt_at=? WHERE event_id=?",
                             (attempts, error, now + _backoff(attempts), r["event_id"]))
    return len(rows)


def start_outbox_worker(stop: threading.Event, interval: float = 2.0) -> threading.Thread:
    def loop():
        while not stop.is_set():
            try:
                process_outbox()
            except Exception:
                pass
            stop.wait(interval)
    t = threading.Thread(target=loop, name="webhook-outbox", daemon=True)
    t.start()
    return t
