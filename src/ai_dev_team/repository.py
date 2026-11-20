"""SQLite repository: the single source of truth for task state.

All state changes go through short BEGIN IMMEDIATE transactions and conditional
UPDATEs (``WHERE status = <expected>``), so two workers or two processes can never
both win the same transition. LLMs never write here directly; the scheduler does.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterable

from .db import Database
from .domain import Claim, ReviewResult, TaskSpec, TaskStatus

S = TaskStatus

# Rule 1: legal transitions only.
ALLOWED: dict[TaskStatus, set[TaskStatus]] = {
    S.PENDING: {S.READY, S.BLOCKED},
    S.READY: {S.ASSIGNED, S.BLOCKED},
    S.ASSIGNED: {S.RUNNING, S.READY, S.FAILED},
    S.RUNNING: {S.TESTING, S.READY, S.REWORK, S.FAILED},
    S.TESTING: {S.REVIEWING, S.REWORK, S.FAILED},
    S.REVIEWING: {S.MERGE_QUEUED, S.REWORK, S.BLOCKED},
    S.REWORK: {S.READY, S.BLOCKED},
    S.MERGE_QUEUED: {S.MERGED, S.REWORK, S.BLOCKED},
    S.MERGED: {S.COMPLETED},
    S.BLOCKED: {S.READY, S.PENDING},
    S.FAILED: {S.READY, S.BLOCKED},
    S.COMPLETED: set(),
}


class TransitionError(ValueError):
    pass


class DependencyError(ValueError):
    pass


def find_cycle(edges: dict[str, set[str]]) -> list[str] | None:
    """Return one cycle (as a list of ids) in task -> depends_on edges, or None."""
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        color[n] = GREY
        stack.append(n)
        for m in edges.get(n, ()):
            c = color.get(m, WHITE)
            if c == GREY:
                return stack[stack.index(m):] + [m]
            if c == WHITE:
                found = visit(m)
                if found:
                    return found
        stack.pop()
        color[n] = BLACK
        return None

    for node in list(edges):
        if color.get(node, WHITE) == WHITE:
            found = visit(node)
            if found:
                return found
    return None


class TaskRepository:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ setup
    def create_project(self, project_id: str, name: str, repo_path: str, branch: str = "main"):
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO projects(id,name,repo_path,default_branch) VALUES(?,?,?,?)",
                (project_id, name, repo_path, branch),
            )
            self._event(conn, "PROJECT_CREATED", project_id=project_id, name=name)

    def get_project(self, project_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            return dict(row) if row else None

    def register_agent(self, agent_id: str, role: str, model_id: str | None = None):
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO agents(id,role,model_id,status) VALUES(?,?,?,'IDLE')
                   ON CONFLICT(id) DO UPDATE SET role=excluded.role, model_id=excluded.model_id""",
                (agent_id, role, model_id),
            )

    # ------------------------------------------------------------------ tasks
    def create_task(self, task: TaskSpec) -> None:
        self.create_tasks([task])

    def create_tasks(self, tasks: Iterable[TaskSpec]) -> None:
        """Insert a batch atomically. Rejects unknown dependencies and cycles (rule 3)."""
        tasks = list(tasks)
        with self.db.transaction() as conn:
            new_ids = {t.id for t in tasks}
            if len(new_ids) != len(tasks):
                raise DependencyError("duplicate task ids in batch")
            existing = {r[0] for r in conn.execute("SELECT id FROM tasks")}
            clash = new_ids & existing
            if clash:
                raise DependencyError(f"task ids already exist: {sorted(clash)}")
            known = existing | new_ids
            edges: dict[str, set[str]] = {}
            for r in conn.execute("SELECT task_id, depends_on_task_id FROM task_dependencies"):
                edges.setdefault(r[0], set()).add(r[1])
            for t in tasks:
                missing = [d for d in t.dependencies if d not in known]
                if missing:
                    raise DependencyError(f"{t.id} depends on unknown tasks {missing}")
                if t.id in t.dependencies:
                    raise DependencyError(f"{t.id} depends on itself")
                edges.setdefault(t.id, set()).update(t.dependencies)
            cycle = find_cycle(edges)
            if cycle:
                raise DependencyError("dependency cycle: " + " -> ".join(cycle))
            for t in tasks:
                conn.execute(
                    """INSERT INTO tasks(
                        id,project_id,title,description,acceptance_criteria_json,
                        required_capabilities_json,priority,max_retries,status
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        t.id, t.project_id, t.title, t.description,
                        json.dumps(t.acceptance_criteria, ensure_ascii=False),
                        json.dumps(t.required_capabilities, ensure_ascii=False),
                        t.priority, min(t.max_retries, 3), S.PENDING.value,
                    ),
                )
                for dep in t.dependencies:
                    conn.execute(
                        "INSERT INTO task_dependencies(task_id,depends_on_task_id) VALUES(?,?)",
                        (t.id, dep),
                    )
                self._event(conn, "TASK_CREATED", task_id=t.id, project_id=t.project_id)

    def list_tasks(self, project_id: str | None = None) -> list[dict]:
        query = ("SELECT id,project_id,title,status,priority,retry_count,max_retries,"
                 "assigned_worker,lease_owner,commit_sha FROM tasks")
        params: tuple = ()
        if project_id:
            query += " WHERE project_id=?"
            params = (project_id,)
        query += " ORDER BY priority DESC, created_at, id"
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def get_task(self, task_id: str) -> dict:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task: {task_id}")
            return dict(row)

    def status_of(self, task_id: str) -> TaskStatus:
        return TaskStatus(self.get_task(task_id)["status"])

    # ------------------------------------------------------------ transitions
    def transition(self, task_id: str, new_status: TaskStatus, *, reason: str | None = None,
                   expected: TaskStatus | None = None) -> TaskStatus:
        with self.db.transaction() as conn:
            return self._transition(conn, task_id, new_status, reason=reason, expected=expected)

    def _transition(self, conn: sqlite3.Connection, task_id: str, new_status: TaskStatus, *,
                    reason: str | None = None, expected: TaskStatus | None = None,
                    extra_sql: str = "", extra_params: tuple = ()) -> TaskStatus:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        current = TaskStatus(row["status"])
        if expected is not None and current != expected:
            raise TransitionError(f"{task_id}: expected {expected}, found {current}")
        if new_status not in ALLOWED[current]:
            raise TransitionError(f"Illegal task transition: {current} -> {new_status}")
        if new_status == S.READY:
            self._assert_deps_completed(conn, task_id)               # rule 4
        if new_status == S.MERGE_QUEUED:
            self._assert_latest_review_pass(conn, task_id)           # rule 5
        cur = conn.execute(
            f"UPDATE tasks SET status=?, updated_at=CURRENT_TIMESTAMP{extra_sql} "
            "WHERE id=? AND status=?",
            (new_status.value, *extra_params, task_id, current.value),
        )
        if cur.rowcount != 1:
            raise TransitionError(f"{task_id}: concurrent modification")
        self._event(conn, "TASK_TRANSITION", task_id=task_id,
                    **{"from": current.value, "to": new_status.value, "reason": reason})
        return current

    def _assert_deps_completed(self, conn, task_id: str) -> None:
        open_deps = conn.execute(
            """SELECT d.depends_on_task_id FROM task_dependencies d
               JOIN tasks t ON t.id = d.depends_on_task_id
               WHERE d.task_id=? AND t.status <> 'COMPLETED'""",
            (task_id,),
        ).fetchall()
        if open_deps:
            raise DependencyError(
                f"{task_id} has unfinished dependencies {[r[0] for r in open_deps]}"
            )

    def _assert_latest_review_pass(self, conn, task_id: str) -> None:
        row = conn.execute(
            "SELECT verdict FROM reviews WHERE task_id=? ORDER BY created_at DESC, rowid DESC "
            "LIMIT 1", (task_id,)
        ).fetchone()
        if row is None or row["verdict"] != "PASS":
            raise TransitionError(f"{task_id}: only a PASS review may enter the merge queue")

    def promote_ready(self, project_id: str | None = None) -> list[str]:
        """Move PENDING tasks whose dependencies are all COMPLETED to READY."""
        promoted: list[str] = []
        with self.db.transaction() as conn:
            q = """SELECT t.id FROM tasks t WHERE t.status='PENDING'
                   AND NOT EXISTS (
                     SELECT 1 FROM task_dependencies d JOIN tasks dt ON dt.id=d.depends_on_task_id
                     WHERE d.task_id=t.id AND dt.status <> 'COMPLETED')"""
            params: tuple = ()
            if project_id:
                q += " AND t.project_id=?"
                params = (project_id,)
            for r in conn.execute(q, params).fetchall():
                self._transition(conn, r["id"], S.READY, reason="dependencies satisfied")
                promoted.append(r["id"])
        return promoted

    # --------------------------------------------------------- claim & leases
    def claim_next(self, worker_id: str, model_id: str | None, lease_seconds: int,
                   project_id: str | None = None) -> Claim | None:
        """Atomically lease the highest-priority READY task to ``worker_id`` (rule 2)."""
        now = time.time()
        with self.db.transaction() as conn:
            busy = conn.execute(
                "SELECT 1 FROM executions WHERE worker_id=? AND status='ACTIVE'", (worker_id,)
            ).fetchone()
            if busy:
                return None
            q = "SELECT * FROM tasks WHERE status='READY'"
            params: tuple = ()
            if project_id:
                q += " AND project_id=?"
                params = (project_id,)
            q += " ORDER BY priority DESC, created_at, id LIMIT 1"
            row = conn.execute(q, params).fetchone()
            if row is None:
                return None
            expires = now + lease_seconds
            self._transition(
                conn, row["id"], S.ASSIGNED, reason=f"claimed by {worker_id}",
                extra_sql=", assigned_worker=?, assigned_model=?, lease_owner=?, "
                          "lease_expires_at=?",
                extra_params=(worker_id, model_id, worker_id, expires),
            )
            execution_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO executions(id,task_id,worker_id,model_id,started_at,status)
                   VALUES(?,?,?,?,datetime('now'),'ACTIVE')""",
                (execution_id, row["id"], worker_id, model_id),
            )
            conn.execute("UPDATE agents SET status='BUSY' WHERE id=?", (worker_id,))
            deps = [r[0] for r in conn.execute(
                "SELECT depends_on_task_id FROM task_dependencies WHERE task_id=?", (row["id"],))]
            task = TaskSpec(
                id=row["id"], project_id=row["project_id"], title=row["title"],
                description=row["description"],
                acceptance_criteria=json.loads(row["acceptance_criteria_json"]),
                required_capabilities=json.loads(row["required_capabilities_json"]),
                dependencies=deps, priority=row["priority"], max_retries=row["max_retries"],
            )
            return Claim(
                execution_id=execution_id, task=task, worker_id=worker_id, model_id=model_id,
                lease_expires_at=expires, attempt=row["retry_count"] + 1,
                rework_notes=json.loads(row["rework_notes_json"] or "[]"),
            )

    def renew_lease(self, task_id: str, worker_id: str, lease_seconds: int) -> bool:
        with self.db.transaction() as conn:
            cur = conn.execute(
                """UPDATE tasks SET lease_expires_at=? WHERE id=? AND lease_owner=?
                   AND status IN ('ASSIGNED','RUNNING')""",
                (time.time() + lease_seconds, task_id, worker_id),
            )
            return cur.rowcount == 1

    def mark_running(self, claim: Claim) -> None:
        with self.db.transaction() as conn:
            self._check_owner(conn, claim)
            self._transition(conn, claim.task.id, S.RUNNING, expected=S.ASSIGNED,
                             reason=f"execution {claim.execution_id}")

    def finish_execution(self, claim: Claim, *, success: bool, exit_code: int,
                         timed_out: bool = False, commit_sha: str | None = None,
                         error: str | None = None, stdout_path: str | None = None,
                         stderr_path: str | None = None) -> TaskStatus:
        """Close the execution and release the lease.

        success -> TESTING (P3 test gate picks it up); failure -> FAILED, or back to READY
        while rework attempts remain.
        """
        with self.db.transaction() as conn:
            self._check_owner(conn, claim)
            conn.execute(
                """UPDATE executions SET finished_at=datetime('now'), exit_code=?, timed_out=?,
                   commit_sha=?, stdout_path=?, stderr_path=?, status=? WHERE id=?""",
                (exit_code, int(timed_out), commit_sha, stdout_path, stderr_path,
                 "SUCCEEDED" if success else "FAILED", claim.execution_id),
            )
            conn.execute("UPDATE agents SET status='IDLE' WHERE id=?", (claim.worker_id,))
            release = ", lease_owner=NULL, lease_expires_at=NULL, commit_sha=?, last_error=?"
            if success:
                self._transition(conn, claim.task.id, S.TESTING, expected=S.RUNNING,
                                 reason="execution succeeded", extra_sql=release,
                                 extra_params=(commit_sha, None))
                return S.TESTING
            self._transition(conn, claim.task.id, S.FAILED, expected=S.RUNNING,
                             reason=error or f"exit {exit_code}", extra_sql=release,
                             extra_params=(commit_sha, error))
            row = conn.execute("SELECT retry_count, max_retries FROM tasks WHERE id=?",
                               (claim.task.id,)).fetchone()
            if row["retry_count"] < row["max_retries"]:
                conn.execute("UPDATE tasks SET retry_count=retry_count+1 WHERE id=?",
                             (claim.task.id,))
                self._transition(conn, claim.task.id, S.READY, reason="retry after failure")
                return S.READY
            self._transition(conn, claim.task.id, S.BLOCKED, reason="retries exhausted")
            self._event(conn, "MANAGER_REPLAN", task_id=claim.task.id, cause="execution failed")
            return S.BLOCKED

    def _check_owner(self, conn, claim: Claim) -> None:
        row = conn.execute(
            "SELECT lease_owner FROM tasks WHERE id=?", (claim.task.id,)).fetchone()
        ex = conn.execute("SELECT status FROM executions WHERE id=?",
                          (claim.execution_id,)).fetchone()
        if row is None or row["lease_owner"] != claim.worker_id or ex is None \
                or ex["status"] != "ACTIVE":
            raise TransitionError(f"{claim.task.id}: lease lost by {claim.worker_id}")

    def recover_expired_leases(self, now: float | None = None) -> list[str]:
        """Return tasks whose worker vanished (crash / restart) to READY.

        A lost lease is not counted as a rework attempt; the abandoned execution is kept.
        """
        now = now or time.time()
        recovered: list[str] = []
        with self.db.transaction() as conn:
            rows = conn.execute(
                """SELECT id, status, lease_owner FROM tasks
                   WHERE status IN ('ASSIGNED','RUNNING')
                   AND (lease_expires_at IS NULL OR lease_expires_at < ?)""", (now,)
            ).fetchall()
            for r in rows:
                conn.execute(
                    """UPDATE executions SET status='ABANDONED', finished_at=datetime('now')
                       WHERE task_id=? AND status='ACTIVE'""", (r["id"],))
                if r["lease_owner"]:
                    conn.execute("UPDATE agents SET status='IDLE' WHERE id=?", (r["lease_owner"],))
                self._transition(
                    conn, r["id"], S.READY, reason=f"lease expired ({r['lease_owner']})",
                    extra_sql=", lease_owner=NULL, lease_expires_at=NULL, assigned_worker=NULL",
                )
                recovered.append(r["id"])
        return recovered

    # ------------------------------------------------------------ review/merge
    def start_review(self, task_id: str) -> None:
        self.transition(task_id, S.REVIEWING, expected=S.TESTING, reason="tests passed")

    def apply_review(self, result: ReviewResult, execution_id: str | None = None) -> TaskStatus:
        """Record a review and move the task. REWORK keeps history and carries the
        reviewer's required fixes into the next attempt; >max_retries -> BLOCKED (rule 8)."""
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO reviews(id,task_id,execution_id,verdict,score,reviewer_model,
                   findings_json,required_fixes_json) VALUES(?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), result.task_id, execution_id, result.verdict, result.score,
                 result.reviewer_model,
                 json.dumps([f.model_dump() for f in result.findings], ensure_ascii=False),
                 json.dumps(result.required_fixes, ensure_ascii=False)),
            )
            tid = result.task_id
            if result.verdict == "PASS":
                self._transition(conn, tid, S.MERGE_QUEUED, expected=S.REVIEWING,
                                 reason=f"review PASS ({result.score})")
                return S.MERGE_QUEUED
            if result.verdict == "BLOCKED":
                self._transition(conn, tid, S.BLOCKED, expected=S.REVIEWING,
                                 reason="reviewer BLOCKED")
                self._event(conn, "MANAGER_REPLAN", task_id=tid, cause="reviewer blocked")
                return S.BLOCKED
            self._transition(conn, tid, S.REWORK, expected=S.REVIEWING, reason="review REWORK")
            return self._requeue_rework(conn, tid, result.required_fixes)

    def fail_tests(self, task_id: str, failures: list[str]) -> TaskStatus:
        """Test gate failed: TESTING -> REWORK -> READY (with failures as fixes) or BLOCKED."""
        with self.db.transaction() as conn:
            self._transition(conn, task_id, S.REWORK, expected=S.TESTING, reason="tests failed")
            return self._requeue_rework(conn, task_id, failures)

    def merge_failed(self, task_id: str, fixes: list[str]) -> TaskStatus:
        """Merge conflict or post-merge regression: MERGE_QUEUED -> REWORK -> READY/BLOCKED."""
        with self.db.transaction() as conn:
            self._transition(conn, task_id, S.REWORK, expected=S.MERGE_QUEUED,
                             reason="merge conflict / regression")
            return self._requeue_rework(conn, task_id, fixes)

    def _requeue_rework(self, conn, tid: str, fixes: list[str]) -> TaskStatus:
        row = conn.execute("SELECT retry_count, max_retries, rework_notes_json FROM tasks "
                           "WHERE id=?", (tid,)).fetchone()
        if row["retry_count"] >= row["max_retries"]:
            self._transition(conn, tid, S.BLOCKED, reason="rework limit reached")
            self._event(conn, "MANAGER_REPLAN", task_id=tid, cause="rework limit reached")
            return S.BLOCKED
        attempt = row["retry_count"] + 1
        notes = json.loads(row["rework_notes_json"] or "[]") + [
            f"[attempt {attempt}] {f}" for f in fixes]
        self._transition(
            conn, tid, S.READY, reason=f"rework {attempt}",
            extra_sql=", retry_count=retry_count+1, rework_notes_json=?",
            extra_params=(json.dumps(notes, ensure_ascii=False),),
        )
        return S.READY

    def latest_execution(self, task_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM executions WHERE task_id=? ORDER BY started_at DESC, rowid DESC "
                "LIMIT 1", (task_id,)).fetchone()
            return dict(row) if row else None

    def task_spec(self, task_id: str) -> TaskSpec:
        t = self.get_task(task_id)
        with self.db.connect() as conn:
            deps = [r[0] for r in conn.execute(
                "SELECT depends_on_task_id FROM task_dependencies WHERE task_id=?", (task_id,))]
        return TaskSpec(
            id=t["id"], project_id=t["project_id"], title=t["title"],
            description=t["description"],
            acceptance_criteria=json.loads(t["acceptance_criteria_json"]),
            required_capabilities=json.loads(t["required_capabilities_json"]),
            dependencies=deps, priority=t["priority"], max_retries=t["max_retries"])

    def rework_notes(self, task_id: str) -> list[str]:
        return json.loads(self.get_task(task_id)["rework_notes_json"] or "[]")

    def add_artifact(self, task_id: str, artifact_type: str, metadata: dict,
                     path: str | None = None, commit_sha: str | None = None) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO artifacts(id,task_id,artifact_type,path,commit_sha,metadata_json)
                   VALUES(?,?,?,?,?,?)""",
                (str(uuid.uuid4()), task_id, artifact_type, path, commit_sha,
                 json.dumps(metadata, ensure_ascii=False)))

    def set_worktree(self, task_id: str, path: str, branch: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE tasks SET worktree_path=?, branch_name=? WHERE id=?",
                         (path, branch, task_id))

    def dashboard(self, project_id: str | None = None, event_limit: int = 60) -> dict:
        """Read-only snapshot for the local dashboard."""
        where, params = ("WHERE t.project_id=?", (project_id,)) if project_id else ("", ())
        with self.db.connect() as conn:
            tasks = [dict(r) for r in conn.execute(f"""
                SELECT t.id, t.project_id, t.title, t.status, t.priority, t.assigned_worker,
                       t.assigned_model, t.retry_count, t.max_retries, t.branch_name,
                       t.last_error, t.updated_at, t.lease_expires_at,
                       (SELECT verdict || ' ' || score FROM reviews r WHERE r.task_id=t.id
                        ORDER BY r.created_at DESC, r.rowid DESC LIMIT 1) AS last_review,
                       (SELECT group_concat(depends_on_task_id, ', ') FROM task_dependencies d
                        WHERE d.task_id=t.id) AS depends_on
                FROM tasks t {where} ORDER BY t.priority DESC, t.created_at""", params)]
            ev_where = "WHERE e.project_id=? OR e.task_id IN (SELECT id FROM tasks " \
                       "WHERE project_id=?)" if project_id else ""
            ev_params = (project_id, project_id) if project_id else ()
            events = [dict(r) for r in conn.execute(
                f"SELECT * FROM events e {ev_where} ORDER BY e.id DESC LIMIT ?",
                (*ev_params, event_limit))]
            usage = [dict(r) for r in conn.execute("""
                SELECT model_id, status, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens
                FROM model_usage GROUP BY model_id, status ORDER BY calls DESC""")]
            projects = [dict(r) for r in conn.execute(
                "SELECT id, name, repo_path, default_branch FROM projects ORDER BY created_at")]
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        return {"projects": projects, "tasks": tasks, "counts": counts, "events": events,
                "usage": usage}

    def reviews(self, task_id: str) -> list[dict]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM reviews WHERE task_id=? ORDER BY created_at, rowid", (task_id,))]

    def record_merge(self, task_id: str, commit_sha: str) -> bool:
        """Idempotent: a duplicate completion event never merges twice (rule 7).
        Returns True if this call performed the merge record, False if already merged."""
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT commit_sha FROM artifacts WHERE task_id=? AND artifact_type='MERGE'",
                (task_id,)).fetchone()
            if existing:
                if existing["commit_sha"] != commit_sha:
                    raise TransitionError(
                        f"{task_id} already merged at {existing['commit_sha']}, got {commit_sha}")
                return False
            self._transition(conn, task_id, S.MERGED, expected=S.MERGE_QUEUED,
                             reason=f"merged {commit_sha}", extra_sql=", commit_sha=?",
                             extra_params=(commit_sha,))
            conn.execute(
                "INSERT INTO artifacts(id,task_id,artifact_type,commit_sha) VALUES(?,?,'MERGE',?)",
                (str(uuid.uuid4()), task_id, commit_sha))
            # Rule 6: COMPLETED only together with a recorded merge.
            self._transition(conn, task_id, S.COMPLETED, reason="merge recorded")
            return True

    # ------------------------------------------------------------- usage/events
    def record_usage(self, model_id: str, input_tokens: int, output_tokens: int,
                     status: str, task_id: str | None = None, cost: float | None = None) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO model_usage(id,task_id,model_id,input_tokens,output_tokens,
                   estimated_cost,status) VALUES(?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), task_id, model_id, input_tokens, output_tokens, cost, status))

    def events(self, task_id: str | None = None) -> list[dict]:
        with self.db.connect() as conn:
            if task_id:
                rows = conn.execute("SELECT * FROM events WHERE task_id=? ORDER BY id",
                                    (task_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def _event(conn, event_type: str, task_id: str | None = None,
               project_id: str | None = None, **payload) -> None:
        conn.execute(
            "INSERT INTO events(project_id,task_id,event_type,payload_json) VALUES(?,?,?,?)",
            (project_id, task_id, event_type, json.dumps(payload, ensure_ascii=False)))
