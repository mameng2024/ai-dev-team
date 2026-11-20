"""OpenCode CLI executor (P2).

Verified against the OpenCode CLI docs (https://opencode.ai/docs/cli/commands/, V2):
`opencode run [--model provider/model] [--standalone] [--auto] [--archived]
[--format json] "<prompt>"`. V2 has no `--dir`: the working directory is the process cwd,
so each task's worktree is passed as cwd. Flags are probed from `opencode run --help`
once, so older/newer versions only get the flags they support.

- --standalone: private server per run, so the API key in this process env is used and
  two Developers never share one server-side session/cwd.
- --auto: auto-approve tool permissions that are not explicitly denied (no human at the
  terminal). Only ever runs inside a disposable worktree. Can be disabled in config.
- The process runs in its own process group and the whole group is killed on timeout.
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..domain import ExecutionResult, TaskSpec


@dataclass
class OpenCodeCapabilities:
    version: str = "unknown"
    flags: set[str] = field(default_factory=set)
    error: str | None = None

    def has(self, flag: str) -> bool:
        return flag in self.flags


def probe(binary: str = "opencode", timeout: int = 20) -> OpenCodeCapabilities:
    caps = OpenCodeCapabilities()
    try:
        v = subprocess.run([binary, "--version"], capture_output=True, text=True,
                           timeout=timeout, check=False)
        caps.version = (v.stdout or v.stderr).strip().splitlines()[0] if (
            v.stdout or v.stderr).strip() else "unknown"
        h = subprocess.run([binary, "run", "--help"], capture_output=True, text=True,
                           timeout=timeout, check=False)
        caps.flags = set(re.findall(r"(--[a-z][a-z0-9-]+)", h.stdout + h.stderr))
        if "--model" not in caps.flags:
            caps.error = "`opencode run --help` does not list --model; unsupported version?"
    except FileNotFoundError:
        caps.error = f"OpenCode binary '{binary}' not found"
    except subprocess.TimeoutExpired:
        caps.error = "OpenCode did not answer --help in time"
    return caps


def build_prompt(task: TaskSpec, rework_notes: list[str] | None) -> str:
    criteria = "\n".join(f"- {item}" for item in task.acceptance_criteria)
    fixes = ""
    if rework_notes:
        fixes = ("Required fixes from earlier attempts (tests, Reviewer or merge). The latest "
                 "attempt is last; all must be resolved:\n"
                 + "\n".join(f"- {n}" for n in rework_notes) + "\n\n")
    return (
        "You are a software development worker. Implement only this task.\n"
        f"Task ID: {task.id}\nTitle: {task.title}\n\n{task.description}\n\n"
        f"Acceptance criteria:\n{criteria}\n\n{fixes}"
        "Rules: work only inside the current directory. Respect the existing architecture. "
        "Add or update tests that prove each acceptance criterion; never delete or weaken "
        "existing tests. Do not read secrets. Do not commit, merge, push, rebase or switch "
        "branches (the orchestrator does that). Run the relevant tests before finishing. "
        "End with a short summary of changed files, tests and remaining risks."
    )


class OpenCodeExecutor:
    """Runs one isolated OpenCode CLI invocation in the task's worktree (cwd)."""

    def __init__(self, binary: str = "opencode", timeout_seconds: int = 1800,
                 auto_approve: bool = True, env: dict[str, str] | None = None,
                 model_map: dict[str, str] | None = None,
                 caps: OpenCodeCapabilities | None = None):
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self.auto_approve = auto_approve
        self.extra_env = env or {}
        self.model_map = model_map or {}
        self.caps = caps
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    def command(self, model: str, prompt: str, workspace: Path) -> list[str]:
        caps = self.caps or probe(self.binary)
        self.caps = caps
        if caps.error:
            raise RuntimeError(caps.error)
        cmd = [self.binary, "run"]
        if model:
            cmd += ["--model", self.model_map.get(model, model)]
        if caps.has("--standalone"):
            cmd.append("--standalone")
        if self.auto_approve and caps.has("--auto"):
            cmd.append("--auto")
        if caps.has("--archived"):
            cmd.append("--archived")
        if caps.has("--dir"):                  # older CLIs
            cmd += ["--dir", str(workspace)]
        cmd.append(prompt)
        return cmd

    async def execute(self, task: TaskSpec, workspace: Path, model: str, worker_id: str,
                      timeout: int | None = None,
                      rework_notes: list[str] | None = None) -> ExecutionResult:
        execution_id = str(uuid.uuid4())
        started = datetime.now(UTC)
        cmd = await asyncio.to_thread(self.command, model, build_prompt(task, rework_notes),
                                      workspace)
        env = {**os.environ, **self.extra_env}
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(workspace), env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        self._processes[execution_id] = proc
        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout or self.timeout_seconds)
        except TimeoutError:
            timed_out = True
            self._kill_group(proc)
            stdout_b, stderr_b = await proc.communicate()
        except asyncio.CancelledError:
            self._kill_group(proc)
            raise
        finally:
            self._processes.pop(execution_id, None)
        return ExecutionResult(
            execution_id=execution_id, task_id=task.id, worker_id=worker_id,
            exit_code=proc.returncode if proc.returncode is not None else -9,
            stdout=stdout_b.decode("utf-8", errors="replace")[-20000:],
            stderr=stderr_b.decode("utf-8", errors="replace")[-20000:],
            started_at=started, finished_at=datetime.now(UTC), timed_out=timed_out)

    @staticmethod
    def _kill_group(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()

    async def cancel(self, execution_id: str) -> None:
        proc = self._processes.get(execution_id)
        if proc and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
