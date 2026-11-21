# Enterprise Field Agent

Autonomous ERP/CRM tool-calling agent: Thought -> Action -> Observation cycles, schema validation loops,
bounded error retries, human-in-the-loop approval before every sensitive write, idempotent writes,
in-doubt write reconciliation, RBAC + tenant isolation, tamper-evident audit log, signed webhooks, SSE streaming.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                      # then replace every secret (see comments inside)
pytest -q                                                 # 84 tests, hermetic (temp DB, no LLM key needed)

# terminal 1 - mock ERP/CRM service (port 8001)
python -m api.mock_erp_crm
# terminal 2 - offline demo of the full TAO + approval cycle (no LLM key needed)
python main.py --demo
# or the real thing with Groq:
python main.py "Check stock for SKU-992 and remove 5 units for Project Helios"
# or the HTTP gateway (port 8000)
python server.py
```

## Quick start (Windows PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env      # then replace every secret in .env
pytest -q                        # hermetic tests, no LLM key needed

# terminal 1 - mock ERP/CRM service (port 8001)
python -m api.mock_erp_crm
# terminal 2 - offline demo (no LLM key needed)
python main.py --demo
```

After the tests pass, run `pip freeze > requirements.lock.txt` to record the exact versions that worked.

## HTTP walkthrough

```bash
# 1. operator starts a run -> stops at AWAITING_APPROVAL
curl -X POST localhost:8000/api/v1/agent/run -H "X-API-Key: $OPERATOR_API_KEY" \
     -H "Content-Type: application/json" -d '{"query":"Check SKU-992 and remove 5 units for Project Helios"}'
# 2. a DIFFERENT human (approver) fetches the pending action + one-time token
curl localhost:8000/api/v1/approvals/pending -H "X-API-Key: $APPROVER_API_KEY"
# 3. approver approves (or POST /api/v1/agent/reject)
curl -X POST localhost:8000/api/v1/agent/approve -H "X-API-Key: $APPROVER_API_KEY" \
     -H "Content-Type: application/json" -d '{"session_id":"sess_...","approval_token":"..."}'
# 4. watch Thought/Action/Observation live (ticket keeps the API key out of the URL)
curl -X POST localhost:8000/api/v1/agent/sessions/sess_.../stream-ticket -H "X-API-Key: $OPERATOR_API_KEY"
curl -N "localhost:8000/api/v1/agent/sessions/sess_.../events?ticket=..."
# 5. auditor verifies the hash chain
curl localhost:8000/api/v1/audit/verify -H "X-API-Key: $AUDITOR_API_KEY"
```

## Scope

SQLite (WAL) is for single-node/demo use. Multi-instance deployments need PostgreSQL. See the Build Guide for the
production-hardening roadmap and known limitations.
