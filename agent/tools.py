# agent/tools.py
"""Schema-validated tool clients for the ERP/CRM API.

All HTTP goes through _send() (single choke point, easy to fault-inject in tests). Failures are
classified: a write is IN-DOUBT when the request may have been processed (read timeout, dropped
connection, 5xx); it is NOT in doubt when it never left the machine (connect error)."""
from typing import Any, Dict, Optional

import httpx
from pydantic import BaseModel, Field

from api.config import settings


class InventoryLookupInput(BaseModel):
    sku: str = Field(..., pattern=r"^SKU-\d{3}$")


class InventoryAdjustInput(BaseModel):
    sku: str = Field(..., pattern=r"^SKU-\d{3}$")
    adjustment_units: int = Field(..., description="Non-zero integer; negative removes stock")
    reason: str = Field(..., min_length=10, max_length=300)


class LeadLookupInput(BaseModel):
    lead_id: str = Field(..., pattern=r"^LEAD-\d{3}$")


class LeadStatusUpdateInput(BaseModel):
    lead_id: str = Field(..., pattern=r"^LEAD-\d{3}$")
    status: str = Field(..., pattern=r"^(NEW|CONTACTED|QUALIFIED|UNQUALIFIED|CLOSED_WON|CLOSED_LOST)$")
    note_update: str = Field(default="", max_length=500)


class CreateTicketInput(BaseModel):
    customer_id: str = Field(..., min_length=3, max_length=50)
    subject: str = Field(..., min_length=5, max_length=100)
    priority: str = Field(..., pattern=r"^(LOW|MEDIUM|HIGH|CRITICAL)$")
    details: str = Field(..., min_length=15, max_length=2000)


def _send(method: str, path: str, tenant_id: str, idempotency_key: Optional[str] = None,
          json_body: Optional[dict] = None) -> httpx.Response:
    headers = {"X-Internal-Token": settings.ERP_INTERNAL_API_KEY, "X-Tenant-ID": tenant_id}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    with httpx.Client(base_url=settings.ERP_API_BASE_URL, timeout=settings.ERP_HTTP_TIMEOUT_SECONDS) as client:
        return client.request(method, path, json=json_body, headers=headers)


def _call(method: str, path: str, tenant_id: str, write: bool, idempotency_key: Optional[str] = None,
          json_body: Optional[dict] = None) -> Dict[str, Any]:
    try:
        res = _send(method, path, tenant_id, idempotency_key, json_body)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:   # request never reached the server
        return {"error": True, "status_code": None, "retryable": True, "in_doubt": False,
                "message": f"Connection failed before send: {type(exc).__name__}"}
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        # The request may or may not have been processed. For writes this is the IN-DOUBT case.
        return {"error": True, "status_code": None, "retryable": True, "in_doubt": write,
                "message": f"Network failure after send: {type(exc).__name__}"}
    if res.is_success:
        return res.json()
    try:
        detail = res.json().get("detail", res.text)
    except Exception:
        detail = res.text
    return {"error": True, "status_code": res.status_code, "retryable": res.status_code >= 500,
            "in_doubt": write and res.status_code >= 500, "message": str(detail)[:500]}


def execute_inventory_lookup(sku: str, tenant_id: str, **_: Any) -> Dict[str, Any]:
    return _call("GET", f"/api/erp/inventory/{sku.upper().strip()}", tenant_id, write=False)


def execute_inventory_adjustment(sku: str, adjustment_units: int, reason: str, idempotency_key: str,
                                 tenant_id: str, **_: Any) -> Dict[str, Any]:
    return _call("POST", f"/api/erp/inventory/{sku.upper().strip()}/adjust", tenant_id, True, idempotency_key,
                 {"adjustment_units": adjustment_units, "reason": reason})


def execute_lead_lookup(lead_id: str, tenant_id: str, **_: Any) -> Dict[str, Any]:
    return _call("GET", f"/api/crm/leads/{lead_id.upper().strip()}", tenant_id, write=False)


def execute_lead_status_update(lead_id: str, status: str, note_update: str, idempotency_key: str,
                               tenant_id: str, **_: Any) -> Dict[str, Any]:
    return _call("PATCH", f"/api/crm/leads/{lead_id.upper().strip()}/status", tenant_id, True, idempotency_key,
                 {"status": status.upper().strip(), "note_update": note_update})


def execute_create_ticket(customer_id: str, subject: str, priority: str, details: str, idempotency_key: str,
                          tenant_id: str, **_: Any) -> Dict[str, Any]:
    return _call("POST", "/api/crm/tickets", tenant_id, True, idempotency_key,
                 {"customer_id": customer_id.strip(), "subject": subject.strip(),
                  "priority": priority.upper().strip(), "details": details.strip()})


def fetch_idempotency_status(idempotency_key: str, tenant_id: str) -> Dict[str, Any]:
    """Reconciliation probe (not exposed to the LLM): returns {'status': 'APPLIED'|'NOT_FOUND', ...}."""
    return _call("GET", f"/api/idempotency/{idempotency_key}", tenant_id, write=False)


TOOL_REGISTRY = {
    "lookup_inventory": {"func": execute_inventory_lookup, "schema": InventoryLookupInput, "is_sensitive_write": False,
                         "description": "Fetch current stock, warehouse zone and unit price for a SKU."},
    "adjust_inventory": {"func": execute_inventory_adjustment, "schema": InventoryAdjustInput, "is_sensitive_write": True,
                         "description": "Adjust stock by a non-zero integer with an audit reason. Requires human approval."},
    "lookup_lead": {"func": execute_lead_lookup, "schema": LeadLookupInput, "is_sensitive_write": False,
                    "description": "Fetch CRM lead company, status and deal value."},
    "update_lead_status": {"func": execute_lead_status_update, "schema": LeadStatusUpdateInput, "is_sensitive_write": True,
                           "description": "Move a lead to a new pipeline stage with an audit note. Requires human approval."},
    "create_support_ticket": {"func": execute_create_ticket, "schema": CreateTicketInput, "is_sensitive_write": True,
                              "description": "Create a support escalation ticket. Requires human approval."},
}
