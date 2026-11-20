from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    TESTING = "TESTING"
    REVIEWING = "REVIEWING"
    REWORK = "REWORK"
    MERGE_QUEUED = "MERGE_QUEUED"
    MERGED = "MERGED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


TERMINAL_STATUSES = {TaskStatus.COMPLETED}
ACTIVE_STATUSES = {TaskStatus.ASSIGNED, TaskStatus.RUNNING}


class AgentRole(StrEnum):
    MANAGER = "MANAGER"
    ARCHITECT = "ARCHITECT"
    DEVELOPER = "DEVELOPER"
    REVIEWER = "REVIEWER"


class TaskSpec(BaseModel):
    id: str
    project_id: str
    title: str
    description: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    priority: int = 5
    max_retries: int = 3


class Claim(BaseModel):
    """A task leased to one worker; returned by TaskRepository.claim_next()."""
    execution_id: str
    task: TaskSpec
    worker_id: str
    model_id: str | None
    lease_expires_at: float
    attempt: int
    rework_notes: list[str] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    severity: Literal["critical", "major", "minor", "info"]
    file_path: str | None = None
    line: int | None = None
    description: str
    evidence: str | None = None


class ReviewResult(BaseModel):
    task_id: str
    verdict: Literal["PASS", "REWORK", "BLOCKED"]
    score: int = Field(ge=0, le=100)
    findings: list[ReviewFinding] = Field(default_factory=list)
    required_fixes: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    reviewer_model: str


class ExecutionResult(BaseModel):
    execution_id: str
    task_id: str
    worker_id: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    started_at: datetime
    finished_at: datetime
    timed_out: bool = False
    commit_sha: str | None = None
