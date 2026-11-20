import yaml

from ai_dev_team import desktop


def test_first_start_seeds_free_preset(tmp_path, monkeypatch):
    for var in ("AI_DEV_TEAM_CONFIG", "AI_DEV_TEAM_DB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AI_DEV_TEAM_APP_DIR", str(tmp_path))
    folder = desktop.prepare_environment()
    cfg = yaml.safe_load((folder / "agents.yaml").read_text())
    models = [a["model"] for a in cfg["agents"].values()]
    assert models and all(m.endswith(":free") for m in models)


def test_existing_config_is_kept(tmp_path, monkeypatch):
    for var in ("AI_DEV_TEAM_CONFIG", "AI_DEV_TEAM_DB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AI_DEV_TEAM_APP_DIR", str(tmp_path))
    (tmp_path / "agents.yaml").write_text("mine: true\n")
    desktop.prepare_environment()
    assert (tmp_path / "agents.yaml").read_text() == "mine: true\n"
