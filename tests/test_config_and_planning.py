import pytest
import yaml

from ai_dev_team.agents.planning import (
    PlannedTask,
    Planner,
    ProjectPlan,
    TaskGraph,
    to_task_specs,
    validate_graph,
)
from ai_dev_team.config import ModelRegistry


def test_registry_loads_cross_vendor_config(config_path):
    reg = ModelRegistry.load(config_path)
    m, r = reg.get("manager"), reg.get("reviewer")
    assert m.provider == "dashscope" and r.provider == "deepseek"
    assert r.model == "deepseek/deepseek-v4-pro"
    assert [a.id for a in reg.by_role("DEVELOPER")] == ["developer-a", "developer-b"]
    assert reg.defaults.max_retries == 3 and reg.defaults.max_concurrency == 2


def test_manager_and_reviewer_same_model_rejected(config_path):
    data = yaml.safe_load(config_path.read_text())
    data["agents"]["reviewer"].update(provider="dashscope", model=data["agents"]["manager"]["model"])
    with pytest.raises(ValueError, match="must not use the same"):
        ModelRegistry.model_validate(data)


def test_retired_style_model_without_provider_rejected(config_path):
    data = yaml.safe_load(config_path.read_text())
    data["agents"]["developer-a"]["model"] = "deepseek-chat"
    with pytest.raises(ValueError):
        ModelRegistry.model_validate(data)


class FakeLLM:
    def __init__(self, graphs):
        self.graphs = list(graphs)
        self.calls = []

    def complete_json(self, agent_id, system, user, schema, task_id=None):
        self.calls.append((agent_id, user))
        if schema is ProjectPlan:
            return ProjectPlan(summary="build it", milestones=[{"name": "m1", "goal": "g"}],
                               completion_criteria=["tests pass"])
        return self.graphs.pop(0)


def T(key, deps=(), files=(), crit=("ok",)):
    return PlannedTask(key=key, title=key, description="d", acceptance_criteria=list(crit),
                       dependencies=list(deps), files=list(files))


def test_validate_graph_catches_problems():
    bad = TaskGraph(tasks=[T("A", ["B"]), T("B", ["A"]), T("C", ["Z"]),
                           T("D", files=["../secrets.txt"]), T("E", files=["~/.ssh/id_rsa"])])
    probs = " | ".join(validate_graph(bad))
    assert "cycle" in probs and "unknown dependency Z" in probs and "unsafe path" in probs


def test_planner_retries_architect_until_valid(repo):
    bad = TaskGraph(tasks=[T("A", ["B"]), T("B", ["A"])])
    good = TaskGraph(tasks=[T("A", files=["src/app.py"]), T("B", ["A"])])
    llm = FakeLLM([bad, good])
    state = Planner(llm).run("做一个待办事项 CLI")
    assert not state["problems"] and state["attempts"] == 2
    assert [c[0] for c in llm.calls] == ["manager", "architect", "architect"]
    assert "rejected" in llm.calls[2][1]                      # architect saw the problems
    specs = to_task_specs(state["graph"], "p1")
    repo.create_tasks(specs)
    assert repo.promote_ready("p1") == ["p1:A"]
    assert repo.get_task("p1:B")["status"] == "PENDING"
