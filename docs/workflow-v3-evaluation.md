# Workflow v3 implementation and evaluation record

Date: 2026-10-13. Status: implementation integrated; live development and final
acceptance are still in progress. This document must not be read as a passing
ten-run matrix report until the matrix results and content review are recorded.

## Isolation and reproducibility

All checks use the sibling `paper-reader-runtime/test-runs/html-long-v3` tree,
new PostgreSQL databases ending in `_test`, and the existing Qwen3.8-27B endpoint
with explicit `qwen-chat-template` / `enable_thinking=false`. API keys are never
included in source, prompts, reports or command arguments. The endpoint ignores
authentication; the harness uses its existing non-secret placeholder option.
The existing preview database, API/worker processes and `web/dist` are retained.

The checkout has no Git metadata. `baseline-source.tar.gz` and its extracted
`baseline-tree` provide the before-image for independent source review. Upstream
skill bytes remain unchanged. `implementation.diff` is an intermediate review
aid; new files must also be read directly.

New guided jobs freeze `guided-v3.0` and `source-structured-v3`. Prior guided-v2.3
revisions retain their original parser and workflow semantics. New long jobs use
at most two concurrent segments, 240 shared tool calls, 2400 seconds overall and
300 seconds per model phase. Normal chunks use 16 KiB of original UTF-8 text;
32 KiB remains the hard ceiling. Short papers keep one extraction/review pair.

## Preserved baseline

Both baseline jobs used the unchanged runner build and the original implementation.

| Skill | Input selected | Result | Elapsed | Actual observed failure |
|---|---|---|---:|---|
| ljg-paper | DSpark 2607.05147v1 HTML | failed, no revision | 200.66 s | E19 still did not match its declared source after repair |
| DeepPaperNote | DSpark 2607.05147v1 PDF | failed, no revision | 325.23 s | extraction reached the 300-second phase limit |

Reports and full available outputs are retained in `baseline-ljg` and
`baseline-deep`. These were guided runs, not native skill completion tests.

## Development runs, separate from the fixed matrix

| Run | Result | Observation |
|---|---|---|
| development-01-ljg | failed, 305.49 s | attempted nonexistent skill files; extraction exceeded phase deadline |
| development-02-ljg | failed, 46.11 s | repeated source rereads exhausted the per-phase tool budget |
| development-03-ljg | failed | structured tool streams terminated before a valid submission; retained error events show `terminated` |
| development-04-ljg | failed | full responses arrived after aligning transport timeout, but oversized/malformed extraction needed more repair than the phase allowed |
| development-05-ljg | failed, 627.36 s | independent review mis-added a table row and invented an arithmetic contradiction; earlier completed segments were retained |
| development-06-retry | running | reuses prior extraction checkpoints; receives deterministic table arithmetic for reviews |

Implementation changed during development. These runs are not pooled into a
fixed-configuration success rate. Every failed job, stage, raw model response
and provider usage report remains available. Failed or truncated provider
responses may omit usage; a missing usage value is not evidence of zero cost.

The table-arithmetic issue was independently checked against the original cells:
the reviewer had used an incorrect sum. Production now computes numeric row
sums, means and changes from source cells, with header paths and an explicit
warning that arithmetic alone does not establish experimental comparability.
The implementation contains no paper-specific answer key or numerical fixes.

## Engineering checks so far

- Backend full suite: 380 passed before the final cache-recovery/pagination and
  table-arithmetic additions; affected follow-up tests are recorded separately.
- Long-workflow focused suite: 16 passed, including actual worker/JSONL/PostgreSQL
  execution, retry reuse, corrupted/missing attempt-copy recovery, a real PNG
  scanned-page fixture, scanned presentation reuse, table grouping and visual
  pagination. Model observations in these tests are explicitly scripted.
- Runner: 83 passed; includes actual SDK protocol tests, vision payload handling,
  output truncation, frozen legacy prompts and v3-only diagnostics.
- Frontend: typecheck and isolated build passed. V3 browser fixtures: 34 passed
  after review repairs. These browser checks use mocked HTTP and are not live
  model evidence.
- Source-only DSpark diagnostic: 330 retained sections, 33 appendix records,
  110 references, one structured table, 71,483 body bytes; all eight original
  figures rendered. PDF has 33 pages and no missing text pages. Rendering an
  image is not a model interpretation or a complete application run.

Independent review found and drove repairs for legacy revision compatibility,
multi-table omission, visual context loss, newer cache-state rollback, claim-ID
remapping, scanned reuse, source-asset protection, source-copy recovery,
pagination and UI completeness/evidence labels. The final review and final
required check totals will be appended after the implementation is frozen.

## Fixed ten-run acceptance

`tests/evaluate_long_matrix.py` defines six DSpark HTML-first runs (three per
skill), two original-DSpark-PDF uploads, and two BitFit v5 PDF regressions. Each
case has a separate database and runtime. Cases run sequentially so two jobs do
not compete with the two allowed segment sessions on the same deployment.
The plan records original PDF hashes, model parameters and implementation hashes;
implementation drift prevents remaining cases from starting.

`dspark-reference-audit.md` is an evaluation-only answer key created from actual
retained HTML/PDF/figure bytes. It must never enter production prompts, source
tools or repair inputs. It distinguishes model-specific Figure 7 workpoints,
offline accepted lengths, TPS, throughput, MTP comparison conditions, and the
full Appendix A selection-bias counterexample. Completed products will be
checked against it individually; a host `succeeded` state is not a content score.

The completed batch is in
`paper-reader-runtime/test-runs/html-long-v3/matrix-v3-02`. It finished all ten
cases with five workflow successes and five failures. The detailed per-case
table, source hashes and retained errors are in `matrix-results.json`; the
strict paper-content review is in `content-audit.md`. The four successful DSpark
HTML products passed source, evidence, chapter and figure checks, but the audit
found a repeated terminology issue in the brief/full composition around
conditional `c_k` versus cumulative prefix survival, and all four briefs exceed
the 600–1000-character target. They are therefore recorded as workflow-success
samples with partial content acceptance.

Ten runs constitute evidence only for that fixed batch, not a general stability
claim. Guided success is never reported as native success. The first interrupted
batch is retained separately in `matrix-v3-01/ABORTED.md` and is excluded from
the final ten-case denominator.
