# main.py
"""Interactive CLI that visualises Thought -> Action -> Observation cycles live.

    python main.py                 # interactive, uses Groq
    python main.py "your goal"     # one-shot
    python main.py --demo          # offline scripted planner (no LLM key needed; shows self-correction)
    python main.py --demo --yes    # ... and auto-approve the staged write

The CLI authenticates as TWO different users (OPERATOR then APPROVER) so separation of duties holds.
The ERP service must be running:  python -m api.mock_erp_crm
"""
import argparse
import json
import sys

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from agent import llm, service
from agent.state import AgentState, SessionStatus
from api.auth import authenticate
from api.config import settings

console = Console()


class TraceView:
    """Prints each Thought / Action / Observation the moment it is persisted."""

    def __init__(self):
        self.shown_action, self.shown_obs = set(), set()

    def __call__(self, state: AgentState) -> None:
        for s in state.steps:
            if s.step_number not in self.shown_action:
                self.shown_action.add(s.step_number)
                body = f"[bold cyan]THOUGHT[/]  {s.thought}"
                if s.tool_call:
                    flag = "  [yellow](sensitive write - needs approval)[/]" if s.tool_call.is_sensitive_write else ""
                    body += (f"\n[bold magenta]ACTION[/]   {s.tool_call.tool_name}"
                             f"({json.dumps(s.tool_call.arguments)}){flag}")
                console.print(Panel(body, title=f"Step {s.step_number}", border_style="blue", expand=False))
            if s.observation is not None and s.step_number not in self.shown_obs:
                self.shown_obs.add(s.step_number)
                colour = "red" if s.is_error else "green"
                tag = " [IN DOUBT]" if s.in_doubt else ""
                console.print(f"   [bold {colour}]OBSERVATION{tag}[/]  {s.observation[:600]}")


def demo_planner(_system: str, user: str) -> str:
    """Deterministic stand-in for the LLM: bad arg -> self-correct -> lookup -> write -> finish."""
    if "Action: adjust_inventory" in user:
        return json.dumps({"thought": "The adjustment is confirmed; the goal is met.",
                           "final_response": "Checked SKU-992 and reduced stock by 5 units for Project Helios "
                                             "after human approval."})
    if "Action: lookup_inventory" in user and "Schema ValidationError" in user and "Observation: <tool_output>{" in user:
        return json.dumps({"thought": "Stock is sufficient. Request the approved deduction.",
                           "action": {"tool_name": "adjust_inventory",
                                      "arguments": {"sku": "SKU-992", "adjustment_units": -5,
                                                    "reason": "Dispatch for Project Helios"}}})
    if "Schema ValidationError" in user:
        return json.dumps({"thought": "The SKU format was wrong (needs 3 digits). Retrying with SKU-992.",
                           "action": {"tool_name": "lookup_inventory", "arguments": {"sku": "SKU-992"}}})
    return json.dumps({"thought": "Check current stock first.",
                       "action": {"tool_name": "lookup_inventory", "arguments": {"sku": "SKU-99"}}})


def erp_up() -> bool:
    try:
        return httpx.get(f"{settings.ERP_API_BASE_URL}/health", timeout=2).status_code == 200
    except Exception:
        return False


def handle_approval(operator, approver, state: AgentState, auto_yes: bool, view: TraceView) -> AgentState:
    pending = [p for p in service.pending_approvals(approver) if p["session_id"] == state.session_id]
    if not pending:
        console.print("[red]No pending approval found for this session.[/]")
        return state
    p = pending[0]
    table = Table(title="Human-in-the-loop approval required", show_header=False)
    table.add_row("Tool", p["tool"])
    table.add_row("Arguments", json.dumps(p["arguments"], indent=2))
    table.add_row("Action hash", p["action_hash"][:16] + "...")
    table.add_row("Expires", f"{settings.APPROVAL_TTL_SECONDS}s window")
    console.print(table)
    if auto_yes or Confirm.ask("Approve this write?", default=False):
        return service.approve(approver, state.session_id, p["approval_token"], on_update=view)
    return service.reject(approver, state.session_id, "Rejected from CLI")


def run_goal(goal: str, auto_yes: bool) -> None:
    operator = authenticate(settings.OPERATOR_API_KEY)
    approver = authenticate(settings.APPROVER_API_KEY)
    console.rule(f"[bold]Goal:[/] {goal}")
    view = TraceView()
    state = service.start_run(operator, goal, on_update=view)
    while state.status in (SessionStatus.AWAITING_APPROVAL, SessionStatus.PAUSED_RECONCILIATION_REQUIRED):
        if state.status == SessionStatus.AWAITING_APPROVAL:
            state = handle_approval(operator, approver, state, auto_yes, view)
        else:
            console.print(Panel(state.final_response or "Paused", title="In-doubt write", border_style="yellow"))
            if not Confirm.ask("Retry reconciliation now?", default=True):
                break
            state = service.reconcile(operator, state.session_id, on_update=view)
    colour = {"COMPLETED": "green", "ABORTED": "yellow"}.get(state.status.value, "red")
    console.print(Panel(state.final_response or "(no summary)", title=f"Result: {state.status.value}", border_style=colour))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("goal", nargs="?")
    ap.add_argument("--demo", action="store_true", help="use the offline scripted planner")
    ap.add_argument("--yes", action="store_true", help="auto-approve staged writes (demo only)")
    args = ap.parse_args()

    if not erp_up():
        console.print(f"[red]ERP service not reachable at {settings.ERP_API_BASE_URL}.[/] "
                      "Start it first:  python -m api.mock_erp_crm")
        return 1
    if args.demo:
        llm.invoke_llm = demo_planner
        console.print("[yellow]Demo mode: offline scripted planner (no LLM calls).[/]")
        run_goal(args.goal or "Check stock for SKU-992 and remove 5 units for Project Helios.", args.yes)
        return 0
    if args.goal:
        run_goal(args.goal, args.yes)
        return 0
    console.print("[bold]Enterprise Field Agent[/] - type a goal, or 'exit'.")
    while True:
        goal = Prompt.ask("\n[bold]Goal[/]")
        if goal.strip().lower() in {"exit", "quit", ""}:
            return 0
        try:
            run_goal(goal, args.yes)
        except service.ServiceError as e:
            console.print(f"[red]{e.status_code}: {e.detail}[/]")


if __name__ == "__main__":
    sys.exit(main())
