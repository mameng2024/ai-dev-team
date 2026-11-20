import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ai_dev_team.domain import TaskSpec
from ai_dev_team.domain import TaskStatus as S
from ai_dev_team.gitdiff import collect_diff
from ai_dev_team.pipeline import ReviewPipeline
from ai_dev_team.reviewer import (
    ModelVerdict,
    Reviewer,
    apply_guardrails,
    build_input,
    render_prompt,
)
from ai_dev_team.scheduler import DurableScheduler, WorkerSpec
from ai_dev_team.tester import Tester, TestReport


def git(ws, *a):
    subprocess.run(["git", "-C", str(ws), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                   check=True, capture_output=True)


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "proj"
    w.mkdir()
    git(w, "init", "-q", "-b", "main")
    (w / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (w / "tests").mkdir()
    (w / "tests" / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    (w / ".ai-dev-team.yaml").write_text(
        f"test_commands:\n  - [{sys.executable!r}, -m, pytest, -q, -p, no:cacheprovider]\n")
    git(w, "add", "-A")
    git(w, "commit", "-qm", "init")
    git(w, "checkout", "-qb", "task/T1")
    return w


def verdict_file(tmp_path, **kw):
    data = {"verdict": "PASS", "score": 90, "criteria_check": ["add works: met"],
            "findings": [], "required_fixes": [], "evidence": ["calc.py:2"]} | kw
    p = tmp_path / f"verdict-{len(list(tmp_path.glob('verdict-*')))}.json"
    p.write_text(json.dumps(data))
    return p


def make_task(repo, tid="T1"):
    repo.create_task(TaskSpec(id=tid, project_id="p1", title="fix add",
                              description="add must add", acceptance_criteria=["add(2,3)==5"]))
    repo.promote_ready()
    c = repo.claim_next("developer-a", "m", 60)
    repo.mark_running(c)
    repo.finish_execution(c, success=True, exit_code=0)
    assert repo.status_of(tid) == S.TESTING


def pipeline(repo, ws, tmp_path):
    cfg = Path(__file__).resolve().parents[1] / "config" / "agents.yaml"
    return ReviewPipeline(repo, Tester(), Reviewer(cfg), lambda _t: ws, base_ref="main")


def test_tester_runs_project_commands(ws):
    report = Tester().run_checks(ws)
    assert not report.passed and "test_add" in report.failures()[0]
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    assert Tester().run_checks(ws).passed


def test_tester_scrubs_secrets(ws, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")
    (ws / ".ai-dev-team.yaml").write_text(
        f"test_commands:\n  - [{sys.executable!r}, -c, \"import os,sys; "
        "sys.exit(1 if 'DEEPSEEK_API_KEY' in os.environ else 0)\"]\n")
    assert Tester().run_checks(ws).passed


def test_failing_tests_go_to_rework_with_output(repo, ws, tmp_path):
    make_task(repo)
    (ws / "README.md").write_text("changed\n")        # a change, but the bug remains
    assert pipeline(repo, ws, tmp_path).process("T1") == S.READY
    notes = repo.rework_notes("T1")
    assert "failed" in notes[0] and "test_add" in notes[0]
    assert repo.reviews("T1") == []                    # reviewer never called


def test_empty_diff_is_rework(repo, ws, tmp_path, monkeypatch):
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git(ws, "commit", "-qam", "fix on base")          # diff vs main is non-empty...
    git(ws, "checkout", "-q", "main")
    git(ws, "merge", "-q", "task/T1")                  # ...until main has it too
    git(ws, "checkout", "-q", "task/T1")
    make_task(repo)
    assert pipeline(repo, ws, tmp_path).process("T1") == S.READY
    assert "No code changes" in repo.rework_notes("T1")[0]


def test_pass_in_separate_process_enters_merge_queue(repo, ws, tmp_path, monkeypatch):
    make_task(repo)
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    monkeypatch.setenv("AI_DEV_TEAM_REVIEW_FAKE", str(verdict_file(tmp_path)))
    assert pipeline(repo, ws, tmp_path).process("T1") == S.MERGE_QUEUED
    r = repo.reviews("T1")[0]
    assert r["verdict"] == "PASS" and r["reviewer_model"] == "fake/reviewer"


def test_reviewer_rework_then_fix_then_pass(repo, ws, tmp_path, monkeypatch):
    make_task(repo)
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    monkeypatch.setenv("AI_DEV_TEAM_REVIEW_FAKE", str(verdict_file(
        tmp_path, verdict="REWORK", score=50, required_fixes=["add type hints"])))
    p = pipeline(repo, ws, tmp_path)
    assert p.process("T1") == S.READY
    assert repo.rework_notes("T1") == ["[attempt 1] add type hints"]
    c = repo.claim_next("developer-b", "m", 60)
    assert c.rework_notes == ["[attempt 1] add type hints"]
    repo.mark_running(c)
    repo.finish_execution(c, success=True, exit_code=0)
    monkeypatch.setenv("AI_DEV_TEAM_REVIEW_FAKE", str(verdict_file(tmp_path)))
    assert p.process("T1") == S.MERGE_QUEUED


def test_review_worker_failure_keeps_reviewing_and_can_retry(repo, ws, tmp_path, monkeypatch):
    make_task(repo)
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    monkeypatch.setenv("AI_DEV_TEAM_REVIEW_FAKE", str(tmp_path / "missing.json"))
    p = pipeline(repo, ws, tmp_path)
    assert p.process("T1") == S.REVIEWING
    monkeypatch.setenv("AI_DEV_TEAM_REVIEW_FAKE", str(verdict_file(tmp_path)))
    assert p.process("T1") == S.MERGE_QUEUED


def _inp(ws, tests_passed=True):
    task = TaskSpec(id="T", project_id="p", title="t", description="d",
                    acceptance_criteria=["c"])
    rep = TestReport(passed=tests_passed)
    return build_input(task, [], collect_diff(ws, "main"), rep)


@pytest.mark.parametrize("change,kw,reason", [
    ("tests_fail", {}, "tests failed"),
    ("major", {"findings": [{"severity": "major", "description": "race"}]}, "critical/major"),
    ("low", {"score": 40}, "score 40"),
])
def test_guardrails_override_pass(ws, change, kw, reason):
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    inp = _inp(ws, tests_passed=change != "tests_fail")
    v = ModelVerdict.model_validate({"verdict": "PASS", "score": 90,
                                     "criteria_check": ["c: met"]} | kw)
    res = apply_guardrails("T", v, inp, "m", diff_empty=False)
    assert res.verdict == "REWORK" and res.required_fixes
    assert any(reason in f.description for f in res.findings)


def test_deleted_tests_detected_and_block_pass(ws):
    (ws / "tests" / "test_calc.py").unlink()
    inp = _inp(ws)
    assert any("test files deleted" in w for w in inp.warnings)
    v = ModelVerdict(verdict="PASS", score=95, criteria_check=["c: met"])
    assert apply_guardrails("T", v, inp, "m", diff_empty=False).verdict == "REWORK"


def test_removed_assertions_and_untracked_files_in_diff(ws):
    (ws / "tests" / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n"
                                               "    pass\n")
    (ws / "new_mod.py").write_text("X = 1\n")
    d = collect_diff(ws, "main")
    assert d.removed_assertions == 1
    assert {f.path for f in d.files} == {"tests/test_calc.py", "new_mod.py"}
    assert "+X = 1" in d.patch


def test_prompt_excludes_developer_output(ws):
    inp = _inp(ws)
    text = render_prompt(inp)
    assert "Acceptance criteria" in text and "stdout" not in inp.model_dump_json()


def test_scheduler_runs_pipeline_outside_developer_slots(repo, ws, tmp_path, monkeypatch):
    from ai_dev_team.executors import DryRunExecutor

    class FixingExecutor(DryRunExecutor):
        async def execute(self, task, workspace, *a, **k):
            (workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n")
            return await super().execute(task, workspace, *a, **k)

    repo.create_task(TaskSpec(id="T1", project_id="p1", title="fix add", description="d",
                              acceptance_criteria=["add(2,3)==5"]))
    monkeypatch.setenv("AI_DEV_TEAM_REVIEW_FAKE", str(verdict_file(tmp_path)))
    sched = DurableScheduler(repo, FixingExecutor(0.01),
                             [WorkerSpec("developer-a", "m")], lambda c: ws,
                             poll_seconds=0.01, on_testing=pipeline(repo, ws, tmp_path))
    report = asyncio.run(sched.run_until_idle())
    assert report.outcomes["T1"] == "MERGE_QUEUED"


def test_swift_test_paths_and_xcode_junk(ws):
    from ai_dev_team.gitdiff import DiffBundle, is_junk
    d = DiffBundle(base_ref="main", head_sha=None)
    assert d.is_test_path("MiniTotTests/FeedingStoreTests.swift")
    assert d.is_test_path("Tests/MiniTotCoreTests/TimerTests.swift")
    assert not d.is_test_path("MiniTot/Views/FeedingView.swift")
    assert is_junk(".build/debug/x.o") and is_junk("DerivedData/Logs/a.log")
    assert is_junk("MiniTot.xcodeproj/xcuserdata/me.xcuserdatad/a.plist")
