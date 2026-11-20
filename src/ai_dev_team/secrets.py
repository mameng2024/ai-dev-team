"""Local API-key store: ~/.ai-dev-team/secrets.env (chmod 600), KEY=value per line.

Environment variables always win. Keys are never written to agents.yaml, the database,
logs or the settings page (which only shows whether a key is set).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def home_dir() -> Path:
    return Path(os.getenv("AI_DEV_TEAM_HOME", Path.home() / ".ai-dev-team"))


def secrets_path() -> Path:
    return home_dir() / "secrets.env"


def _read() -> dict[str, str]:
    p = secrets_path()
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def get_secret(name: str) -> str | None:
    return os.getenv(name) or _read().get(name) or None


def secret_source(name: str) -> str | None:
    if os.getenv(name):
        return "env"
    if _read().get(name):
        return "file"
    return None


def set_secret(name: str, value: str) -> None:
    if not NAME_RE.match(name):
        raise ValueError(f"invalid variable name: {name}")
    value = value.strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("key must be a single non-empty line")
    data = _read()
    data[name] = value
    _write(data)


def delete_secret(name: str) -> None:
    data = _read()
    if data.pop(name, None) is not None:
        _write(data)


def _write(data: dict[str, str]) -> None:
    p = secrets_path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# ai-dev-team API keys. Do not commit. Managed by `ai-dev-team ui`.\n")
        for k, v in sorted(data.items()):
            f.write(f"{k}={v}\n")
    os.replace(tmp, p)
    os.chmod(p, 0o600)
