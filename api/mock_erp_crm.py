# api/mock_erp_crm.py
"""Mock ERP/CRM REST service. Service-to-service auth via X-Internal-Token; tenant via X-Tenant-ID.
All writes require an Idempotency-Key and are applied atomically with their ledger entry."""
import hashlib
import json
import uuid
from enum import Enum
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from api.auth import verify_internal_service_token
from api.database import (atomic_transaction, check_idempotency, get_raw_connection,
                          record_idempotency)

app = FastAPI(title="Secured Enterprise Mock ERP/CRM", version="5.0.0")
INTERNAL = [Depends(verify_internal_service_token)]


class LeadStatus(str, Enum):
    NEW = "NEW"
    CONTACTED = "CONTACTED"
    QUALIFIED = "QUALIFIED"
    UNQUALIFIED = "UNQUALIFIED"
    CLOSED_WON = "CLOSED_WON"
    CLOSED_LOST = "CLOSED_LOST"


class TicketPriority(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class LeadStatusUpdateRequest(BaseModel):
    status: LeadStatus
    note_update: Optional[str] = Field(None, max_length=500)


class InventoryAdjustmentRequest(BaseModel):
    adjustment_units: int = Field(..., description="Non-zero integer")
    reason: str = Field(..., min_length=10, max_length=300)

    @field_validator("adjustment_units")
    @classmethod
    def _nonzero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("adjustment_units must not be zero.")
        return v


class CreateTicketRequest(BaseModel):
    customer_id: str = Field(..., min_length=3, max_length=50)
    subject: str = Field(..., min_length=5, max_length=100)
    priority: TicketPriority
    details: str = Field(..., min_length=15, max_length=2000)


def canonical_hash(body: BaseModel) -> str:
    """Pydantic v2 has no sort_keys on model_dump_json, so canonicalise via json.dumps."""
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _tenant(x_tenant_id: str = Header(..., alias="X-Tenant-ID", min_length=1, max_length=64)) -> str:
    return x_tenant_id


def _idem(idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=16, max_length=128)) -> str:
    return idempotency_key


@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------ Reads (tenant scoped) ------------------------------

@app.get("/api/crm/leads/{lead_id}", dependencies=INTERNAL)
def get_lead(lead_id: str, tenant_id: str = Depends(_tenant)):
    conn = get_raw_connection()
    try:
        row = conn.execute("SELECT * FROM leads WHERE tenant_id=? AND lead_id=?",
                           (tenant_id, lead_id.upper().strip())).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, f"Lead '{lead_id}' not found.")
    return {"status": "SUCCESS", "lead": dict(row)}


@app.get("/api/erp/inventory/{sku}", dependencies=INTERNAL)
def get_inventory(sku: str, tenant_id: str = Depends(_tenant)):
    conn = get_raw_connection()
    try:
        row = conn.execute("SELECT * FROM inventory WHERE tenant_id=? AND sku=?",
                           (tenant_id, sku.upper().strip())).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, f"SKU '{sku}' not found.")
    return {"status": "SUCCESS", "inventory": dict(row)}


@app.get("/api/idempotency/{key}", dependencies=INTERNAL)
def get_idempotency_status(key: str, tenant_id: str = Depends(_tenant)):
    """Reconciliation endpoint: was the write with this key applied? Safe, read-only."""
    conn = get_raw_connection()
    try:
        row = conn.execute(
            "SELECT http_method, endpoint, response_code, response_body FROM idempotency_ledger "
            "WHERE tenant_id=? AND idempotency_key=?", (tenant_id, key)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"status": "NOT_FOUND"}
    return {"status": "APPLIED", "http_method": row["http_method"], "endpoint": row["endpoint"],
            "response_code": row["response_code"], "response": json.loads(row["response_body"])}


# ------------------------------ Writes (idempotent) ------------------------------

@app.patch("/api/crm/leads/{lead_id}/status", dependencies=INTERNAL)
def update_lead_status(lead_id: str, request: LeadStatusUpdateRequest,
                       idempotency_key: str = Depends(_idem), tenant_id: str = Depends(_tenant)):
    lead_id = lead_id.upper().strip()
    endpoint = f"/api/crm/leads/{lead_id}/status"
    req_hash = canonical_hash(request)
    try:
        with atomic_transaction() as conn:
            cached, _ = check_idempotency(conn, idempotency_key, tenant_id, "PATCH", endpoint, req_hash)
            if cached is not None:
                return cached
            row = conn.execute("SELECT * FROM leads WHERE tenant_id=? AND lead_id=?",
                               (tenant_id, lead_id)).fetchone()
            if not row:
                raise HTTPException(404, f"Lead '{lead_id}' not found.")
            notes = row["notes"]
            if request.note_update:
                notes = f"{notes} | [AUDIT]: {request.note_update}"
            conn.execute("UPDATE leads SET status=?, notes=?, updated_at=CURRENT_TIMESTAMP "
                         "WHERE tenant_id=? AND lead_id=?", (request.status.value, notes, tenant_id, lead_id))
            updated = dict(conn.execute("SELECT * FROM leads WHERE tenant_id=? AND lead_id=?",
                                        (tenant_id, lead_id)).fetchone())
            body = {"status": "SUCCESS", "lead": updated}
            record_idempotency(conn, idempotency_key, tenant_id, "PATCH", endpoint, req_hash, 200, body)
            return body
    except ValueError as ve:
        raise HTTPException(status.HTTP_409_CONFLICT, str(ve))


@app.post("/api/erp/inventory/{sku}/adjust", dependencies=INTERNAL)
def adjust_inventory(sku: str, request: InventoryAdjustmentRequest,
                     idempotency_key: str = Depends(_idem), tenant_id: str = Depends(_tenant)):
    sku = sku.upper().strip()
    endpoint = f"/api/erp/inventory/{sku}/adjust"
    req_hash = canonical_hash(request)
    try:
        with atomic_transaction() as conn:
            cached, _ = check_idempotency(conn, idempotency_key, tenant_id, "POST", endpoint, req_hash)
            if cached is not None:
                return cached
            row = conn.execute("SELECT * FROM inventory WHERE tenant_id=? AND sku=?", (tenant_id, sku)).fetchone()
            if not row:
                raise HTTPException(404, f"SKU '{sku}' not found.")
            current = row["stock_level"]
            new_stock = current + request.adjustment_units
            if new_stock < 0:
                raise HTTPException(400, f"Insufficient inventory. Requested: {abs(request.adjustment_units)}, "
                                         f"Available: {current}.")
            conn.execute("UPDATE inventory SET stock_level=?, updated_at=CURRENT_TIMESTAMP "
                         "WHERE tenant_id=? AND sku=?", (new_stock, tenant_id, sku))
            body = {"status": "SUCCESS", "sku": sku, "previous_stock": current, "current_stock": new_stock,
                    "adjustment": request.adjustment_units, "reason": request.reason}
            record_idempotency(conn, idempotency_key, tenant_id, "POST", endpoint, req_hash, 200, body)
            return body
    except ValueError as ve:
        raise HTTPException(status.HTTP_409_CONFLICT, str(ve))


@app.post("/api/crm/tickets", status_code=status.HTTP_201_CREATED, dependencies=INTERNAL)
def create_ticket(request: CreateTicketRequest, idempotency_key: str = Depends(_idem),
                  tenant_id: str = Depends(_tenant)):
    endpoint = "/api/crm/tickets"
    req_hash = canonical_hash(request)
    cust = request.customer_id.upper().strip()
    try:
        with atomic_transaction() as conn:
            cached, _ = check_idempotency(conn, idempotency_key, tenant_id, "POST", endpoint, req_hash)
            if cached is not None:
                return cached
            known = (conn.execute("SELECT 1 FROM customers WHERE tenant_id=? AND customer_id=?",
                                  (tenant_id, cust)).fetchone()
                     or conn.execute("SELECT 1 FROM leads WHERE tenant_id=? AND lead_id=?",
                                     (tenant_id, cust)).fetchone())
            if not known:
                raise HTTPException(400, f"Customer '{request.customer_id}' is not a known customer or lead.")
            ticket_id = f"TICK-{uuid.uuid4().hex[:8].upper()}"
            conn.execute("INSERT INTO support_tickets (tenant_id, ticket_id, customer_id, subject, priority, "
                         "details, status) VALUES (?,?,?,?,?,?,'OPEN')",
                         (tenant_id, ticket_id, cust, request.subject, request.priority.value, request.details))
            ticket = dict(conn.execute("SELECT * FROM support_tickets WHERE tenant_id=? AND ticket_id=?",
                                       (tenant_id, ticket_id)).fetchone())
            body = {"status": "SUCCESS", "ticket": ticket}
            record_idempotency(conn, idempotency_key, tenant_id, "POST", endpoint, req_hash, 201, body)
            return body
    except ValueError as ve:
        raise HTTPException(status.HTTP_409_CONFLICT, str(ve))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)
