import asyncio
import json
import shlex
import shutil
import sqlite3
import subprocess
from pathlib import Path

import typer
from rich import print
from rich.table import Table

from .config import Settings
from .db import Database
from .domain import TaskStatus
from .repository import TaskRepository

app = typer.Typer(no_args_is_help=True, help="AI Dev Team orchestrator")


def _repo(settings: Settings | None = None) -> tuple[Settings, TaskRepository]:
    settings = settings or Settings.load()
    db = Database(settings.database_path)
    db.initialize()
    return settings, TaskRepository(db)


@app.command()
def init():
    """Create/upgrade the SQLite database and register agents from config/agents.yaml."""
    settings, repo = _repo()
    reg = settings.registry()
    for agent in reg.agents.values():
        repo.register_agent(agent.id, agent.role.value, agent.model)
    print(f"[green]Initialized[/green] database: {settings.database_path.resolve()}")
    print(f"Registered {len(reg.agents)} agents from {settings.config_path}.")
    problems = settings.config_problems()
    if problems:
        print("[yellow]Not ready for real model calls yet:[/yellow]")
        for p in problems:
            print(f"  - {p}")


@app.command("use-preset")
def use_preset(name: str = typer.Argument(..., help="Preset name, e.g. openrouter")):
    """Replace the active agents.yaml with a built-in preset (old file kept as .bak)."""
    from importlib.resources import files
    src = files("ai_dev_team").joinpath("presets", f"{name}.yaml")
    if not src.is_file():
        avail = sorted(p.name[:-5] for p in files("ai_dev_team").joinpath("presets").iterdir()
                       if p.name.endswith(".yaml"))
        print(f"[red]No preset '{name}'.[/red] Available: {', '.join(avail)}")
        raise typer.Exit(1)
    settings = Settings.load()
    dest = settings.config_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.copy2(dest, dest.with_suffix(".yaml.bak"))
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"[green]Preset '{name}' written to[/green] {dest}")
    for p in settings.config_problems():
        print(f"  - {p}")


@app.command()
def status():
    """Show database counts and task status breakdown."""
    _, repo = _repo()
    counts = repo.db.counts()
    table = Table(title="AI Dev Team")
    table.add_column("Resource")
    table.add_column("Count", justify="right")
    for key, value in counts.items():
        table.add_row(key, str(value))
    by_status: dict[str, int] = {}
    for t in repo.list_tasks():
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
    for s, n in sorted(by_status.items()):
        table.add_row(f"tasks.{s}", str(n))
    print(table)


@app.command()
def agents():
    """List agents with model, fallbacks and whether their API key is present."""
    settings = Settings.load()
    reg = settings.registry()
    table = Table(title="Agent Registry")
    for col in ("ID", "Role", "Model", "Fallbacks", "Reasoning", "Key"):
        table.add_column(col)
    for a in reg.agents.values():
        key_ok = reg.providers[a.provider].api_key() is not None
        table.add_row(a.id, a.role.value, a.model, ", ".join(a.fallbacks) or "-",
                      a.reasoning, "[green]set[/green]" if key_ok else "[red]missing[/red]")
    print(table)


@app.command("config-check")
def config_check():
    """Validate config, API key env vars and the OpenCode binary (no model is called)."""
    settings = Settings.load()
    problems = settings.config_problems()
    oc = shutil.which(settings.opencode_bin)
    if oc:
        try:
            ver = subprocess.run([oc, "--version"], check=False, capture_output=True, text=True,
                                 timeout=15).stdout.strip()
            print(f"OpenCode: {oc} ({ver or 'version unknown'})")
        except Exception as exc:  # noqa: BLE001
            print(f"OpenCode found at {oc} but --version failed: {exc}")
    if problems:
        print("[red]Problems:[/red]")
        for p in problems:
            print(f"  - {p}")
        raise typer.Exit(1)
    print("[green]Config OK[/green]")


@app.command("project-add")
def project_add(project_id: str, repo_path: Path, name: str = "", branch: str = "main"):
    """Register a Git repository as a project (nothing in it is modified)."""
    _, repo = _repo()
    repo_path = repo_path.expanduser().resolve()
    if not (repo_path / ".git").exists():
        raise typer.BadParameter(f"{repo_path} is not a Git repository")
    repo.create_project(project_id, name or project_id, str(repo_path), branch)
    print(f"[green]Project added[/green] {project_id} -> {repo_path}")


@app.command()
def plan(project_id: str, request: str,
         save: bool = typer.Option(True, help="Save the task DAG to SQLite"),
         context_file: Path | None = typer.Option(None, help="Extra project context")):
    """Manager plans, Architect builds a validated task DAG (calls cloud models)."""
    ctx = context_file.read_text(encoding="utf-8") if context_file else ""
    _plan(project_id, request, save, ctx)


def _plan(project_id: str, request: str, save: bool, ctx: str = "") -> None:
    from .agents.planning import Planner, to_task_specs
    from .llm import LiteLLMClient

    settings, repo = _repo()
    if repo.get_project(project_id) is None:
        raise typer.BadParameter(f"unknown project {project_id}; run project-add first")
    reg = settings.registry()
    llm = LiteLLMClient(reg, usage_sink=repo.record_usage)
    state = Planner(llm).run(request, ctx)
    if state.get("problems"):
        print("[red]Architect could not produce a valid DAG:[/red]")
        for p in state["problems"]:
            print(f"  - {p}")
        raise typer.Exit(1)
    plan_obj = state["plan"]
    print("[bold]Plan[/bold]: " + plan_obj.summary)
    checks = plan_obj.checks()
    if checks:
        print("[bold]Acceptance[/bold] (checked when every task is done):")
        for c in checks:
            how = shlex.join(c.command) if c.kind == "command" else (c.how_to_verify or "by you")
            print(f"  - [{'program' if c.kind == 'command' else 'you'}] {c.criterion}: {how}")
    specs = to_task_specs(state["graph"], project_id, reg.defaults.max_retries)
    t = Table(title="Task DAG")
    for c in ("ID", "Title", "Depends on", "Priority"):
        t.add_column(c)
    for s in specs:
        t.add_row(s.id, s.title, ", ".join(s.dependencies) or "-", str(s.priority))
    print(t)
    if save:
        repo.create_tasks(specs)
        repo.save_plan(project_id, request, plan_obj.model_dump(), [s.id for s in specs])
        promoted = repo.promote_ready(project_id)
        print(f"[green]Saved {len(specs)} tasks[/green]; {len(promoted)} READY now.")


@app.command()
def tasks(project_id: str | None = None):
    """List tasks."""
    _, repo = _repo()
    t = Table(title="Tasks")
    for c in ("ID", "Title", "Status", "Retries", "Worker"):
        t.add_column(c)
    for r in repo.list_tasks(project_id):
        t.add_row(r["id"], r["title"], r["status"], f"{r['retry_count']}/{r['max_retries']}",
                  r["assigned_worker"] or "-")
    print(t)


@app.command()
def events(task_id: str | None = None, limit: int = 50):
    """Show the event log."""
    _, repo = _repo()
    for e in repo.events(task_id)[-limit:]:
        print(f"{e['created_at']}  {e['event_type']:<16} {e['task_id'] or '-':<24} "
              f"{json.dumps(json.loads(e['payload_json']), ensure_ascii=False)}")


@app.command()
def recover():
    """Return tasks with expired leases (crashed/stopped workers) to READY."""
    _, repo = _repo()
    ids = repo.recover_expired_leases()
    print(f"Recovered {len(ids)} task(s): {', '.join(ids) or '-'}")


@app.command()
def run(project_id: str,
        dry_run: bool = typer.Option(True, "--dry-run/--opencode",
                                     help="dry-run touches no code (default). --opencode runs "
                                          "OpenCode in one disposable worktree per task."),
        review: bool | None = typer.Option(
            None, "--review/--no-review",
            help="Test gate + independent Reviewer + merge queue after each task "
                 "(default: on with --opencode, off with --dry-run).")):
    """Dispatch READY tasks to developer-a / developer-b (max 2 concurrent)."""
    from .executors import DryRunExecutor
    from .scheduler import DurableScheduler, WorkerSpec

    settings, repo = _repo()
    reg = settings.registry()
    devs = [WorkerSpec(a.id, a.model) for a in reg.by_role("DEVELOPER")]
    on_testing = None
    if dry_run:
        # Rehearse on a copy of the database: real task states never change.
        import tempfile

        from .db import Database
        tmpdir = Path(tempfile.mkdtemp(prefix="aidt-dry-"))
        copy = tmpdir / "rehearsal.sqlite3"
        src_db = settings.database_path
        with sqlite3.connect(src_db) as src, sqlite3.connect(copy) as dst:
            src.backup(dst)
        repo = TaskRepository(Database(copy))
        print("[dim]Dry run on a copy of the database; real task states are not changed.[/dim]")
        executor = DryRunExecutor()
        workspace_fn = lambda claim: Path(".")
        if review:
            print("[yellow]--review needs --opencode (dry-run writes no code).[/yellow]")
    else:
        from .executors.opencode import OpenCodeExecutor, probe

        wt = _worktrees(repo, project_id)
        caps = probe(settings.opencode_bin)
        if caps.error:
            print(f"[red]{caps.error}[/red]")
            raise typer.Exit(1)
        executor = OpenCodeExecutor(
            settings.opencode_bin, settings.command_timeout_seconds,
            auto_approve=reg.opencode.auto_approve, env=reg.key_env(),
            model_map=reg.opencode.model_map, caps=caps)
        def workspace_fn(claim):
            path = wt.ensure_task(claim.task.id)
            repo.set_worktree(claim.task.id, str(path), wt.branch_for(claim.task.id))
            return path
        print(f"OpenCode {caps.version}; worktrees under {wt.root}; "
              f"integration branch {wt.integration_branch} (main is never touched)")
        if review is not False:
            from .merger import MergeQueue
            from .pipeline import resume_stuck, review_then_merge
            from .tester import Tester

            pipeline = _pipeline(settings, repo, wt.path_for, base_ref=wt.integration_branch)
            merger = MergeQueue(repo, wt, Tester())
            for tid, st in resume_stuck(repo, project_id, pipeline, merger).items():
                print(f"resumed {tid} -> {st}")
            on_testing = review_then_merge(pipeline, merger)
    sched = DurableScheduler(
        repo, executor, devs, workspace_fn, on_testing=on_testing,
        routing=reg.developer_routing,
        max_concurrency=min(reg.defaults.max_concurrency, len(devs)),
        lease_seconds=reg.defaults.lease_seconds,
        timeout_seconds=settings.command_timeout_seconds, project_id=project_id)
    report = asyncio.run(sched.run_until_idle())
    if report.recovered:
        print(f"Recovered after restart: {', '.join(report.recovered)}")
    for tid, worker in report.claimed:
        print(f"{worker:<12} {tid:<28} -> {report.outcomes.get(tid, '?')}")
    print(f"Peak concurrency: {report.peak_concurrency}")
    counts: dict[str, int] = {}
    for t in repo.list_tasks(project_id):
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    print("Status: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    if counts.get("BLOCKED"):
        print("[yellow]Blocked tasks need the Manager to re-plan: `ai-dev-team tasks`, "
              "`ai-dev-team events <task>`.[/yellow]")


def _worktrees(repo: TaskRepository, project_id: str):
    from .worktrees import WorktreeManager

    project = repo.get_project(project_id)
    if project is None:
        raise typer.BadParameter(f"unknown project '{project_id}' (use project-add)")
    return WorktreeManager(Path(project["repo_path"]), project_id, project["default_branch"])


def _pipeline(settings: Settings, repo: TaskRepository, workspace_fn, base_ref=None):
    from .pipeline import ReviewPipeline
    from .reviewer import Reviewer
    from .tester import Tester

    reviewer = Reviewer(settings.config_path.resolve())
    return ReviewPipeline(repo, Tester(), reviewer, workspace_fn, base_ref=base_ref)


@app.command()
def review(task_id: str,
           workspace: Path | None = typer.Option(
               None, help="Override the task workspace (default: its worktree)")):
    """Run the test gate + independent Reviewer for a task in TESTING (or retry REVIEWING);
    a PASS then goes through the merge queue."""
    settings, repo = _repo()
    project_id = repo.get_task(task_id)["project_id"]
    try:
        if workspace is not None:
            ws = workspace.expanduser().resolve()
            status = _pipeline(settings, repo, lambda _t: ws).process(task_id)
        else:
            from .merger import MergeQueue
            from .tester import Tester

            wt = _worktrees(repo, project_id)
            status = _pipeline(settings, repo, wt.path_for,
                               base_ref=wt.integration_branch).process(task_id)
            if status == TaskStatus.MERGE_QUEUED:
                status = MergeQueue(repo, wt, Tester()).process(task_id)
    except ValueError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    print(f"{task_id} -> {status.value}")
    notes = repo.rework_notes(task_id)
    if status.value in ("READY", "BLOCKED") and notes:
        print("Rework notes for the next attempt:")
        print(f"  {notes[-1][:1500]}")
    for r in repo.reviews(task_id)[-1:]:
        print(f"verdict {r['verdict']} score {r['score']} by {r['reviewer_model']}")
        for fix in json.loads(r["required_fixes_json"]):
            print(f"  - {fix}")


@app.command()
def merge(project_id: str):
    """Process the merge queue (MERGE_QUEUED tasks) into the integration branch."""
    from .merger import MergeQueue
    from .tester import Tester

    _, repo = _repo()
    wt = _worktrees(repo, project_id)
    out = MergeQueue(repo, wt, Tester()).drain(project_id)
    for tid, st in out.items():
        print(f"{tid:<28} -> {st}")
    if not out:
        print("Merge queue is empty.")


@app.command()
def integration(project_id: str):
    """Show what the integration branch has on top of main, and how to take it."""
    _, repo = _repo()
    wt = _worktrees(repo, project_id)
    log_lines = wt.integration_log(50)
    print(f"Branch {wt.integration_branch} ({len(log_lines)} commit(s) ahead of "
          f"{wt.default_branch}); worktree {wt.integration_path}")
    for line in log_lines:
        print(f"  {line}")
    from .acceptance import state as acceptance_state
    st = acceptance_state(repo, project_id, wt)
    print(f"Acceptance: {STATUS_TEXT[st['status']]}")
    if st["status"] != "APPROVED" and log_lines:
        print("[yellow]Not accepted yet: run `ai-dev-team acceptance` and accept before "
              "merging.[/yellow]")
    print(f"[dim]Review, then merge it yourself: git -C {wt.repo} merge --no-ff "
          f"{wt.integration_branch}  (ai-dev-team never does this)[/dim]")


STATUS_TEXT = {
    "NO_PLAN": "no saved plan", "IN_PROGRESS": "tasks still running",
    "NEEDS_MANAGER_CHECK": "ready for the Manager check", "GAPS": "Manager found gaps",
    "AWAITING_HUMAN": "waiting for your acceptance", "APPROVED": "accepted by you",
    "REJECTED": "rejected by you",
}


@app.command()
def acceptance(project_id: str):
    """Final acceptance, step 1: run the plan's checks and the tests on the integration
    branch, then the Manager lines the evidence up against every completion criterion."""
    from .acceptance import AcceptanceError, ManagerAcceptance
    from .llm import LiteLLMClient

    settings, repo = _repo()
    wt = _worktrees(repo, project_id)
    llm = LiteLLMClient(settings.registry(), usage_sink=repo.record_usage)
    try:
        report = ManagerAcceptance(llm, repo, wt).run(project_id)
    except AcceptanceError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    mark = {"met": "[green]met[/green]", "unmet": "[red]unmet[/red]",
            "needs_human": "[yellow]you decide[/yellow]"}
    print(f"Tests on integration: {'PASS' if report.tests_passed else '[red]FAIL[/red]'}")
    for c in report.criteria:
        print(f"  {mark[c.status]:<28} {c.criterion}  [dim]({c.decided_by})[/dim]")
        if c.evidence:
            print(f"      [dim]{c.evidence.splitlines()[0][:200]}[/dim]")
        if c.status == "needs_human" and c.how_to_verify:
            print(f"      how: {c.how_to_verify}")
    if report.summary:
        print(f"Manager: {report.summary}")
    for f in report.human_focus:
        print(f"  look at: {f}")
    for s in report.screenshots:
        print(f"  screenshot: {wt.integration_path / s}")
    if report.verdict == "GAPS":
        print("[yellow]Gaps found.[/yellow] Fix with `ai-dev-team reject <project> --note ... "
              "--replan`, or accept anyway with `accept --force`.")
        for f in report.follow_ups:
            print(f"  - {f[:300]}")
    else:
        print("[green]Ready for your acceptance:[/green] check the items above, then "
              f"`ai-dev-team accept {project_id}` or `ai-dev-team reject {project_id} --note ...`")


@app.command()
def accept(project_id: str, note: str = typer.Option("", help="Optional note"),
           force: bool = typer.Option(False, "--force", help="Accept even with gaps")):
    """Final acceptance, step 2: you accept the result (still never merges into main)."""
    from .acceptance import AcceptanceError, decide

    _, repo = _repo()
    wt = _worktrees(repo, project_id)
    try:
        st = decide(repo, project_id, wt, approve=True, note=note, force=force)
    except AcceptanceError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    print(f"[green]Accepted.[/green] {STATUS_TEXT[st['status']]}. Merge it yourself: "
          f"git -C {wt.repo} merge --no-ff {wt.integration_branch}")


@app.command()
def reject(project_id: str, note: str = typer.Option(..., help="What is wrong"),
           replan: bool = typer.Option(False, "--replan",
                                       help="Plan and save follow-up tasks from the note now")):
    """Final acceptance, step 2: you reject; the note becomes the next planning request."""
    from .acceptance import AcceptanceError, decide, replan_request

    _, repo = _repo()
    wt = _worktrees(repo, project_id)
    try:
        st = decide(repo, project_id, wt, approve=False, note=note)
    except AcceptanceError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    request = replan_request(st, note)
    print("[yellow]Rejected.[/yellow] Next planning request:\n" + request)
    if replan:
        _plan(project_id, request, save=True)


@app.command("opencode-check")
def opencode_check():
    """Probe the installed OpenCode CLI: version, supported `run` flags, model ids."""
    from .executors.opencode import probe

    settings = Settings.load()
    reg = settings.registry()
    caps = probe(settings.opencode_bin)
    if caps.error:
        print(f"[red]{caps.error}[/red]")
        raise typer.Exit(1)
    print(f"OpenCode {caps.version}")
    for flag in ("--model", "--standalone", "--auto", "--archived", "--format", "--dir"):
        print(f"  {flag:<13} {'yes' if caps.has(flag) else 'no'}")
    r = subprocess.run([settings.opencode_bin, "models"], capture_output=True, text=True,
                       timeout=60, check=False)
    available = set(r.stdout.split())
    models = {m for a in reg.by_role("DEVELOPER") for m in (a.model, *a.fallbacks)}
    models |= set(reg.developer_routing.values())
    print("Developer models (LiteLLM id -> OpenCode id):")
    for m in sorted(models):
        oc = reg.opencode.model_map.get(m, m)
        mark = "?" if not available else ("ok" if oc in available
                                         else "NOT in `opencode models`: add opencode.model_map")
        print(f"  {m} -> {oc}: {mark}")


@app.command()
def ui(port: int = 8765, no_browser: bool = typer.Option(False, "--no-browser"),
       url_file: Path | None = typer.Option(None, help="Write the page URL here (for the app)")):
    """Open the unified local page: projects & runs, dashboard, model settings."""
    from .settings_server import serve

    settings = Settings.load()
    path = settings.config_path
    if not path.exists():
        print(f"[red]config not found: {path.resolve()}[/red] (run from the project root "
              "or set AI_DEV_TEAM_CONFIG)")
        raise typer.Exit(1)
    try:
        init()                         # idempotent: DB schema + agents from the config
    except Exception as exc:  # noqa: BLE001 - the page shows config problems itself
        print(f"[yellow]init skipped: {exc}[/yellow]")
    serve(path, port=port, open_browser=not no_browser, db_path=settings.database_path,
          url_file=url_file)


@app.command()
def desktop(browser: bool = typer.Option(False, "--browser", help="Use the browser instead")):
    """Open AI Dev Team in its own window (the packaged app's entry point)."""
    from .desktop import main as desktop_main

    raise typer.Exit(desktop_main(["--browser"] if browser else []))


@app.command("test-check")
def test_check(workspace: Path = typer.Option(Path("."), help="Directory to test")):
    """Run only the test gate in a workspace and print the report (no state change)."""
    from .tester import Tester

    report = Tester().run_checks(workspace.expanduser().resolve())
    print(report.summary())
    raise typer.Exit(0 if report.passed else 1)


if __name__ == "__main__":
    app()
