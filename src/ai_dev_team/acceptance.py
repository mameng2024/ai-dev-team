"""Final acceptance (v0.7.0): program checks -> Manager review -> human sign-off.

Why three layers
- A model only reads text; it cannot run the app. So whatever a program can check, a
  program checks: the plan's command checks and the full test suite run on the
  integration branch, and their exit codes decide those criteria. The Manager cannot
  overrule them.
- The Manager then lines the evidence up against every completion criterion. It may only
  say "met" with evidence; a "manual" criterion (looks, wording, feel) is never met by the
  model, it goes to the person.
- The person has the last word: approve, or reject with a note that becomes the request
  for the next round of planning. ai-dev-team still never merges into main.
"""
from __future__ import annotations

import logging
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .agents.planning import AcceptanceCheck, ProjectPlan
from .agents.prompts import MANAGER_ACCEPT_SYSTEM
from .domain import TaskStatus
from .llm import LLMError
from .repository import TaskRepository
from .tester import PROJECT_CONFIG, Tester, TestReport, scrubbed_env
from .worktrees import GitError, WorktreeManager, git

log = logging.getLogger(__name__)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
DEFAULT_ARTIFACT_DIR = ".ai-dev-team/acceptance"
BLOCKED_PROGRAMS = {
    "rm", "rmdir", "sudo", "su", "doas", "curl", "wget", "ssh", "scp", "rsync", "nc",
    "ncat", "dd", "mkfs", "chmod", "chown", "kill", "killall", "pkill", "shutdown",
    "reboot", "launchctl", "osascript", "security", "open", "sh", "bash", "zsh", "fish",
    "powershell", "pwsh", "cmd", "eval",
}
BLOCKED_GIT = {"push", "reset", "clean", "checkout", "switch", "merge", "rebase", "commit",
               "branch", "tag", "remote", "fetch", "pull", "worktree"}
INLINE_CODE_FLAGS = {
    "python": {"-c"}, "node": {"-e", "--eval", "-p", "--print"}, "deno": {"eval"},
    "bun": {"-e", "--eval", "-p", "--print"}, "ruby": {"-e"}, "perl": {"-e", "-E"},
}
SECRET_ARG = re.compile(r"\.env\b|\.ssh\b|id_rsa|Keychain|secrets\.env", re.IGNORECASE)


def unsafe_command(cmd: list[str]) -> str | None:
    """Why a model-written check command must not run, or None when it may."""
    if not cmd or not all(isinstance(a, str) and a for a in cmd):
        return "empty command"
    prog = Path(cmd[0]).name.lower()
    if prog in BLOCKED_PROGRAMS:
        return f"`{cmd[0]}` is not allowed in an acceptance check"
    if prog == "git" and len(cmd) > 1 and cmd[1] in BLOCKED_GIT:
        return f"`git {cmd[1]}` changes the repository"
    inline = INLINE_CODE_FLAGS.get(re.sub(r"[\d.]+$", "", prog).removesuffix(".exe"), set())
    if any(a in inline for a in cmd[1:]):
        return "inline code is not allowed; put the check in a test file"
    for a in cmd:
        if a.startswith(("/", "~")) or "../" in a or a == "..":
            return f"path outside the repository: {a}"
        if SECRET_ARG.search(a):
            return f"touches secrets: {a}"
    return None


class CheckOutcome(BaseModel):
    criterion: str
    command: list[str]
    exit_code: int | None = None
    timed_out: bool = False
    duration_s: float = 0.0
    output_tail: str = ""
    refused: str | None = None

    @property
    def passed(self) -> bool:
        return self.refused is None and self.exit_code == 0


class CriterionJudgement(BaseModel):
    criterion: str
    status: Literal["met", "unmet", "needs_human"]
    evidence: str = ""


class ManagerJudgement(BaseModel):
    criteria: list[CriterionJudgement] = Field(default_factory=list)
    summary: str = ""
    follow_ups: list[str] = Field(default_factory=list,
                                  description="one planner request per unmet criterion")
    human_focus: list[str] = Field(default_factory=list,
                                   description="what the person should look at")


class CriterionResult(BaseModel):
    criterion: str
    kind: Literal["command", "manual"]
    status: Literal["met", "unmet", "needs_human"]
    evidence: str = ""
    decided_by: Literal["program", "manager", "guardrail"] = "manager"
    how_to_verify: str = ""


class AcceptanceReport(BaseModel):
    verdict: Literal["READY_FOR_HUMAN", "GAPS"]
    criteria: list[CriterionResult]
    tests_passed: bool
    test_summary: str
    checks: list[CheckOutcome]
    summary: str
    follow_ups: list[str] = Field(default_factory=list)
    human_focus: list[str] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)
    manager_model_ok: bool = True


class AcceptanceError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- helpers
def artifact_dir(workspace: Path) -> Path:
    cfg = workspace / PROJECT_CONFIG
    rel = DEFAULT_ARTIFACT_DIR
    if cfg.exists():
        try:
            rel = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get(
                "acceptance_artifacts", rel) or rel
        except yaml.YAMLError:
            pass
    path = (workspace / rel).resolve()
    if not path.is_relative_to(workspace.resolve()):
        path = (workspace / DEFAULT_ARTIFACT_DIR).resolve()
    return path


def collect_screenshots(workspace: Path, limit: int = 40) -> list[str]:
    """Images the checks left behind (e.g. Playwright/XCUITest screenshots), repo-relative."""
    root = artifact_dir(workspace)
    if not root.is_dir():
        return []
    files = sorted((p for p in root.rglob("*") if p.suffix.lower() in IMAGE_EXT and p.is_file()),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [str(p.relative_to(workspace.resolve())) for p in files]


def run_command_checks(workspace: Path, checks: list[AcceptanceCheck], timeout_s: int = 900,
                       tail_chars: int = 4000) -> list[CheckOutcome]:
    out: list[CheckOutcome] = []
    for chk in checks:
        if chk.kind != "command":
            continue
        why = unsafe_command(chk.command)
        if why:
            out.append(CheckOutcome(criterion=chk.criterion, command=chk.command, refused=why))
            continue
        start = time.monotonic()
        try:
            r = subprocess.run(chk.command, cwd=workspace, capture_output=True, text=True,
                               timeout=timeout_s, env=scrubbed_env(), check=False)
            out.append(CheckOutcome(criterion=chk.criterion, command=chk.command,
                                    exit_code=r.returncode,
                                    duration_s=time.monotonic() - start,
                                    output_tail=(r.stdout + "\n" + r.stderr)[-tail_chars:]))
        except subprocess.TimeoutExpired:
            out.append(CheckOutcome(criterion=chk.criterion, command=chk.command, exit_code=-9,
                                    timed_out=True, duration_s=time.monotonic() - start))
        except (FileNotFoundError, PermissionError) as exc:
            out.append(CheckOutcome(criterion=chk.criterion, command=chk.command,
                                    exit_code=127, duration_s=time.monotonic() - start,
                                    output_tail=str(exc)))
    return out


def _outcome_text(o: CheckOutcome) -> str:
    cmd = shlex.join(o.command)
    if o.refused:
        return f"`{cmd}` was not run: {o.refused}"
    tail = "\n".join(o.output_tail.strip().splitlines()[-8:])
    state = "passed" if o.passed else f"failed (exit {o.exit_code}{', timeout' if o.timed_out else ''})"
    return f"`{cmd}` {state}" + (f"\n{tail}" if tail and not o.passed else "")


def apply_guardrails(plan: ProjectPlan, judgement: ManagerJudgement | None,
                     outcomes: list[CheckOutcome]) -> list[CriterionResult]:
    """Program results decide command criteria; the model never marks a manual one met."""
    by_text = {j.criterion.strip(): j for j in (judgement.criteria if judgement else [])}
    ordered = judgement.criteria if judgement else []
    results: list[CriterionResult] = []
    for i, chk in enumerate(plan.checks()):
        j = by_text.get(chk.criterion.strip()) or (ordered[i] if i < len(ordered) else None)
        if chk.kind == "command":
            o = next((x for x in outcomes if x.criterion == chk.criterion), None)
            if o is None or o.refused:
                results.append(CriterionResult(
                    criterion=chk.criterion, kind="command", status="needs_human",
                    decided_by="guardrail", how_to_verify=chk.how_to_verify,
                    evidence=_outcome_text(o) if o else "check did not run"))
            else:
                results.append(CriterionResult(
                    criterion=chk.criterion, kind="command",
                    status="met" if o.passed else "unmet", decided_by="program",
                    evidence=_outcome_text(o), how_to_verify=chk.how_to_verify))
            continue
        if j is None:
            results.append(CriterionResult(criterion=chk.criterion, kind="manual",
                                           status="needs_human", decided_by="guardrail",
                                           evidence="the Manager gave no judgement",
                                           how_to_verify=chk.how_to_verify))
        elif j.status == "met":
            results.append(CriterionResult(
                criterion=chk.criterion, kind="manual", status="needs_human",
                decided_by="guardrail", how_to_verify=chk.how_to_verify,
                evidence=f"Manager thinks it is met ({j.evidence}); a person decides."))
        else:
            results.append(CriterionResult(criterion=chk.criterion, kind="manual",
                                           status=j.status, decided_by="manager",
                                           evidence=j.evidence,
                                           how_to_verify=chk.how_to_verify))
    return results


# ----------------------------------------------------------------------------- main flow
class ManagerAcceptance:
    def __init__(self, llm, repo: TaskRepository, wt: WorktreeManager,
                 tester: Tester | None = None, manager_id: str = "manager",
                 check_timeout_s: int = 900):
        self.llm = llm
        self.repo = repo
        self.wt = wt
        self.tester = tester or Tester()
        self.manager_id = manager_id
        self.check_timeout_s = check_timeout_s

    def run(self, project_id: str) -> AcceptanceReport:
        rec = self.repo.latest_plan(project_id)
        if rec is None:
            raise AcceptanceError("no saved plan for this project; plan (and save) first")
        plan = ProjectPlan.model_validate(rec["plan"])
        open_tasks = [t for t in self.repo.list_tasks(project_id)
                      if t["id"] in rec["task_ids"] and t["status"] != TaskStatus.COMPLETED]
        if open_tasks:
            raise AcceptanceError("tasks not finished yet: " + ", ".join(
                f"{t['id']} ({t['status']})" for t in open_tasks))

        integ = self.wt.ensure_integration()
        sha = git(integ, "rev-parse", "HEAD").stdout.strip()
        tests: TestReport = self.tester.run_checks(integ)
        outcomes = run_command_checks(integ, plan.checks(), self.check_timeout_s)
        shots = collect_screenshots(integ)
        changes = self._changes()

        judgement, model_ok = None, True
        try:
            judgement = self.llm.complete_json(
                self.manager_id, MANAGER_ACCEPT_SYSTEM,
                self._prompt(plan, rec, project_id, tests, outcomes, changes, shots),
                ManagerJudgement)
        except LLMError as exc:  # program checks still count; the rest goes to the person
            log.warning("manager acceptance model failed: %s", exc)
            model_ok = False

        criteria = apply_guardrails(plan, judgement, outcomes)
        follow_ups = list(judgement.follow_ups) if judgement else []
        for c in criteria:
            if c.status == "unmet" and c.decided_by == "program" and not any(
                    c.criterion in f for f in follow_ups):
                follow_ups.append(f"Make this completion criterion pass: {c.criterion}. "
                                  f"Evidence: {c.evidence[:600]}")
        if not tests.passed:
            follow_ups.append("The test suite fails on the integration branch: "
                              + " | ".join(tests.failures())[:1500])
        gaps = (not tests.passed) or any(c.status == "unmet" for c in criteria)
        report = AcceptanceReport(
            verdict="GAPS" if gaps else "READY_FOR_HUMAN", criteria=criteria,
            tests_passed=tests.passed, test_summary=tests.summary()[-4000:], checks=outcomes,
            summary=(judgement.summary if judgement else
                     "Manager model unavailable; only the program checks were judged."),
            follow_ups=follow_ups, human_focus=judgement.human_focus if judgement else [],
            screenshots=shots, manager_model_ok=model_ok)
        self.repo.add_acceptance(project_id, rec["id"], "MANAGER", report.verdict,
                                 report.model_dump(), integration_sha=sha)
        return report

    def _changes(self) -> str:
        try:
            rng = f"{self.wt.default_branch}...{self.wt.integration_branch}"
            stat = git(self.wt.repo, "diff", "--stat", rng, check=False).stdout
            return stat[-6000:] or "(no changes)"
        except GitError as exc:
            return f"(could not read changes: {exc})"

    def _prompt(self, plan, rec, project_id, tests, outcomes, changes, shots) -> str:
        tasks = [t for t in self.repo.list_tasks(project_id) if t["id"] in rec["task_ids"]]
        lines = ["Original request:", rec["request"], "", "Approved plan:",
                 plan.model_dump_json(indent=2), "", "Tasks:"]
        lines += [f"- {t['id']} [{t['status']}] {t['title']}" for t in tasks]
        lines += ["", "Integration branch changes (git diff --stat):", changes, "",
                  "Full test run on the integration branch:", tests.summary()[-3000:], "",
                  "Command checks (exit code decides; you cannot change these):"]
        lines += [f"- {o.criterion}: {_outcome_text(o)}" for o in outcomes] or ["- (none)"]
        if shots:
            lines += ["", "Screenshots saved for the person: " + ", ".join(shots[:20])]
        return "\n".join(lines)


def integration_sha(wt: WorktreeManager) -> str | None:
    r = git(wt.repo, "rev-parse", "--verify", "--quiet", wt.integration_branch, check=False)
    return r.stdout.strip() or None


def state(repo: TaskRepository, project_id: str, wt: WorktreeManager | None = None) -> dict:
    """Where acceptance stands, for the CLI and the app."""
    rec = repo.latest_plan(project_id)
    if rec is None:
        return {"status": "NO_PLAN"}
    tasks = {t["id"]: t["status"] for t in repo.list_tasks(project_id)}
    pending = [tid for tid in rec["task_ids"] if tasks.get(tid) != TaskStatus.COMPLETED]
    rows = repo.acceptances(project_id, rec["id"])
    manager = next((r for r in reversed(rows) if r["stage"] == "MANAGER"), None)
    human = next((r for r in reversed(rows) if r["stage"] == "HUMAN"), None)
    if human and manager and human["created_at"] < manager["created_at"]:
        human = None                       # a newer Manager check replaces an old decision
    sha = integration_sha(wt) if wt else None
    stale = bool(manager and sha and manager["integration_sha"] != sha)
    if pending:
        status = "IN_PROGRESS"
    elif manager is None or stale:
        status = "NEEDS_MANAGER_CHECK"
    elif human:
        status = human["verdict"]          # APPROVED / REJECTED
    else:
        status = "AWAITING_HUMAN" if manager["verdict"] == "READY_FOR_HUMAN" else "GAPS"
    return {"status": status, "plan_id": rec["id"], "request": rec["request"],
            "plan_summary": rec["plan"].get("summary", ""), "pending": pending,
            "stale": stale, "manager": manager, "human": human}


def decide(repo: TaskRepository, project_id: str, wt: WorktreeManager | None, approve: bool,
           note: str = "", force: bool = False) -> dict:
    """Record the person's decision on the latest Manager check."""
    st = state(repo, project_id, wt)
    if st["status"] in ("NO_PLAN", "IN_PROGRESS", "NEEDS_MANAGER_CHECK"):
        raise AcceptanceError({
            "NO_PLAN": "no saved plan for this project",
            "IN_PROGRESS": "tasks are not finished yet",
            "NEEDS_MANAGER_CHECK": "run the Manager acceptance check first "
                                   "(the integration branch changed or was never checked)",
        }[st["status"]])
    if approve and st["status"] == "GAPS" and not force:
        raise AcceptanceError("the Manager check found gaps; fix them or approve with --force")
    if not approve and not note.strip():
        raise AcceptanceError("say what is wrong: the note becomes the next planning request")
    manager = st["manager"]
    payload = {"note": note.strip(), "forced": bool(force and approve and st["status"] == "GAPS"),
               "manager_acceptance_id": manager["id"]}
    repo.add_acceptance(project_id, st["plan_id"], "HUMAN",
                        "APPROVED" if approve else "REJECTED", payload,
                        integration_sha=manager["integration_sha"])
    return state(repo, project_id, wt)


def replan_request(st: dict, note: str) -> str:
    """The request the next planning round starts from after a rejection."""
    parts = [f"Follow-up to: {st.get('request', '')}".strip(), "",
             "The person rejected the result at acceptance:", note.strip()]
    gaps = (st.get("manager") or {}).get("payload", {}).get("follow_ups") or []
    if gaps:
        parts += ["", "Open gaps from the Manager check:", *[f"- {g}" for g in gaps]]
    return "\n".join(parts)
