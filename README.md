# Enterprise Field Agent

This is an AI agent that does ERP/CRM tasks through mock REST APIs. You type a goal like "check stock for SKU-992 and remove 5 units for Project Helios", and the agent works out which tools to call, looks at what each call returns, and decides the next step. Before it changes any data it stops and asks a person to approve.

I built it for an assignment on tool-calling agents. It uses LangGraph for the agent loop, FastAPI for the services, Pydantic for checking tool inputs, SQLite for storage and Groq for the LLM.

## How it works

Each step the agent writes a short thought, picks a tool and reads the result. The terminal prints these as THOUGHT, ACTION and OBSERVATION, so you can follow what it's doing. A real run looks like this:

```
Goal: Check stock for SKU-992 and remove 5 units for Project Helios

Step 1  THOUGHT  Lookup current stock for SKU-992 before making any adjustment
        ACTION   lookup_inventory({"sku": "SKU-992"})
        OBSERVATION  {"status": "SUCCESS", "inventory": {"sku": "SKU-992", "stock_level": 30, ...}}

Step 2  THOUGHT  Remove 5 units from SKU-992 for Project Helios after confirming stock
        ACTION   adjust_inventory({"sku": "SKU-992", "adjustment_units": -5, ...})  (sensitive write - needs approval)
        Approve this write? [y/n]: y
        OBSERVATION  {"status": "SUCCESS", "previous_stock": 30, "current_stock": 25, ...}

Step 3  THOUGHT  Goal achieved: stock checked and 5 units removed for Project Helios
Result: COMPLETED
```

A few things I added so it behaves sensibly:

- Every tool call is validated first. If the model sends something wrong (like `SKU-99`), the error goes back to it so it can fix the call and try again.
- It gives up after a few failed attempts instead of looping forever, and it never repeats a call that already failed.
- Every write (inventory change, lead update, new ticket) waits for approval. The approval is for that exact call, expires after 10 minutes, and the person who started the run can't be the one who approves it.
- Writes carry an idempotency key, so a retry after a timeout doesn't apply the same change twice.

## Setup

You need Python 3.11 or newer (I used 3.14 on Windows) and Git. The demo mode and the tests don't need an API key. Real runs need a Groq key.

On Windows (PowerShell):

```powershell
git clone https://github.com/25f2005396/enterprise-field-agent.git
cd enterprise-field-agent
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

If PowerShell won't run the activate script, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first. On macOS or Linux it's `python3 -m venv .venv`, `source .venv/bin/activate` and `cp .env.example .env`.

Then open `.env`. The app won't start while any value still says `replace_with...`, so replace each of those with a random string. This prints one:

```
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The role keys (admin, operator, approver, auditor) must all be different, and each needs at least 24 characters. Put your Groq key in `GROQ_API_KEY`. `.env` is in `.gitignore`, so don't commit it.

The model in `.env.example` (`llama-3.3-70b-versatile`) gave me a `model_not_found` error, so I set `LLM_MODEL=openai/gpt-oss-120b` in my `.env` and that worked. Groq changes its models now and then. You can see what your key can use by sending a GET request to `https://api.groq.com/openai/v1/models` with your key as a bearer token.

## Running it

Start the mock ERP service in one terminal and leave it running:

```
python -m api.mock_erp_crm
```

In a second terminal:

```
python main.py --demo
python main.py "Check stock for SKU-992 and remove 5 units for Project Helios"
```

`--demo` uses a scripted planner instead of the LLM, so it works without a key. It sends a wrong SKU on purpose to show the agent correcting itself, and `--yes` approves the write automatically. The second command uses the real LLM. Running `python main.py` with no goal gives you an interactive prompt.

When the agent wants to write something it shows the tool and arguments and asks `Approve this write? [y/n]`. If you answer `n`, nothing is written.

The sample data starts with three products (SKU-992 with 42 units, SKU-104 with 5 and SKU-550 with 150), two leads (LEAD-401, LEAD-402) and one customer (CUST-001). Every approved run changes the stock, so to reset it, stop the ERP service and delete `erp_crm_enterprise.db`.

## Tools

The agent has five tools: `lookup_inventory`, `adjust_inventory`, `lookup_lead`, `update_lead_status` and `create_support_ticket`. The three that change data need approval.

## HTTP version

There's also a REST version of the agent. Run `python server.py` and open `http://127.0.0.1:8000/docs`. An operator starts a run, a different user with the approver role approves or rejects it, and each user sends their own key in an `X-API-Key` header. It also has an audit log and a live event stream. I mostly used the terminal version, so the CLI is the better one to try first.

## Tests

```
pytest -q
```

There are 84 tests. They use a temporary database and a fake planner, so they don't need the network or an API key.

## Problems I ran into

- `Python was not found` on Windows: use `py` instead of `python`.
- `ERP service not reachable`: start `python -m api.mock_erp_crm` first.
- `Errno 10048` when starting the ERP service: it's already running in another terminal.
- `401 Invalid API Key`: the Groq key in `.env` is missing or wrong.
- `404 model_not_found`: change `LLM_MODEL` to a model your key can use.

## Limitations

It's a prototype. SQLite is fine on one machine but not for several servers, the ERP and CRM services are fake, and the API keys in `.env` are demo credentials. Before anyone used it for real it would need HTTPS, proper login and rate limiting. How good the plans are also depends on the model.

## Author

Neelisetty Venkata Naga Teja

- Computer Science and Engineering (AI/ML), Vellore Institute of Technology, Chennai
- Data Science and Applications, IIT Madras
