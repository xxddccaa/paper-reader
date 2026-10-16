# HTML-first structured reading, workflow v3

Approved scope: the 2026-10-13 user plan, DSpark 2607.05147v1 baseline
followed by ten fixed-configuration live runs. No upstream skill byte changes.
Source snapshot before edits: sibling runtime/test-runs/html-long-v3/baseline-source.tar.gz.
The checkout has no Git metadata; compare against this retained snapshot.

## Exclusive task map

| Owner | Allowed writes | Outcome and acceptance | Dependencies |
|---|---|---|---|
| source worker | backend/app/source_tools.py, new backend/app/structured_source.py, backend/tests/test_source_v3.py | HTML identity/version/DOM, formulas/tables, immutable representations, images, PDF coverage; focused parser/security tests | Contract below |
| evidence worker | backend/app/evidence.py, backend/app/pipeline.py, new backend/tests/test_evidence_v3.py | Versioned evidence and actual visual read tool, preserve native gates; evidence/forgery tests | Source interfaces below |
| runner worker | agent-runner/src/**, agent-runner/tests/** | v3 phases and image tool content; retain v2/native compatibility | Phase/evidence contracts below; do not rebuild dist until lead releases baseline gate |
| frontend worker | web/src/** except generated-api.ts, web/tests/** | Sources, section coverage, chart checks and honest typed evidence labels; typecheck and browser tests | Additive JSON contract below |
| lead | workflow.py, new long_workflow.py, worker.py, api.py, schemas.py, config.py, tests and docs not assigned above | Safe parallel checkpoints, frozen versions, integration, real matrix/content audit | Baselines first; integrate worker outputs; independent read-only review |

Workers must not edit other scopes or upstream skills. Tests use a new isolated
PostgreSQL database and runtime. No model calls by workers. Lead owns integration.

## Frozen versions and source interface

New guided job snapshot: workflow_version=guided-v3.0,
source_rules_version=source-structured-v3. Missing versions retain guided-v2.3
and source-sections-v2 behavior. Native remains legacy unless explicitly versioned.
SourceLibrary gains keyword defaults workflow_mode='native',
rules_version='source-sections-v2'. Pipeline construction forwards these options.
Its checkpoint rules use its selected version; do not reinterpret old sections.

Structured SourceDocument keeps existing fields and gains optional structured
metadata through coverage: source_selection {format,reason,requested_id,resolved_id},
section_inventory [{id,title,kind,parent_id?}], figures [{id,section_id,caption,
images:[{url,alt?}],page?}], tables [{id,section_id,caption,rows:[[{text,row,col,
rowspan,colspan,header,headers:[]}]],footnotes:[]}], references [{id,text}],
representations [{format,path,sha256,url,arxiv_id}]. Section records retain
id/source_id/text/page/section and add kind, parent_id, anchors and table metadata
as appropriate. Paragraph section IDs are stable DOM IDs where possible.
Original HTML/PDF representations must share the same resolved arXiv version.
Images and PDF pages are supplementary main-source representations, not external papers.

SourceLibrary.prepare_visual(figure_id: str) -> {figure_id,source_id:'main',
section_id,caption,page?,path,sha256,mime_type}; downloads validated original
HTML image or renders PDF page to bounded PNG. figure IDs also include page:N
for text-deficient PDF pages and PDF pages with figures/tables. Images are
immutable, recorded and hashed in source checkpoints. Source rules can provide
all required public interfaces in structured_source.py helpers.

## Evidence v2 and visual records

Packet version1 remains unchanged. Version2 same top-level fields, allows up to
80 claims (1–20 in segment extraction), up to six independently checked evidence
items per claim, and optional evidence_type on evidence. The packet retains its
256 KiB bound. Repeated JSON encoding of object/array arguments is decoded only
at the transport boundary; scalar values and quotes are untouched, and the
entire original schema/evidence/coverage validation then applies.
text_quote (default): existing explicit quote rules. A v2 HTML paragraph locator
may omit quote; the host then copies the complete original 12–8000-character
paragraph and records selection='section' / verification_method='section_lookup'.
Headings, tables, oversized sections and PDF pages still require explicit quotes
or their appropriate typed locators. The copied source text is not evidence of
semantic support by itself; independent review must verify that support.
table_cell: {id,source_id,section_id,
quote,evidence_type:'table_cell',table_id,row,column,headers:string[]} validates
the exact cell plus complete header chain, retains original cell value. Quote
and headers may be omitted to request materialization from the exact cell
coordinates; conflicting supplied values still fail. Minimum
12-character quote rule applies only to text_quote. image_observation:
{id,source_id,section_id,quote:'',evidence_type:'image_observation',figure_id,
image_sha256,observation_id} resolves only against host-accepted visual records.
Library.visual_observations stores these records separately from source text;
host alone writes {id,figure_id,image_sha256,section_id,observation,readable,
conditions,uncertainties,claim_ids}. Actual image hashes must be in the host
image-read ledger before accepting a visual review. No image observation is a
matched original quote. Pipeline read_visual {figure_id} invokes prepare_visual
then read_artifact, returning actual image payload plus identity metadata.
Publication exposes evidence_type and verification_method (quote_match,section_lookup,
table_cell_match,visual_observation). Retain existing native quote checks.

## Guided phases and long workflow

Existing phases remain, v3 adds chunk_extract, figure_review, merge_review.
chunk_extract input includes source_sections, skill_guidance, segment_id,
instruction, paper_title. Output {packet:version2 packet,checked_section_ids:[],
unresolved:[]}. Every supplied non-reference section must be acknowledged;
appendix counterexamples and precise experiment conditions must be retained.
No paper-specific acceptance answers go into production prompts.
Each chunk is independently quote-validated and fact-reviewed using existing
packet_repair/fact_review phases (version2 packets accepted). Completed chunks
can be reused under tenant/reading, rules, hashes and lease checks.

figure_review input {figures:[metadata],packet,source_sections,instruction}.
Runner must call read_visual for each figure in this phase. Output
{observations:[{figure_id,image_sha256,section_id,observation,readable:boolean,
conditions:string,uncertainties:string[],claim_ids:string[]}],
issues:[{claim_id,message}],passed:boolean}. Unreadable numbers stay unresolved;
no guessed values. Host validates image ledger, identities and coverage.

merge_review input {packet,segment_summaries,section_index,source_sections,
figure_checks,instruction}. source_sections contains original evidence passages
and necessary context, with read_source available for targeted full-source
inspection. Output same fact_review schema; checks all claims, cross-segment
contradictions, condition preservation and missing material from segment summaries.
Host deterministically namespaces claim/evidence IDs when combining chunks;
merge must not discard checked material through summary-only compression.

Long trigger: normalized body+appendix UTF-8 >64 KiB OR full extract/review input
exceeds safe context budget. Chunks <=32 KiB UTF-8 of original text, paragraph
boundaries preferred; the current normal working size is 16 KiB to bound
structured output and repair latency. Table row groups repeat header metadata, formulas/captions
stay associated. References are indexed, not silently lost. At most two model
chunks concurrently. Job defaults 2400s and shared 240 tool calls for v3 long
execution; every phase <=300s. Protect shared source/usage/checkpoint mutation
with locks; never share a per-phase counter. Budget/deadline/cancellation and
tenant fencing apply equally to parallel work and reuse.

Extraction receives its complete assigned original text and pinned skill
guidance directly. Only structured submission is exposed to that model phase;
independent reviews retain targeted full-source access. Header reads include
their descendant paragraphs with canonical quote spans, and image observations
are separately pageable without changing original text. Source-derived numeric
table aggregates are supplied to reviews; they do not imply scientific
comparability and never contain paper-specific acceptance answers.

## Additive API/UI projection

Revision.verification gains source_selection, section_coverage
{total,processed,complete,sections:[{id,title,kind,status,segment_ids:[]}],
segments:[{id,section_ids,status}],unresolved:[]}, figure_checks
{total,verified,complete,items:[{figure_id,section_id,caption,status,
image_sha256?,observation?,conditions?,uncertainties:[],claim_ids:[]}],unresolved:[]}.
Equivalent progress summaries appear in Job.stage_records with name
source_inventory/segment_plan/segment_N_extract/segment_N_review/figure_N_review/
merge_review; generic stage UI must remain readable for old names.
Sources and pending/failed sections/figures must also be inspectable for failed
jobs via host-owned stage record summaries. Complete success requires all
required section and figure checks plus reviews; guided native_status always
not_requested. The brief remains about 600–1000 Chinese characters.

## Validation

Preserve two baseline results before implementation dist changes. Retain all
ten fixed-config live outcomes and raw artifacts, separate parser checks,
direct model probes, full application runs and content audit. Engineering
coverage includes error HTML/version mismatch/table headers/formulas/images,
PDF missing text, appendices, recovery, parallel leases/cancellation/tenant
isolation. Run backend tests, runner tests/typecheck, web build/Playwright,
migrations and independent read-only review. Any live failure remains a failure.
