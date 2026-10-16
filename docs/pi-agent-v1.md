# Persistent Pi task contract (implementation gate)

This contract supersedes the guided phase scheduler only for snapshots with
`execution_engine: pi-agent-v1`. Historical snapshots without it remain legacy;
native mode remains available. Ordinary new guided readings, revisions and private
chat use the engine when `PAPER_READER_PI_AGENT_ENABLED` is enabled (default true).

## Ownership and dependency map

| Task | Exclusive owner / allowed files | Prerequisites | Acceptance |
| --- | --- | --- | --- |
| Persistence, migration, API | persistence agent: models.py, schemas.py, api.py, chat_api.py, config.py, new agent_state.py, backend/alembic/**, new tests/test_agent_state.py and test_agent_api.py | this contract | PostgreSQL fencing, grants, privacy, continuation, old-mode compatibility |
| Persistent Pi session | runner agent: agent-runner/** | JSONL below, existing SDK 1.1.0 | request recovery, checkpoint/resume, truncation, tools, independent review, build/tests |
| Agent tools and checks | tools agent: new backend/app/agent_tools.py, new backend/tests/test_agent_tools.py | store and review callback below | incremental evidence, versioned drafts/sources, review seals and final gate |
| Isolated scripts | sandbox agent: new backend/app/agent_sandbox.py, new backend/tests/test_agent_sandbox.py | sandbox interface below | real isolation, timeout, resource caps, no host fallback |
| UI | UI agent: web/src/** except generated-api.ts; web/tests/** | public projection below | dynamic progress, waiting/continue, private chat pending drafts, browser/build |
| Integration | lead: worker.py, chat_jobs.py, pipeline.py, source_tools.py, new agent_runtime.py, generated-api.ts, docs/**, tests/**, deployment | dependent handoffs | complete integrated and real-model acceptance |

All other paths are forbidden to each worker. No broad formatting, global generated
files, deployment or secrets in worker output. Independent read-only review after
integration. Baseline outside repository: runtime/test-runs/pi-agent-v1/baseline-*.

## Persistence / public state

One logical task uses the existing Job UUID through automatic recovery and explicit
continuation. `AgentTask.job_id` is its PK and FK; state, messages and summary are
private JSON/text. `AgentTaskEvent` is append-only, indexed by task and sequence.
State writes and event append are one transaction, fenced by running Job lease.
No credentials or model-authored passing verdicts in checkpoints.

Initial allocations: read/revise 3600 seconds / 500 tool attempts; chat 600 / 120.
Explicit continuation adds one allocation and retains accumulated usage. Heartbeats
charge elapsed running time, including interrupted model requests. Restart uses the
remaining allocation; it never resets it. Pending tool reservations count even if
the process dies. Tool attempts are idempotently reserved by tool call UUID before
SDK argument validation. Checkpoints include only complete trusted message/tool
boundaries; incomplete, length-stopped or cancelled tool arguments never execute.

`AgentStore(job_id, lease_token)` exports:
`load() -> dict` containing state, messages, summary, remaining_seconds,
remaining_tool_calls, elapsed_seconds, tool_calls;
`mutate(event_type, data, change: Callable[[dict], Any], request_id=None) -> Any`
atomically mutates state and returns/caches callback result for a request ID;
`consume(call_id, name) -> dict` reserves one attempt (idempotent);
`checkpoint(messages, summary=None) -> dict`; `tick() -> dict` charges wall time;
`wait(reason) -> None` clears lease and sets waiting; `start() -> dict` initializes
lease clock, preserving prior elapsed time. All methods fence ownership.
The state is an extensible object owned by host tools; API never accepts state.

Job public projection adds `execution_engine` (legacy or pi-agent-v1),
`agent_task` (nullable object): goal, current_action, accepted_work (string list),
issue, coverage (object), remaining_work (string list), elapsed_seconds,
remaining_seconds, tool_calls, remaining_tool_calls, can_continue.
Waiting is terminal for SSE but resumable. Generic job routes exclude chat.
POST `/workspaces/{wid}/jobs/{jid}/continue` returns `{job_id}`:
waiting/cancelled/failed new-engine tasks resume same Job with another allocation;
legacy failed/partial/cancelled create new pi-agent-v1 Job, preserve old record,
record authorized `retry_from_job_id`, refresh current public model snapshot and
enforce same reading/base/workspace and no active writer.
POST `/workspaces/{wid}/conversations/{cid}/messages/{mid}/continue` returns the
same shape as existing private retry; owner only, latest answer only, no duplicate
user message, preserve accepted task artifacts. Use the actual existing chat route
prefix if it differs; notify lead/UI at handoff.

## Runner / host JSONL

RunSpec adds `execution_engine: pi-agent-v1`, `agent` object:
`operation: run | review | subtask`, `goal`, `state`, `messages`, `summary`,
`tools: [{name,description,parameters:JSONSchema}]`, optional `review_input`.
Legacy phase/chat dispatch is bypassed for this engine, but chat input/history may
be supplied as context. Existing run identity/model/source fields remain.

Model tool definitions come from host spec. Runner forwards validated execution:
`host_agent_action {call_id,name,args}`. Before SDK argument validation it calls
`host_agent_attempt {call_id,name}`. A reservation is mandatory for execution;
host caches effects by call_id, no duplicate writes after crash.
`host_agent_checkpoint {messages,summary?}` is internal, not a model tool.
`host_agent_observation {message}` records recoverable request error/current action.
Main runner returns `{status: completed, submission: <host submit_final result>}`
or `{status: waiting, reason}`. Submit result is an opaque host-issued saved version
reference, not a full model-authored product.
Per-request timeout 300s (inside persistent runtime), overall supplied remaining
budget. Failure/length/context errors return feedback to same logical agent, which
chooses changed scope. No-progress feedback then waiting if persistent. Compact
at safe idle boundary using Pi SDK; checkpoint summary plus complete messages.
Fresh reviewer/subtask sessions use same selected model, share global budget via
host RPC; reviewer has read-only tools plus `submit_review` and returns
`{passed:boolean,issues:string[],...}`; the host alone binds/stamps the result.
At most two simultaneous auxiliary sessions. Main Agent cannot call internal host
review attestations or choose different credentials/models.

## Tools / publication

`AgentTools(pipeline, store, spec, review: Callable[[str,dict],dict])` exports
`definitions() -> list`, `call(name,args,call_id) -> dict`,
`final_result(submission) -> dict` and `review_context(kind) -> dict`.
`pipeline` is existing protected Pipeline/ChatPipeline; budget for all calls belongs
to store, not per-session counters. Host mutation wraps store.mutate; no direct DB
use in tools. Sandbox import can be lazy until sandbox handoff.
Provide source preparation/representation/index/ranged reads/local search/visual
reads, protected scholarly search/download, skill read, save plan/findings/evidence
(at most five claims each), edit section drafts with expected version, deterministic
validation, request_review, run_python, submit_final, wait_for_input.
Review kinds fact/visual/coverage/product (chat: fact/product question-scoped).
Reviews are bound to sources + findings/evidence + document hashes and invalidated
by relevant changes. Only host callback can produce accepted verdicts. Original
source versions preserved when representation changes; evidence binds source hash.
Reading requires full body+appendix coverage, references indexed, figure/table
inspection, evidence/numeric and independent fact/product/coverage/visual checks.
Chat uses saved answer draft and verified evidence, pending drafts never become
normal context. Final product uses current validated evidence; no partial formal
publication. Submit by saved document version and actual review record IDs.

## Sandbox interface

`run_python(work_dir:Path, code:str, inputs:list[str], cancelled:Event) -> dict`
returns stdout/stderr/exit_code/files and available/error metadata. Inputs must
resolve to existing regular task files; mount individually read-only under /inputs;
designated /outputs writable, no project/config/credentials mounts or network.
Bubblewrap + fixed Python/document dependencies, 60s/1GiB/16 processes/50MiB total
output. No host fallback if sandbox unsupported. Derived outputs cannot attest
original evidence. Return safe task-relative output paths for host registration.
