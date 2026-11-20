"""Collect what the Developer changed in a workspace, for the Reviewer and the guardrails.

Includes committed + uncommitted changes against the base ref, plus untracked files.
Read-only: never stages, commits or checks anything out.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

TEST_PATH_HINTS = ("test", "tests", "spec", "__tests__")
# Untracked build/test by-products that are never "changes" (when not already gitignored).
JUNK_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules",
              ".venv", "venv", ".tox", "dist", "build", ".DS_Store", ".coverage", "htmlcov",
              # Swift / Xcode
              ".build", "DerivedData", "xcuserdata", ".swiftpm"}


def is_junk(path: str) -> bool:
    p = Path(path)
    return any(part in JUNK_PARTS for part in p.parts) \
        or p.suffix in (".pyc", ".pyo", ".xcresult")


class FileChange(BaseModel):
    path: str
    status: str          # A, M, D, R, ?? (untracked)
    added: int = 0
    removed: int = 0


class DiffBundle(BaseModel):
    base_ref: str
    head_sha: str | None
    files: list[FileChange] = Field(default_factory=list)
    patch: str = ""
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not self.files

    def is_test_path(self, path: str) -> bool:
        parts = [p.lower() for p in Path(path).parts]
        name = Path(path).name
        return any(p in TEST_PATH_HINTS or p.endswith("tests") for p in parts[:-1]) \
            or name.startswith("test_") \
            or name.endswith(("_test.py", ".test.ts", ".test.js", ".spec.ts", ".spec.js",
                              "Tests.swift", "Test.swift"))

    @property
    def deleted_tests(self) -> list[str]:
        return [f.path for f in self.files if f.status == "D" and self.is_test_path(f.path)]

    @property
    def conflict_markers(self) -> list[str]:
        """Files whose added lines still contain git conflict markers."""
        files, current = [], None
        for line in self.patch.splitlines():
            if line.startswith("diff --git"):
                current = line.split(" b/", 1)[-1]
            elif (line.startswith(("+<<<<<<< ", "+>>>>>>> ")) or line == "+=======") \
                    and current and current not in files:
                files.append(current)
        return files

    @property
    def removed_assertions(self) -> int:
        """Lines removed from test files that contained an assertion."""
        count, in_test = 0, False
        for line in self.patch.splitlines():
            if line.startswith("diff --git"):
                in_test = self.is_test_path(line.split(" b/", 1)[-1])
            elif in_test and line.startswith("-") and not line.startswith("---"):
                low = line.lower()
                if "assert" in low or "expect(" in low:
                    count += 1
        return count


def _git(ws: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True,
                       check=False, timeout=60)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def collect_diff(workspace: Path, base_ref: str, max_chars: int = 60_000) -> DiffBundle:
    ws = workspace
    head = _git(ws, "rev-parse", "HEAD", check=False).strip() or None
    merge_base = _git(ws, "merge-base", base_ref, "HEAD", check=False).strip() or base_ref
    files: dict[str, FileChange] = {}
    for line in _git(ws, "diff", "--name-status", "-M", merge_base).splitlines():
        parts = line.split("\t")
        status, path = parts[0][0], parts[-1]
        files[path] = FileChange(path=path, status=status)
    for line in _git(ws, "diff", "--numstat", "-M", merge_base).splitlines():
        a, r, *rest = line.split("\t")
        path = rest[-1].split(" => ")[-1].rstrip("}") if rest else ""
        if path in files and a != "-":
            files[path].added, files[path].removed = int(a), int(r)
    patch = _git(ws, "diff", "-M", "--unified=3", merge_base)
    for path in _git(ws, "ls-files", "--others", "--exclude-standard").splitlines():
        if is_junk(path):
            continue
        r = subprocess.run(["git", "-C", str(ws), "diff", "--no-index", "--", "/dev/null", path],
                           capture_output=True, text=True, check=False, timeout=60)
        patch += r.stdout
        files[path] = FileChange(path=path, status="??",
                                 added=sum(1 for ln in r.stdout.splitlines()
                                           if ln.startswith("+") and not ln.startswith("+++")))
    truncated = len(patch) > max_chars
    if truncated:
        patch = patch[:max_chars] + "\n... [diff truncated] ...\n"
    return DiffBundle(base_ref=base_ref, head_sha=head, files=list(files.values()),
                      patch=patch, truncated=truncated)
