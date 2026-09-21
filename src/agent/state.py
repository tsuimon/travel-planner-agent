"""Public state channels used by the six-node LangGraph."""

from typing import TypedDict


class AgentState(TypedDict, total=False):
    query: str
    constraints: dict
    objective: str
    iteration_count: int
    token_usage: int
    tool_outputs: dict
    combined_plans: list
    errors: list[str]
    risk_labels: list[str]
    response: dict
    terminal: bool
    stop: bool
    trace: list[str]
