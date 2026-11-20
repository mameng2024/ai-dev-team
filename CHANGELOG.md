# Changelog

## 0.7.0 — Desktop app for macOS, Windows and Linux

- The app opens in its own window (pywebview), no browser. Falls back to the browser
  when a machine has no webview.
- Self-contained builds with Python inside (PyInstaller): `.dmg` for macOS (Apple
  Silicon), `.zip` with `AI Dev Team.exe` for Windows, `.tar.gz` for Linux.
- GitHub Actions builds all three on every pull request; a `v*` tag publishes a Release.
- First start uses the free OpenRouter preset; settings live in the per-user app folder.
- OpenRouter presets (`openrouter`, `openrouter-free`) and `ai-dev-team use-preset`.
- `ai-dev-team desktop` runs the same window from a source install.
- Final acceptance in three layers. The Manager now writes how each completion criterion
  will be checked (a command, or a manual step) and the plan is saved. When every task is
  done, `ai-dev-team acceptance` runs those commands and the full test suite on the
  integration branch (exit codes decide, the Manager cannot overrule them), then the
  Manager lines the evidence up against each criterion: no evidence, no "met", and manual
  criteria always go to you. Screenshots the checks save under `.ai-dev-team/acceptance/`
  show up in the app. You finish with `accept` or `reject --note ... --replan` (the note
  becomes the next planning request), or the buttons in card ③ of the app. Check commands
  run without a shell, with API keys scrubbed; deleting, network, sudo, inline code and
  paths outside the repo are refused. SQLite schema 3 adds `plans` and `acceptances`.

## 0.6.0 — Mac app + one unified page

- `ai-dev-team ui` is now one page with tabs: 项目与运行 / 看板 / 模型设置.
- 项目与运行 does everything without a terminal: add a project (with a native macOS folder picker),
  preview the plan or plan and save it, rehearse a run or run for real, process the merge queue,
  recover, and see the integration branch and its merge command. Every action runs as a
  background job with a live log and a Stop button (SIGINT, then SIGKILL).
- Jobs are a fixed whitelist of `ai-dev-team` commands with validated arguments, never a
  shell. Only one mutating job runs per project at a time. 退出 stops the jobs and the server.
- `run --dry-run` now rehearses on a copy of the database, so real task states never change.
- `packaging/macos/build_app.sh` builds `AI Dev Team.app` on the Mac. The app environment lives
  in ~/Library/Application Support/AI Dev Team and survives updates. The script captures the login
  PATH plus the opencode, git and swift locations. Options: `--install`, `--dmg`, `--sign`.
- The UI tries ports 8765 to 8775 and writes its URL to a file, so the app reuses a running server.

## 0.5.0 — P2 worktrees + OpenCode, P4 merge queue, P6 dashboard, P7 resilience

P2
- `WorktreeManager`: one worktree + branch `aidt/<project>/<task>` per task under
  ~/.ai-dev-team/worktrees, started from the integration branch (so dependencies are in).
  Reused on rework; a deleted or broken worktree is rebuilt from its branch. Refuses
  main/master/develop/trunk and any branch not under `aidt/`. Per-repo lock for git updates.
- `OpenCodeExecutor` rewritten for the OpenCode V2 CLI: cwd = worktree (V2 has no --dir),
  `--model`, `--standalone`, `--auto`, `--archived`, picked by probing `opencode run --help`;
  provider keys passed in env; own process group, whole group killed on timeout/cancel;
  `opencode.model_map` for differing model ids. Prompt forbids commit/merge/branching and
  deleting tests.
- `--opencode` now runs both Developers in parallel. New `opencode-check` command.
P4
- `MergeQueue`: serialized merge into `aidt/<project>/integration` (never main), conflict →
  REWORK with markers left in the task worktree, post-merge regression tests on integration
  with automatic undo, idempotent completion after a crash, worktree/branch cleanup.
- Conflict markers left in a diff fail the test gate. New `merge` and `integration` commands.
P6
- `/dashboard` in `ai-dev-team ui`: counts by status, tasks with deps/worker/model/rework/
  last review/branch, recent events, model usage; auto-refresh 5 s. OpenCode section in the
  settings page.
P7
- Same-model retry with exponential backoff and Retry-After on 429/5xx/timeouts before any
  fallback (the Reviewer no longer fails on a single rate limit).
- `run` resumes tasks stuck in TESTING / REVIEWING / MERGE_QUEUED after a crash.
- Tests never read the real ~/.ai-dev-team. 63 tests incl. end-to-end runs with a fake
  OpenCode: parallel + dependent tasks, merge conflict and resolution, regression undo,
  reviewer disagreement → BLOCKED, timeout kills child processes, corrupted worktree,
  crash after merge, crash mid-merge, main untouched (incl. the user's uncommitted files).

## 0.4.0 — local model-settings page
- `ai-dev-team ui`: local page (127.0.0.1 only, random token) to set each agent's
  provider, model, reasoning and ordered fallbacks, Developer routing by complexity,
  providers/API base and run defaults. Live validation with the same rules as the CLI
  (Manager != Reviewer model, Reviewer never falls back to Manager's model) plus warnings.
- "Test" button per model: one tiny real completion, shows latency/tokens or the error.
- API keys stored in ~/.ai-dev-team/secrets.env (chmod 600), never shown back;
  environment variables still win. Saving keeps a backup in config/backups/.
- Scheduler now uses `developer_routing` (simple/complex from the Architect) to pick the
  Developer model per task.
- Stdlib HTTP server, no new dependencies. 48 tests.

## 0.3.1 — Swift / Xcode support for MiniTot
- Test-file detection recognises `*Tests.swift`, `*Test.swift` and `*Tests/` folders, so
  deleted XCTest/Swift Testing files and removed `XCTAssert`/`#expect` lines are flagged.
- Ignores `.build`, `DerivedData`, `xcuserdata`, `.swiftpm`, `*.xcresult` as non-changes.
- Example `.ai-dev-team.yaml` with `swift test` and `xcodebuild test` templates.

## 0.3.0 — P3 test gate + independent Reviewer + rework loop

Added
- `Tester`: runs the target repo's own checks from `.ai-dev-team.yaml` (`test_commands`),
  else auto-detects pytest / npm / cargo / go / swift. No shell, timeout, API keys and
  tokens stripped from the environment.
- `gitdiff.collect_diff`: committed + uncommitted + untracked changes vs the base branch
  (read-only), flags deleted test files and removed assertions, ignores build junk.
- `Reviewer`: model call in a separate process (`python -m ai_dev_team.review_worker`),
  fresh session, input = task, criteria, earlier fixes, diff, test report only. Never the
  Developer's output.
- Veto guardrails that can only tighten a verdict: failing tests, empty diff, deleted
  tests, critical/major findings or score < 70 turn PASS into REWORK. REWORK always
  carries concrete required fixes.
- `ReviewPipeline`: TESTING -> tests (fail -> REWORK with test output) -> REVIEWING ->
  PASS -> MERGE_QUEUED / REWORK -> READY with fixes / BLOCKED. Reviewer errors keep the
  task in REVIEWING for retry. Runs outside the Developer semaphore.
- CLI: `review <task> --workspace`, `test-check --workspace`, `run --review/--no-review`.
- 39 tests (14 new, using real Git repos and the real review subprocess).

## 0.2.0 — P1 durable scheduler + per-agent model config

Added
- `config/agents.yaml`: per-agent provider, model ID, API-key env var, reasoning level and
  fallbacks. Default: Manager Qwen4-Max (DashScope), Architect/Reviewer DeepSeek-V4-Pro,
  Developer A/B DeepSeek-V4.1-Flash. Config refuses Manager == Reviewer model, and the
  Reviewer never falls back to the Manager's model.
- Atomic task claims with `BEGIN IMMEDIATE` + conditional UPDATE; partial unique indexes
  guarantee one active execution per task and per worker.
- Worker leases with heartbeat renewal; `recover_expired_leases()` on every start returns
  crashed work to READY without counting it as a rework attempt; a returning zombie
  worker is rejected.
- Dependency readiness (`promote_ready`), cycle / unknown-dependency rejection on insert.
- Review application: PASS -> MERGE_QUEUED (only after a PASS review), REWORK carries the
  reviewer's required fixes into the next attempt, 3-rework limit -> BLOCKED +
  MANAGER_REPLAN event. Idempotent merge record (duplicate events never merge twice).
- `DurableScheduler`: two developers, `asyncio.Semaphore(2)`, stops at TESTING for P3.
- LiteLLM SDK client: JSON output validated by Pydantic, one repair retry per model,
  ordered fallbacks, token usage stored in `model_usage`.
- LangGraph planning graph Manager -> Architect -> validate (retries Architect with the
  concrete problems, up to 3 times).
- CLI: `config-check`, `project-add`, `plan`, `tasks`, `events`, `recover`, `run`.
- 25 tests (concurrency with 8 threads x 20 tasks, restart recovery, rework limit,
  idempotent merge, DAG order, LLM fallback with mocked provider).

Not yet done (honest boundary)
- No real model or OpenCode call has been made; model IDs must be verified.
- Test gate / Reviewer model call (P3), per-task Git worktrees and merge queue (P4).
- `run --opencode` is limited to one developer until P4 provides isolated worktrees.
