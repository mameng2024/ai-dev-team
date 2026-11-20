"""P4: serialized merge queue into the integration branch, never into main.

For each MERGE_QUEUED task, one at a time:
 1. commit the task worktree (Developer output + any resolved conflict)
 2. if the task branch is already in integration (crash after merge) -> just record it
 3. merge --no-ff into the integration worktree
    - conflict: abort, start the same merge inside the task worktree so the conflict
      markers are there for the Developer, task -> REWORK with the conflicted files
 4. run the project's tests on integration (post-merge regression)
    - fail: undo the merge (reset to the previous integration HEAD), task -> REWORK
 5. record_merge (idempotent) -> COMPLETED, remove the task worktree, unlock dependents
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

from .domain import TaskStatus
from .repository import TaskRepository
from .tester import Tester
from .worktrees import GitError, WorktreeManager, git

log = logging.getLogger(__name__)


class MergeQueue:
    def __init__(self, repo: TaskRepository, worktrees: WorktreeManager, tester: Tester,
                 cleanup: bool = True):
        self.repo = repo
        self.wt = worktrees
        self.tester = tester
        self.cleanup = cleanup
        self._lock = threading.Lock()          # one merge at a time, across threads

    async def __call__(self, task_id: str) -> TaskStatus:
        return await asyncio.to_thread(self.process, task_id)

    def drain(self, project_id: str | None = None) -> dict[str, str]:
        out = {}
        for t in self.repo.list_tasks(project_id):
            if t["status"] == TaskStatus.MERGE_QUEUED.value:
                out[t["id"]] = self.process(t["id"]).value
        return out

    def process(self, task_id: str) -> TaskStatus:
        with self._lock:
            status = self.repo.status_of(task_id)
            if status != TaskStatus.MERGE_QUEUED:
                return status
            integ = self.wt.ensure_integration()
            self._clean_integration(integ)
            head_task = self.wt.commit_task(task_id, f"{task_id}: reviewed changes")
            if self.wt.is_merged(task_id):
                return self._complete(task_id, git(integ, "rev-parse", "HEAD").stdout.strip())
            before = git(integ, "rev-parse", "HEAD").stdout.strip()
            with self.wt.lock:
                m = git(integ, "merge", "--no-ff", "--no-edit", "-m",
                        f"Merge {task_id} ({head_task[:10]})", self.wt.branch_for(task_id),
                        check=False)
                if m.returncode != 0:
                    files = self._conflicts(integ)
                    git(integ, "merge", "--abort", check=False)
                    git(integ, "reset", "--hard", before, check=False)
            if m.returncode != 0:
                return self._conflict_rework(task_id, files)
            report = self.tester.run_checks(integ)
            self.repo.add_artifact(task_id, "REGRESSION_REPORT", report.model_dump())
            if not report.passed:
                with self.wt.lock:
                    git(integ, "reset", "--hard", before)
                return self.repo.merge_failed(task_id, [
                    ("The change passed on its own branch but broke the integration branch "
                     "after merging (it conflicts in behaviour with work merged earlier). "
                     "Make it pass together with the latest integration code."),
                    *report.failures()])
            sha = git(integ, "rev-parse", "HEAD").stdout.strip()
            return self._complete(task_id, sha)

    # ---------------------------------------------------------------- helpers
    def _complete(self, task_id: str, sha: str) -> TaskStatus:
        self.repo.record_merge(task_id, sha)
        if self.cleanup:
            try:
                self.wt.remove_task(task_id)
            except GitError as exc:  # cleanup failure never undoes a merge
                log.warning("cleanup of %s failed: %s", task_id, exc)
        self.repo.promote_ready()
        return TaskStatus.COMPLETED

    def _clean_integration(self, integ: Path) -> None:
        """Recover from a crash mid-merge: abort and drop anything uncommitted."""
        git(integ, "merge", "--abort", check=False)
        git(integ, "reset", "--hard", "HEAD", check=False)
        git(integ, "clean", "-fdq", check=False)

    @staticmethod
    def _conflicts(cwd: Path) -> list[str]:
        out = git(cwd, "diff", "--name-only", "--diff-filter=U", check=False).stdout
        return [f for f in out.splitlines() if f]

    def _conflict_rework(self, task_id: str, files: list[str]) -> TaskStatus:
        task_path = self.wt.path_for(task_id)
        # Bring integration into the task branch; leave conflict markers for the Developer.
        with self.wt.lock:
            r = git(task_path, "merge", "--no-ff", "--no-edit", self.wt.integration_branch,
                    check=False)
        if r.returncode == 0:
            # Merged cleanly in this direction: re-test and re-review the combined code.
            return self.repo.merge_failed(task_id, [
                ("The integration branch moved on; it has been merged into your branch. "
                 "Re-check that the task still works with the new code.")])
        files = self._conflicts(task_path) or files
        return self.repo.merge_failed(task_id, [
            "Merge conflict with work merged earlier. The merge is in progress in your "
            "workspace; resolve every conflict marker (<<<<<<< ======= >>>>>>>) in: "
            + ", ".join(files) + ". Keep both features working; do not drop either side."])
