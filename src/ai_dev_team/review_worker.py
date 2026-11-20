"""Reviewer process entry point: `python -m ai_dev_team.review_worker < input.json`.

Runs in its own process with a fresh model session. Reads a ReviewInput on stdin, prints
one ReviewOutput JSON line on stdout. It has no database handle, no workspace and no tools.

Test hook: AI_DEV_TEAM_REVIEW_FAKE=<path to a ModelVerdict JSON> skips the model call.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .agents.prompts import REVIEWER_SYSTEM
from .config import ModelRegistry
from .llm import LiteLLMClient
from .reviewer import ModelVerdict, ReviewInput, ReviewOutput, render_prompt


def main() -> int:
    inp = ReviewInput.model_validate_json(sys.stdin.read())
    fake = os.getenv("AI_DEV_TEAM_REVIEW_FAKE")
    if fake:
        verdict = ModelVerdict.model_validate_json(Path(fake).read_text(encoding="utf-8"))
        print(ReviewOutput(verdict=verdict, model="fake/reviewer").model_dump_json())
        return 0
    reg = ModelRegistry.load()
    usage: list[tuple] = []
    client = LiteLLMClient(reg, usage_sink=lambda *a, **k: usage.append(a))
    verdict = client.complete_json("reviewer", REVIEWER_SYSTEM, render_prompt(inp), ModelVerdict,
                                   task_id=inp.task.id)
    ok = [u for u in usage if u[3] == "OK"]
    model = ok[-1][0] if ok else reg.get("reviewer").model
    print(ReviewOutput(verdict=verdict, model=model,
                       input_tokens=sum(u[1] for u in usage),
                       output_tokens=sum(u[2] for u in usage)).model_dump_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
