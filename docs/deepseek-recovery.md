# DeepSeek reading recovery — 2026-10-15

Reported reading: `5f488e82-e4c6-4708-9f15-0e4f83f24917`, DeepSeek-V4.1-Flash (`2609.19969v1`). Original job `93e14da2-4c2a-428d-bc02-56688dd5051d` stopped after the first segment spent 300 seconds repeatedly submitting malformed nested JSON. The second segment completed; other segments were stopped or remained pending. Original diagnostics and checkpoints are retained.

## Causes and changes

1. The real HTML table `S5.T3.5.1` declares `rowspan=16` on the Agentic label with only 15 remaining rows in its `tbody`. The old parser rejected the whole HTML and fell back to 51 PDF pages. The parser now clips effective row spans at the browser row-group boundary, while checking declared-span, column, grid-size and overlap limits. It preserves all 171 original cells of the affected table. Saved HTML parses into 415 sections, 4 tables, 13 figure entries and 9 normal extraction segments.
2. Five retained model submissions contain an extra closing brace before `open_questions` in a JSON-encoded packet string. The previous error identified only `/packet`; the model repeatedly resent the same defect. The decoder now provides bounded path, offset, line/column, nearby submitted-data and expected-structure diagnostics. It recommends native object/array arguments and preserves the full schema gate. It never repairs JSON or invents evidence on the model's behalf.
3. Guided v3 calls can explicitly request up to two completion reminders after a normal model `stop` without a valid submission, using the same Pi session and original deadline. Provider failures, cancellation and output truncation still stop the operation. Submission accounting now precedes SDK argument validation, so malformed arguments consume the same shared host budget exactly once.
4. The configured Qwen3.8-27B connection had `vision=false`. A real image request returned HTTP 200 and correctly identified a generated blue rectangle. Enabling this confirmed capability allows required figure checks. It does not establish the model's scientific visual accuracy.

The source parser version remains unchanged. Existing authenticated source snapshots are not retroactively rewritten. For this reading, the changed public model capability changes the workflow input fingerprint, so its retry reacquires source and uses the fixed HTML parser. Unrelated retries with identical configuration may continue reusing their previous source representation.

## Work allocation

The lead used `/root/.codex/skills/parallel-feature-delivery/SKILL.md` with exclusive ownership: `deepseek_source` for source parsing/tests, `deepseek_runner` for structured argument/phase recovery/tests, lead for session integration and operational verification, and `deepseek_review` for independent read-only review. No new dependencies or database schema changes were introduced.

## Validation

The retained artifacts, task map and scoped pre-change source copies are under sibling `paper-reader-runtime/test-runs/deepseek-failure/`. This checkout has no Git metadata.

- Source-focused tests: 84 passed; evidence v3 tests: 99 passed.
- Runner full suite: 127 passed; typecheck/build passed. New tests exercise the real Pi SDK against explicit local HTTP fixtures, including malformed submission feedback, corrected resubmission, completion reminders and budget exhaustion. These are not live-provider quality tests.
- Frontend typecheck/build passed; full browser suite: 74 passed.
- Full backend: 571 passed in the isolated PostgreSQL `paper_reader_deepseek_test`, including migrations, lease/cancellation and workflow tests.
- Independent read-only review: no blocking findings. The cache scope described above is retained explicitly.

## Deployment and live retry

The service was updated after checking no active jobs. A protected database/storage/config backup is retained at `preview/backups/before-deepseek-recovery-20261015T060829Z/`. The schema remains `4c7b35a912df`; health returned 200. The model connection's verified image capability was enabled through the authenticated API, then the normal retry API created job `e4c7377d-c347-4930-b24f-382a8652668f` on the original reading.

The live retry selected validated HTML. Its first extraction reproduced the malformed nested packet, received the new diagnostic, switched to native object arguments, then corrected a separately reported `baseline_value: null` schema error. It submitted successfully in 228.479 seconds and passed host evidence validation plus independent fact review. This is actual provider recovery, separate from the fixture tests. Later segments exercised evidence and factual repairs as well.

Final full-paper outcome: **failed**, with no published revision. Segments 1–6 completed extraction, host evidence validation and independent factual review (including repairs where needed). Segment 7 later exhausted its 300-second phase deadline; the fixed host workflow then stopped segments 8/9. The two original fixes and actual first-segment recovery do not establish end-to-end success. The retry's complete result is retained in `retry-job.json` / `reading-after.json`.

The user subsequently clarified that Pi should own the sustained task and adapt its plan, rather than have the host stop the reading after a prescribed phase fails. The present patch does not implement that architectural change: Python still owns the phase graph and fatal phase-timeout policy. A continuous Pi task controller with recoverable tool feedback is separate remaining work, not an achieved property of this release.

Actual source acquisition and the real image capability probe are distinct from the fixture-based recovery tests. A successful automated test does not imply a completed real paper reading.
