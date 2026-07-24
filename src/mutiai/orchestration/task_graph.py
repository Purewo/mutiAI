"""LangGraph task fan-out and organization-lead aggregation."""

from __future__ import annotations

import operator
from collections.abc import Callable
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send


class AssignmentWork(TypedDict):
    assignment_id: str
    execution_id: str
    role_key: str
    instructions: str
    output_schema: dict[str, Any] | None


class AssignmentResult(TypedDict):
    assignment_id: str
    execution_id: str
    role_key: str
    summary: str


class LeadReviewState(TypedDict):
    decision: str
    final_summary: str
    issues: list[str]


class TaskGraphState(TypedDict):
    task_id: str
    assignments: list[AssignmentWork]
    results: Annotated[list[AssignmentResult], operator.add]
    summary: str
    review: LeadReviewState | None


class AssignmentNodeState(TypedDict):
    task_id: str
    assignment: AssignmentWork


def build_task_graph(
    execute_assignment: Callable[[AssignmentWork], AssignmentResult],
    review_assignments: Callable[[str, list[AssignmentResult]], LeadReviewState],
) -> StateGraph:
    def dispatch(state: TaskGraphState) -> list[Send]:
        return [
            Send(
                "execute_assignment",
                {"task_id": state["task_id"], "assignment": assignment},
            )
            for assignment in state["assignments"]
        ]

    def execute_node(state: AssignmentNodeState) -> dict:
        return {"results": [execute_assignment(state["assignment"])]}

    def finalize(state: TaskGraphState) -> dict:
        review = review_assignments(state["task_id"], state["results"])
        return {
            "review": review,
            "summary": review["final_summary"],
        }

    builder = StateGraph(TaskGraphState)
    builder.add_node("execute_assignment", execute_node)
    builder.add_node("finalize", finalize)
    builder.add_conditional_edges(START, dispatch, ["execute_assignment"])
    builder.add_edge("execute_assignment", "finalize")
    builder.add_edge("finalize", END)
    return builder
