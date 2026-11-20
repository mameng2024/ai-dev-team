"""Final acceptance: program checks decide, the Manager only judges with evidence,
the person has the last word."""
import sys
from pathlib import Path

import pytest

from ai_dev_team.acceptance import (
    AcceptanceError,
    ManagerAcceptance,
    ManagerJudgement,
    apply_guardrails,
    collect_screenshots,
    decide,
    replan_request,
    state,
    unsafe_command,
)
from ai_dev_team.agents.planning import AcceptanceCheck, ProjectPlan
from ai_dev_team.domain import TaskSpec
from ai_dev_team.jobs import JobError, build_argv
from ai_dev_team.llm import LLMError
from ai_dev_team.settings_server import acceptance_file
from ai_dev_team.worktrees import WorktreeManager, git

PY = sys.executable


def sh(cwd, *a):
    return git(Path(cwd), *a).stdout.strip()


class FakeLLM:
    def __init__(self, judgement=None, fail=False):
        self.judgement, self.fail, self.calls = judgement, fail, []

    def complete_json(self, agent_id, system, user, schema, task_id=None):
        self.calls.append((agent_id, user))
        if self.fail:
            raise LLMError("all models failed")
        return self.judgement


def plan_with(checks, extra_criteria=()):
    return ProjectPlan(summary="dark mode", milestones=[{"name": "m", "goal": "g"}],
                       completion_criteria=[c.criterion for c in checks] + list(extra_criteria),
                       acceptance_checks=checks)


@pytest.mark.parametrize("cmd,ok", [
    (["python", "-m", "pytest", "-q", "tests/test_x.py"], True),
    (["npx", "playwright", "test", "e2e/settings.spec.ts"], True),
    (["xcodebuild", "test", "-scheme", "App"], True),
    (["rm", "-rf", "build"], False),
    (["curl", "https://example.com"], False),
    (["git", "push"], False),
    (["python", "-c", "import os"], False),
    (["python3.12", "-c", "x"], False),
    (["node", "-e", "x"], False),
    (["cat", "/etc/passwd"], False),
    (["pytest", "../other"], False),
    (["cat", ".env"], False),
    ([], False),
])
def test_unsafe_command(cmd, ok):
    assert (unsafe_command(cmd) is None) is ok


def test_plan_checks_fill_missing_criteria_as_manual():
    p = plan_with([AcceptanceCheck(criterion="tests", kind="command", command=["pytest"])],
                  extra_criteria=["looks good"])
    kinds = {c.criterion: c.kind for c in p.checks()}
    assert kinds == {"tests": "command", "looks good": "manual"}


def test_guardrails_program_decides_and_manual_never_met():
    from ai_dev_team.acceptance import CheckOutcome
    p = plan_with([
        AcceptanceCheck(criterion="export works", kind="command", command=["pytest"]),
        AcceptanceCheck(criterion="looks nice", kind="manual", how_to_verify="open settings"),
        AcceptanceCheck(criterion="copy is clear", kind="manual"),
    ])
    j = ManagerJudgement(criteria=[
        {"criterion": "export works", "status": "met", "evidence": "trust me"},
        {"criterion": "looks nice", "status": "met", "evidence": "css changed"},
    ])
    out = [CheckOutcome(criterion="export works", command=["pytest"], exit_code=1,
                        output_tail="1 failed")]
    r = {c.criterion: c for c in apply_guardrails(p, j, out)}
    assert r["export works"].status == "unmet" and r["export works"].decided_by == "program"
    assert r["looks nice"].status == "needs_human" and r["looks nice"].decided_by == "guardrail"
    assert r["looks nice"].how_to_verify == "open settings"
    assert r["copy is clear"].status == "needs_human"


@pytest.fixture
def proj(tmp_path, repo, monkeypatch):
    monkeypatch.setenv("AI_DEV_TEAM_HOME", str(tmp_path / "home"))
    project = tmp_path / "MiniTot"
    (project / "tests").mkdir(parents=True)
    sh(project, "init", "-q", "-b", "main")
    (project / "app.py").write_text("def greet():\n    return 'hi'\n")
    (project / "tests" / "test_app.py").write_text(
        "from app import greet\n\ndef test_greet():\n    assert greet() == 'hi'\n")
    (project / "tests" / "test_fail.py").write_text("def test_no():\n    assert False\n")
    (project / "shot.py").write_text(
        "from pathlib import Path\np = Path('.ai-dev-team/acceptance')\n"
        "p.mkdir(parents=True, exist_ok=True)\n(p / 'settings.png').write_bytes(b'png')\n")
    (project / ".ai-dev-team.yaml").write_text(
        f"test_commands:\n  - [{PY!r}, -m, pytest, -q, -p, no:cacheprovider, tests/test_app.py]\n")
    sh(project, "add", ".")
    sh(project, "commit", "-qm", "base")
    with repo.db.transaction() as conn:
        conn.execute("UPDATE projects SET repo_path=? WHERE id='p1'", (str(project),))
    repo.create_tasks([TaskSpec(id="p1:T1", project_id="p1", title="t", description="d",
                                acceptance_criteria=["ok"])])
    wt = WorktreeManager(project, "p1", "main")
    return repo, wt, project


def _save(repo, checks, extra=()):
    return repo.save_plan("p1", "add dark mode", plan_with(checks, extra).model_dump(), ["p1:T1"])


def _complete(repo):
    with repo.db.transaction() as conn:
        conn.execute("UPDATE tasks SET status='COMPLETED' WHERE id='p1:T1'")


PASS = AcceptanceCheck(criterion="greets", kind="command",
                       command=["python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                "tests/test_app.py"])
FAIL = AcceptanceCheck(criterion="never", kind="command",
                       command=["python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                "tests/test_fail.py"])
SHOT = AcceptanceCheck(criterion="screenshot taken", kind="command", command=["python", "shot.py"])
LOOK = AcceptanceCheck(criterion="dark mode looks right", kind="manual",
                       how_to_verify="open Settings, switch theme")


def test_acceptance_refuses_until_tasks_done(proj):
    repo, wt, _ = proj
    with pytest.raises(AcceptanceError, match="no saved plan"):
        ManagerAcceptance(FakeLLM(), repo, wt).run("p1")
    _save(repo, [PASS])
    assert state(repo, "p1", wt)["status"] == "IN_PROGRESS"
    with pytest.raises(AcceptanceError, match="not finished"):
        ManagerAcceptance(FakeLLM(), repo, wt).run("p1")


def test_full_flow_ready_then_human_approves(proj):
    repo, wt, _ = proj
    _save(repo, [PASS, SHOT, LOOK])
    _complete(repo)
    assert state(repo, "p1", wt)["status"] == "NEEDS_MANAGER_CHECK"
    llm = FakeLLM(ManagerJudgement(criteria=[
        {"criterion": "dark mode looks right", "status": "met", "evidence": "css"}],
        summary="looks complete", human_focus=["check contrast"]))
    report = ManagerAcceptance(llm, repo, wt).run("p1")
    assert report.verdict == "READY_FOR_HUMAN" and report.tests_passed
    by = {c.criterion: c.status for c in report.criteria}
    assert by == {"greets": "met", "screenshot taken": "met",
                  "dark mode looks right": "needs_human"}
    assert report.screenshots == [".ai-dev-team/acceptance/settings.png"]
    assert "Command checks" in llm.calls[0][1]
    st = state(repo, "p1", wt)
    assert st["status"] == "AWAITING_HUMAN"
    with pytest.raises(AcceptanceError, match="note"):
        decide(repo, "p1", wt, approve=False, note="  ")
    assert decide(repo, "p1", wt, approve=True)["status"] == "APPROVED"
    # new work on integration -> the decision no longer covers it
    integ = wt.ensure_integration()
    (integ / "new.txt").write_text("x")
    sh(integ, "add", "new.txt")
    sh(integ, "commit", "-qm", "more")
    st = state(repo, "p1", wt)
    assert st["status"] == "NEEDS_MANAGER_CHECK" and st["stale"]
    with pytest.raises(AcceptanceError, match="Manager acceptance check first"):
        decide(repo, "p1", wt, approve=True)


def test_gaps_need_force_and_reject_builds_replan_request(proj):
    repo, wt, _ = proj
    _save(repo, [PASS, FAIL])
    _complete(repo)
    report = ManagerAcceptance(FakeLLM(ManagerJudgement(summary="ok")), repo, wt).run("p1")
    assert report.verdict == "GAPS"
    assert any("never" in f for f in report.follow_ups)
    with pytest.raises(AcceptanceError, match="--force"):
        decide(repo, "p1", wt, approve=True)
    st = decide(repo, "p1", wt, approve=False, note="button is too small")
    assert st["status"] == "REJECTED"
    req = replan_request(st, "button is too small")
    assert "add dark mode" in req and "button is too small" in req and "never" in req
    assert decide(repo, "p1", wt, approve=True, force=True)["status"] == "APPROVED"


def test_model_failure_keeps_program_results(proj):
    repo, wt, _ = proj
    _save(repo, [PASS, LOOK])
    _complete(repo)
    report = ManagerAcceptance(FakeLLM(fail=True), repo, wt).run("p1")
    assert not report.manager_model_ok
    assert {c.criterion: c.status for c in report.criteria} == {
        "greets": "met", "dark mode looks right": "needs_human"}
    assert report.verdict == "READY_FOR_HUMAN"


def test_refused_command_goes_to_person(proj):
    repo, wt, _ = proj
    _save(repo, [AcceptanceCheck(criterion="deploy", kind="command", command=["rm", "-rf", "x"])])
    _complete(repo)
    report = ManagerAcceptance(FakeLLM(ManagerJudgement()), repo, wt).run("p1")
    c = report.criteria[0]
    assert c.status == "needs_human" and "not allowed" in c.evidence


def test_screenshot_endpoint_only_serves_artifact_images(proj, tmp_path):
    repo, wt, _ = proj
    integ = wt.ensure_integration()
    d = integ / ".ai-dev-team" / "acceptance"
    d.mkdir(parents=True)
    (d / "a.png").write_bytes(b"img")
    (integ / "app.png").write_bytes(b"outside")
    assert collect_screenshots(integ) == [".ai-dev-team/acceptance/a.png"]
    db = repo.db.path
    assert acceptance_file(db, "p1", ".ai-dev-team/acceptance/a.png") == (b"img", "image/png")
    assert acceptance_file(db, "p1", "app.png") is None
    assert acceptance_file(db, "p1", ".ai-dev-team/acceptance/../../app.py") is None


def test_job_argv_for_acceptance():
    assert build_argv("acceptance", {"project": "minitot"}) == (["acceptance", "minitot"],
                                                                "minitot")
    assert build_argv("accept", {"project": "minitot", "force": True})[0] == [
        "accept", "minitot", "--force"]
    assert build_argv("reject", {"project": "minitot", "note": "too dark"})[0] == [
        "reject", "minitot", "--note", "too dark", "--replan"]
    with pytest.raises(JobError):
        build_argv("reject", {"project": "minitot", "note": ""})
