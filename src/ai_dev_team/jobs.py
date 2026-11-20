"""Background jobs started from the unified page.

Only a fixed set of `ai-dev-team` commands can be started (no arbitrary shell). Each job
is a child process `python -m ai_dev_team <command> ...` in its own process group, with
its output kept in memory for the page. One mutating job per project at a time.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
MUTATING = {"plan", "run", "merge", "review", "acceptance", "accept", "reject"}

LABELS = {
    "init": "初始化", "project_add": "添加项目", "plan": "规划", "run": "运行",
    "merge": "处理合并队列", "integration": "集成分支", "recover": "恢复租约",
    "review": "重新审核", "config_check": "检查配置", "opencode_check": "检查 OpenCode",
    "acceptance": "Manager 验收", "accept": "人工验收通过", "reject": "人工验收打回",
}


class JobError(ValueError):
    pass


def _need(args: dict, key: str, rx: re.Pattern, label: str) -> str:
    v = str(args.get(key, "")).strip()
    if not rx.match(v):
        raise JobError(f"{label}格式不对")
    return v


def build_argv(kind: str, args: dict) -> tuple[list[str], str | None]:
    """Whitelisted command line for a job kind -> (argv, project_id)."""
    if kind == "init":
        return ["init"], None
    if kind == "config_check":
        return ["config-check"], None
    if kind == "opencode_check":
        return ["opencode-check"], None
    if kind == "recover":
        return ["recover"], None
    if kind == "project_add":
        pid = _need(args, "id", ID_RE, "项目 ID ")
        path = Path(str(args.get("path", "")).strip()).expanduser()
        if not (path / ".git").exists():
            raise JobError(f"{path} 不是 Git 仓库（没有 .git）")
        branch = _need({"b": args.get("branch") or "main"}, "b", BRANCH_RE, "主分支名")
        argv = ["project-add", pid, str(path.resolve()), "--branch", branch]
        if args.get("name"):
            argv += ["--name", str(args["name"])[:80]]
        return argv, pid
    if kind == "plan":
        pid = _need(args, "project", ID_RE, "项目 ID ")
        req = str(args.get("request", "")).strip()
        if not 5 <= len(req) <= 8000:
            raise JobError("需求描述太短或太长")
        argv = ["plan", pid, req]
        if not args.get("save"):
            argv.append("--no-save")
        return argv, pid
    if kind == "run":
        pid = _need(args, "project", ID_RE, "项目 ID ")
        mode = args.get("mode", "dry")
        if mode not in ("dry", "opencode"):
            raise JobError("运行模式只能是 dry 或 opencode")
        return ["run", pid, "--opencode" if mode == "opencode" else "--dry-run"], pid
    if kind in ("merge", "integration"):
        pid = _need(args, "project", ID_RE, "项目 ID ")
        return [kind, pid], pid
    if kind == "acceptance":
        pid = _need(args, "project", ID_RE, "项目 ID ")
        return ["acceptance", pid], pid
    if kind in ("accept", "reject"):
        pid = _need(args, "project", ID_RE, "项目 ID ")
        note = str(args.get("note", "")).strip()
        if len(note) > 4000:
            raise JobError("备注太长")
        if kind == "reject":
            if len(note) < 2:
                raise JobError("打回时请写清楚哪里不对")
            argv = ["reject", pid, "--note", note]
            if args.get("replan", True):
                argv.append("--replan")
            return argv, pid
        argv = ["accept", pid] + (["--note", note] if note else [])
        if args.get("force"):
            argv.append("--force")
        return argv, pid
    if kind == "review":
        tid = _need(args, "task", TASK_RE, "任务 ID ")
        return ["review", tid], tid.split(":", 1)[0]
    raise JobError(f"不支持的操作: {kind}")


@dataclass
class Job:
    id: str
    kind: str
    argv: list[str]
    project: str | None
    started: float = field(default_factory=time.time)
    finished: float | None = None
    exit_code: int | None = None
    stopped: bool = False
    lines: deque = field(default_factory=lambda: deque(maxlen=3000))
    total: int = 0
    proc: subprocess.Popen | None = None

    @property
    def status(self) -> str:
        if self.finished is None:
            return "running"
        if self.stopped:
            return "stopped"
        return "succeeded" if self.exit_code == 0 else "failed"

    def view(self, since: int = 0) -> dict:
        first = self.total - len(self.lines)
        start = max(since, first)
        out = list(self.lines)[start - first:]
        return {"id": self.id, "kind": self.kind, "label": LABELS.get(self.kind, self.kind),
                "project": self.project, "status": self.status, "exit_code": self.exit_code,
                "started": self.started, "finished": self.finished,
                "command": "ai-dev-team " + " ".join(self.argv[:3]) +
                           (" …" if len(self.argv) > 3 else ""),
                "lines": out, "next": self.total}


class JobManager:
    def __init__(self, env: dict[str, str] | None = None, python: str = sys.executable):
        self.python = python
        self.env = env
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def running(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.finished is None]

    def start(self, kind: str, args: dict) -> Job:
        argv, project = build_argv(kind, args or {})
        with self._lock:
            if kind in MUTATING:
                for j in self.running():
                    if j.project == project and j.kind in MUTATING:
                        raise JobError(f"项目 {project} 已经有「{LABELS[j.kind]}」在进行，"
                                       "等它结束或先停止它")
            job = Job(uuid.uuid4().hex[:12], kind, argv, project)
            env = {**(self.env or os.environ), "PYTHONUNBUFFERED": "1", "NO_COLOR": "1",
                   "TERM": "dumb", "COLUMNS": "140"}
            job.proc = subprocess.Popen(
                [self.python, "-m", "ai_dev_team", *argv], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                start_new_session=True, text=True, encoding="utf-8", errors="replace",
                bufsize=1)
            self.jobs[job.id] = job
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        return job

    def _pump(self, job: Job) -> None:
        assert job.proc and job.proc.stdout
        for line in job.proc.stdout:
            job.lines.append(line.rstrip("\n"))
            job.total += 1
        job.exit_code = job.proc.wait()
        job.finished = time.time()

    def stop(self, job_id: str, grace: float = 8.0) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobError("没有这个任务")
        if job.finished is None and job.proc:
            job.stopped = True
            try:
                os.killpg(job.proc.pid, signal.SIGINT)     # like Ctrl+C: leases recover
            except ProcessLookupError:
                return job

            def hard_kill():
                time.sleep(grace)
                if job.finished is None:
                    try:
                        os.killpg(job.proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            threading.Thread(target=hard_kill, daemon=True).start()
        return job

    def list(self) -> list[dict]:
        jobs = sorted(self.jobs.values(), key=lambda j: j.started, reverse=True)[:30]
        return [{k: v for k, v in j.view(j.total).items() if k != "lines"} for j in jobs]

    def stop_all(self) -> None:
        for j in self.running():
            self.stop(j.id, grace=3.0)
