"""Settings and per-agent model registry.

Every agent has its own provider, model ID, API key env var, reasoning mode and
fallback list. Nothing is assumed to exist: `ai-dev-team config-check` reports
missing keys / binaries before anything calls a model.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .domain import AgentRole

DEFAULT_CONFIG = Path("config/agents.yaml")


class ProviderConfig(BaseModel):
    api_key_env: str
    api_base: str | None = None

    def api_key(self) -> str | None:
        from .secrets import get_secret

        return get_secret(self.api_key_env)


class AgentConfig(BaseModel):
    id: str
    role: AgentRole
    provider: str
    model: str
    reasoning: Literal["low", "medium", "high"] = "medium"
    fallbacks: list[str] = Field(default_factory=list)
    executor: Literal["opencode", "llm"] = "llm"
    independent: bool = False

    @field_validator("model")
    @classmethod
    def model_has_provider_prefix(cls, v: str) -> str:
        if "/" not in v:
            raise ValueError(f"model '{v}' must use '<provider>/<model-id>' (LiteLLM format)")
        return v


class Defaults(BaseModel):
    timeout_s: int = 180
    max_retries: int = Field(default=3, ge=0, le=3)
    max_concurrency: int = Field(default=2, ge=1)
    lease_seconds: int = Field(default=900, ge=30)


class OpenCodeConfig(BaseModel):
    auto_approve: bool = True        # `--auto`: no human at the terminal; worktree only
    # LiteLLM model id -> OpenCode model id, when the two spell a model differently.
    # Check `opencode models` for OpenCode's ids. Unmapped ids are passed through as-is.
    model_map: dict[str, str] = Field(default_factory=dict)


class ModelRegistry(BaseModel):
    defaults: Defaults = Field(default_factory=Defaults)
    opencode: OpenCodeConfig = Field(default_factory=OpenCodeConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    developer_routing: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def inject_agent_ids(cls, data):
        if isinstance(data, dict):
            for agent_id, cfg in (data.get("agents") or {}).items():
                if isinstance(cfg, dict):
                    cfg.setdefault("id", agent_id)
        return data

    @model_validator(mode="after")
    def check_rules(self) -> ModelRegistry:
        for a in self.agents.values():
            if a.provider not in self.providers:
                raise ValueError(f"agent '{a.id}' uses unknown provider '{a.provider}'")
        managers = self.by_role(AgentRole.MANAGER)
        reviewers = self.by_role(AgentRole.REVIEWER)
        if len(managers) != 1 or len(reviewers) != 1:
            raise ValueError("exactly one MANAGER and one REVIEWER are required")
        # Rule: Manager and Reviewer must not share the same provider AND model.
        m, r = managers[0], reviewers[0]
        if m.provider == r.provider and m.model == r.model:
            raise ValueError("Manager and Reviewer must not use the same provider and model")
        if r.fallbacks and any(fb == m.model for fb in r.fallbacks):
            raise ValueError("Reviewer must not fall back to the Manager's model")
        return self

    def by_role(self, role: AgentRole) -> list[AgentConfig]:
        return [a for a in self.agents.values() if a.role == role]

    def get(self, agent_id: str) -> AgentConfig:
        try:
            return self.agents[agent_id]
        except KeyError as exc:
            raise KeyError(f"agent '{agent_id}' not configured") from exc

    def provider_for(self, agent_id: str) -> ProviderConfig:
        return self.providers[self.get(agent_id).provider]

    def key_env(self) -> dict[str, str]:
        """Provider API keys (env or secrets file) to hand to child processes like OpenCode."""
        out = {}
        for p in self.providers.values():
            key = p.api_key()
            if key:
                out[p.api_key_env] = key
        return out

    def provider_of_model(self, model: str) -> ProviderConfig | None:
        return self.providers.get(model.split("/", 1)[0])

    @classmethod
    def load(cls, path: Path | None = None) -> ModelRegistry:
        path = Path(path or os.getenv("AI_DEV_TEAM_CONFIG", DEFAULT_CONFIG))
        if not path.exists():
            raise FileNotFoundError(f"model config not found: {path}")
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


class Settings(BaseModel):
    app_name: str = "AI Dev Team"
    database_path: Path = Field(
        default_factory=lambda: Path(os.getenv("AI_DEV_TEAM_DB", "data/ai-dev-team.sqlite3"))
    )
    config_path: Path = Field(
        default_factory=lambda: Path(os.getenv("AI_DEV_TEAM_CONFIG", str(DEFAULT_CONFIG)))
    )
    opencode_bin: str = Field(default_factory=lambda: os.getenv("OPENCODE_BIN", "opencode"))
    command_timeout_seconds: int = 1800
    default_branch: str = "main"

    @classmethod
    def load(cls) -> Settings:
        return cls()

    def registry(self) -> ModelRegistry:
        return ModelRegistry.load(self.config_path)

    def config_problems(self) -> list[str]:
        """Human-readable list of what is missing before a real run."""
        problems: list[str] = []
        try:
            reg = self.registry()
        except Exception as exc:  # noqa: BLE001
            return [f"config invalid: {exc}"]
        needed: set[str] = set()
        for a in reg.agents.values():
            needed.add(a.provider)
            for fb in a.fallbacks:
                needed.add(fb.split("/", 1)[0])
        for name in sorted(needed):
            p = reg.providers.get(name)
            if p is None:
                problems.append(f"provider '{name}' referenced but not defined")
            elif not p.api_key():
                problems.append(f"{p.api_key_env} not set (provider '{name}'): export it or "
                                "add it in `ai-dev-team ui`")
        if shutil.which(self.opencode_bin) is None:
            problems.append(f"OpenCode binary '{self.opencode_bin}' not found on PATH")
        return problems
