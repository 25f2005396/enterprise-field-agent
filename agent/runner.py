# agent/runner.py
"""Runs the graph and PERSISTS AFTER EVERY NODE (compare-and-swap), so a crash never loses an
in-flight write record and SSE consumers see steps as they happen."""
from typing import Callable, Optional

from agent.graph import build_enterprise_agent_graph
from agent.state import AgentState
from agent.store import save_session

_GRAPH = build_enterprise_agent_graph()


def run_graph(state: AgentState, on_update: Optional[Callable[[AgentState], None]] = None) -> AgentState:
    """`state` must already be persisted at state.state_version (the claim)."""
    version = state.state_version
    latest = state
    first = True
    for emitted in _GRAPH.stream(state, config={"recursion_limit": 100}, stream_mode="values"):
        current = emitted if isinstance(emitted, AgentState) else AgentState.model_validate(emitted)
        if first:                      # initial input == what is already stored
            first = False
            latest = current
            continue
        version = save_session(current, version)
        current.state_version = version
        latest = current
        if on_update:
            on_update(current)
    latest.state_version = version
    return latest
