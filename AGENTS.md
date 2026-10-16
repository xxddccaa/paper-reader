# Paper Reader engineering rules

Use the adjacent MemBench reference for React/TypeScript/Ant Design/Vite and FastAPI/SQLAlchemy/PostgreSQL/Alembic conventions. Keep the reference unchanged. Code belongs here; persistent operational data belongs in the sibling paper-reader-runtime directory (PAPER_READER_RUNTIME_DIR override).

Follow docs/contracts.md. Never infer workspace identity from an untrusted resource ID without checking membership. Keys are write-only and encrypted. No credentials in logs, artifacts, responses or fixtures. Real model calls must not be presented as tested when only a mock was used.

Use project-local dependencies. Changes to shared contracts require coordination with the lead. Workers edit only their assigned paths. No broad formatting, commits, publishing, or deployment during implementation. Tests use an explicitly isolated PostgreSQL database and runtime.

Validation: backend pytest, frontend typecheck/build/Playwright, runner typecheck/tests, PostgreSQL migrations, worker lease/cancellation tests, and an independent read-only review. Record externally blocked checks honestly.
