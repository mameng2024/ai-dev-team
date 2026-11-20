"""Independent Reviewer (P3).

Independence rules
- The model call runs in a *separate OS process* (`python -m ai_dev_team.review_worker`),
  fresh session, its own model (config `reviewer`), read-only: it gets text, no tools.
- Its input is only: task, acceptance criteria, earlier required fixes, the Git diff and
  the test report. Never the Developer's stdout, reasoning or session id.

Veto power
The model's verdict is then passed through deterministic guardrails that can only make it
stricter, never looser: failing tests, an empty diff, deleted tests, critical/major
findings or a low score all block a PASS.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .domain import ReviewFinding, ReviewResult, TaskSpec
from .gitdiff import DiffBundle
from .tester import TestReport

PASS_MIN_SCORE = 70


class ReviewInput(BaseModel):
    task: TaskSpec
    previous_fixes: list[str] = Field(default_factory=list)
    diff_files: list[str]
    diff_patch: str
    diff_truncated: bool = False
    test_summary: str
    tests_passed: bool
    warnings: list[str] = Field(default_factory=list)


class ModelVerdict(BaseModel):
    """What the reviewer model must return."""
    verdict: Literal["PASS", "REWORK", "BLOCKED"]
    score: int = Field(ge=0, le=100)
    criteria_check: list[str] = Field(
        min_length=1, description="one line per acceptance criterion: met / not met + why")
    findings: list[ReviewFinding] = Field(default_factory=list)
    required_fixes: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list,
                                description="file:line references or test output quoted")


class ReviewOutput(BaseModel):
    verdict: ModelVerdict
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


def build_input(task: TaskSpec, previous_fixes: list[str], diff: DiffBundle,
                tests: TestReport) -> ReviewInput:
    warnings = []
    if diff.deleted_tests:
        warnings.append(f"test files deleted: {diff.deleted_tests}")
    if diff.removed_assertions:
        warnings.append(f"{diff.removed_assertions} assertion line(s) removed from tests")
    if diff.truncated:
        warnings.append("diff truncated; request smaller tasks if you cannot judge")
    if tests.detected:
        warnings.append("test commands were auto-detected (project has no .ai-dev-team.yaml)")
    files = [f"{f.status} {f.path} (+{f.added}/-{f.removed})" for f in diff.files]
    return ReviewInput(task=task, previous_fixes=previous_fixes, diff_files=files,
                       diff_patch=diff.patch, diff_truncated=diff.truncated,
                       test_summary=tests.summary(), tests_passed=tests.passed,
                       warnings=warnings)


def render_prompt(inp: ReviewInput) -> str:
    crit = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(inp.task.acceptance_criteria))
    prev = "\n".join(f"- {f}" for f in inp.previous_fixes) or "(first attempt)"
    warn = "\n".join(f"- {w}" for w in inp.warnings) or "(none)"
    return f"""# Task {inp.task.id}: {inp.task.title}

{inp.task.description}

## Acceptance criteria
{crit}

## Fixes required in earlier reviews (verify each is resolved)
{prev}

## Automated warnings
{warn}

## Test report
{inp.test_summary}

## Changed files
{chr(10).join(inp.diff_files) or "(none)"}

## Diff
```diff
{inp.diff_patch}
```

Check each acceptance criterion against the diff. Look for: broken existing behaviour,
concurrency and resource-release bugs, missed edge cases, security problems, tests that
do not actually cover the requirement, and changes that only make tests pass without
meeting the requirement. Every finding needs file/line evidence. REWORK must list concrete
required_fixes. Use BLOCKED only when the task itself is wrong or impossible as specified.
"""


def apply_guardrails(task_id: str, v: ModelVerdict, inp: ReviewInput, model: str,
                     diff_empty: bool) -> ReviewResult:
    verdict, fixes = v.verdict, list(v.required_fixes)
    findings = list(v.findings)
    reasons: list[str] = []
    if verdict == "PASS":
        if not inp.tests_passed:
            reasons.append("tests failed")
        if diff_empty:
            reasons.append("no code changes")
        if any("test files deleted" in w for w in inp.warnings):
            reasons.append("test files were deleted")
        if any(f.severity in ("critical", "major") for f in findings):
            reasons.append("critical/major findings present")
        if v.score < PASS_MIN_SCORE:
            reasons.append(f"score {v.score} < {PASS_MIN_SCORE}")
        if reasons:
            verdict = "REWORK"
            findings.append(ReviewFinding(severity="major", description=(
                "Guardrail overrode PASS: " + "; ".join(reasons))))
    if verdict == "REWORK" and not fixes:
        fixes = [f.description for f in findings if f.severity != "info"] or reasons or [
            "Reviewer requested rework without specifics; re-check all acceptance criteria."]
        if reasons:
            fixes += [f"Resolve: {r}" for r in reasons if f"Resolve: {r}" not in fixes]
    elif verdict == "REWORK" and reasons:
        fixes += [f"Resolve: {r}" for r in reasons]
    evidence = list(v.evidence) + [f"criterion: {c}" for c in v.criteria_check]
    score = v.score if verdict == v.verdict else min(v.score, PASS_MIN_SCORE - 1)
    return ReviewResult(task_id=task_id, verdict=verdict, score=score, findings=findings,
                        required_fixes=fixes, evidence=evidence, reviewer_model=model)


class Reviewer:
    """Runs the review in a separate process and applies the guardrails."""

    def __init__(self, config_path: Path, timeout_s: int = 600,
                 python: str = sys.executable):
        self.config_path = config_path
        self.timeout_s = timeout_s
        self.python = python

    def review(self, inp: ReviewInput, diff_empty: bool) -> tuple[ReviewResult, ReviewOutput]:
        env = dict(os.environ)
        env["AI_DEV_TEAM_CONFIG"] = str(self.config_path)
        r = subprocess.run(
            [self.python, "-m", "ai_dev_team.review_worker"],
            input=inp.model_dump_json(), capture_output=True, text=True,
            timeout=self.timeout_s, env=env, check=False,
            cwd="/",  # no access to the workspace via relative paths
        )
        if r.returncode != 0:
            raise RuntimeError(f"review worker failed (exit {r.returncode}): "
                               f"{r.stderr.strip()[-2000:]}")
        out = ReviewOutput.model_validate_json(r.stdout.strip().splitlines()[-1])
        return apply_guardrails(inp.task.id, out.verdict, inp, out.model, diff_empty), out


