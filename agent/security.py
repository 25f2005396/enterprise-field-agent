# agent/security.py
"""Action hashing, approval tokens, server-side bindings and short-lived SSE stream tickets."""
import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

from api.auth import AuthenticatedUser, UserRole
from api.config import settings


def _canon(arguments: Dict[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"))


def _hmac(label: str, *parts: Any) -> str:
    msg = "|".join([label, *[str(p) for p in parts]])
    return hmac.new(settings.AGENT_SECURITY_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()


def compute_action_hash(session_id: str, tenant_id: str, tool_name: str,
                        arguments: Dict[str, Any], idempotency_key: str) -> str:
    raw = f"{session_id}|{tenant_id}|{tool_name}|{_canon(arguments)}|{idempotency_key}"
    return hashlib.sha256(raw.encode()).hexdigest()


def action_fingerprint(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Identity of a *call shape* (tool + args), independent of the per-step idempotency key.
    Used to block the agent from repeating an identical failing call."""
    return hashlib.sha256(f"{tool_name}|{_canon(arguments)}".encode()).hexdigest()[:16]


# ---- Approval: approver-facing token (not stored) + server-side binding (stored) ----

def make_approval_token(action_hash: str, expires_at: int) -> str:
    return f"{expires_at}.{_hmac('approval-token', action_hash, expires_at)}"


def make_approval_binding(action_hash: str, expires_at: int) -> str:
    return _hmac("approval-binding", action_hash, expires_at)


def verify_approval_token(token: str, action_hash: str, expires_at: int) -> bool:
    try:
        exp_str, _sig = token.split(".", 1)
        if int(exp_str) != expires_at or time.time() > expires_at:
            return False
        return hmac.compare_digest(token.encode(), make_approval_token(action_hash, expires_at).encode())
    except Exception:
        return False


def verify_binding(binding: Optional[str], action_hash: str, expires_at: int) -> bool:
    return bool(binding) and hmac.compare_digest(binding.encode(), make_approval_binding(action_hash, expires_at).encode())


# ---- SSE stream tickets: 60s, bound to one user and one session; keeps API keys out of URLs ----

def make_stream_ticket(user: AuthenticatedUser, session_id: str, ttl: int = 60) -> str:
    payload = {"u": user.user_id, "t": user.tenant_id, "r": user.role.value, "s": session_id,
               "e": int(time.time()) + ttl}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{body}.{_hmac('stream-ticket', body)}"


def verify_stream_ticket(ticket: str, session_id: str) -> Optional[AuthenticatedUser]:
    try:
        body, sig = ticket.split(".", 1)
        if not hmac.compare_digest(sig.encode(), _hmac("stream-ticket", body).encode()):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if payload["s"] != session_id or time.time() > payload["e"]:
            return None
        return AuthenticatedUser(user_id=payload["u"], tenant_id=payload["t"], role=UserRole(payload["r"]))
    except Exception:
        return None
