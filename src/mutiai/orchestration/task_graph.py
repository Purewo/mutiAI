"""LangGraph task fan-out and dependency-driven plan graphs."""

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


class LinearTaskGraphState(TypedDict):
    task_id: str
    work: AssignmentWork | None
    result: AssignmentResult | None
    review: LeadReviewState | None
    done: bool


class ParallelTaskGraphState(TypedDict):
    task_id: str
    assignments: list[AssignmentWork]
    results: Annotated[list[AssignmentResult], operator.add]
    review_work: AssignmentWork | None
    review_result: AssignmentResult | None
    review: LeadReviewState | None
    terminal: bool


class PlanningGraphState(TypedDict):
    task_id: str
    work: AssignmentWork
    result: AssignmentResult | None


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


def build_linear_task_graph(
    prepare_step: Callable[[str], AssignmentWork | None],
    execute_assignment: Callable[[AssignmentWork], AssignmentResult],
    finalize_step: Callable[
        [str, AssignmentWork, AssignmentResult], dict[str, Any]
    ],
) -> StateGraph:
    """Build a loop that executes one persisted dependency-ready step at a time."""

    def prepare(state: LinearTaskGraphState) -> dict[str, Any]:
        work = prepare_step(state["task_id"])
        return {"work": work, "result": None, "done": work is None}

    def route_prepare(state: LinearTaskGraphState) -> str:
        return "execute" if state.get("work") is not None else "__end__"

    def execute(state: LinearTaskGraphState) -> dict[str, Any]:
        work = state["work"]
        if work is None:
            raise RuntimeError("linear graph reached execute without a step")
        return {"result": execute_assignment(work)}

    def finalize(state: LinearTaskGraphState) -> dict[str, Any]:
        work = state["work"]
        result = state["result"]
        if work is None or result is None:
            raise RuntimeError("linear graph reached finalize without a result")
        return finalize_step(state["task_id"], work, result)

    def route_finalize(state: LinearTaskGraphState) -> str:
        return "__end__" if state.get("done") else "prepare"

    builder = StateGraph(LinearTaskGraphState)
    builder.add_node("prepare", prepare)
    builder.add_node("execute", execute)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "prepare")
    builder.add_conditional_edges("prepare", route_prepare, ["execute", END])
    builder.add_edge("execute", "finalize")
    builder.add_conditional_edges("finalize", route_finalize, ["prepare", END])
    return builder


def build_parallel_task_graph(
    prepare_specialists: Callable[[str], list[AssignmentWork]],
    execute_assignment: Callable[[AssignmentWork], AssignmentResult],
    finalize_specialists: Callable[
        [str, list[AssignmentWork], list[AssignmentResult]], dict[str, Any]
    ],
    prepare_review: Callable[[str], AssignmentWork | None],
    finalize_review: Callable[
        [str, AssignmentWork, AssignmentResult], dict[str, Any]
    ],
) -> StateGraph:
    """Build one Artifact-producing specialist fan-out and lead-review join."""

    def prepare_specialists_node(state: ParallelTaskGraphState) -> dict[str, Any]:
        return {"assignments": prepare_specialists(state["task_id"])}

    def dispatch_specialists(
        state: ParallelTaskGraphState,
    ) -> list[Send] | str:
        assignments = state.get("assignments", [])
        if not assignments:
            return "prepare_review"
        return [
            Send(
                "execute_specialist",
                {"task_id": state["task_id"], "assignment": assignment},
            )
            for assignment in assignments
        ]

    def execute_specialist_node(state: AssignmentNodeState) -> dict[str, Any]:
        return {"results": [execute_assignment(state["assignment"])]}

    def finalize_specialists_node(
        state: ParallelTaskGraphState,
    ) -> dict[str, Any]:
        return finalize_specialists(
            state["task_id"],
            state["assignments"],
            state["results"],
        )

    def route_after_specialists(state: ParallelTaskGraphState) -> str:
        return (
            "__end__"
            if state.get("review") or state.get("terminal")
            else "prepare_review"
        )

    def prepare_review_node(state: ParallelTaskGraphState) -> dict[str, Any]:
        work = prepare_review(state["task_id"])
        if work is None:
            raise RuntimeError("parallel graph reached review without ready work")
        return {"review_work": work, "review_result": None}

    def execute_review_node(state: ParallelTaskGraphState) -> dict[str, Any]:
        work = state["review_work"]
        if work is None:
            raise RuntimeError("parallel graph reached review execution without work")
        return {"review_result": execute_assignment(work)}

    def finalize_review_node(state: ParallelTaskGraphState) -> dict[str, Any]:
        work = state["review_work"]
        result = state["review_result"]
        if work is None or result is None:
            raise RuntimeError("parallel graph reached review finalization without result")
        outcome = finalize_review(state["task_id"], work, result)
        if not outcome.get("review"):
            raise RuntimeError("parallel graph completed without lead review")
        return outcome

    builder = StateGraph(ParallelTaskGraphState)
    builder.add_node("prepare_specialists", prepare_specialists_node)
    builder.add_node("execute_specialist", execute_specialist_node)
    builder.add_node("finalize_specialists", finalize_specialists_node)
    builder.add_node("prepare_review", prepare_review_node)
    builder.add_node("execute_review", execute_review_node)
    builder.add_node("finalize_review", finalize_review_node)
    builder.add_edge(START, "prepare_specialists")
    builder.add_conditional_edges(
        "prepare_specialists",
        dispatch_specialists,
        ["execute_specialist", "prepare_review"],
    )
    builder.add_edge("execute_specialist", "finalize_specialists")
    builder.add_conditional_edges(
        "finalize_specialists",
        route_after_specialists,
        ["prepare_review", END],
    )
    builder.add_edge("prepare_review", "execute_review")
    builder.add_edge("execute_review", "finalize_review")
    builder.add_edge("finalize_review", END)
    return builder


def build_planning_graph(
    execute_assignment: Callable[[AssignmentWork], AssignmentResult],
) -> StateGraph:
    """Build the single durable Runtime boundary used by lead planning."""

    def execute(state: PlanningGraphState) -> dict[str, Any]:
        return {"result": execute_assignment(state["work"])}

    builder = StateGraph(PlanningGraphState)
    builder.add_node("execute", execute)
    builder.add_edge(START, "execute")
    builder.add_edge("execute", END)
    return builder
