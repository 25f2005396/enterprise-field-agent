# Enterprise Field Agent

An AI agent that carries out ERP/CRM tasks through a set of mock REST APIs. You give it a goal in plain English, it decides which tools to call and checks what comes back, and it asks a human for approval before anything is written.

It was built by Neelisetty Venkata Naga Teja for an assignment on tool-calling enterprise agents. The agent loop uses LangGraph, the services use FastAPI, validation is done with Pydantic, data lives in SQLite, and the LLM is served by Groq.

## What it does

The agent works in a loop. At each step it writes a short thought, picks a tool, and reads the result before deciding what to do next. The command line prints each step as Thought, Action and Observation so you can follow along.

- It has five tools: inventory lookup, inventory adjustment, lead lookup, lead status update and support ticket creation.
- Every tool call is checked against a schema. If the model sends bad arguments, for example SKU-99 instead of SKU-992, the error goes back to the model and it tries again.
- Retries are capped, and a call that already failed is never repeated with the same arguments.
- Every write stops and waits for a person to approve it. The approval is tied to the exact tool and arguments, it expires after 10 minutes, and the person who started the run cannot approve it.
- Writes carry an idempotency key, so a retry after a timeout cannot apply the same change twice.
- The HTTP version adds role-based API keys, a tamper-evident audit log, live event streaming and optional signed webhooks.

Example run:

```
Goal: Check stock for SKU-992 and remove 5 units for Project Helios

Step 1  THOUGHT  Need to verify current stock for SKU-992 before adjusting inventory
        ACTION   lookup_inventory({"sku": "SKU-992"})
        OBSERVATION  {"status": "SUCCESS", "inventory": {"sku": "SKU-992", "stock_level": 42, ...}}

Step 2  THOUGHT  Stock is sufficient, so request the adjustment with a reason.
        ACTION   adjust_inventory({"sku": "SKU-992", "adjustment_units": -5, ...})  (needs approval)
        Approve this write? [y/n]: y
        OBSERVATION  {"status": "SUCCESS", "previous_stock": 42, "current_stock": 37, ...}

Step 3  THOUGHT  Goal achieved.
Result: COMPLETED
```

## Setup

You need Python 3.11 or newer (I used 3.14 on Windows) and Git. A Groq API key is only needed for real LLM runs. The demo mode and the tests work without one.

On Windows (PowerShell):

```powershell
git clone https://github.com/25f2005396/enterprise-field-agent.git
cd enterprise-field-agent
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

If PowerShell refuses to run the activate script, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first.

On macOS or Linux, use `python3 -m venv .venv`, `source .venv/bin/activate` and `cp .env.example .env` instead.

Next, fill in the secrets in `.env`. The app refuses to start while any of them is a placeholder. On Windows this replaces all seven at once:

```powershell
$c = Get-Content .env -Raw
$c = [regex]::Replace($c, '(?m)^(AGENT_SECURITY_SECRET|WEBHOOK_SIGNING_SECRET|ERP_INTERNAL_API_KEY|ADMIN_API_KEY|OPERATOR_API_KEY|APPROVER_API_KEY|AUDITOR_API_KEY)=.*$', { param($m) $m.Groups[1].Value + '=' + (python -c "import secrets; print(secrets.token_urlsafe(48))") })
[System.IO.File]::WriteAllText("$PWD\.env", $c, (New-Object System.Text.UTF8Encoding($false)))
```

On other systems, generate each value with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` and paste it in by hand. Keep `.env` private. It is already in `.gitignore`.

## Configuration

Everything is set in `.env`. The settings you will actually touch:

- `GROQ_API_KEY`: your Groq key. Leave the placeholder for demo mode and tests.
- `LLM_MODEL`: the model to use. It must be one your key can access (see below).
- `AGENT_SECURITY_SECRET`, `WEBHOOK_SIGNING_SECRET`: random strings, at least 32 characters.
- `ERP_INTERNAL_API_KEY`: key the agent uses to call the mock ERP service, at least 24 characters.
- `ADMIN_API_KEY`, `OPERATOR_API_KEY`, `APPROVER_API_KEY`, `AUDITOR_API_KEY`: one key per role, at least 24 characters each and all different.
- `DB_PATH`: SQLite file, `erp_crm_enterprise.db` by default.
- `MAX_PLAN_STEPS`, `MAX_RETRIES`, `APPROVAL_TTL_SECONDS`: loop limits and approval lifetime (8, 3 and 600 by default).

Groq changes its model list from time to time, and the model named in `.env.example` may not be available to you. If a run fails with `model_not_found`, list what your key can use:

```powershell
$k = ((Get-Content .env | Where-Object { $_ -match '^GROQ_API_KEY=' }) -replace '^GROQ_API_KEY=','').Trim()
(Invoke-RestMethod https://api.groq.com/openai/v1/models -Headers @{Authorization="Bearer $k"}).data.id | Sort-Object
```

Pick a chat model (skip the whisper, tts and guard ones) and put it in `.env`, for example `LLM_MODEL=openai/gpt-oss-120b`.

## Running it

The agent calls the mock ERP service, so start that first in its own terminal and leave it running:

```
python -m api.mock_erp_crm
```

Then, in a second terminal with the virtual environment active:

```
python main.py --demo
python main.py "Check stock for SKU-992 and remove 5 units for Project Helios"
python main.py
```

The first command is an offline demo with a scripted planner, so no LLM key is needed. It sends a bad SKU on purpose to show the agent correcting itself. Add `--yes` to auto-approve the write. The second command runs a real goal through the LLM, and the third starts an interactive prompt.

When a write is proposed, the program shows the exact tool and arguments and asks for approval. Answer `n` and the run is aborted with nothing written.

To reset the sample data, stop the ERP service and delete `erp_crm_enterprise.db`.

## Tools and sample data

- `lookup_inventory`: stock level, warehouse zone and price for a SKU.
- `adjust_inventory`: change stock by a non-zero number, with a reason. Needs approval.
- `lookup_lead`: company, status and deal value for a lead.
- `update_lead_status`: move a lead to NEW, CONTACTED, QUALIFIED, UNQUALIFIED, CLOSED_WON or CLOSED_LOST. Needs approval.
- `create_support_ticket`: open a ticket with priority LOW, MEDIUM, HIGH or CRITICAL. Needs approval.

The mock database starts with three SKUs (SKU-992 with 42 units, SKU-104 with 5, SKU-550 with 150), two leads (LEAD-401 and LEAD-402) and one customer (CUST-001).

## HTTP gateway

To use the REST version instead of the command line, run `python server.py`. It listens on port 8000 and the interactive docs are at `/docs`. An operator starts a run with `POST /api/v1/agent/run`, an approver finds it with `GET /api/v1/approvals/pending` and approves it with `POST /api/v1/agent/approve`, and anyone with access can read the trace from `GET /api/v1/agent/sessions/{id}`. There are also endpoints for rejecting, reconciling an interrupted write, streaming events, and reading and verifying the audit log. Send the role key in an `X-API-Key` header.

## Tests

```
pytest -q
```

There are 84 tests. They use a temporary database and a scripted planner, so they need no network and no API key. They cover approvals, access control, idempotent writes, recovery of interrupted writes, retry limits, the audit log, webhooks and streaming.

## Troubleshooting

- `Python was not found` on Windows: use `py` instead of `python`, or install Python with the "Add to PATH" box ticked.
- `ERP service not reachable`: the mock service is not running. Start `python -m api.mock_erp_crm` in another terminal.
- `401 Invalid API Key`: the Groq key in `.env` is missing or wrong.
- `404 model_not_found`: pick a model your key can use, as described under Configuration.
- Startup stops with `FATAL CONFIGURATION ERROR`: a secret in `.env` is missing, too short or still a placeholder.

## Limitations

This is a prototype. SQLite is fine for one machine but not for several servers, the role keys are demo credentials, and the ERP and CRM services are simulated. Before exposing it beyond localhost it would need TLS, key rotation, rate limiting and a real identity provider. The quality of the plans also depends on the LLM you choose.

## Author

Neelisetty Venkata Naga Teja

- Computer Science and Engineering (AI/ML), Vellore Institute of Technology, Chennai
- Data Science and Applications, IIT Madras
