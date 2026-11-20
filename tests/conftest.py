import shutil
from pathlib import Path

import pytest

from ai_dev_team.db import Database
from ai_dev_team.repository import TaskRepository

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo(tmp_path: Path) -> TaskRepository:
    db = Database(tmp_path / "test.sqlite3")
    db.initialize()
    r = TaskRepository(db)
    r.create_project("p1", "demo", str(tmp_path))
    for aid, role in [("developer-a", "DEVELOPER"), ("developer-b", "DEVELOPER"),
                      ("reviewer", "REVIEWER")]:
        r.register_agent(aid, role)
    return r


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    dst = tmp_path / "agents.yaml"
    shutil.copy(ROOT / "config" / "agents.yaml", dst)
    return dst


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never read the developer's real ~/.ai-dev-team (saved keys, worktrees) in tests."""
    monkeypatch.setenv("AI_DEV_TEAM_HOME", str(tmp_path / "aidt-home"))
