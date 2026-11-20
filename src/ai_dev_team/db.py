import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

# Columns added after v0.1.0, applied to existing databases by `initialize()`.
MIGRATIONS_V2 = [
    ("tasks", "lease_owner", "TEXT"),
    ("tasks", "lease_expires_at", "REAL"),
    ("tasks", "rework_notes_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("tasks", "last_error", "TEXT"),
    ("executions", "status", "TEXT NOT NULL DEFAULT 'ACTIVE'"),
]

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    model_id TEXT,
    status TEXT NOT NULL DEFAULT 'IDLE',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
    required_capabilities_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'PENDING',
    priority INTEGER NOT NULL DEFAULT 5,
    assigned_worker TEXT REFERENCES agents(id),
    assigned_model TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
    max_retries INTEGER NOT NULL DEFAULT 3 CHECK(max_retries BETWEEN 0 AND 3),
    worktree_path TEXT,
    branch_name TEXT,
    commit_sha TEXT,
    lease_owner TEXT,
    lease_expires_at REAL,
    rework_notes_json TEXT NOT NULL DEFAULT '[]',
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY(task_id, depends_on_task_id),
    CHECK(task_id <> depends_on_task_id)
);

CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    worker_id TEXT NOT NULL REFERENCES agents(id),
    model_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    timed_out INTEGER NOT NULL DEFAULT 0,
    stdout_path TEXT,
    stderr_path TEXT,
    commit_sha TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK(status IN ('ACTIVE','SUCCEEDED','FAILED','ABANDONED'))
);

CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    execution_id TEXT REFERENCES executions(id),
    verdict TEXT NOT NULL CHECK(verdict IN ('PASS','REWORK','BLOCKED')),
    score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
    reviewer_model TEXT NOT NULL,
    findings_json TEXT NOT NULL DEFAULT '[]',
    required_fixes_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    artifact_type TEXT NOT NULL,
    path TEXT,
    commit_sha TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS model_usage (
    id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(id),
    model_id TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost REAL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT,
    task_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_exec_task ON executions(task_id);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
-- At most one ACTIVE execution per task and per worker (rule 2: single claim).
CREATE UNIQUE INDEX IF NOT EXISTS uq_exec_active_task
    ON executions(task_id) WHERE status = 'ACTIVE';
CREATE UNIQUE INDEX IF NOT EXISTS uq_exec_active_worker
    ON executions(worker_id) WHERE status = 'ACTIVE';
-- Rule 7: one merge artifact per task.
CREATE UNIQUE INDEX IF NOT EXISTS uq_merge_artifact
    ON artifacts(task_id) WHERE artifact_type = 'MERGE';

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            self._migrate_columns(conn)
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version(version) VALUES(?)", (SCHEMA_VERSION,))

    @staticmethod
    def _migrate_columns(conn: sqlite3.Connection) -> None:
        for table, column, ddl in MIGRATIONS_V2:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @contextmanager
    def transaction(self):
        """Short write transaction taken with BEGIN IMMEDIATE (reserves the write lock
        up front, so check-then-update sequences cannot interleave between processes).
        Never await a model/API call while inside this block."""
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("projects", "tasks", "agents", "executions", "reviews")
            }
