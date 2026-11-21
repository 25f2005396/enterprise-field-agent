# server.py
"""Enterprise FastAPI gateway: RBAC, tenant isolation, approvals, reconciliation, SSE, audit."""
import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent import service
from agent.audit import read_audit, verify_audit_chain
from agent.security import make_stream_ticket, verify_stream_ticket
from agent.service import ServiceError
from agent.state import SessionPublicDTO, SessionStatus, TERMINAL, to_public
from agent.store import load_session
from agent.webhooks import start_outbox_worker
from api.auth import AuthenticatedUser, UserRole, authenticate, get_current_user, require_roles


@asynccontextmanager
async def lifespan(_: FastAPI):
    stop = threading.Event()
    start_outbox_worker(stop)
    yield
    stop.set()


app = FastAPI(title="Enterprise Field Agent Gateway", version="5.0.0", lifespan=lifespan)


@app.exception_handler(ServiceError)
async def _service_error(_, exc: ServiceError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


class AgentRunRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=1000)
    session_id: Optional[str] = Field(None, description="Continue an existing, finished session")


class AgentApprovalRequest(BaseModel):
    session_id: str
    approval_token: str


class AgentRejectRequest(BaseModel):
    session_id: str
    reason: str = Field("Operation rejected by authorized approver.", max_length=300)


class ReconcileRequest(BaseModel):
    session_id: str


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/api/v1/agent/run", response_model=SessionPublicDTO)
def run_agent(req: AgentRunRequest, user: AuthenticatedUser = Depends(require_roles([UserRole.OPERATOR]))):
    return to_public(service.start_run(user, req.query, req.session_id))


@app.get("/api/v1/approvals/pending")
def list_pending(user: AuthenticatedUser = Depends(require_roles([UserRole.APPROVER]))):
    return {"pending": service.pending_approvals(user)}


@app.post("/api/v1/agent/approve", response_model=SessionPublicDTO)
def approve_action(req: AgentApprovalRequest, user: AuthenticatedUser = Depends(require_roles([UserRole.APPROVER]))):
    return to_public(service.approve(user, req.session_id, req.approval_token))


@app.post("/api/v1/agent/reject", response_model=SessionPublicDTO)
def reject_action(req: AgentRejectRequest,
                  user: AuthenticatedUser = Depends(require_roles([UserRole.APPROVER, UserRole.OPERATOR]))):
    return to_public(service.reject(user, req.session_id, req.reason))


@app.post("/api/v1/agent/reconcile", response_model=SessionPublicDTO)
def reconcile_session(req: ReconcileRequest,
                      user: AuthenticatedUser = Depends(require_roles([UserRole.APPROVER, UserRole.OPERATOR]))):
    return to_public(service.reconcile(user, req.session_id))


@app.get("/api/v1/agent/sessions/{session_id}", response_model=SessionPublicDTO)
def get_session(session_id: str, user: AuthenticatedUser = Depends(get_current_user)):
    return to_public(service.get_session(user, session_id))


@app.post("/api/v1/agent/sessions/{session_id}/stream-ticket")
def stream_ticket(session_id: str, user: AuthenticatedUser = Depends(get_current_user)):
    service.get_session(user, session_id)                       # access check
    return {"ticket": make_stream_ticket(user, session_id), "expires_in": 60}


@app.get("/api/v1/agent/sessions/{session_id}/events")
async def stream_events(session_id: str, ticket: Optional[str] = Query(None),
                        x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """SSE. Browsers use a 60-second ticket (no API key in the URL); CLI clients may send X-API-Key."""
    user = verify_stream_ticket(ticket, session_id) if ticket else authenticate(x_api_key)
    if user is None:
        raise HTTPException(403, "Invalid or expired stream ticket.")
    try:
        await asyncio.to_thread(service.get_session, user, session_id)
    except ServiceError as e:
        raise HTTPException(e.status_code, e.detail)

    async def gen() -> AsyncGenerator[str, None]:
        seen_action, seen_obs = set(), set()
        deadline = time.time() + 900
        while time.time() < deadline:
            s = await asyncio.to_thread(load_session, session_id)
            if s is None:
                break
            for st in s.steps:
                if st.step_number not in seen_action:
                    seen_action.add(st.step_number)
                    yield "event: thought_action\ndata: " + json.dumps({
                        "step": st.step_number, "thought": st.thought,
                        "action": ({"tool": st.tool_call.tool_name, "arguments": st.tool_call.arguments}
                                   if st.tool_call else None)}) + "\n\n"
                if st.observation is not None and st.step_number not in seen_obs:
                    seen_obs.add(st.step_number)
                    yield "event: observation\ndata: " + json.dumps({
                        "step": st.step_number, "observation": st.observation,
                        "is_error": st.is_error, "in_doubt": st.in_doubt}) + "\n\n"
            if s.status == SessionStatus.AWAITING_APPROVAL and s.pending_approval:   # never includes a token
                yield "event: approval_required\ndata: " + json.dumps({
                    "tool": s.pending_approval.tool_name, "arguments": s.pending_approval.arguments,
                    "action_hash": s.pending_approval.action_hash, "expires_at": s.approval_expires_at}) + "\n\n"
                break
            if s.status in TERMINAL or s.status == SessionStatus.PAUSED_RECONCILIATION_REQUIRED:
                yield "event: status\ndata: " + json.dumps({"status": s.status.value,
                                                            "final_response": s.final_response}) + "\n\n"
                break
            yield ": ping\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/v1/audit")
def audit_trail(session_id: Optional[str] = None,
                user: AuthenticatedUser = Depends(require_roles([UserRole.AUDITOR]))):
    return {"events": read_audit(user.tenant_id, session_id)}


@app.get("/api/v1/audit/verify")
def audit_verify(user: AuthenticatedUser = Depends(require_roles([UserRole.AUDITOR]))):
    return verify_audit_chain()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
