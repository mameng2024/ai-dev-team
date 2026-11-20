"""Local model-settings page: `ai-dev-team ui` -> http://127.0.0.1:8765/?token=...

- Binds to 127.0.0.1 only; every API call needs the random token printed at startup
  (blocks other web pages in your browser from changing settings).
- Edits config/agents.yaml (validated with the same ModelRegistry rules as the CLI;
  the previous file is kept in config/backups/).
- API keys go to ~/.ai-dev-team/secrets.env (chmod 600) and are never sent back.
Stdlib only: no web framework dependency.
"""
from __future__ import annotations

import json
import os
import secrets as pysecrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml
from pydantic import ValidationError

from .config import ModelRegistry
from .domain import AgentRole
from .secrets import delete_secret, secret_source, set_secret

HEADER = """# AI Dev Team - per-agent model configuration (edited by `ai-dev-team ui`)
# Model IDs use LiteLLM format "<provider>/<model-id>". Verify IDs in your provider console.
# API keys are NOT stored here: environment variables or ~/.ai-dev-team/secrets.env.
"""


def load_raw(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


ZH = {
    "Manager and Reviewer must not use the same provider and model":
        "Manager 和 Reviewer 不能用同一个模型",
    "Reviewer must not fall back to the Manager's model":
        "Reviewer 的备用模型不能是 Manager 的模型",
    "exactly one MANAGER and one REVIEWER are required": "必须正好有一个 Manager 和一个 Reviewer",
}


def _zh(msg: str) -> str:
    for en, zh in ZH.items():
        if en in msg:
            return zh
    return msg



def _vendor(model: str) -> str:
    """Model maker: 'openrouter/qwen/x' -> 'qwen', 'deepseek/x' -> 'deepseek'."""
    parts = model.split("/")
    return parts[1] if parts[0] == "openrouter" and len(parts) > 2 else parts[0]

def check(data: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a config dict, without saving."""
    try:
        reg = ModelRegistry.model_validate(json.loads(json.dumps(data)))
    except ValidationError as exc:
        return [_zh(f"{'.'.join(str(x) for x in e['loc']) or 'config'}: {e['msg']}")
                if e.get("loc") else _zh(e["msg"]) for e in exc.errors()], []
    except ValueError as exc:
        return [_zh(str(exc))], []
    warnings: list[str] = []
    m = reg.by_role(AgentRole.MANAGER)[0]
    r = reg.by_role(AgentRole.REVIEWER)[0]
    if _vendor(m.model) == _vendor(r.model):
        warnings.append(f"Manager 和 Reviewer 都用 {_vendor(m.model)} 的模型，建议用不同厂商保持审核独立")
    if r.model in m.fallbacks:
        warnings.append("Manager 的备用模型和 Reviewer 主模型相同：Manager 降级时两者会是同一个模型")
    if r.fallbacks:
        warnings.append("Reviewer 配了备用模型：主模型不可用时会换模型审核（不会降级到 Manager 的模型）")
    for a in reg.agents.values():
        for model in [a.model, *a.fallbacks]:
            prefix = model.split("/", 1)[0]
            if prefix not in reg.providers:
                warnings.append(f"{a.id}: 模型 {model} 的前缀 '{prefix}' 不是已配置的厂商，"
                                "将不带 API Key 调用")
    for name, p in reg.providers.items():
        used = any(a.provider == name or any(f.startswith(name + "/") for f in a.fallbacks)
                   for a in reg.agents.values())
        if used and not secret_source(p.api_key_env):
            warnings.append(f"{name}: 还没有设置 {p.api_key_env}")
    return [], warnings


def save(path: Path, data: dict) -> Path | None:
    errors, _ = check(data)
    if errors:
        raise ValueError("; ".join(errors))
    backup = None
    if path.exists():
        bdir = path.parent / "backups"
        bdir.mkdir(exist_ok=True)
        backup = bdir / f"{path.stem}-{datetime.now():%Y%m%d-%H%M%S-%f}.yaml"
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(HEADER + "\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    tmp.replace(path)
    return backup


def test_model(data: dict, model: str, reasoning: str = "low") -> dict:
    """One tiny completion to prove the model ID + key + base URL work."""
    from .llm import LiteLLMClient

    reg = ModelRegistry.model_validate(json.loads(json.dumps(data)))
    client = LiteLLMClient(reg, timeout_s=60)
    start = time.monotonic()
    try:
        text, usage = client._call(model, "Reply with JSON only.",
                                   'Return {"ok": true}.', reasoning)
        return {"ok": True, "latency_ms": int((time.monotonic() - start) * 1000),
                "reply": text[:200], **usage}
    except Exception as exc:  # noqa: BLE001 - shown to the user verbatim
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}


def state(path: Path) -> dict:
    data = load_raw(path)
    errors, warnings = check(data)
    keys = {name: {"env": p.get("api_key_env"), "source": secret_source(p.get("api_key_env", ""))}
            for name, p in (data.get("providers") or {}).items()}
    return {"config": data, "errors": errors, "warnings": warnings, "keys": keys,
            "path": str(path.resolve())}


def overview(config_path: Path, db_path: Path | None) -> dict:
    """Projects with task counts + integration status + readiness problems."""
    from .config import Settings

    settings = Settings(config_path=config_path,
                        database_path=db_path or Path("data/ai-dev-team.sqlite3"))
    problems = [_cn_problem(p, settings.opencode_bin) for p in settings.config_problems()]
    projects: list[dict] = []
    if db_path is not None and Path(db_path).exists():
        from .db import Database
        from .repository import TaskRepository
        from .worktrees import GitError, WorktreeManager

        repo = TaskRepository(Database(Path(db_path)))
        snap = repo.dashboard()
        for p in snap["projects"]:
            tasks = [t for t in snap["tasks"] if t["project_id"] == p["id"]]
            counts: dict[str, int] = {}
            for t in tasks:
                counts[t["status"]] = counts.get(t["status"], 0) + 1
            entry = {**p, "counts": counts, "total": len(tasks), "integration": None,
                     "repo_ok": Path(p["repo_path"], ".git").exists()}
            if entry["repo_ok"]:
                try:
                    wt = WorktreeManager(Path(p["repo_path"]), p["id"], p["default_branch"])
                    entry["integration"] = {"branch": wt.integration_branch,
                                            "commits": wt.integration_log(30),
                                            "merge_cmd": f"git -C '{wt.repo}' merge --no-ff "
                                                         f"{wt.integration_branch}"}
                except (GitError, OSError):
                    pass
            projects.append(entry)
    return {"projects": projects, "problems": problems,
            "db_ready": bool(db_path and Path(db_path).exists()),
            "platform": sys.platform}


def _cn_problem(text: str, opencode_bin: str) -> str:
    import re
    m = re.match(r"(\w+) not set \(provider '([^']+)'\)", text)
    if m:
        return f"还没有填 {m.group(2)} 的 API Key（{m.group(1)}），在「模型设置」页底部填写"
    if text.startswith("OpenCode binary"):
        return (f"找不到 OpenCode（{opencode_bin}）。请先安装 OpenCode；已经装了的话，"
                "重新运行一次打包脚本 build_app.sh，它会自动记下路径")
    if text.startswith("config invalid"):
        return "模型配置有错误：" + text.split(":", 1)[-1].strip()
    return text


def pick_folder() -> str | None:
    """macOS native folder chooser (the page itself cannot read local paths)."""
    if sys.platform != "darwin":
        raise ValueError("只有 macOS 上能弹出文件夹选择框，请直接粘贴路径")
    r = subprocess.run(
        ["osascript", "-e", 'POSIX path of (choose folder with prompt "选择项目的 Git 仓库")'],
        capture_output=True, text=True, timeout=300, check=False)
    return r.stdout.strip().rstrip("/") or None if r.returncode == 0 else None


def make_handler(config_path: Path, token: str, db_path: Path | None = None,
                 jobs=None):
    from .jobs import JobError, JobManager

    files = resources.files("ai_dev_team")
    shell = files.joinpath("app.html").read_text(encoding="utf-8")
    page = files.joinpath("settings.html").read_text(encoding="utf-8")
    board = files.joinpath("dashboard.html").read_text(encoding="utf-8")
    jobs = jobs or JobManager()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, code: int, body, ctype="application/json"):
            raw = body if isinstance(body, bytes) else (
                body.encode() if isinstance(body, str) else
                json.dumps(body, ensure_ascii=False).encode())
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.end_headers()
            self.wfile.write(raw)

        def _authorized(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                return False
            got = self.headers.get("X-Token") or parse_qs(urlparse(self.path).query).get(
                "token", [""])[0]
            return pysecrets.compare_digest(got, token)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if not self._authorized():
                return self._send(403, {"error": "forbidden: open the URL printed by the CLI"})
            route = urlparse(self.path).path
            q = parse_qs(urlparse(self.path).query)
            if route == "/":
                return self._send(200, shell, "text/html")
            if route == "/settings":
                return self._send(200, page, "text/html")
            if route == "/api/overview":
                return self._send(200, overview(config_path, db_path))
            if route == "/api/jobs":
                return self._send(200, {"jobs": jobs.list()})
            if route.startswith("/api/jobs/"):
                job = jobs.jobs.get(route.rsplit("/", 1)[-1])
                if job is None:
                    return self._send(404, {"error": "没有这个任务"})
                return self._send(200, job.view(int((q.get("since") or ["0"])[0] or 0)))
            if route == "/api/state":
                return self._send(200, state(config_path))
            if route == "/dashboard":
                return self._send(200, board, "text/html")
            if route == "/api/dashboard":
                if db_path is None or not Path(db_path).exists():
                    return self._send(200, {"projects": [], "tasks": [], "counts": {},
                                            "events": [], "usage": [],
                                            "note": "database not initialised (ai-dev-team init)"})
                from .db import Database
                from .repository import TaskRepository
                project = (q.get("project") or [None])[0] or None
                return self._send(200, TaskRepository(Database(Path(db_path))).dashboard(project))
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized() or self.headers.get("X-Token") is None:
                return self._send(403, {"error": "forbidden"})
            route = urlparse(self.path).path
            try:
                body = self._body()
                if route == "/api/jobs":
                    job = jobs.start(body.get("kind", ""), body.get("args") or {})
                    return self._send(200, job.view())
                if route.startswith("/api/jobs/") and route.endswith("/stop"):
                    return self._send(200, jobs.stop(route.split("/")[3]).view())
                if route == "/api/pick-folder":
                    return self._send(200, {"path": pick_folder()})
                if route == "/api/shutdown":
                    if jobs.running() and not body.get("force"):
                        return self._send(409, {"error": "还有操作在进行",
                                                "running": len(jobs.running())})
                    jobs.stop_all()
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return self._send(200, {"ok": True})
                if route == "/api/validate":
                    errors, warnings = check(body["config"])
                    return self._send(200, {"errors": errors, "warnings": warnings})
                if route == "/api/save":
                    backup = save(config_path, body["config"])
                    return self._send(200, {**state(config_path),
                                            "backup": str(backup) if backup else None})
                if route == "/api/test":
                    return self._send(200, test_model(body["config"], body["model"],
                                                      body.get("reasoning", "low")))
                if route == "/api/key":
                    if body.get("delete"):
                        delete_secret(body["name"])
                    else:
                        set_secret(body["name"], body["value"])
                    return self._send(200, state(config_path))
                return self._send(404, {"error": "not found"})
            except JobError as exc:
                return self._send(409, {"error": str(exc)})
            except (ValueError, KeyError) as exc:
                return self._send(400, {"error": str(exc)})

    return Handler


def start_server(config_path: Path, port: int = 8765, db_path: Path | None = None):
    """Bind the local page and return (server, url, jobs) without blocking.

    The caller runs ``server.serve_forever()`` (in a thread for the desktop app) and
    calls ``stop_server`` when done.
    """
    from .jobs import JobManager

    token = pysecrets.token_urlsafe(24)
    jobs = JobManager()
    handler = make_handler(config_path, token, db_path, jobs)
    server = None
    for candidate in [port, *range(port + 1, port + 11)] if port else [0]:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), handler)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit(f"ports {port}-{port + 10} are all in use")
    url = f"http://127.0.0.1:{server.server_port}/?token={token}"
    return server, url, jobs


def stop_server(server, jobs) -> None:
    jobs.stop_all()
    server.shutdown()
    server.server_close()


def serve(config_path: Path, port: int = 8765, open_browser: bool = True,
          db_path: Path | None = None, url_file: Path | None = None) -> None:
    server, url, jobs = start_server(config_path, port, db_path)
    print(f"AI Dev Team: {url}\nCtrl+C to stop.", flush=True)
    if url_file:
        url_file.parent.mkdir(parents=True, exist_ok=True)
        url_file.write_text(url, encoding="utf-8")
        os.chmod(url_file, 0o600)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        jobs.stop_all()
        server.server_close()
        if url_file and url_file.exists():
            url_file.unlink()
