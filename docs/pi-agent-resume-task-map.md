# Pi Agent completion task map — 2026-10-16

Shared contract: docs/pi-agent-v1.md. Existing implementation is preserved in
runtime/test-runs/pi-agent-v1/resume-baseline-20261016T072020Z.tar.gz.
No Git metadata. No worker deployment or credentials in output.

| Task/outcome | Owner | Allowed edits | Forbidden | Dependencies and acceptance |
| --- | --- | --- | --- | --- |
| Persistence/API compatibility and continuation review | persistence | agent_state.py, models.py, schemas.py, api.py, chat_api.py, config.py, migrations/**, test_agent_state.py, test_agent_api.py | runner, tools, worker, web, operational data | Existing v1 contract; PostgreSQL grants/fencing/privacy/compatibility tests; dedicated persistence_test DB |
| Persistent session correctness | runner | agent-runner/** | backend, web, shared docs, runtime | Existing JSONL v1 contract; recovery/truncation/cancel/replay/compaction; runner build/typecheck/full tests |
| Source/evidence/review tools and isolation | tools | agent_tools.py, agent_sandbox.py, test_agent_tools.py, test_agent_sandbox.py | runtime, worker, APIs, runner, web | Existing v1 tool/store contract; real isolation and final gate tests |
| Integration, frontend, full regression, real-model acceptance and deployment | lead | agent_runtime.py, worker.py, chat_jobs.py, pipeline.py, source_tools.py, web/**, docs/**, scripts/**, remaining tests and runtime operations | worker-owned paths until handoff | Integrate reports; all required gates; isolated real acceptance then preview backup/migration/restart and real takeover |
| Independent final review | reviewer after integration | read-only repository and sanitized artifacts | all edits | Complete requirements, changes and validation evidence; severity/file/line/remediation |

Implementation/review batch is parallel with exclusive file ownership. Integration,
independent review, fixes, final acceptance and deployment are sequential gates.
