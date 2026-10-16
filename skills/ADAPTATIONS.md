# Skill provenance and product adapters

## Guided workflow v2 and native execution

Guided mode uses the selected pinned skill's reading principles through independently scoped phases: extraction, evidence/fact review, composition and product review, with bounded repair deltas where required. Python chooses the phase, validates source quotes and structured claims, owns checkpoints and publishes only after the actual required gates. Each model phase runs in a fresh Pi context. Composition receives the checked packet and templates; it receives neither the original paper nor the native workflow transcript. Related-paper tools are available only to factual review when the packet contains explicit unresolved questions. Guided mode does not execute native scripts, invoke the native validator or claim native skill completion (`native_status: not_requested`).

Runner rules `guided-v2.1` bound packet fields, per-phase output tokens and available tools. The host additionally bounds source context, task time and aggregate budget. Source material is never silently truncated to obtain success. Successful structured submission ends the model session after the accepted tool result, avoiding an extra acknowledgement request. Invalid submission, output truncation and cancellation remain failures with actual usage retained.

Native mode retains the complete upstream workflow and original validator gates. `patch_artifact` enables bounded exact unique-match edits with a whole-file SHA-256 precondition and atomic validation; source, host reports and sealed artifacts remain protected. Independent native reviews use a task-scoped host cache whose policy key includes public model settings, selected skill commit and actual review rules, never the API key. Quality additionally depends on the semantic map; readability depends on the note and source. The host alone writes the real reader's structured answers into ljg-paper's existing independent-reader section and emits PASS labels only for an actual passing review. That deterministic record write alone does not trigger another model review. The unchanged original validator still runs on the final host-updated note/map before sealing. Model edits to substantive content or changes to sources/model/rules invalidate the relevant cached reviews.

The two upstream directories are pinned, unmodified copies. `<id>-provenance.json` records the upstream commit, branch and SHA-256 of every vendored source file. `LICENSE` in each directory is upstream's license. No update is downloaded at runtime. To upgrade, update the upstream files, manifest, registry and adapter together, then run native and product regressions.

| Skill | Upstream | Pin | Product adapter |
| --- | --- | --- | --- |
| DeepPaperNote 2.3.1 | https://github.com/917Dhj/DeepPaperNote | `62e91c315d926ee4d6184b069aa5efcbdfc63818` | `agent-runner/src/adapter.ts`, adapter 1.3.0 |
| ljg-paper Markdown branch | https://github.com/lijigang/ljg-skills/tree/md/skills/ljg-paper | `dae8ba8e8f513ac1be0697c3a8495b8894c82278` | `agent-runner/src/adapter.ts`, adapter 1.3.0 |

`available` means the product has a packaged adapter and required assets. It is not a model-quality certification or a claim that every paper/model combination has passed evaluation.

## DeepPaperNote

- Complete run overrides select Chinese, workspace output and Zotero off. The service's verified local PDF is the input. Scripts run without network; platform source tools mediate all acquisition and cross-paper retrieval. Unavailable external metadata is kept missing rather than invented.
- Retain native raw sections/source manifest, structural evidence, image decisions, bundle, model-generated note plan, native note, grounding lint, note lint, independent model quality/readability reviews and formal workspace save.
- Native lints are actually executed. A successful subprocess alone does not establish a passed lint: the runner inspects `passes_*` and the current note hash. The writing agent revises failed gates within task limits. Inability to pass stops native completion with a truthful failure.
- Native artifact format stays intact; arbitrary user Markdown templates are applied to a separate product result. Native success and product evidence/review status are different fields.
- Visual inspection requires a vision-capable configured model. Images are returned through a controlled artifact tool. A text-only model must never claim it inspected a figure; native mandatory gates can block a paper with unresolved visual review rather than being silently bypassed.
- Adapter 1.3.0 documents the upstream bounded recrop path, now supported by the host allowlist: pass a valid **complete** existing decision table through `plan_figure_table_decisions.py --review-decisions`, retain the original source-manifest/figures/assets arguments for host lineage, preserve all unaffected entries, and inspect each new image before recording approval. Truncated stdout and single-figure wrappers cannot replace the complete decision artifact. The upstream repair limit and failure conditions remain in force.

## ljg-paper

- Use the upstream `md` branch, retaining Markdown and paper-map version 3. The product explicitly supplies a reader with AI research/engineering background, which the upstream skill permits.
- The platform source tool tries arXiv HTML, then PDF. The native note and map are stored under this task's `native/` directory. Workspace isolation and source identity replace desktop `~/Context`, Emacs and consult-notes integration. No Emacs check is claimed.
- The pinned branch has a format mismatch: its template uses YAML/Markdown while `validate_note.ts` still parses Org metadata, headings and example blocks. The upstream validator and its map compatibility module remain unmodified. `agent-runner/src/validate-ljg.ts` bundles them into a Node entrypoint and uses `src/ljg-projection.ts` for the deterministic `markdown-to-org-v1` validation projection described below. No Bun installation is required.
- The reader review runs in a fresh Pi model session, records actual findings, and is followed by native map updates and the real validator. The note hash must still match the independently reviewed text at completion.

### Markdown validation projection (ljg adapter 1.3.0)

The original Markdown artifact is never rewritten. The adapter accepts the pinned template's YAML mapping with single-line string values; values containing colon+space and bracketed dates must be quoted. Strict YAML parsing rejects malformed syntax, duplicate keys, unknown tags, arrays, nested objects, aliases, anchors, multiline scalars and non-string values. Decoded metadata values also reject Org example delimiters, including YAML-escaped ones. Empty values fail, and missing required fields are still rejected by the original validator. `tags` is mapped to `filetags`; providing both fails as a duplicate. No values are invented or normalized into a passing result.

After CRLF line-ending normalization, metadata becomes `#+key: value`, outside-fence Markdown ATX headings become the same-depth Org headings, and closed backtick or tilde fences become Org example blocks. Fence content and all prose remain unchanged; a Markdown `#` inside a fence never becomes a heading. Fence delimiters/language markers are syntax and are not projected as prose. All fenced content is therefore subject to the original 80-column diagram check. Unterminated fences fail. Because the upstream parser scans globally, raw `#+` directive lines and column-zero star headings are rejected everywhere, including fences; use Markdown headings and `-` list bullets. Its example-block regex also matches inline delimiters, so raw `#+begin_example` and `#+end_example` substrings are rejected anywhere, case-insensitively. Bare CR and Unicode line/paragraph separators also fail to prevent hidden native lines. This explicitly limited format prevents body text from masquerading as generated metadata or sections.

The unchanged upstream `validate` function receives the projection, the **original filename**, and the unmodified map. Its errors, warnings, statistics and `ok` result are preserved. Adapter failures return `ok: false`. Validator JSON also records `adapter.version`, `projection`, `note_sha256`, `map_sha256`, `projection_sha256` and `upstream_validator_executed`. These bind the in-memory projection to the exact original bytes. The worker independently binds input hashes and protected full validator logs in its execution ledger and host seal; no generated projection file or new writable completion record is introduced.

Map fields referring to the note are exact Chinese note substrings, including identity/early-section/mechanism/result/evidence/final-section anchors, frontstage names and post-diagram examples. English source quotations belong in source-evidence fields, not note-anchor fields. Projection does not translate or repair mismatched anchors. `agent-runner/tests/fixtures/ljg-markdown` contains a valid version-3 Markdown/map fixture derived from upstream's MIT-licensed validator test, clearly marked synthetic. Tests run the actual bundled validator for success and for missing/malformed/duplicate metadata, source or identifier mismatch, anchor mismatch, oversized diagrams and syntax injection.

## Revision and security

Revisions reuse prior results, evidence and native artifacts. They can re-read sources and fetch bounded related papers, then create new product output and review artifacts. They do not claim to have rerun native checks. The platform retains immutable previous versions and validates exact quoted evidence independently of the agent's flags.

All scripts use the worker's allowlist, relative task paths and process confinement. Credentials exist only in model runtime memory; they are not stored in skills, notes, protocol logs or provenance.

## Host-attested native completion (adapter 1.1.0)

Independent review results are written through host-only RPC after the fresh reviewer actually returns. The agent cannot call these RPCs or overwrite their records. The worker binds executed script inputs and outputs, real semantic gates, reviewed note/map hashes, and image inspection hashes. It seals native material before the product stage; later edits or altered checkpoint contents fail verification. Product tools can write only under `product/`; source retrieval remains mediated. Revision tasks validate and reuse the sealed native result.

Model HTTP requests use a dedicated transport that validates every DNS answer at socket connection and dials those same checked addresses. Non-public addresses require the deployment's explicit private-model setting. Redirects and requests outside the configured model origin/base path are rejected. This applies to actual SDK calls, not only API setup probes.

## Phase-aware reviews (adapter 1.2.0)

Content review checks evidence, factual claims, contribution, mechanism and required substantive coverage. It does not demand completed downstream readability records, validator execution, formal save or host seals. The fresh readability context creates the independent reader evidence from the note alone; the host still enforces all final native checks before sealing. This removes a circular dependency without accepting fabricated completion records.

Accurately attributed qualitative findings remain qualitative when the paper supplies no exact numbers. Reviewers must not demand or invent unavailable numeric precision. Optional polish and additional examples are suggestions; factual errors, unsupported claims, missing required substantive content and actual comprehension barriers remain blocking issues.
