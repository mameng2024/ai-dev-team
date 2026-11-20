"""Manager (plan) and Architect (task DAG), orchestrated with LangGraph.

Flow: manager -> architect -> validate -> (invalid and attempts left) architect | done
All LLM output is Pydantic-validated; DAGs with cycles, unknown deps, missing
acceptance criteria or unsafe paths are rejected before anything touches SQLite.
"""
from __future__ import annotations

import re
from typing import TypedDict

from pydantic import BaseModel, Field, field_validator

from ..domain import TaskSpec
from ..llm import LLMClient
from ..repository import find_cycle
from .prompts import ARCHITECT_SYSTEM, MANAGER_SYSTEM

UNSAFE_PATH = re.compile(r"(^|[\s'\"`])(/|~|\.\./)|\.env\b|\.ssh\b|id_rsa|Keychain", re.IGNORECASE)


class Milestone(BaseModel):
    name: str
    goal: str


class ProjectPlan(BaseModel):
    summary: str
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    milestones: list[Milestone] = Field(min_length=1)
    completion_criteria: list[str] = Field(min_length=1)


class PlannedTask(BaseModel):
    key: str = Field(description="short unique key, e.g. T001")
    title: str
    description: str
    acceptance_criteria: list[str] = Field(min_length=1)
    dependencies: list[str] = Field(default_factory=list, description="keys of other tasks")
    files: list[str] = Field(default_factory=list, description="repo-relative paths it owns")
    priority: int = Field(default=5, ge=1, le=10)
    complexity: str = Field(default="simple", description="simple | complex")

    @field_validator("key")
    @classmethod
    def key_ok(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", v):
            raise ValueError("key must be 1-32 chars of [A-Za-z0-9_-]")
        return v


class TaskGraph(BaseModel):
    tasks: list[PlannedTask] = Field(min_length=1)


def validate_graph(graph: TaskGraph, max_tasks: int = 30) -> list[str]:
    problems: list[str] = []
    keys = [t.key for t in graph.tasks]
    if len(set(keys)) != len(keys):
        problems.append("duplicate task keys")
    if len(keys) > max_tasks:
        problems.append(f"too many tasks ({len(keys)} > {max_tasks}); merge small tasks")
    known = set(keys)
    for t in graph.tasks:
        if not t.acceptance_criteria or any(not c.strip() for c in t.acceptance_criteria):
            problems.append(f"{t.key}: empty acceptance criteria")
        for d in t.dependencies:
            if d not in known:
                problems.append(f"{t.key}: unknown dependency {d}")
        for f in t.files:
            if UNSAFE_PATH.search(f):
                problems.append(f"{t.key}: unsafe path {f!r}")
    cycle = find_cycle({t.key: set(t.dependencies) for t in graph.tasks})
    if cycle:
        problems.append("dependency cycle: " + " -> ".join(cycle))
    return problems


def to_task_specs(graph: TaskGraph, project_id: str, max_retries: int = 3) -> list[TaskSpec]:
    """Namespace keys by project so ids stay globally unique in SQLite."""
    def tid(key: str) -> str:
        return f"{project_id}:{key}"
    return [
        TaskSpec(
            id=tid(t.key), project_id=project_id, title=t.title,
            description=t.description + (
                "\n\nOwned files: " + ", ".join(t.files) if t.files else ""),
            acceptance_criteria=t.acceptance_criteria,
            dependencies=[tid(d) for d in t.dependencies],
            required_capabilities=[t.complexity],
            priority=t.priority, max_retries=max_retries,
        )
        for t in graph.tasks
    ]


class PlanningState(TypedDict, total=False):
    request: str
    project_context: str
    plan: ProjectPlan
    graph: TaskGraph
    problems: list[str]
    attempts: int


class Planner:
    def __init__(self, llm: LLMClient, manager_id: str = "manager",
                 architect_id: str = "architect", max_attempts: int = 3):
        self.llm = llm
        self.manager_id = manager_id
        self.architect_id = architect_id
        self.max_attempts = max_attempts

    # Interfaces from the spec ------------------------------------------------
    def plan_project(self, request: str, project_context: str = "") -> ProjectPlan:
        user = f"User request:\n{request}\n\nProject context:\n{project_context or '(none)'}"
        return self.llm.complete_json(self.manager_id, MANAGER_SYSTEM, user, ProjectPlan)

    def design_tasks(self, plan: ProjectPlan, problems: list[str] | None = None) -> TaskGraph:
        user = "Approved plan:\n" + plan.model_dump_json(indent=2)
        if problems:
            user += "\n\nYour previous task graph was rejected:\n- " + "\n- ".join(problems)
        return self.llm.complete_json(self.architect_id, ARCHITECT_SYSTEM, user, TaskGraph)

    # Graph ---------------------------------------------------------------
    def _manager_node(self, s: PlanningState) -> PlanningState:
        return {"plan": self.plan_project(s["request"], s.get("project_context", "")),
                "attempts": 0}

    def _architect_node(self, s: PlanningState) -> PlanningState:
        graph = self.design_tasks(s["plan"], s.get("problems"))
        return {"graph": graph, "attempts": s.get("attempts", 0) + 1}

    def _validate_node(self, s: PlanningState) -> PlanningState:
        return {"problems": validate_graph(s["graph"])}

    def _route(self, s: PlanningState) -> str:
        if s.get("problems") and s.get("attempts", 0) < self.max_attempts:
            return "architect"
        return "done"

    def run(self, request: str, project_context: str = "") -> PlanningState:
        state: PlanningState = {"request": request, "project_context": project_context}
        try:
            from langgraph.graph import END, StateGraph
        except ImportError:  # sequential fallback with identical semantics
            state |= self._manager_node(state)
            while True:
                state |= self._architect_node(state)
                state |= self._validate_node(state)
                if self._route(state) == "done":
                    return state
        g = StateGraph(PlanningState)
        g.add_node("manager", self._manager_node)
        g.add_node("architect", self._architect_node)
        g.add_node("validate", self._validate_node)
        g.set_entry_point("manager")
        g.add_edge("manager", "architect")
        g.add_edge("architect", "validate")
        g.add_conditional_edges("validate", self._route, {"architect": "architect", "done": END})
        return g.compile().invoke(state)
