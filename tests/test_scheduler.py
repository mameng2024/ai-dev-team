import asyncio
from pathlib import Path

from ai_dev_team.domain import ReviewResult, TaskSpec
from ai_dev_team.executors import DryRunExecutor
from ai_dev_team.scheduler import DurableScheduler, Scheduler, WorkerSpec

WORKERS = [WorkerSpec("developer-a", "deepseek/deepseek-flash"),
           WorkerSpec("developer-b", "deepseek/deepseek-flash")]


def test_scheduler_respects_parallel_limit():
    async def run():
        active = 0
        peak = 0

        async def worker(task, worker_id):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

        tasks = [
            (TaskSpec(id=f"t{i}", project_id="p", title=f"T{i}", description="x"), f"w{i%2}")
            for i in range(6)
        ]
        await Scheduler(2).run_batch(tasks, worker)
        assert peak == 2

    asyncio.run(run())


def specs(edges):
    return [TaskSpec(id=k, project_id="p1", title=k, description="x",
                     acceptance_criteria=["ok"], dependencies=v) for k, v in edges.items()]


def test_two_developers_take_different_tasks(repo):
    repo.create_tasks(specs({f"t{i}": [] for i in range(6)}))
    ex = DryRunExecutor(delay=0.05)
    sched = DurableScheduler(repo, ex, WORKERS, lambda c: Path("."), poll_seconds=0.01)
    report = asyncio.run(sched.run_until_idle())
    ids = [t for t, _ in report.claimed]
    assert sorted(ids) == [f"t{i}" for i in range(6)] and len(set(ids)) == 6
    assert {w for _, w in report.claimed} == {"developer-a", "developer-b"}
    assert report.peak_concurrency == 2
    assert set(report.outcomes.values()) == {"TESTING"}   # P3 gate takes over from here


def test_dag_runs_in_dependency_order_with_simulated_pipeline(repo):
    # a -> (b, c) -> d ; pipeline stub: review PASS + merge so dependants unlock
    repo.create_tasks(specs({"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}))
    order: list[str] = []

    async def pipeline(tid):
        repo.start_review(tid)
        repo.apply_review(ReviewResult(task_id=tid, verdict="PASS", score=90,
                                       reviewer_model="deepseek/deepseek-v4-pro"))
        repo.record_merge(tid, f"sha-{tid}")
        order.append(tid)

    sched = DurableScheduler(repo, DryRunExecutor(0.02), WORKERS, lambda c: Path("."),
                             poll_seconds=0.01, on_testing=pipeline)
    asyncio.run(sched.run_until_idle())
    assert order[0] == "a" and order[-1] == "d" and set(order[1:3]) == {"b", "c"}
    assert {t["status"] for t in repo.list_tasks()} == {"COMPLETED"}


def test_failed_execution_is_retried(repo):
    repo.create_tasks(specs({"x": []}))

    class FlakyOnce(DryRunExecutor):
        async def execute(self, task, *a, **k):
            r = await super().execute(task, *a, **k)
            if len(self.calls) == 1:
                r.exit_code = 1
            return r

    sched = DurableScheduler(repo, FlakyOnce(0.01), WORKERS, lambda c: Path("."),
                             poll_seconds=0.01)
    report = asyncio.run(sched.run_until_idle())
    assert [t for t, _ in report.claimed] == ["x", "x"]
    assert repo.status_of("x").value == "TESTING"
    assert repo.get_task("x")["retry_count"] == 1
