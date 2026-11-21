# agent/state.py
"""Typed state, status machine and the public (secret-free) DTO."""
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from api.config import settings


class SessionStatus(str, Enum):
    IDLE = "IDLE"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    PAUSED_RECONCILIATION_REQUIRED = "PAUSED_RECONCILIATION_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


TERMINAL = {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.ABORTED}
RESTARTABLE = TERMINAL | {SessionStatus.IDLE}


class ToolCallRecord(BaseModel):
    tool_name: str
    arguments: Dict[str, Any]
    idempotency_key: str
    action_hash: str
    is_sensitive_write: bool


class ExecutionStep(BaseModel):
    """One Thought -> Action -> Observation cycle."""
    step_number: int
    thought: str
    tool_call: Optional[ToolCallRecord] = None   # the Action
    observation: Optional[str] = None            # the Observation
    is_error: bool = False
    in_doubt: bool = False


class AgentState(BaseModel):
    session_id: str
    tenant_id: str
    owner_id: str
    state_version: int = 1
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    status: SessionStatus = SessionStatus.IDLE
    user_query: str
    steps: List[ExecutionStep] = Field(default_factory=list)

    # Human-in-the-loop
    pending_approval: Optional[ToolCallRecord] = None
    approval_expires_at: Optional[int] = None
    approval_binding: Optional[str] = None   # server-side HMAC; the approver-facing token is NOT stored
    approved_by: Optional[str] = None
    staged_action: Optional[ToolCallRecord] = None

    # Write whose outcome is unknown (timeout / 5xx). Never replanned; reconciled by idempotency key.
    in_doubt_action: Optional[ToolCallRecord] = None

    failed_fingerprints: List[str] = Field(default_factory=list)
    plan_steps_count: int = 0
    max_plan_steps: int = Field(default_factory=lambda: settings.MAX_PLAN_STEPS)
    retry_count: int = 0
    max_retries: int = Field(default_factory=lambda: settings.MAX_RETRIES)
    final_response: Optional[str] = None

    def begin_new_run(self, query: str) -> None:
        """Re-use a finished session for a new request: fresh trace, counters and idempotency namespace."""
        self.user_query = query
        self.run_id = uuid.uuid4().hex[:16]
        self.status = SessionStatus.PLANNING
        self.steps = []
        self.pending_approval = self.staged_action = self.in_doubt_action = None
        self.approval_expires_at = self.approval_binding = self.approved_by = None
        self.failed_fingerprints = []
        self.plan_steps_count = self.retry_count = 0
        self.final_response = None


class SessionPublicDTO(BaseModel):
    """Safe to return to clients: no tokens, bindings or secrets."""
    session_id: str
    tenant_id: str
    owner_id: str
    state_version: int
    status: SessionStatus
    user_query: str
    requires_approval: bool
    pending_tool: Optional[str] = None
    pending_arguments: Optional[Dict[str, Any]] = None
    pending_action_hash: Optional[str] = None
    approval_expires_at: Optional[int] = None
    in_doubt_tool: Optional[str] = None
    steps: List[ExecutionStep]
    final_response: Optional[str] = None


def to_public(state: AgentState) -> SessionPublicDTO:
    p = state.pending_approval
    d = state.in_doubt_action
    return SessionPublicDTO(
        session_id=state.session_id, tenant_id=state.tenant_id, owner_id=state.owner_id,
        state_version=state.state_version, status=state.status, user_query=state.user_query,
        requires_approval=state.status == SessionStatus.AWAITING_APPROVAL and p is not None,
        pending_tool=p.tool_name if p else None, pending_arguments=p.arguments if p else None,
        pending_action_hash=p.action_hash if p else None, approval_expires_at=state.approval_expires_at,
        in_doubt_tool=d.tool_name if d else None, steps=state.steps, final_response=state.final_response,
    )
