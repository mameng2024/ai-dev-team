"""P2 + P4 + P7 end-to-end: fake OpenCode binary, real Git worktrees, real test gate,
real reviewer subprocess (fake verdict), real merge queue."""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ai_dev_team.domain import TaskSpec
from ai_dev_team.domain import TaskStatus as S
from ai_dev_team.executors.opencode import OpenCodeExecutor, probe
from ai_dev_team.merger import MergeQueue
from ai_dev_team.pipeline import ReviewPipeline, review_then_merge
from ai_dev_team.reviewer import Reviewer
from ai_dev_team.scheduler import DurableScheduler, WorkerSpec
from ai_dev_team.tester import Tester
from ai_dev_team.worktrees import GitError, WorktreeManager, git

FAKE = Path(__file__).parent / "fakes" / "opencode"
CFG = Path(__file__).resolve().parents[1] / "config" / "agents.yaml"
PY = sys.executable


def sh(cwd, *a):
    return git(Path(cwd), *a).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch, repo):
    project = tmp_path / "MiniTot"
    project.mkdir()
    sh(project, "init", "-q", "-b", "main")
    (project / "app.py").write_text("VALUE = 1\n\ndef greet():\n    return 'hi'\n")
    (project / "shared.py").write_text("NAME = 'base'\n")
    (project / "tests").mkdir()
    (project / "tests" / "test_base.py").write_text(
        "from app import greet\n\ndef test_greet():\n    assert greet() == 'hi'\n")
    (project / ".ai-dev-team.yaml").write_text(
        f"test_commands:\n  - [{PY!r}, -m, pytest, -q, -p, no:cacheprovider]\n")
    (project / "uncommitted-user-work.txt").write_text("the user's own WIP on main\n")
    sh(project, "add", "app.py", "shared.py", "tests", ".ai-dev-team.yaml")
    sh(project, "commit", "-qm", "base")
    with repo.db.transaction() as conn:
        conn.execute("UPDATE projects SET repo_path=? WHERE id='p1'", (str(project),))
    verdict = tmp_path / "pass.json"
    verdict.write_text(json.dumps({"verdict": "PASS", "score": 90, "criteria_check": ["ok"],
                                   "evidence": ["x"]}))
    monkeypatch.setenv("AI_DEV_TEAM_REVIEW_FAKE", str(verdict))
    monkeypatch.setenv("FAKE_OC_STATE", str(tmp_path / "ocstate"))
    monkeypatch.setenv("FAKE_OC_LOG", str(tmp_path / "oc.log"))
    monkeypatch.setenv("AI_DEV_TEAM_HOME", str(tmp_path / "home"))
    wt = WorktreeManager(project, "p1", "main", root=tmp_path / "wt")
    return {"repo": repo, "project": project, "wt": wt, "tmp": tmp_path,
            "main_sha": sh(project, "rev-parse", "main")}


def plan(env, data):
    p = env["tmp"] / "plan.json"
    p.write_text(json.dumps(data))
    os.environ["FAKE_OC_PLAN"] = str(p)


def oc_log(env):
    f = env["tmp"] / "oc.log"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def task(repo, tid, deps=(), title=None):
    repo.create_task(TaskSpec(id=tid, project_id="p1", title=title or tid, description="d",
                              acceptance_criteria=["works"], dependencies=list(deps)))


def run_all(env, workers=2, timeout=60, **exec_kw):
    repo, wt = env["repo"], env["wt"]
    ex = OpenCodeExecutor(str(FAKE), timeout, env={"DEEPSEEK_API_KEY": "sk-test"},
                          model_map={"deepseek/deepseek-flash": "deepseek/deepseek-v4-flash"},
                          **exec_kw)
    pipeline = ReviewPipeline(repo, Tester(), Reviewer(CFG), wt.path_for,
                              base_ref=wt.integration_branch)
    hook = review_then_merge(pipeline, MergeQueue(repo, wt, Tester()))
    devs = [WorkerSpec("developer-a", "deepseek/deepseek-flash"),
            WorkerSpec("developer-b", "deepseek/deepseek-flash")][:workers]
    sched = DurableScheduler(repo, ex, devs, lambda c: wt.ensure_task(c.task.id),
                             on_testing=hook, poll_seconds=0.02, max_concurrency=workers,
                             timeout_seconds=timeout, project_id="p1")
    return asyncio.run(sched.run_until_idle(max_idle_polls=5))


def assert_main_untouched(env):
    p = env["project"]
    assert sh(p, "rev-parse", "main") == env["main_sha"]
    assert sh(p, "branch", "--show-current") == "main"
    assert (p / "uncommitted-user-work.txt").read_text() == "the user's own WIP on main\n"
    assert (p / "app.py").read_text().startswith("VALUE = 1")


def integ_file(env, rel):
    return sh(env["project"], "show", f"{env['wt'].integration_branch}:{rel}")


# ----------------------------------------------------------------------------- P2
def test_probe_and_command_flags(tmp_path):
    caps = probe(str(FAKE))
    assert caps.error is None and caps.version == "2.0.99-fake"
    ex = OpenCodeExecutor(str(FAKE), caps=caps, model_map={"a/b": "a/b-oc"})
    cmd = ex.command("a/b", "PROMPT", tmp_path)
    assert cmd[:4] == [str(FAKE), "run", "--model", "a/b-oc"]
    assert "--standalone" in cmd and "--auto" in cmd and "--dir" not in cmd
    assert cmd[-1] == "PROMPT"
    assert "--auto" not in OpenCodeExecutor(str(FAKE), caps=caps, auto_approve=False) \
        .command("a/b", "P", tmp_path)
    assert probe(str(tmp_path / "missing")).error


def test_full_run_two_parallel_then_dependent(env):
    repo = env["repo"]
    plan(env, {
        "p1:T1": [{"files": {"feature_a.py": "def a():\n    return 1\n",
                             "tests/test_a.py": "from feature_a import a\n\n"
                                                "def test_a():\n    assert a() == 1\n"},
                   "sleep": 0.6}],
        "p1:T2": [{"files": {"feature_b.py": "def b():\n    return 2\n",
                             "tests/test_b.py": "from feature_b import b\n\n"
                                                "def test_b():\n    assert b() == 2\n"},
                   "sleep": 0.6}],
        "p1:T3": [{"files": {"feature_c.py": "from feature_a import a\n\n"
                                             "def c():\n    return a() + 10\n",
                             "tests/test_c.py": "from feature_c import c\n\n"
                                                "def test_c():\n    assert c() == 11\n"}}],
    })
    task(repo, "p1:T1")
    task(repo, "p1:T2")
    task(repo, "p1:T3", deps=["p1:T1"])
    report = run_all(env)
    assert {t["id"]: t["status"] for t in repo.list_tasks("p1")} == {
        "p1:T1": "COMPLETED", "p1:T2": "COMPLETED", "p1:T3": "COMPLETED"}
    assert report.peak_concurrency == 2
    log = oc_log(env)
    by_task = {e["task"]: e for e in log}
    assert len({e["cwd"] for e in log}) == 3                      # one worktree per task
    assert all(e["model"] == "deepseek/deepseek-v4-flash" and e["has_key"] for e in log)
    t1, t2 = by_task["p1:T1"], by_task["p1:T2"]
    assert abs(t1["t"] - t2["t"]) < 0.5                           # really ran in parallel
    # T3 started from integration that already contained T1
    assert "feature_a" in integ_file(env, "feature_c.py")
    for f in ("feature_a.py", "feature_b.py", "feature_c.py"):
        integ_file(env, f)
    assert_main_untouched(env)
    assert not env["wt"].path_for("p1:T1").exists()               # cleaned up
    assert sh(env["project"], "branch", "--list", "aidt/p1/T1") == ""


def test_merge_conflict_goes_back_with_markers_and_is_resolved(env):
    repo = env["repo"]
    plan(env, {
        "p1:T1": [{"files": {"shared.py": "NAME = 'from-T1'\n"}, "sleep": 0.2}],
        "p1:T2": [{"files": {"shared.py": "NAME = 'from-T2'\n"}, "sleep": 0.8},
                  {"files": {"shared.py": "NAME = 'from-T1'\nNAME2 = 'from-T2'\n"}}],
    })
    task(repo, "p1:T1")
    task(repo, "p1:T2")
    run_all(env)
    assert repo.status_of("p1:T1") == S.COMPLETED
    assert repo.status_of("p1:T2") == S.COMPLETED
    notes = repo.rework_notes("p1:T2")
    assert any("Merge conflict" in n and "shared.py" in n for n in notes)
    attempts = [e for e in oc_log(env) if e["task"] == "p1:T2"]
    assert len(attempts) == 2 and attempts[1]["fixes_in_prompt"]
    assert integ_file(env, "shared.py") == "NAME = 'from-T1'\nNAME2 = 'from-T2'"
    assert_main_untouched(env)


def test_unresolved_conflict_markers_never_reach_review(env):
    repo = env["repo"]
    plan(env, {
        "p1:T1": [{"files": {"shared.py": "NAME = 'from-T1'\n"}, "sleep": 0.2}],
        "p1:T2": [{"files": {"shared.py": "NAME = 'from-T2'\n"}, "sleep": 0.8},
                  {}],                                  # 2nd attempt: does nothing
    })
    task(repo, "p1:T1")
    task(repo, "p1:T2")
    run_all(env)
    notes = repo.rework_notes("p1:T2")
    assert any("conflict markers" in n for n in notes)
    assert repo.status_of("p1:T2") in (S.BLOCKED, S.READY)
    assert "<<<<<<<" not in integ_file(env, "shared.py")


def test_post_merge_regression_is_undone_and_reworked(env):
    repo = env["repo"]
    plan(env, {
        "p1:T1": [{"files": {"app.py": "VALUE = 2\n\ndef greet():\n    return 'hi'\n",
                             "tests/test_v1.py": "import app\n\ndef test_v():\n"
                                                 "    assert app.VALUE == 2\n"}, "sleep": 0.1}],
        # passes alone (VALUE is still 1 on its branch) but not after T1 is merged
        "p1:T2": [{"files": {"tests/test_v2.py": "import app\n\ndef test_v2():\n"
                                                 "    assert app.VALUE == 1\n"}, "sleep": 0.8},
                  {"files": {"tests/test_v2.py": "import app\n\ndef test_v2():\n"
                                                 "    assert app.VALUE >= 1\n"}}],
    })
    task(repo, "p1:T1")
    task(repo, "p1:T2")
    run_all(env)
    assert repo.status_of("p1:T2") == S.COMPLETED
    assert any("broke the integration branch" in n for n in repo.rework_notes("p1:T2"))
    assert "VALUE >= 1" in integ_file(env, "tests/test_v2.py")
    # the failed merge left no trace: exactly one merge commit per task on integration
    merges = sh(env["project"], "log", "--merges", "--oneline", env["wt"].integration_branch)
    assert len(merges.splitlines()) == 2


# ----------------------------------------------------------------------------- P7
def test_reviewer_disagreement_blocks_after_three_reworks(env, monkeypatch):
    repo = env["repo"]
    rework = env["tmp"] / "rework.json"
    rework.write_text(json.dumps({"verdict": "REWORK", "score": 40, "criteria_check": ["no"],
                                  "required_fixes": ["handle the empty case"]}))
    monkeypatch.setenv("AI_DEV_TEAM_REVIEW_FAKE", str(rework))
    plan(env, {"p1:T1": [{"files": {"x.py": f"X = {i}\n"}} for i in range(5)]})
    task(repo, "p1:T1")
    run_all(env, workers=1)
    assert repo.status_of("p1:T1") == S.BLOCKED
    assert len([e for e in oc_log(env)]) == 4                    # 1 + 3 reworks
    assert any(e["event_type"] == "MANAGER_REPLAN" for e in repo.events("p1:T1"))
    assert_main_untouched(env)


def test_timeout_kills_whole_process_group(env):
    repo = env["repo"]
    plan(env, {"p1:T1": [{"sleep": 30, "spawn_child": True}, {"files": {"y.py": "Y=1\n"}}]})
    task(repo, "p1:T1")
    start = time.monotonic()
    run_all(env, workers=1, timeout=2)
    assert time.monotonic() - start < 25
    ex = repo.latest_execution("p1:T1")
    assert ex is not None
    out = subprocess.run(["pgrep", "-f", "import time; time.sleep(60)"],
                         check=False, capture_output=True, text=True).stdout.strip()
    assert out == ""                                              # child was killed too
    assert repo.status_of("p1:T1") == S.COMPLETED                 # retried and finished


def test_corrupted_worktree_is_recreated_from_branch(env):
    wt = env["wt"]
    p = wt.ensure_task("p1:T9")
    (p / "work.py").write_text("W = 1\n")
    wt.commit_task("p1:T9", "wip")
    import shutil
    shutil.rmtree(p)                                              # directory vanished
    p2 = wt.ensure_task("p1:T9")
    assert (p2 / "work.py").read_text() == "W = 1\n"
    (p2 / ".git").write_text("gitdir: /nonexistent\n")            # broken link
    p3 = wt.ensure_task("p1:T9")
    assert (p3 / "work.py").read_text() == "W = 1\n"
    assert_main_untouched(env)


def test_crash_after_merge_before_record_is_idempotent(env):
    repo, wt = env["repo"], env["wt"]
    task(repo, "p1:T1")
    repo.promote_ready()
    c = repo.claim_next("developer-a", "m", 60)
    repo.mark_running(c)
    p = wt.ensure_task("p1:T1")
    (p / "z.py").write_text("Z = 1\n")
    repo.finish_execution(c, success=True, exit_code=0)
    repo.start_review("p1:T1")
    from ai_dev_team.domain import ReviewResult
    repo.apply_review(ReviewResult(task_id="p1:T1", verdict="PASS", score=90,
                                   reviewer_model="m"))
    # simulate: merge done in git, process died before the DB recorded it
    wt.commit_task("p1:T1", "work")
    integ = wt.ensure_integration()
    git(integ, "merge", "--no-ff", "--no-edit", wt.branch_for("p1:T1"))
    mq = MergeQueue(repo, wt, Tester())
    assert mq.process("p1:T1") == S.COMPLETED
    assert mq.process("p1:T1") == S.COMPLETED                     # duplicate event: no-op
    merges = sh(env["project"], "log", "--merges", "--oneline", wt.integration_branch)
    assert len(merges.splitlines()) == 1


def test_crash_mid_merge_leaves_integration_recoverable(env):
    repo, wt = env["repo"], env["wt"]
    integ = wt.ensure_integration()
    (integ / "app.py").write_text("half-written merge\n")         # dirty state after a crash
    MergeQueue(repo, wt, Tester())._clean_integration(integ)
    assert (integ / "app.py").read_text().startswith("VALUE = 1")


def test_protected_branches_are_refused(env):
    wt = env["wt"]
    with pytest.raises(GitError):
        wt._ensure(env["tmp"] / "x", "main", "main")
    with pytest.raises(GitError):
        wt._guard("feature/not-ours")


def test_resume_after_crash_in_testing(env):
    from ai_dev_team.pipeline import resume_stuck
    repo, wt = env["repo"], env["wt"]
    task(repo, "p1:T1")
    repo.promote_ready()
    c = repo.claim_next("developer-a", "m", 60)
    repo.mark_running(c)
    p = wt.ensure_task("p1:T1")
    (p / "r.py").write_text("R = 1\n")
    repo.finish_execution(c, success=True, exit_code=0)        # then the process died
    pipeline = ReviewPipeline(repo, Tester(), Reviewer(CFG), wt.path_for,
                              base_ref=wt.integration_branch)
    out = resume_stuck(repo, "p1", pipeline, MergeQueue(repo, wt, Tester()))
    assert out == {"p1:T1": "COMPLETED"}
    assert integ_file(env, "r.py") == "R = 1"
