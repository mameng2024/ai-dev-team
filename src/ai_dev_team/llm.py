"""Model access through the LiteLLM SDK (no proxy process: keeps memory low on 8 GB Macs).

Every call: per-agent model -> fallbacks in order, JSON output validated by Pydantic,
token usage recorded in SQLite. No SQLite transaction is held while waiting on a model.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .config import ModelRegistry

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

REASONING_EFFORT = {"low": "low", "medium": "medium", "high": "high"}


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    def complete_json(self, agent_id: str, system: str, user: str,
                      schema: type[T], task_id: str | None = None) -> T: ...


def extract_json(text: str) -> str:
    """Accept raw JSON or JSON inside a ```json fence."""
    m = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise LLMError("model returned no JSON")
    return text[start:]


TRANSIENT_NAMES = ("RateLimit", "Timeout", "APIConnection", "ServiceUnavailable",
                   "InternalServerError", "BadGateway")


def is_transient(exc: Exception) -> bool:
    """429 / 5xx / timeouts / connection drops: worth retrying the same model."""
    code = getattr(exc, "status_code", None)
    if isinstance(code, int) and (code == 429 or code >= 500):
        return True
    return any(n in type(exc).__name__ for n in TRANSIENT_NAMES)


def retry_after(exc: Exception) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        return float(headers.get("retry-after")) if headers.get("retry-after") else None
    except (TypeError, ValueError):
        return None


class LiteLLMClient:
    def __init__(self, registry: ModelRegistry, usage_sink=None, timeout_s: int | None = None,
                 transient_retries: int = 3, backoff_s: float = 2.0, sleep=None):
        self.transient_retries = transient_retries
        self.backoff_s = backoff_s
        import time as _time
        self.sleep = sleep or _time.sleep
        self.registry = registry
        self.usage_sink = usage_sink  # TaskRepository.record_usage or None
        self.timeout_s = timeout_s or registry.defaults.timeout_s

    def _call(self, model: str, system: str, user: str, reasoning: str) -> tuple[str, dict]:
        import litellm  # lazy: keeps CLI startup fast and tests offline

        provider = self.registry.provider_of_model(model)
        kwargs: dict = {}
        if provider:
            key = provider.api_key()
            if not key:
                raise LLMError(f"{provider.api_key_env} not set for {model}")
            kwargs["api_key"] = key
            if provider.api_base:
                kwargs["api_base"] = provider.api_base
        resp = litellm.completion(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            timeout=self.timeout_s,
            reasoning_effort=REASONING_EFFORT.get(reasoning),
            drop_params=True,  # providers that don't support a param simply ignore it
            **kwargs,
        )
        text = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        return text, {
            "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
        }

    def complete_json(self, agent_id: str, system: str, user: str,
                      schema: type[T], task_id: str | None = None) -> T:
        agent = self.registry.get(agent_id)
        models = [agent.model, *agent.fallbacks]
        errors: list[str] = []
        sys_prompt = (system + "\n\nReturn ONLY one JSON object matching this JSON schema:\n"
                      + json.dumps(schema.model_json_schema(), ensure_ascii=False))
        for model in models:
            for attempt in range(2):  # one repair retry per model on invalid JSON
                prompt = user if attempt == 0 else (
                    user + f"\n\nYour previous answer was invalid: {errors[-1]}\nReturn valid JSON.")
                try:
                    text, usage = self._call_with_backoff(model, sys_prompt, prompt,
                                                          agent.reasoning, task_id)
                except LLMError:
                    raise
                except Exception as exc:  # noqa: BLE001 - network / provider error -> next model
                    errors.append(f"{model}: {exc}")
                    self._usage(model, {}, "ERROR", task_id)
                    log.warning("model %s failed: %s", model, exc)
                    break
                try:
                    result = schema.model_validate_json(extract_json(text))
                except (ValidationError, LLMError, ValueError) as exc:
                    errors.append(f"{model}: invalid output: {str(exc)[:300]}")
                    self._usage(model, usage, "INVALID", task_id)
                    continue
                self._usage(model, usage, "OK", task_id)
                return result
        raise LLMError(f"all models failed for {agent_id}: " + " | ".join(errors))

    def _call_with_backoff(self, model, system, user, reasoning, task_id):
        """Retry the same model on rate limits / transient errors (exponential backoff,
        honouring Retry-After) before the caller falls back to the next model."""
        for attempt in range(self.transient_retries + 1):
            try:
                return self._call(model, system, user, reasoning)
            except LLMError:
                raise
            except Exception as exc:
                if not is_transient(exc) or attempt == self.transient_retries:
                    raise
                wait = retry_after(exc) or self.backoff_s * (2 ** attempt)
                self._usage(model, {}, "RETRY", task_id)
                log.warning("model %s transient error (%s); retry in %.1fs", model,
                            type(exc).__name__, wait)
                self.sleep(min(wait, 60))
        raise AssertionError("unreachable")

    def _usage(self, model: str, usage: dict, status: str, task_id: str | None) -> None:
        if self.usage_sink:
            self.usage_sink(model, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                            status, task_id=task_id)
