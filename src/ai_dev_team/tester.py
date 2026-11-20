"""Test gate: run the project's own checks inside the task workspace.

Commands come from `.ai-dev-team.yaml` in the repo (`test_commands:`), else are detected
(pytest / npm test / cargo test / go test / swift test). Commands are argument lists run
without a shell, with a timeout and a scrubbed environment (no API keys reach tests).
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

PROJECT_CONFIG = ".ai-dev-team.yaml"
SECRET_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


class CommandResult(BaseModel):
    command: list[str]
    exit_code: int
    duration_s: float
    timed_out: bool = False
    output_tail: str = ""


class TestReport(BaseModel):
    __test__ = False  # not a pytest test class
    passed: bool
    commands: list[CommandResult] = Field(default_factory=list)
    detected: bool = False      # commands were auto-detected, not configured
    note: str | None = None

    def summary(self) -> str:
        lines = [f"overall: {'PASS' if self.passed else 'FAIL'}"]
        if self.note:
            lines.append(f"note: {self.note}")
        for c in self.commands:
            lines.append(f"$ {shlex.join(c.command)} -> exit {c.exit_code}"
                         f"{' (timeout)' if c.timed_out else ''} in {c.duration_s:.1f}s")
            if c.exit_code != 0:
                lines.append(c.output_tail[-3000:])
        return "\n".join(lines)

    def failures(self) -> list[str]:
        out = []
        for c in self.commands:
            if c.exit_code != 0:
                tail = c.output_tail.strip().splitlines()[-15:]
                out.append(f"Test command `{shlex.join(c.command)}` failed "
                           f"(exit {c.exit_code}{', timeout' if c.timed_out else ''}). "
                           "Last output:\n" + "\n".join(tail))
        if not self.commands and not self.passed:
            out.append(self.note or "no test command could be run")
        return out


def load_project_commands(workspace: Path) -> tuple[list[list[str]], bool, int]:
    """Return (commands, detected, timeout_s)."""
    cfg_path = workspace / PROJECT_CONFIG
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        cmds = [shlex.split(c) if isinstance(c, str) else list(c)
                for c in cfg.get("test_commands", [])]
        return cmds, False, int(cfg.get("test_timeout_s", 900))
    detected: list[list[str]] = []
    if (workspace / "pyproject.toml").exists() or (workspace / "pytest.ini").exists() \
            or (workspace / "tests").is_dir():
        detected.append(["python", "-m", "pytest", "-q"])
    if (workspace / "package.json").exists():
        detected.append(["npm", "test", "--silent"])
    if (workspace / "Cargo.toml").exists():
        detected.append(["cargo", "test", "--quiet"])
    if (workspace / "go.mod").exists():
        detected.append(["go", "test", "./..."])
    if (workspace / "Package.swift").exists():
        detected.append(["swift", "test"])
    # Xcode projects (.xcodeproj / .xcworkspace) need a scheme and destination, so they are
    # never auto-detected: configure `xcodebuild test ...` in .ai-dev-team.yaml.
    return detected, True, 900


def scrubbed_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not any(m in k.upper() for m in SECRET_ENV_MARKERS)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class Tester:
    __test__ = False
    def __init__(self, require_tests: bool = True, tail_chars: int = 8000):
        self.require_tests = require_tests
        self.tail_chars = tail_chars

    def run_checks(self, workspace: Path) -> TestReport:
        commands, detected, timeout_s = load_project_commands(workspace)
        if not commands:
            return TestReport(
                passed=not self.require_tests, detected=detected,
                note=f"no test commands found; add `test_commands:` to {PROJECT_CONFIG}")
        results: list[CommandResult] = []
        for cmd in commands:
            start = time.monotonic()
            try:
                r = subprocess.run(cmd, cwd=workspace, capture_output=True, text=True,
                                   timeout=timeout_s, env=scrubbed_env(), check=False)
                out = (r.stdout + "\n" + r.stderr)[-self.tail_chars:]
                results.append(CommandResult(command=cmd, exit_code=r.returncode,
                                             duration_s=time.monotonic() - start,
                                             output_tail=out))
            except subprocess.TimeoutExpired as exc:
                out = ((exc.stdout or "") if isinstance(exc.stdout, str) else "")[-self.tail_chars:]
                results.append(CommandResult(command=cmd, exit_code=-9, timed_out=True,
                                             duration_s=time.monotonic() - start,
                                             output_tail=out))
            except FileNotFoundError:
                results.append(CommandResult(command=cmd, exit_code=127,
                                             duration_s=time.monotonic() - start,
                                             output_tail=f"command not found: {cmd[0]}"))
        return TestReport(passed=all(c.exit_code == 0 for c in results), commands=results,
                          detected=detected)
