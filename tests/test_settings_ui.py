import json
import os
import shutil
import stat
import sys
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from ai_dev_team.config import ModelRegistry
from ai_dev_team.domain import TaskSpec
from ai_dev_team.scheduler import DurableScheduler, WorkerSpec
from ai_dev_team.settings_server import make_handler

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t0ken"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_DEV_TEAM_HOME", str(tmp_path / "home"))
    for k in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = tmp_path / "config" / "agents.yaml"
    cfg.parent.mkdir()
    shutil.copy(ROOT / "config" / "agents.yaml", cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, TOKEN))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", cfg
    srv.shutdown()


def call(base, path, body=None, token=TOKEN, host=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Token"] = token
    if host:
        headers["Host"] = host
    req = urllib.request.Request(base + path, headers=headers,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with OPENER.open(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_token_and_host_required(server):
    base, _ = server
    assert call(base, "/api/state", token=None)[0] == 403
    assert call(base, "/api/state", token="wrong")[0] == 403
    assert call(base, "/api/state", host="evil.example")[0] == 403
    code, page = call(base, f"/?token={TOKEN}", token=None)
    assert code == 200 and "模型设置" in page
    # POST must carry the header token, not just the query string
    assert call(base + "", f"/api/validate?token={TOKEN}", {"config": {}}, token=None)[0] == 403


def test_state_reports_missing_keys(server):
    base, _ = server
    st = json.loads(call(base, "/api/state")[1])
    assert st["errors"] == []
    assert st["keys"]["deepseek"] == {"env": "DEEPSEEK_API_KEY", "source": None}
    assert any("DEEPSEEK_API_KEY" in w for w in st["warnings"])


def test_validate_blocks_same_manager_and_reviewer_model(server):
    base, cfg = server
    data = yaml.safe_load(cfg.read_text())
    data["agents"]["manager"].update(provider="deepseek", model="deepseek/deepseek-v4-pro")
    res = json.loads(call(base, "/api/validate", {"config": data})[1])
    assert any("不能用同一个模型" in e for e in res["errors"])
    code, _ = call(base, "/api/save", {"config": data})
    assert code == 400 and yaml.safe_load(cfg.read_text())["agents"]["manager"]["provider"] \
        == "dashscope"


def test_save_writes_valid_config_and_backup(server):
    base, cfg = server
    data = yaml.safe_load(cfg.read_text())
    data["agents"]["developer-b"]["model"] = "deepseek/deepseek-v4-pro"
    data["agents"]["developer-b"]["fallbacks"] = []
    st = json.loads(call(base, "/api/save", {"config": data})[1])
    assert st["backup"] and Path(st["backup"]).exists()
    reg = ModelRegistry.load(cfg)
    assert reg.get("developer-b").model == "deepseek/deepseek-v4-pro"
    assert "ai-dev-team ui" in cfg.read_text().splitlines()[0]


def test_keys_saved_privately_and_never_returned(server, tmp_path):
    base, _ = server
    code, body = call(base, "/api/key", {"name": "DEEPSEEK_API_KEY", "value": "sk-abc123"})
    assert code == 200 and "sk-abc123" not in body
    assert json.loads(body)["keys"]["deepseek"]["source"] == "file"
    f = tmp_path / "home" / "secrets.env"
    assert "DEEPSEEK_API_KEY=sk-abc123" in f.read_text()
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert ModelRegistry.load(ROOT / "config" / "agents.yaml").providers["deepseek"].api_key() \
        == "sk-abc123"
    assert call(base, "/api/key", {"name": "bad name", "value": "x"})[0] == 400
    call(base, "/api/key", {"name": "DEEPSEEK_API_KEY", "delete": True})
    assert "sk-abc123" not in f.read_text()


def test_env_var_wins_over_file(server, monkeypatch):
    base, _ = server
    call(base, "/api/key", {"name": "DEEPSEEK_API_KEY", "value": "from-file"})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    reg = ModelRegistry.load(ROOT / "config" / "agents.yaml")
    assert reg.providers["deepseek"].api_key() == "from-env"


def test_model_test_button(server, monkeypatch):
    base, cfg = server
    seen = {}

    def completion(**kw):
        seen.update(kw)
        msg = types.SimpleNamespace(content='{"ok": true}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)],
                                     usage=types.SimpleNamespace(prompt_tokens=9,
                                                                 completion_tokens=3))
    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(completion=completion))
    data = yaml.safe_load(cfg.read_text())
    res = json.loads(call(base, "/api/test", {"config": data,
                                              "model": "deepseek/deepseek-v4-pro"})[1])
    assert res["ok"] is False and "DEEPSEEK_API_KEY" in res["error"]   # no key yet
    call(base, "/api/key", {"name": "DEEPSEEK_API_KEY", "value": "sk-x"})
    res = json.loads(call(base, "/api/test", {"config": data,
                                              "model": "deepseek/deepseek-v4-pro"})[1])
    assert res["ok"] and res["input_tokens"] == 9
    assert seen["api_key"] == "sk-x" and seen["api_base"] == "https://api.deepseek.com"


def test_developer_routing_by_complexity(repo):
    from ai_dev_team.executors import DryRunExecutor
    s = DurableScheduler(repo, DryRunExecutor(0), [WorkerSpec("developer-a", "flash")],
                         lambda c: Path("."), routing={"simple": "s-model", "complex": "c-model"})
    w = s.workers[0]

    def t(caps):
        return TaskSpec(id="x", project_id="p", title="t", description="d",
                        acceptance_criteria=["c"], required_capabilities=caps)
    assert s.model_for(t(["complex"]), w) == "c-model"
    assert s.model_for(t(["simple"]), w) == "s-model"
    assert s.model_for(t([]), w) == "flash"


def test_dashboard_api(tmp_path, repo):
    from ai_dev_team.domain import TaskSpec
    repo.create_task(TaskSpec(id="p1:T1", project_id="p1", title="first", description="d",
                              acceptance_criteria=["c"]))
    repo.create_task(TaskSpec(id="p1:T2", project_id="p1", title="second", description="d",
                              acceptance_criteria=["c"], dependencies=["p1:T1"]))
    repo.promote_ready()
    repo.record_usage("deepseek/deepseek-v4-pro", 100, 20, "OK", "p1:T1")
    cfg = tmp_path / "agents.yaml"
    shutil.copy(ROOT / "config" / "agents.yaml", cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, TOKEN, repo.db.path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        assert call(base, "/api/dashboard", token=None)[0] == 403
        assert "看板" in call(base, f"/dashboard?token={TOKEN}", token=None)[1]
        d = json.loads(call(base, "/api/dashboard?project=p1")[1])
        assert d["counts"] == {"READY": 1, "PENDING": 1}
        assert {t["id"]: t["depends_on"] for t in d["tasks"]}["p1:T2"] == "p1:T1"
        assert d["usage"][0]["input_tokens"] == 100
        assert any(e["event_type"] == "TASK_CREATED" for e in d["events"])
    finally:
        srv.shutdown()
