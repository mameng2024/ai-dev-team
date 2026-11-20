import threading
import time

import pytest

from ai_dev_team.db import Database
from ai_dev_team.domain import TaskSpec
from ai_dev_team.domain import TaskStatus as S
from ai_dev_team.repository import TaskRepository, TransitionError


def make(repo, n):
    repo.create_tasks([TaskSpec(id=f"t{i}", project_id="p1", title=f"t{i}", description="x",
                                acceptance_criteria=["ok"]) for i in range(n)])
    repo.promote_ready()


def test_concurrent_claims_never_double_assign(repo, tmp_path):
    make(repo, 20)
    workers = [f"w{i}" for i in range(8)]
    for w in workers:
        repo.register_agent(w, "DEVELOPER")
    claimed: list[tuple[str, str]] = []
    lock = threading.Lock()

    def worker(wid):
        # each thread uses its own connections, like separate processes would
        r = TaskRepository(Database(repo.db.path))
        while True:
            c = r.claim_next(wid, "m", 60)
            if c is None:
                if not any(t["status"] == "READY" for t in r.list_tasks()):
                    return
                continue
            with lock:
                claimed.append((c.task.id, wid))
            r.mark_running(c)
            r.finish_execution(c, success=True, exit_code=0)

    threads = [threading.Thread(target=worker, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    ids = [t for t, _ in claimed]
    assert sorted(ids) == sorted(f"t{i}" for i in range(20))
    assert len(ids) == len(set(ids))


def test_worker_holds_one_task_at_a_time(repo):
    make(repo, 3)
    assert repo.claim_next("developer-a", "m", 60) is not None
    assert repo.claim_next("developer-a", "m", 60) is None
    assert repo.claim_next("developer-b", "m", 60) is not None


def test_expired_lease_recovered_and_stale_worker_rejected(repo):
    make(repo, 1)
    c = repo.claim_next("developer-a", "m", 60)
    repo.mark_running(c)
    assert repo.recover_expired_leases(now=time.time()) == []          # lease still valid
    assert repo.recover_expired_leases(now=time.time() + 120) == ["t0"]
    assert repo.status_of("t0") == S.READY
    assert repo.get_task("t0")["retry_count"] == 0                      # not a rework attempt
    with pytest.raises(TransitionError):                                # zombie worker returns
        repo.finish_execution(c, success=True, exit_code=0)
    c2 = repo.claim_next("developer-b", "m", 60)
    assert c2.task.id == "t0" and c2.execution_id != c.execution_id


def test_lease_renewal(repo):
    make(repo, 1)
    repo.claim_next("developer-a", "m", 1)
    assert repo.renew_lease("t0", "developer-a", 300)
    assert not repo.renew_lease("t0", "developer-b", 300)
    assert repo.recover_expired_leases(now=time.time() + 10) == []


def test_restart_recovery_on_existing_db(repo):
    make(repo, 2)
    c = repo.claim_next("developer-a", "m", 0)  # simulate a crash: lease expires immediately
    time.sleep(0.01)
    fresh = TaskRepository(Database(repo.db.path))   # "restarted" process
    fresh.db.initialize()                             # idempotent migration
    assert fresh.recover_expired_leases() == [c.task.id]
    assert {t["status"] for t in fresh.list_tasks()} == {"READY"}
