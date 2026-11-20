import pytest

from ai_dev_team.domain import ReviewResult, TaskSpec
from ai_dev_team.domain import TaskStatus as S
from ai_dev_team.repository import DependencyError, TransitionError


def spec(i, deps=(), **kw):
    return TaskSpec(id=i, project_id="p1", title=i, description="x",
                    acceptance_criteria=["works"], dependencies=list(deps), **kw)


def to_reviewing(repo, tid, worker="developer-a"):
    repo.promote_ready()
    c = repo.claim_next(worker, "m", 60)
    assert c and c.task.id == tid
    repo.mark_running(c)
    assert repo.finish_execution(c, success=True, exit_code=0, commit_sha="abc") == S.TESTING
    repo.start_review(tid)
    return c


def review(tid, verdict, fixes=()):
    return ReviewResult(task_id=tid, verdict=verdict, score=80 if verdict == "PASS" else 40,
                        required_fixes=list(fixes), reviewer_model="deepseek/deepseek-v4-pro")


def test_valid_transition(repo):
    repo.create_task(spec("t1"))
    repo.transition("t1", S.READY)
    assert repo.status_of("t1") == S.READY


def test_invalid_transition_rejected(repo):
    repo.create_task(spec("t1"))
    with pytest.raises(TransitionError):
        repo.transition("t1", S.COMPLETED)


def test_cycle_rejected(repo):
    repo.create_task(spec("a"))
    with pytest.raises(DependencyError, match="cycle"):
        repo.create_tasks([spec("b", ["c"]), spec("c", ["b"])])
    assert [t["id"] for t in repo.list_tasks()] == ["a"]  # batch rolled back


def test_unknown_dependency_rejected(repo):
    with pytest.raises(DependencyError):
        repo.create_task(spec("b", ["missing"]))


def test_ready_requires_completed_dependencies(repo):
    repo.create_tasks([spec("a"), spec("b", ["a"])])
    assert repo.promote_ready() == ["a"]
    with pytest.raises(DependencyError):
        repo.transition("b", S.READY)


def test_merge_queue_requires_pass(repo):
    repo.create_task(spec("t1"))
    to_reviewing(repo, "t1")
    with pytest.raises(TransitionError):
        repo.transition("t1", S.MERGE_QUEUED)
    assert repo.apply_review(review("t1", "PASS")) == S.MERGE_QUEUED


def test_rework_carries_fixes_and_is_bounded(repo):
    repo.create_task(spec("t1"))
    for attempt in range(3):
        to_reviewing(repo, "t1")
        assert repo.apply_review(review("t1", "REWORK", [f"fix {attempt}"])) == S.READY
    claim = to_reviewing(repo, "t1")
    assert claim.rework_notes == ["[attempt 1] fix 0", "[attempt 2] fix 1", "[attempt 3] fix 2"]
    assert claim.attempt == 4
    assert repo.apply_review(review("t1", "REWORK", ["fix 3"])) == S.BLOCKED
    assert any(e["event_type"] == "MANAGER_REPLAN" for e in repo.events("t1"))
    assert repo.get_task("t1")["retry_count"] == 3


def test_duplicate_merge_event_is_idempotent(repo):
    repo.create_tasks([spec("a"), spec("b", ["a"])])
    to_reviewing(repo, "a")
    repo.apply_review(review("a", "PASS"))
    assert repo.record_merge("a", "sha1") is True
    assert repo.record_merge("a", "sha1") is False        # duplicate event: no second merge
    with pytest.raises(TransitionError):
        repo.record_merge("a", "other-sha")
    assert repo.status_of("a") == S.COMPLETED
    assert repo.promote_ready() == ["b"]                   # dependency now satisfied


def test_failed_execution_retries_then_blocks(repo):
    repo.create_task(spec("t1"))
    repo.promote_ready()
    for _ in range(3):
        c = repo.claim_next("developer-a", "m", 60)
        repo.mark_running(c)
        assert repo.finish_execution(c, success=False, exit_code=1, error="boom") == S.READY
    c = repo.claim_next("developer-a", "m", 60)
    repo.mark_running(c)
    assert repo.finish_execution(c, success=False, exit_code=1) == S.BLOCKED
