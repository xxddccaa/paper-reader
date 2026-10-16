# Adaptive evidence recovery — 2026-10-14

DSpark stopped after a nominally completed repair because E3 quoted `𝛾 input tokens`, while the retained PDF page says `𝛾input tokens`. The old repair stage validated only the patch structure, so an ineffective proposal appeared successful before the next stage aborted the reading.

The change fixes this specific typography mismatch and adds bounded, diagnostic-driven evidence recovery to guided legacy and v3 workflows. Source matching still requires the declared source/section/page and returns the retained original span. Every repair is applied and validated before its checkpoint can succeed. Failed attempts remain visible while later attempts run; up to three proposals share the original task timeout, cancellation and tool budget. Independent factual and product reviews remain publication requirements.

## Ownership and integration

The lead used [parallel-feature-delivery](/root/.codex/skills/parallel-feature-delivery/SKILL.md) with exclusive file ownership in the shared directory.

| Task | Actual owner | Files | Acceptance |
|---|---|---|---|
| Conservative PDF typography matching | `evidence_typography` | `backend/app/evidence.py`, `backend/tests/test_source_typography.py` | Actual DSpark E3 accepted; numeric, symbol, negation and locator changes rejected |
| Adaptive host workflow | `adaptive_workflow` | `backend/app/evidence_repair.py`, `workflow.py`, `long_workflow.py`, `backend/tests/test_adaptive_evidence.py` | Failed first proposal followed by source-backed repair and publication; limits/cancellation/cache regression |
| Agent feedback and targeted rereading | `adaptive_runner` | `agent-runner/src/phases.ts`, `workflow-v3.ts`, corresponding tests | Legacy and v3 carry round feedback and original passages into actual SDK phase calls |
| UI, contracts, integration and live verification | lead | `web/src/WorkflowProgress.tsx`, `web/tests/workbench.spec.ts`, documentation | Round labels, continuing task status, integrated validation and real DSpark rerun |
| Independent read-only review | `review_titles` | No edits | Review matching, source integrity, budgets, checkpoint status, publication and test coverage |

No shared schema or database migration was needed. Repair inputs carry `evidence-recovery-v1`, invalidating incompatible repair checkpoints without changing source/extraction workflow versions. Existing compatible stages can be reused. The lead inspected baseline diffs and final code; no ownership conflict occurred. An implicit recursive TypeScript return type found during the build was fixed with an explicit `string` annotation.

## Automated validation

Tests used isolated PostgreSQL database `paper_reader_adaptive_test` and runtime artifacts under `paper-reader-runtime/test-runs/adaptive-evidence/`.

| Check | Result |
|---|---|
| Backend full `python -m pytest -q` | **473 passed** in 79.73 seconds; 6 dependency deprecation warnings |
| PostgreSQL migration, worker lease/cancellation and recovery tests | Included in the passing full backend suite |
| Frontend `npm run build` including TypeScript | **Passed** |
| Full browser `npm run test:e2e` with filesystem polling | **65 passed** |
| Runner `npm run typecheck` and `npm test` | **Passed**, 90 tests |
| Saved DSpark corpus + extraction + historical repair | **14 claims, zero evidence errors**; E3 resolves to original page 5 text |
| Independent read-only integrated review | **No confirmed blocking findings** |
| `git diff --check` | Unavailable: this project directory has no Git repository; scoped baseline diffs were inspected directly |

The adaptive tests include real Worker/JSONL subprocess execution for both legacy and v3 using explicit test model fixtures. Those tests establish orchestration behavior, not real-model reading quality. The saved DSpark regression is an offline check of real prior artifacts. The following live run is tracked separately.

## Live DSpark verification

API and worker were restarted only after confirming no queued/running jobs. Existing model and source-proxy settings were preserved. The source proxy remains `http://127.0.0.1:10826` with a 120-second timeout.

- Reading: `871f66eb-0b66-4c4b-853c-e1ef75de7864`.
- Failed source job: `923095d5-2695-4165-a789-9f2876761a39`.
- Actual retry: `8b9f791d-83da-42b2-ac64-95da5078fb73`, requested through the authenticated retry API.
- Compatible original source and extraction checkpoints were reused; no result was manually injected or marked successful.
- Live outcome: **succeeded**, revision `c8d6b95a-150f-4919-abd0-b36a1cd98777`, published on 2026-10-14. Fresh stages took approximately 140 seconds: repair 18.566 s, evidence validation 0.006 s, independent fact review 74.725 s, composition 28.027 s and product review 18.606 s. This real run passed on its first adaptive proposal; second/third-round behavior is covered by the explicit fixture tests above.
- Fourteen claims were independently reviewed. All **26 quotations passed source matching**, with no server evidence errors. This legacy PDF run did not request separate table-cell or visual-observation verification; the stored coverage warning preserves that distinction.
- The published brief contains 1,176 characters and full reading 3,449 characters (including Markdown). The live browser rendered the full reading with 36 citation buttons, opened the E3 evidence drawer, and reported no browser errors. Verification sessions logged out afterward.
- Live artifacts: `dspark-live-result.json`, `dspark-live-success.png`, and `dspark-live-evidence.png` in the adaptive-evidence test runtime. A first browser probe used a nonexistent `.markdown-body` selector; correcting the probe to the actual `article.markdown` / `.evidence-link` selectors passed without changing application code.

Open the [completed DSpark reading](http://127.0.0.1:18080/readings/871f66eb-0b66-4c4b-853c-e1ef75de7864).

Remaining boundary: recovery cannot manufacture evidence. If the original source cannot support a claim, all three proposals fail, or the shared task budget is exhausted, the task still reports the actual failure. The normal valid path has no added model call.
