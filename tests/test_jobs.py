"""Unified page: whitelisted background jobs and the overview API."""
import json
import shutil
import subprocess
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from ai_dev_team.jobs import JobError, JobManager, build_argv
from ai_dev_team.settings_server import make_handler

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 32
OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def test_build_argv_whitelist(tmp_path):
    with pytest.raises(JobError):
        build_argv("shell", {"cmd": "rm -rf /"})
    with pytest.raises(JobError):
        build_argv("run", {"project": "x; rm -rf /"})
    with pytest.raises(JobError):
        build_argv("run", {"project": "p", "mode": "--evil"})
    with pytest.raises(JobError, match="Git"):
        build_argv("project_add", {"id": "p", "path": str(tmp_path)})
    assert build_argv("run", {"project": "p", "mode": "opencode"}) == (
        ["run", "p", "--opencode"], "p")
    assert build_argv("plan", {"project": "p", "request": "加一个夜间模式"})[0][-1] == "--no-save"
    assert "--no-save" not in build_argv("plan", {"project": "p", "request": "加一个夜间模式",
                                                  "save": True})[0]


def test_job_output_stop_and_single_mutating_job(tmp_path, monkeypatch):
    fake = tmp_path / "fakepy"
    fake.write_text("#!/bin/sh\necho start $@\nsleep 30\n")
    fake.chmod(0o755)
    jm = JobManager(python=str(fake))
    j = jm.start("run", {"project": "p", "mode": "dry"})
    with pytest.raises(JobError, match="已经有"):
        jm.start("merge", {"project": "p"})
    jm.start("integration", {"project": "p"})           # read-only: allowed alongside
    for _ in range(50):
        if j.total:
            break
        time.sleep(0.05)
    assert j.view()["lines"][0].startswith("start -m ai_dev_team run p --dry-run")
    jm.stop(j.id, grace=0.5)
    for _ in range(60):
        if j.finished:
            break
        time.sleep(0.05)
    assert j.status == "stopped"
    jm.start("merge", {"project": "p"})                 # free again
    jm.stop_all()


def call(base, path, body=None):
    req = urllib.request.Request(base + path, headers={"X-Token": TOKEN,
                                                       "Content-Type": "application/json"},
                                 data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    try:
        with OPEN(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            raise AssertionError(f"{e.code}: {raw[:500]!r}") from None


def test_unified_page_end_to_end(tmp_path, monkeypatch):
    """Add a project, rehearse a run (real states untouched), see the overview, quit."""
    cfg = tmp_path / "agents.yaml"
    shutil.copy(ROOT / "config" / "agents.yaml", cfg)
    db = tmp_path / "db.sqlite3"
    monkeypatch.setenv("AI_DEV_TEAM_CONFIG", str(cfg))
    monkeypatch.setenv("AI_DEV_TEAM_DB", str(db))
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))
    subprocess.run(["python", "-m", "ai_dev_team", "init"], check=True, capture_output=True)
    proj = tmp_path / "Mini"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(proj)], check=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, TOKEN, db))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_port}"

    def wait(job_id):
        for _ in range(300):
            _st, j = call(base, f"/api/jobs/{job_id}")
            if j["status"] != "running":
                return j
            time.sleep(0.1)
        raise AssertionError("job did not finish")

    st, j = call(base, "/api/jobs", {"kind": "project_add",
                                     "args": {"id": "mini", "path": str(proj)}})
    assert st == 200
    assert wait(j["id"])["status"] == "succeeded"
    from ai_dev_team.db import Database
    from ai_dev_team.domain import TaskSpec
    from ai_dev_team.repository import TaskRepository
    repo = TaskRepository(Database(db))
    repo.create_task(TaskSpec(id="mini:T1", project_id="mini", title="a", description="d",
                              acceptance_criteria=["c"]))
    repo.promote_ready()
    st, j = call(base, "/api/jobs", {"kind": "run", "args": {"project": "mini", "mode": "dry"}})
    done = wait(j["id"])
    assert done["status"] == "succeeded", done
    assert repo.status_of("mini:T1") == "READY"          # rehearsal used a DB copy
    st, ov = call(base, "/api/overview")
    p = ov["projects"][0]
    assert p["id"] == "mini" and p["counts"] == {"READY": 1} and p["repo_ok"]
    assert any("API Key" in x for x in ov["problems"])
    page = OPEN(f"{base}/?token={TOKEN}").read().decode()
    assert "项目与运行" in page
    st, _ = call(base, "/api/shutdown", {})
    assert st == 200
    t.join(5)
    assert not t.is_alive()
