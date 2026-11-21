# agent/store.py
"""Session persistence with optimistic concurrency (compare-and-swap on state_version)."""
import time
from typing import List, Optional

from agent.state import AgentState, SessionStatus
from api.database import atomic_transaction, get_raw_connection


class ConcurrencyError(Exception):
    """Another request modified the session first."""


def create_session(state: AgentState) -> None:
    with atomic_transaction() as conn:
        conn.execute(
            "INSERT INTO agent_sessions (session_id, tenant_id, owner_id, state_version, status, "
            "approval_expires_at, state_json, updated_epoch) VALUES (?,?,?,?,?,?,?,?)",
            (state.session_id, state.tenant_id, state.owner_id, state.state_version, state.status.value,
             state.approval_expires_at, state.model_dump_json(), int(time.time())))


def load_session(session_id: str) -> Optional[AgentState]:
    conn = get_raw_connection()
    try:
        row = conn.execute("SELECT state_json, state_version, updated_epoch FROM agent_sessions WHERE session_id=?",
                           (session_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    state = AgentState.model_validate_json(row["state_json"])
    state.state_version = row["state_version"]
    return state


def last_update_epoch(session_id: str) -> int:
    conn = get_raw_connection()
    try:
        row = conn.execute("SELECT updated_epoch FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
    finally:
        conn.close()
    return row["updated_epoch"] if row else 0


def save_session(state: AgentState, expected_version: int) -> int:
    """Compare-and-swap. Exactly one concurrent writer per version wins; the rest get ConcurrencyError."""
    new_version = expected_version + 1
    state.state_version = new_version
    with atomic_transaction() as conn:
        cur = conn.execute(
            "UPDATE agent_sessions SET state_version=?, status=?, approval_expires_at=?, state_json=?, "
            "updated_epoch=? WHERE session_id=? AND state_version=?",
            (new_version, state.status.value, state.approval_expires_at, state.model_dump_json(),
             int(time.time()), state.session_id, expected_version))
        if cur.rowcount == 0:
            raise ConcurrencyError("Session was modified by another request.")
    return new_version


def list_awaiting_approval(tenant_id: str) -> List[AgentState]:
    conn = get_raw_connection()
    try:
        rows = conn.execute("SELECT state_json, state_version FROM agent_sessions WHERE tenant_id=? AND status=? "
                            "ORDER BY updated_epoch ASC", (tenant_id, SessionStatus.AWAITING_APPROVAL.value)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        s = AgentState.model_validate_json(r["state_json"])
        s.state_version = r["state_version"]
        out.append(s)
    return out
