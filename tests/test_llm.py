from types import SimpleNamespace

import pytest

from ai_dev_team.agents.planning import ProjectPlan
from ai_dev_team.config import ModelRegistry
from ai_dev_team.llm import LiteLLMClient, LLMError

PLAN = ('```json\n{"summary":"s","milestones":[{"name":"m","goal":"g"}],'
        '"completion_criteria":["c"]}\n```')


def resp(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                           usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


def test_fallback_and_repair(monkeypatch, config_path):
    import litellm
    monkeypatch.setenv("DASHSCOPE_API_KEY", "x")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "y")
    calls = []

    def fake_completion(model, **kw):
        calls.append((model, kw["api_base"]))
        if model.startswith("dashscope/"):
            raise RuntimeError("503 provider down")
        if len([c for c in calls if c[0] == model]) == 1:
            return resp("not json at all")
        return resp(PLAN)

    monkeypatch.setattr(litellm, "completion", fake_completion)
    usage = []
    client = LiteLLMClient(ModelRegistry.load(config_path),
                           usage_sink=lambda *a, **k: usage.append(a[3]))
    plan = client.complete_json("manager", "sys", "req", ProjectPlan)
    assert plan.summary == "s"
    assert [c[0] for c in calls] == ["dashscope/qwen4-max", "deepseek/deepseek-v4-pro",
                                     "deepseek/deepseek-v4-pro"]
    assert calls[1][1] == "https://api.deepseek.com"
    assert usage == ["ERROR", "INVALID", "OK"]


def test_missing_key_fails_loudly(monkeypatch, config_path):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    client = LiteLLMClient(ModelRegistry.load(config_path))
    with pytest.raises(LLMError, match="DASHSCOPE_API_KEY"):
        client.complete_json("manager", "s", "u", ProjectPlan)


class RateLimitError(Exception):
    status_code = 429

    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.response = SimpleNamespace(headers={"retry-after": retry_after} if retry_after
                                        else {})


def test_rate_limit_retries_same_model_with_backoff(monkeypatch, config_path):
    import litellm
    monkeypatch.setenv("DEEPSEEK_API_KEY", "y")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "x")
    calls, sleeps, usage = [], [], []

    def fake_completion(model, **kw):
        calls.append(model)
        if len(calls) <= 2:
            raise RateLimitError("429 slow down", retry_after="3" if len(calls) == 1 else None)
        return resp('{"verdict":"PASS","score":90,"criteria_check":["ok"]}')

    monkeypatch.setattr(litellm, "completion", fake_completion)
    from ai_dev_team.reviewer import ModelVerdict
    client = LiteLLMClient(ModelRegistry.load(config_path), sleep=sleeps.append,
                           usage_sink=lambda *a, **k: usage.append(a[3]))
    v = client.complete_json("reviewer", "s", "u", ModelVerdict)
    assert v.verdict == "PASS"
    assert calls == ["deepseek/deepseek-v4-pro"] * 3          # never switched reviewer model
    assert sleeps == [3.0, 4.0]                               # Retry-After, then 2*2^1
    assert usage == ["RETRY", "RETRY", "OK"]


def test_persistent_rate_limit_gives_up(monkeypatch, config_path):
    import litellm
    monkeypatch.setenv("DEEPSEEK_API_KEY", "y")

    def always_429(model, **kw):
        raise RateLimitError("429")

    monkeypatch.setattr(litellm, "completion", always_429)
    from ai_dev_team.reviewer import ModelVerdict
    client = LiteLLMClient(ModelRegistry.load(config_path), sleep=lambda s: None,
                           transient_retries=2)
    with pytest.raises(LLMError, match="all models failed for reviewer"):
        client.complete_json("reviewer", "s", "u", ModelVerdict)
