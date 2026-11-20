"""P2: one disposable Git worktree + branch per task, plus one integration worktree.

Layout (outside the target repo, nothing is written into its working tree):
    ~/.ai-dev-team/worktrees/<project>/<task-key>/     branch aidt/<project>/<task-key>
    ~/.ai-dev-team/worktrees/<project>/_integration/  branch aidt/<project>/integration

Rules
- The target repo's default branch (main) is never checked out, committed, reset or merged
  into. Task branches start from the integration branch, which starts from main once.
- A worktree that already exists is reused, so rework continues on the previous attempt.
- A corrupted worktree (directory gone, or .git link broken) is pruned and recreated from
  its branch; the branch keeps the work.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
from pathlib import Path

from .gitdiff import is_junk
from .secrets import home_dir

PROTECTED = {"main", "master", "develop", "trunk"}
_REPO_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def repo_lock(repo: Path) -> threading.RLock:
    """One lock per repository: git ref/worktree updates from the scheduler and the merge
    queue never race on .git locks."""
    with _LOCKS_GUARD:
        return _REPO_LOCKS.setdefault(str(repo), threading.RLock())


class GitError(RuntimeError):
    pass


def git(cwd: Path, *args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=ai-dev-team",
         "-c", "user.email=ai-dev-team@localhost", "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, timeout=timeout, check=False)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:800]}")
    return r


def safe_key(task_id: str) -> str:
    key = task_id.split(":", 1)[-1]
    key = re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip(".-") or "task"
    return key[:80]


class WorktreeManager:
    def __init__(self, repo_path: Path, project_id: str, default_branch: str = "main",
                 root: Path | None = None):
        self.repo = Path(repo_path).expanduser().resolve()
        self.project = re.sub(r"[^A-Za-z0-9._-]+", "-", project_id)
        self.default_branch = default_branch
        self.root = Path(root or home_dir() / "worktrees") / self.project
        if not (self.repo / ".git").exists():
            raise GitError(f"{self.repo} is not a git repository")
        self.lock = repo_lock(self.repo)

    # -------------------------------------------------------------- names
    @property
    def integration_branch(self) -> str:
        return f"aidt/{self.project}/integration"

    @property
    def integration_path(self) -> Path:
        return self.root / "_integration"

    def branch_for(self, task_id: str) -> str:
        return f"aidt/{self.project}/{safe_key(task_id)}"

    def path_for(self, task_id: str) -> Path:
        return self.root / safe_key(task_id)

    def _guard(self, branch: str) -> None:
        if branch in PROTECTED or branch == self.default_branch or not branch.startswith("aidt/"):
            raise GitError(f"refusing to operate on protected branch '{branch}'")

    def _branch_exists(self, branch: str) -> bool:
        return git(self.repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                   check=False).returncode == 0

    def _healthy(self, path: Path, branch: str) -> bool:
        if not path.is_dir():
            return False
        r = git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False)
        top = git(path, "rev-parse", "--show-toplevel", check=False).stdout.strip()
        return r.returncode == 0 and r.stdout.strip() == branch and \
            Path(top).resolve() == path.resolve()

    def _ensure(self, path: Path, branch: str, start_point: str) -> Path:
        self._guard(branch)
        with self.lock:
            return self._ensure_locked(path, branch, start_point)

    def _ensure_locked(self, path: Path, branch: str, start_point: str) -> Path:
        if self._healthy(path, branch):
            return path
        if path.exists():                         # corrupted: keep the branch, drop the dir
            shutil.rmtree(path, ignore_errors=True)
        git(self.repo, "worktree", "prune")
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._branch_exists(branch):
            git(self.repo, "worktree", "add", "--force", str(path), branch)
        else:
            git(self.repo, "worktree", "add", "-b", branch, str(path), start_point)
        return path

    # -------------------------------------------------------------- public
    def ensure_integration(self) -> Path:
        return self._ensure(self.integration_path, self.integration_branch,
                            self.default_branch)

    def ensure_task(self, task_id: str) -> Path:
        self.ensure_integration()
        return self._ensure(self.path_for(task_id), self.branch_for(task_id),
                            self.integration_branch)

    def commit_task(self, task_id: str, message: str) -> str:
        """Commit everything the Developer changed (build junk excluded). Also concludes a
        merge the Developer resolved. Returns HEAD."""
        path = self.path_for(task_id)
        self._guard(self.branch_for(task_id))
        with self.lock:
            return self._commit_locked(path, message)

    def _commit_locked(self, path: Path, message: str) -> str:
        raw = git(path, "status", "--porcelain", "-z", "-uall").stdout.split("\0")
        paths, i = [], 0
        while i < len(raw):
            entry = raw[i]
            if len(entry) > 3:
                paths.append(entry[3:])
                if entry[0] in "RC":          # rename/copy: next field is the old path
                    paths.append(raw[i + 1])
                    i += 1
            i += 1
        keep = [p for p in paths if not is_junk(p)]
        if keep:
            git(path, "add", "-A", "--", *keep)
        merging = (Path(git(path, "rev-parse", "--git-dir").stdout.strip()) / "MERGE_HEAD")
        merging = merging if merging.is_absolute() else path / merging
        if keep or merging.exists():
            git(path, "commit", "--no-verify", "-m", message, "--allow-empty")
        return git(path, "rev-parse", "HEAD").stdout.strip()

    def is_merged(self, task_id: str) -> bool:
        return git(self.repo, "merge-base", "--is-ancestor", self.branch_for(task_id),
                   self.integration_branch, check=False).returncode == 0

    def remove_task(self, task_id: str, delete_branch: bool = True) -> None:
        path, branch = self.path_for(task_id), self.branch_for(task_id)
        self._guard(branch)
        with self.lock:
            self._remove_locked(task_id, path, branch, delete_branch)

    def _remove_locked(self, task_id, path, branch, delete_branch) -> None:
        if path.exists():
            git(self.repo, "worktree", "remove", "--force", str(path), check=False)
            shutil.rmtree(path, ignore_errors=True)
        git(self.repo, "worktree", "prune", check=False)
        if delete_branch and self._branch_exists(branch) and self.is_merged(task_id):
            git(self.repo, "branch", "-D", branch, check=False)

    def integration_log(self, limit: int = 20) -> list[str]:
        if not self._branch_exists(self.integration_branch):
            return []
        return git(self.repo, "log", "--oneline", f"-{limit}",
                   f"{self.default_branch}..{self.integration_branch}").stdout.splitlines()
