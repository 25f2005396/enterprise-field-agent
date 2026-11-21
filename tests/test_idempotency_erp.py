import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

from api.mock_erp_crm import InventoryAdjustmentRequest, canonical_hash
from tests.conftest import ENV, INTERNAL, stock


def H(key=None, tenant="tenant-default"):
    h = {**INTERNAL, "X-Tenant-ID": tenant}
    if key:
        h["Idempotency-Key"] = key
    return h


def k():
    return f"key-{uuid.uuid4().hex}"


ADJ = "/api/erp/inventory/SKU-992/adjust"


def test_idempotency_key_is_mandatory(erp):
    r = erp.post(ADJ, json={"adjustment_units": -1, "reason": "Standard decrement test"}, headers=H())
    assert r.status_code == 422


def test_internal_token_is_mandatory(erp):
    r = erp.get("/api/erp/inventory/SKU-992", headers={"X-Tenant-ID": "tenant-default"})
    assert r.status_code == 401


def test_canonical_hash_regression_pydantic_v2():
    """Original code called model_dump_json(sort_keys=True), a TypeError on Pydantic v2."""
    a = InventoryAdjustmentRequest(adjustment_units=-1, reason="A reason long enough")
    assert canonical_hash(a) == canonical_hash(InventoryAdjustmentRequest(reason="A reason long enough", adjustment_units=-1))


def test_replay_returns_cached_response_without_double_apply(erp):
    key, body = k(), {"adjustment_units": -1, "reason": "Inventory deduction unit test"}
    r1 = erp.post(ADJ, json=body, headers=H(key))
    r2 = erp.post(ADJ, json=body, headers=H(key))
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()
    assert stock(erp) == 41


def test_payload_mismatch_is_409(erp):
    key = k()
    assert erp.post(ADJ, json={"adjustment_units": -1, "reason": "Initial decrement action"}, headers=H(key)).status_code == 200
    r = erp.post(ADJ, json={"adjustment_units": -2, "reason": "Altered payload attempt"}, headers=H(key))
    assert r.status_code == 409


def test_endpoint_mismatch_is_409(erp):
    key, body = k(), {"adjustment_units": -1, "reason": "Initial decrement action"}
    assert erp.post(ADJ, json=body, headers=H(key)).status_code == 200
    assert erp.post("/api/erp/inventory/SKU-104/adjust", json=body, headers=H(key)).status_code == 409


def test_failed_business_rule_is_not_cached(erp):
    key = k()
    r = erp.post("/api/erp/inventory/SKU-104/adjust", json={"adjustment_units": -50, "reason": "Too large a decrement"}, headers=H(key))
    assert r.status_code == 400
    r = erp.post("/api/erp/inventory/SKU-104/adjust", json={"adjustment_units": -1, "reason": "Corrected decrement"}, headers=H(key))
    assert r.status_code == 200


def test_concurrent_duplicate_requests_apply_once(erp):
    key, body = k(), {"adjustment_units": -1, "reason": "Concurrent write safety test"}

    def fire(_):
        with httpx.Client(base_url=ENV["ERP_API_BASE_URL"], timeout=20) as c:
            return c.post("/api/erp/inventory/SKU-550/adjust", json=body, headers=H(key))
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(fire, range(8)))
    assert all(r.status_code == 200 for r in results)
    assert len({r.json()["current_stock"] for r in results}) == 1
    assert stock(erp, "SKU-550") == 149


def test_concurrent_distinct_writes_lose_no_updates(erp):
    def fire(i):
        with httpx.Client(base_url=ENV["ERP_API_BASE_URL"], timeout=20) as c:
            return c.post("/api/erp/inventory/SKU-550/adjust",
                          json={"adjustment_units": -1, "reason": f"Distinct write number {i:03d}"}, headers=H(k()))
    with ThreadPoolExecutor(10) as ex:
        results = list(ex.map(fire, range(10)))
    assert all(r.status_code == 200 for r in results)
    assert stock(erp, "SKU-550") == 140


def test_idempotency_status_endpoint_for_reconciliation(erp):
    key = k()
    assert erp.get(f"/api/idempotency/{key}", headers=H()).json()["status"] == "NOT_FOUND"
    erp.post(ADJ, json={"adjustment_units": -1, "reason": "Reconciliation probe test"}, headers=H(key))
    probe = erp.get(f"/api/idempotency/{key}", headers=H()).json()
    assert probe["status"] == "APPLIED" and probe["response"]["current_stock"] == 41


def test_tenants_are_isolated(erp):
    key = k()
    r = erp.post(ADJ, json={"adjustment_units": -10, "reason": "Acme tenant adjustment"}, headers=H(key, "tenant-acme"))
    assert r.status_code == 200
    assert stock(erp, tenant="tenant-acme") == 32 and stock(erp, tenant="tenant-default") == 42
    assert erp.get("/api/erp/inventory/SKU-992", headers=H(tenant="tenant-unknown")).status_code == 404
    # the same key in another tenant is a different ledger entry, and the probe cannot see across tenants
    assert erp.get(f"/api/idempotency/{key}", headers=H()).json()["status"] == "NOT_FOUND"


def test_ticket_requires_known_customer(erp):
    body = {"customer_id": "CUST-999", "subject": "Gateway offline", "priority": "HIGH", "details": "Gateway is not responding at all."}
    assert erp.post("/api/crm/tickets", json=body, headers=H(k())).status_code == 400
    body["customer_id"] = "cust-001"   # case-insensitive match against a real customer
    r = erp.post("/api/crm/tickets", json=body, headers=H(k()))
    assert r.status_code == 201 and r.json()["ticket"]["status"] == "OPEN"
