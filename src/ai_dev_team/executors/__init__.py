"""Execution backends."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ..domain import ExecutionResult, TaskSpec


class CodeExecutor(Protocol):
    async def execute(self, task: TaskSpec, workspace: Path, model: str, worker_id: str,
                      timeout: int | None = None,
                      rework_notes: list[str] | None = None) -> ExecutionResult: ...

    async def cancel(self, execution_id: str) -> None: ...


class DryRunExecutor:
    """Touches nothing. Used for P1 scheduling tests and `ai-dev-team run --dry-run`."""

    def __init__(self, delay: float = 0.05, fail_ids: set[str] | None = None):
        self.delay = delay
        self.fail_ids = fail_ids or set()
        self.calls: list[tuple[str, str]] = []

    async def execute(self, task, workspace, model, worker_id, timeout=None, rework_notes=None):
        started = datetime.now(UTC)
        self.calls.append((task.id, worker_id))
        await asyncio.sleep(self.delay)
        failed = task.id in self.fail_ids
        return ExecutionResult(
            execution_id=str(uuid.uuid4()), task_id=task.id, worker_id=worker_id,
            exit_code=1 if failed else 0, stdout=f"[dry-run] {task.title}",
            started_at=started, finished_at=datetime.now(UTC),
        )

    async def cancel(self, execution_id: str) -> None:
        return None
