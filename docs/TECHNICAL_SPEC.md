# AI Dev Team MVP — Technical Specification

## Fixed MVP configuration

- 1 Manager, 1 Architect, 2 Developers, 1 independent Reviewer
- Maximum two concurrent code execution tasks
- Maximum three rework attempts per task
- OpenCode CLI execution backend
- SQLite persistence
- Cloud-hosted models only
- macOS Apple Silicon, 8 GB RAM target

## Architectural rules

1. Scheduler and database own task state; LLMs cannot directly mutate state.
2. Manager plans; Architect creates a dependency DAG; Developers execute; Reviewer audits.
3. Reviewer receives requirements, acceptance criteria, diff and test evidence, not worker private context.
4. Each task executes in a dedicated Git worktree and branch.
5. Only PASS results enter the merge queue.
6. Merge operations are serialized.
7. External deployment and production pushes require human approval.

## Interfaces

- Manager.plan_project(request, project_context) -> ProjectPlan
- Architect.design_tasks(plan) -> list[TaskSpec]
- Scheduler.dispatch_ready_tasks() -> list[Execution]
- CodeExecutor.execute(task, workspace, model, worker_id) -> ExecutionResult
- Tester.run_checks(workspace, project_config) -> TestReport
- Reviewer.review(task, diff, test_report) -> ReviewResult
- GitManager.create_worktree(project, task_id) -> Workspace
- Integrator.merge_approved(task_id, commit_sha) -> MergeResult

## Persistence

SQLite tables: projects, agents, tasks, task_dependencies, executions, reviews,
artifacts, model_usage, events. Use WAL, foreign keys, busy timeout and short transactions.

## State machine

PENDING -> READY -> ASSIGNED -> RUNNING -> TESTING -> REVIEWING.
PASS -> MERGE_QUEUED -> MERGED -> COMPLETED.
REWORK -> READY, bounded by max_retries=3.
BLOCKED requires Manager replanning.

## Concurrency

A single asyncio semaphore limits active Developer executions to two.
Reviewer calls do not occupy Developer slots. Git merge queue is serialized.
Never hold a SQLite transaction while waiting for a model/API call.

## Model routing

Use a model registry with logical aliases (manager, architect, developer, reviewer).
Actual provider/model IDs are configured by the user. Do not assume API keys or free quotas.
Reviewer should use a different provider/model family from the worker where practical.

## Phases

P0 scaffold, contracts, DB, CLI.
P1 persistent task queue and atomic task claiming.
P2 OpenCode executor integration and worktree provisioning.
P3 independent Reviewer, test gates and bounded rework.
P4 serialized merge queue and conflict handling.
P5 LangGraph planning and orchestration.
P6 dashboard and model usage.
P7 crash recovery, adversarial tests and end-to-end acceptance.
