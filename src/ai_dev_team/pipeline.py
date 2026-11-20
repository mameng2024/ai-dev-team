"""P3 pipeline: TESTING -> test gate -> independent review -> PASS / REWORK / BLOCKED.

Runs after a Developer finishes (DurableScheduler.on_testing) and outside the Developer
semaphore, so reviews never take a Developer slot.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from .domain import TaskStatus
from .gitdiff import collect_diff
from .repository import TaskRepository
from .reviewer import Reviewer, build_input
from .tester import Tester

log = logging.getLogger(__name__)


class ReviewPipeline:
    def __init__(self, repo: TaskRepository, tester: Tester, reviewer: Reviewer,
                 workspace_fn: Callable[[str], Path], base_ref: str | None = None):
        self.repo = repo
        self.tester = tester
        self.reviewer = reviewer
        self.workspace_fn = workspace_fn
        self.base_ref = base_ref

    async def __call__(self, task_id: str) -> TaskStatus:
        return await asyncio.to_thread(self.process, task_id)

    def process(self, task_id: str) -> TaskStatus:
        status = self.repo.status_of(task_id)
        if status == TaskStatus.REVIEWING:       # retry a review that errored earlier
            return self._review(task_id)
        if status != TaskStatus.TESTING:
            raise ValueError(f"{task_id} is {status}, expected TESTING or REVIEWING")
        ws = self.workspace_fn(task_id)
        report = self.tester.run_checks(ws)
        self.repo.add_artifact(task_id, "TEST_REPORT", report.model_dump())
        if not report.passed:
            log.info("%s: tests failed -> rework", task_id)
            return self.repo.fail_tests(task_id, report.failures())
        diff = collect_diff(ws, self._base(task_id))
        markers = diff.conflict_markers
        if markers:
            return self.repo.fail_tests(task_id, [
                "Unresolved merge conflict markers remain in: " + ", ".join(markers)])
        if diff.empty:
            return self.repo.fail_tests(task_id, [
                "No code changes were found in the workspace; implement the task."])
        self.repo.start_review(task_id)
        return self._review(task_id, report=report, diff=diff)

    def _review(self, task_id: str, report=None, diff=None) -> TaskStatus:
        ws = self.workspace_fn(task_id)
        report = report or self.tester.run_checks(ws)
        diff = diff or collect_diff(ws, self._base(task_id))
        inp = build_input(self.repo.task_spec(task_id), self.repo.rework_notes(task_id),
                          diff, report)
        try:
            result, out = self.reviewer.review(inp, diff_empty=diff.empty)
        except Exception as exc:  # noqa: BLE001 - stays REVIEWING; `review` command retries
            log.error("%s: review failed: %s", task_id, exc)
            self.repo.add_artifact(task_id, "REVIEW_ERROR", {"error": str(exc)[:2000]})
            return TaskStatus.REVIEWING
        self.repo.record_usage(out.model, out.input_tokens, out.output_tokens, "OK",
                               task_id=task_id)
        execution = self.repo.latest_execution(task_id)
        self.repo.add_artifact(task_id, "DIFF_STATS", {
            "head": diff.head_sha, "files": [f.model_dump() for f in diff.files],
            "truncated": diff.truncated}, commit_sha=diff.head_sha)
        return self.repo.apply_review(result, execution["id"] if execution else None)

    def _base(self, task_id: str) -> str:
        if self.base_ref:
            return self.base_ref
        project = self.repo.get_project(self.repo.get_task(task_id)["project_id"])
        return project["default_branch"] if project else "main"


def review_then_merge(pipeline: ReviewPipeline, merger) -> Callable:
    """DurableScheduler.on_testing hook: test gate + review, then the serialized merge."""
    async def on_testing(task_id: str) -> None:
        if await pipeline(task_id) == TaskStatus.MERGE_QUEUED:
            await merger(task_id)
    return on_testing


def resume_stuck(repo: TaskRepository, project_id: str, pipeline: ReviewPipeline,
                 merger) -> dict[str, str]:
    """After a crash/restart: finish tasks left in TESTING, REVIEWING or MERGE_QUEUED
    (their Developer work is already in the worktree, so nothing is redone)."""
    out: dict[str, str] = {}
    for t in repo.list_tasks(project_id):
        st = t["status"]
        if st in (TaskStatus.TESTING.value, TaskStatus.REVIEWING.value):
            st = pipeline.process(t["id"]).value
        if st == TaskStatus.MERGE_QUEUED.value:
            st = merger.process(t["id"]).value
        if st != t["status"]:
            out[t["id"]] = st
    return out
