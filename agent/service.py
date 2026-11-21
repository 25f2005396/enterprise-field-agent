# agent/service.py
"""Business logic shared by the HTTP gateway and the CLI. Raises ServiceError(status, detail)."""
import secrets
import time
from typing import Callable, Dict, List, Optional

from agent.audit import record_audit_event
from agent.graph import reconcile_in_doubt
from agent.runner import run_graph
from agent.security import make_approval_token, verify_approval_token, verify_binding, compute_action_hash
from agent.state import AgentState, RESTARTABLE, SessionStatus
from agent.store import (ConcurrencyError, create_session, last_update_epoch, list_awaiting_approval,
                         load_session, save_session)
from agent.webhooks import enqueue_approval_webhook
from api.auth import AuthenticatedUser, UserRole
from api.config import settings

OnUpdate = Optional[Callable[[AgentState], None]]


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def _audit(state: AgentState, user: AuthenticatedUser, event: str, outcome: str, **kw) -> None:
    record_audit_event(state.session_id, state.tenant_id, user.user_id, user.role.value, event, outcome, **kw)


def _load_scoped(user: AuthenticatedUser, session_id: str) -> AgentState:
    """Cross-tenant sessions are indistinguishable from missing ones (404)."""
    state = load_session(session_id)
    if state is None or state.tenant_id != user.tenant_id:
        raise ServiceError(404, "Session not found.")
    return state


def _can_read(user: AuthenticatedUser, state: AgentState) -> bool:
    return user.role in (UserRole.ADMIN, UserRole.AUDITOR, UserRole.APPROVER) or state.owner_id == user.user_id


def get_session(user: AuthenticatedUser, session_id: str) -> AgentState:
    state = _load_scoped(user, session_id)
    if not _can_read(user, state):
        raise ServiceError(403, "Access denied.")
    return state


def _post_run(state: AgentState) -> None:
    """Runs AFTER the awaiting-approval state is durably saved, so approvers never see stale state."""
    if state.status == SessionStatus.AWAITING_APPROVAL and state.pending_approval:
        p = state.pending_approval
        record_audit_event(state.session_id, state.tenant_id, "agent", "AGENT", "APPROVAL_REQUESTED", "PENDING",
                           p.tool_name, p.idempotency_key, p.arguments)
        enqueue_approval_webhook(state.session_id, state.tenant_id, p.tool_name, p.arguments,
                                 p.action_hash, state.approval_expires_at or 0)


def _run(state: AgentState, on_update: OnUpdate) -> AgentState:
    try:
        final = run_graph(state, on_update)
    except ConcurrencyError:
        raise ServiceError(409, "Concurrency collision: session was modified by another request.")
    _post_run(final)
    return final


def start_run(user: AuthenticatedUser, query: str, session_id: Optional[str] = None,
              on_update: OnUpdate = None) -> AgentState:
    if session_id:
        state = _load_scoped(user, session_id)
        if state.owner_id != user.user_id and user.role != UserRole.ADMIN:
            raise ServiceError(403, "Forbidden: you do not own this session.")
        if state.status not in RESTARTABLE:
            raise ServiceError(409, f"Session is busy ({state.status.value}). "
                                    "Approve/reject/reconcile it or wait for it to finish.")
        state.begin_new_run(query)
        try:
            save_session(state, state.state_version)     # CAS claim: only one concurrent run can start
        except ConcurrencyError:
            raise ServiceError(409, "Concurrency collision: session was modified by another request.")
    else:
        state = AgentState(session_id=f"sess_{secrets.token_urlsafe(24)}", tenant_id=user.tenant_id,
                           owner_id=user.user_id, user_query=query, status=SessionStatus.PLANNING)
        create_session(state)
    _audit(state, user, "SESSION_RUN_TRIGGERED", "SUCCESS", details={"query": query[:200]})
    return _run(state, on_update)


def pending_approvals(user: AuthenticatedUser) -> List[Dict]:
    """The ONLY place approval tokens are issued: authenticated approvers of the same tenant."""
    if user.role not in (UserRole.APPROVER, UserRole.ADMIN):
        raise ServiceError(403, "Only approvers can list pending approvals.")
    out = []
    for s in list_awaiting_approval(user.tenant_id):
        p = s.pending_approval
        if not p or not s.approval_expires_at or s.approval_expires_at < time.time():
            continue
        out.append({"session_id": s.session_id, "owner_id": s.owner_id, "tool": p.tool_name,
                    "arguments": p.arguments, "action_hash": p.action_hash,
                    "expires_at": s.approval_expires_at, "can_approve": s.owner_id != user.user_id,
                    "approval_token": make_approval_token(p.action_hash, s.approval_expires_at)})
    return out


def approve(user: AuthenticatedUser, session_id: str, approval_token: str, on_update: OnUpdate = None) -> AgentState:
    if user.role not in (UserRole.APPROVER, UserRole.ADMIN):
        raise ServiceError(403, "Only approvers can approve actions.")
    state = _load_scoped(user, session_id)
    p = state.pending_approval
    if state.status != SessionStatus.AWAITING_APPROVAL or p is None or state.approval_expires_at is None:
        raise ServiceError(409, "No action is awaiting approval.")
    if state.owner_id == user.user_id:                     # separation of duties - applies to ADMIN too
        _audit(state, user, "APPROVAL_DENIED_SELF", "REJECTED", tool_name=p.tool_name)
        raise ServiceError(403, "Separation of duties: you cannot approve your own request.")
    if time.time() > state.approval_expires_at:
        state.status, state.pending_approval, state.approval_binding = SessionStatus.ABORTED, None, None
        state.final_response = "Workflow aborted: approval window expired."
        try:
            save_session(state, state.state_version)
        except ConcurrencyError:
            pass
        _audit(state, user, "APPROVAL_EXPIRED", "ABORTED", tool_name=p.tool_name)
        raise ServiceError(410, "Approval window expired; the workflow was aborted.")

    intact = compute_action_hash(state.session_id, state.tenant_id, p.tool_name, p.arguments,
                                 p.idempotency_key) == p.action_hash
    if not (intact and verify_binding(state.approval_binding, p.action_hash, state.approval_expires_at)
            and verify_approval_token(approval_token, p.action_hash, state.approval_expires_at)):
        _audit(state, user, "APPROVAL_FAILED_VERIFICATION", "REJECTED", tool_name=p.tool_name)
        raise ServiceError(403, "Invalid, expired or tampered approval token.")

    # ---- Atomic one-time claim: persist EXECUTING *before* running. Exactly one approver wins the CAS. ----
    state.staged_action, state.pending_approval, state.approval_binding = p, None, None
    state.approved_by, state.status = user.user_id, SessionStatus.EXECUTING
    try:
        save_session(state, state.state_version)
    except ConcurrencyError:
        raise ServiceError(409, "Already handled: another request claimed this approval.")
    _audit(state, user, "APPROVAL_GRANTED", "SUCCESS", tool_name=p.tool_name, idempotency_key=p.idempotency_key)
    return _run(state, on_update)


def reject(user: AuthenticatedUser, session_id: str, reason: str) -> AgentState:
    state = _load_scoped(user, session_id)
    if user.role == UserRole.AUDITOR or (user.role == UserRole.OPERATOR and state.owner_id != user.user_id):
        raise ServiceError(403, "Not permitted to reject this action.")
    if state.status != SessionStatus.AWAITING_APPROVAL:
        raise ServiceError(409, "No action is awaiting approval.")
    state.pending_approval = state.approval_binding = None
    state.status = SessionStatus.ABORTED
    state.final_response = f"Workflow aborted: {reason[:300]}"
    try:
        save_session(state, state.state_version)
    except ConcurrencyError:
        raise ServiceError(409, "Already handled: another request changed this session.")
    _audit(state, user, "APPROVAL_REJECTED", "ABORTED", details={"reason": reason[:300]})
    return state


def reconcile(user: AuthenticatedUser, session_id: str, on_update: OnUpdate = None) -> AgentState:
    """Resolve an in-doubt write (paused session, or an EXECUTING session abandoned by a crashed worker)."""
    state = _load_scoped(user, session_id)
    if user.role == UserRole.AUDITOR or (user.role == UserRole.OPERATOR and state.owner_id != user.user_id):
        raise ServiceError(403, "Not permitted to reconcile this session.")
    stale = (state.status == SessionStatus.EXECUTING and state.staged_action is not None
             and time.time() - last_update_epoch(state.session_id) > settings.STALE_EXECUTION_SECONDS)
    if stale:                                              # crash recovery: the call may have been sent
        state.in_doubt_action, state.staged_action = state.staged_action, None
    elif state.status != SessionStatus.PAUSED_RECONCILIATION_REQUIRED or state.in_doubt_action is None:
        raise ServiceError(409, "Session has no in-doubt write to reconcile.")

    if reconcile_in_doubt(state):
        state.status, state.final_response = SessionStatus.PLANNING, None
        _audit(state, user, "RECONCILE", "RESOLVED")
    else:
        state.status = SessionStatus.PAUSED_RECONCILIATION_REQUIRED
        _audit(state, user, "RECONCILE", "STILL_IN_DOUBT")
    try:
        save_session(state, state.state_version)
    except ConcurrencyError:
        raise ServiceError(409, "Concurrency collision: session was modified by another request.")
    if state.status == SessionStatus.PLANNING:
        return _run(state, on_update)                       # let the agent continue its plan
    return state
