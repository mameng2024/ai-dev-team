"""Desktop app entry point: the local page in its own native window, no browser.

Used by the packaged app (PyInstaller) on macOS, Windows and Linux, and by
``ai-dev-team desktop`` from a source install.

- Settings, database and model config live in the per-user app folder
  (macOS ~/Library/Application Support/AI Dev Team, Windows %APPDATA%\\AI Dev Team,
  Linux ~/.local/share/ai-dev-team); API keys stay in ~/.ai-dev-team/secrets.env.
- A first start seeds the model config from the free OpenRouter preset.
- A double-clicked app gets a minimal PATH, so the login shell's PATH and common tool
  folders are added to find opencode and git.
- If no native webview is available, the page opens in the default browser instead.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import threading
from pathlib import Path

APP_NAME = "AI Dev Team"
DEFAULT_PRESET = "openrouter-free"


def app_dir() -> Path:
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    elif system == "Windows":
        base = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    else:
        base = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ai-dev-team"
    return Path(os.getenv("AI_DEV_TEAM_APP_DIR", base))


def _login_shell_path() -> str:
    shell = os.getenv("SHELL") or ("/bin/zsh" if platform.system() == "Darwin" else "/bin/bash")
    try:
        out = subprocess.run([shell, "-lic", 'printf "\\n__AIDT_PATH__%s\\n" "$PATH"'],
                             capture_output=True, text=True, timeout=8).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in reversed(out.splitlines()):
        if line.startswith("__AIDT_PATH__"):
            return line[len("__AIDT_PATH__"):]
    return ""


def fix_path() -> None:
    """Make tools from the user's shell (opencode, git, swift) visible to a GUI app."""
    parts = os.getenv("PATH", "").split(os.pathsep)
    extra: list[str] = []
    if platform.system() != "Windows":
        extra += _login_shell_path().split(os.pathsep)
        home = Path.home()
        extra += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                  str(home / ".opencode" / "bin"), str(home / ".local" / "bin"),
                  str(home / ".bun" / "bin"), str(home / ".npm-global" / "bin")]
    else:
        appdata = os.getenv("APPDATA", "")
        if appdata:
            extra.append(str(Path(appdata) / "npm"))
    seen = set()
    merged = []
    for p in parts + extra:
        if p and p not in seen:
            seen.add(p)
            merged.append(p)
    os.environ["PATH"] = os.pathsep.join(merged)


def _load_app_env(folder: Path) -> None:
    """Honour app.env written by the older macOS build script (PATH, OPENCODE_BIN)."""
    env = folder / "app.env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"')
        if k == "PATH":
            os.environ["PATH"] = os.pathsep.join([os.environ.get("PATH", ""), v])
        else:
            os.environ.setdefault(k, v)


def prepare_environment() -> Path:
    folder = app_dir()
    folder.mkdir(parents=True, exist_ok=True)
    _load_app_env(folder)
    fix_path()
    os.environ.setdefault("AI_DEV_TEAM_CONFIG", str(folder / "agents.yaml"))
    os.environ.setdefault("AI_DEV_TEAM_DB", str(folder / "ai-dev-team.sqlite3"))
    cfg = Path(os.environ["AI_DEV_TEAM_CONFIG"])
    if not cfg.exists():
        from importlib.resources import files
        src = files("ai_dev_team").joinpath("presets", f"{DEFAULT_PRESET}.yaml")
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return folder


def _init_db() -> None:
    from .config import Settings
    from .db import Database
    from .repository import TaskRepository

    settings = Settings.load()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        db = Database(settings.database_path)
        db.initialize()
        repo = TaskRepository(db)
        for agent in settings.registry().agents.values():
            repo.register_agent(agent.id, agent.role.value, agent.model)
    except Exception as exc:  # noqa: BLE001 - the page shows config problems itself
        print(f"init skipped: {exc}", file=sys.stderr)


def _open_window(url: str) -> bool:
    """Show the page in a native window; False when no webview backend works."""
    try:
        import webview  # pywebview
    except ImportError:
        return False
    try:
        webview.create_window(APP_NAME, url, width=1280, height=860, min_size=(900, 600))
        webview.start()
        return True
    except Exception as exc:  # noqa: BLE001 - no GUI backend on this machine
        print(f"native window unavailable ({exc}); using the browser", file=sys.stderr)
        return False


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ("--smoke", "--browser"):
        # Packaged app used as a CLI: `AI Dev Team.exe status`, etc.
        prepare_environment()
        from .cli import app
        app(args=argv, prog_name="ai-dev-team")
        return 0

    prepare_environment()
    _init_db()
    from .config import Settings
    from .settings_server import start_server, stop_server

    settings = Settings.load()
    server, url, jobs = start_server(settings.config_path, port=8765,
                                     db_path=settings.database_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    if argv and argv[0] == "--smoke":
        # Packaging check: the server answers and the heavy imports resolve.
        import urllib.request

        import litellm  # noqa: F401
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # skip proxies
        with direct.open(url, timeout=10) as r:
            ok = r.status == 200
        stop_server(server, jobs)
        # A windowed Windows build has no stdout, so the result also goes to a file.
        result = "smoke ok" if ok else "smoke FAILED"
        (app_dir() / "smoke.txt").write_text(result, encoding="utf-8")
        print(result)
        return 0 if ok else 1

    shown = False if (argv and argv[0] == "--browser") else _open_window(url)
    if not shown:
        import webbrowser
        print(f"{APP_NAME}: {url}\nCtrl+C to stop.", flush=True)
        webbrowser.open(url)
        try:
            thread.join()
        except KeyboardInterrupt:
            pass
    stop_server(server, jobs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
