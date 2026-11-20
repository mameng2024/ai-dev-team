# Implementation Plan

## P0 — scaffold (done, v0.1.0)
Project packaging, config, SQLite schema, domain contracts, CLI, scheduler semaphore and
OpenCode subprocess adapter. Add tests for state transitions and concurrency.

## P1 — durable scheduler (done, v0.2.0)
Implement atomic claims (`BEGIN IMMEDIATE`), dependency readiness, worker leases,
execution records, event log and restart recovery.

## P2 — Git + OpenCode (done, v0.5.0; real OpenCode run to verify on the Mac)
Validate installed OpenCode CLI version and `run` flags. Create one worktree per task,
invoke OpenCode with explicit model and timeout, capture stdout/stderr, collect commit SHA.

## P3 — test and review (done, v0.3.0)
Run project-specific tests in the worktree. Launch a separate read-only Reviewer process
with original task, acceptance criteria, diff and test report. Enforce PASS/REWORK/BLOCKED.
Limit rework to three attempts.

## P4 — integration (done, v0.5.0)
Serialize merges, run post-merge regression tests, recover from conflicts, and only mark
COMPLETED after a successful merge and required checks.

## P5 — Manager and Architect (code done in v0.2.0; prompt tuning needs real model runs)
Add LangGraph state graph for planning and decomposition. Validate all LLM output with
Pydantic; reject malformed DAGs, cycles, missing criteria and unsafe paths.

## P6 — dashboard (done, v0.5.0: `ai-dev-team ui` → /dashboard, read-only)
Add lightweight local dashboard only after core automation is reliable.

## P7 — resilience (done, v0.5.0: covered by tests/test_e2e_worktrees.py, test_llm.py, test_claims.py)
Test process termination, API rate limits, duplicate events, SQLite contention, timeouts,
worker crashes, reviewer disagreement and corrupted worktrees.
