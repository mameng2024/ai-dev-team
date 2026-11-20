from pathlib import Path

from typer.testing import CliRunner

from ai_dev_team.cli import app
from ai_dev_team.config import ModelRegistry
from ai_dev_team.settings_server import _vendor

PRESET = Path(__file__).parents[1] / "src/ai_dev_team/presets/openrouter.yaml"


def test_openrouter_preset_is_valid():
    reg = ModelRegistry.load(PRESET)
    assert set(reg.providers) == {"openrouter"}
    for a in reg.agents.values():
        for m in [a.model, *a.fallbacks]:
            assert reg.provider_of_model(m) is reg.providers["openrouter"]


def test_vendor_looks_through_openrouter():
    assert _vendor("openrouter/qwen/qwen4-max") == "qwen"
    assert _vendor("deepseek/deepseek-v4-pro") == "deepseek"


def test_use_preset_writes_and_backs_up(tmp_path, monkeypatch):
    cfg = tmp_path / "agents.yaml"
    cfg.write_text("old: true\n")
    monkeypatch.setenv("AI_DEV_TEAM_CONFIG", str(cfg))
    monkeypatch.setenv("AI_DEV_TEAM_DB", str(tmp_path / "t.db"))
    res = CliRunner().invoke(app, ["use-preset", "openrouter"])
    assert res.exit_code == 0, res.output
    assert "openrouter/qwen/qwen4-max" in cfg.read_text()
    assert (tmp_path / "agents.yaml.bak").read_text() == "old: true\n"
    assert CliRunner().invoke(app, ["use-preset", "nope"]).exit_code == 1


def test_openrouter_free_preset_uses_only_free_models():
    from ai_dev_team.domain import AgentRole
    reg = ModelRegistry.load(PRESET.with_name("openrouter-free.yaml"))
    for a in reg.agents.values():
        for m in [a.model, *a.fallbacks]:
            assert m.endswith(":free"), m
    m = reg.by_role(AgentRole.MANAGER)[0]
    r = reg.by_role(AgentRole.REVIEWER)[0]
    assert _vendor(m.model) != _vendor(r.model)
