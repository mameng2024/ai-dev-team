"""Deterministic scheduler.

- ``Scheduler``: the bounded semaphore primitive (kept from v0.1.0).
- ``DurableScheduler``: P1. SQLite-backed dispatch loop. Each Developer claims tasks
  through ``TaskRepository.claim_next`` (atomic lease), heartbeats the lease while the
  executor runs, and records the result. On start it recovers leases left behind by a
  crash, so a restart never loses or double-runs a task.

The scheduler stops a successful task at TESTING: the test gate and Reviewer (P3) and
the merge queue (P4) take over from there. ``on_testing`` lets tests or a later phase
plug that pipeline in.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .domain import Claim, TaskSpec, TaskStatus
from .executors import CodeExecutor
from .repository import TaskRepository, TransitionError

__all__ = ["DurableScheduler", "RunReport", "Scheduler", "WorkerSpec"]

log = logging.getLogger(__name__)
WorkerFn = Callable[[TaskSpec, str], Awaitable[None]]
WorkspaceFn = Callable[[Claim], Path]
OnTesting = Callable[[str], Awaitable[None]]


class Scheduler:
    """Small bounded dispatcher."""

    def __init__(self, max_parallel_tasks: int = 2):
        if max_parallel_tasks < 1:
            raise ValueError("max_parallel_tasks must be >= 1")
        self.semaphore = asyncio.Semaphore(max_parallel_tasks)

    async def run_one(self, task: TaskSpec, worker_id: str, worker_fn: WorkerFn) -> None:
        async with self.semaphore:
            await worker_fn(task, worker_id)

    async def run_batch(self, assignments: list[tuple[TaskSpec, str]], worker_fn: WorkerFn):
        await asyncio.gather(
            *(self.run_one(task, worker_id, worker_fn) for task, worker_id in assignments)
        )


@dataclass
class WorkerSpec:
    id: str
    model: str | None


@dataclass
class RunReport:
    claimed: list[tuple[str, str]] = field(default_factory=list)   # (task_id, worker)
    outcomes: dict[str, str] = field(default_factory=dict)        # task_id -> final status
    recovered: list[str] = field(default_factory=list)
    peak_concurrency: int = 0


class DurableScheduler:
    def __init__(self, repo: TaskRepository, executor: CodeExecutor,
                 workers: list[WorkerSpec], workspace_fn: WorkspaceFn, *,
                 max_concurrency: int = 2, lease_seconds: int = 900,
                 heartbeat_seconds: float | None = None, timeout_seconds: int = 1800,
                 poll_seconds: float = 0.2, on_testing: OnTesting | None = None,
                 project_id: str | None = None, routing: dict[str, str] | None = None):
        if not workers:
            raise ValueError("at least one developer worker is required")
        self.repo = repo
        self.executor = executor
        self.workers = workers
        self.workspace_fn = workspace_fn
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds or max(lease_seconds / 3, 1)
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self.on_testing = on_testing
        self.project_id = project_id
        self.routing = routing or {}
        self._active = 0      # executions currently holding a semaphore slot
        self._inflight = 0    # claims not yet fully handled (incl. post-execution pipeline)
        self.report = RunReport()

    async def run_until_idle(self, max_idle_polls: int = 3) -> RunReport:
        """Run all workers until no task is READY/ASSIGNED/RUNNING for a few polls."""
        self.report.recovered = self.repo.recover_expired_leases()
        await asyncio.gather(*(self._worker_loop(w, max_idle_polls) for w in self.workers))
        return self.report

    async def _worker_loop(self, worker: WorkerSpec, max_idle_polls: int) -> None:
        idle = 0
        while idle < max_idle_polls:
            self.repo.promote_ready(self.project_id)
            claim = await asyncio.to_thread(
                self.repo.claim_next, worker.id, worker.model, self.lease_seconds,
                self.project_id)
            if claim is None:
                if self._has_inflight():
                    idle = 0
                else:
                    idle += 1
                await asyncio.sleep(self.poll_seconds)
                continue
            idle = 0
            self.report.claimed.append((claim.task.id, worker.id))
            self._inflight += 1
            try:
                await self._run_claim(claim, worker)
            finally:
                self._inflight -= 1

    def model_for(self, task: TaskSpec, worker: WorkerSpec) -> str:
        """developer_routing[complexity] when the Architect tagged the task, else the
        worker's own model."""
        for level in ("complex", "simple"):
            if level in task.required_capabilities and self.routing.get(level):
                return self.routing[level]
        return worker.model or ""

    def _has_inflight(self) -> bool:
        # Another worker is still executing: its result may unlock dependent tasks.
        return self._inflight > 0

    async def _run_claim(self, claim: Claim, worker: WorkerSpec) -> None:
        async with self.semaphore:
            self._active += 1
            self.report.peak_concurrency = max(self.report.peak_concurrency, self._active)
            heartbeat = asyncio.create_task(self._heartbeat(claim))
            try:
                self.repo.mark_running(claim)
                workspace = self.workspace_fn(claim)
                result = await self.executor.execute(
                    claim.task, workspace, self.model_for(claim.task, worker), worker.id,
                    timeout=self.timeout_seconds, rework_notes=claim.rework_notes)
                ok = result.exit_code == 0 and not result.timed_out
                status = self.repo.finish_execution(
                    claim, success=ok, exit_code=result.exit_code, timed_out=result.timed_out,
                    commit_sha=result.commit_sha,
                    error=None if ok else (result.stderr[-2000:] or "execution failed"))
            except TransitionError as exc:
                log.warning("lease lost for %s: %s", claim.task.id, exc)
                status = TaskStatus(self.repo.status_of(claim.task.id))
            except Exception as exc:  # executor crash -> record as failure
                log.exception("executor error on %s", claim.task.id)
                try:
                    status = self.repo.finish_execution(
                        claim, success=False, exit_code=-1, error=str(exc)[:2000])
                except TransitionError:
                    status = TaskStatus(self.repo.status_of(claim.task.id))
            finally:
                heartbeat.cancel()
                self._active -= 1
        self.report.outcomes[claim.task.id] = status.value
        if status == TaskStatus.TESTING and self.on_testing:
            await self.on_testing(claim.task.id)
            self.report.outcomes[claim.task.id] = self.repo.status_of(claim.task.id).value

    async def _heartbeat(self, claim: Claim) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat_seconds)
                if not self.repo.renew_lease(claim.task.id, claim.worker_id, self.lease_seconds):
                    log.warning("could not renew lease for %s", claim.task.id)
                    return
        except asyncio.CancelledError:
            pass
