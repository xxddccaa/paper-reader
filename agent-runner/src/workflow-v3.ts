import {createHash} from 'node:crypto';
import {Type,type TSchema,type Static} from '@sinclair/typebox';
import type {PhaseName} from './protocol.js';

const object=<T extends Record<string,TSchema>>(fields:T)=>Type.Object(fields,{additionalProperties:false});
const text=(maxLength=4000,minLength=1)=>Type.String({minLength,maxLength,pattern:minLength?'^(?=[\\s\\S]*\\S)[^\\u0000]*$':'^[^\\u0000]*$'});
const enumeration=(...values:string[])=>Type.Union(values.map(value=>Type.Literal(value)));
const claimId=()=>Type.String({pattern:'^C[1-9][0-9]{0,5}$',maxLength:7});
const evidenceId=()=>Type.String({pattern:'^E[1-9][0-9]{0,5}$',maxLength:7});
const strings=(maxItems=40,maxLength=4000)=>Type.Array(text(maxLength),{maxItems});
const sha256=()=>Type.String({pattern:'^[a-f0-9]{64}$',minLength:64,maxLength:64});
const evidenceFields={id:evidenceId(),source_id:text(200),section_id:text(200),page:Type.Optional(Type.Integer({minimum:1,maximum:100000}))};

export const packet2EvidenceSchema=Type.Union([
  object({...evidenceFields,quote:Type.Optional(text(8000,12)),selection:Type.Optional(Type.Literal('section')),evidence_type:Type.Optional(Type.Literal('text_quote'))}),
  object({...evidenceFields,quote:Type.Optional(text(8000,0)),evidence_type:Type.Literal('table_cell'),table_id:text(200),row:Type.Integer({minimum:0,maximum:100000}),column:Type.Integer({minimum:0,maximum:100000}),headers:Type.Optional(Type.Array(text(8000,0),{maxItems:100}))}),
  object({...evidenceFields,source_id:Type.Literal('main'),quote:Type.Literal(''),evidence_type:Type.Literal('image_observation'),figure_id:text(200),image_sha256:sha256(),observation_id:text(200)}),
]);
export const packet2ClaimSchema=object({
  id:claimId(),kind:enumeration('contribution','method','result','lesson','limitation','related_work'),statement:text(),
  basis:enumeration('observation','author_hypothesis','inference'),
  conditions:object(Object.fromEntries(['model','dataset','split','metric','baseline','budget'].map(key=>[key,text(1000,0)]))),
  evidence:Type.Array(packet2EvidenceSchema,{minItems:1,maxItems:6}),
  numeric_comparison:Type.Optional(Type.Union([object({value:Type.Number(),baseline_value:Type.Number(),relation:enumeration('higher','lower','equal'),unit:text(100,0)}),Type.Null()])),
});
const packet2=(maxClaims:number)=>object({version:Type.Literal(2),paper_title:text(1000),research_question:text(),claims:Type.Array(packet2ClaimSchema,{minItems:1,maxItems:maxClaims}),open_questions:Type.Array(object({question:text(1500),search_query:text(500),reason:text(1500)}),{maxItems:3}),limitations:strings()});
export const packet2Schema=packet2(80);
export const chunkExtractSchema=object({checked_section_ids:Type.Array(text(200),{maxItems:2048,uniqueItems:true}),unresolved:strings(80),packet:packet2(20)});
export const claimPatch2Schema=object({claims:Type.Array(packet2ClaimSchema,{maxItems:80}),remove_claim_ids:Type.Array(claimId(),{maxItems:80,uniqueItems:true})});
export const factReview2Schema=object({passed:Type.Boolean(),issues:Type.Array(object({claim_id:claimId(),message:text()}),{maxItems:240}),checked_claim_ids:Type.Array(claimId(),{maxItems:80,uniqueItems:true}),suggestions:strings()});
export const mergeReview2Schema=object({...factReview2Schema.properties,gap_resolutions:Type.Optional(Type.Array(object({id:text(20),status:enumeration('resolved','retained_limitation','unresolved'),explanation:text(2000),evidence_ids:Type.Array(evidenceId(),{maxItems:20,uniqueItems:true}),figure_ids:strings(20,200)}),{maxItems:160}))});
export const figureReviewSchema=object({
  observations:Type.Array(object({figure_id:text(200),image_sha256:sha256(),section_id:text(200),observation:text(8000),readable:Type.Boolean(),conditions:text(8000,0),uncertainties:strings(80),claim_ids:Type.Array(claimId(),{maxItems:80,uniqueItems:true})}),{maxItems:240}),
  issues:Type.Array(object({claim_id:claimId(),message:text()}),{maxItems:240}),passed:Type.Boolean(),
});
export type PacketEvidenceV2=Static<typeof packet2EvidenceSchema>;

export const V3_PHASE_SCHEMAS:Partial<Record<PhaseName,TSchema>>={extract:packet2Schema,chunk_extract:chunkExtractSchema,packet_repair:claimPatch2Schema,fact_review:factReview2Schema,figure_review:figureReviewSchema,merge_review:mergeReview2Schema};
export const V3_PHASE_OUTPUT_CAPS:Record<PhaseName,number>={extract:6000,chunk_extract:7500,packet_repair:7000,fact_review:6000,figure_review:3500,merge_review:5000,compose:12000,product_review:3000,product_repair:4000,revision_plan:512};
export const V3_STAGE_TOOL_CAPS:Record<PhaseName,number>={extract:24,chunk_extract:24,packet_repair:24,fact_review:32,figure_review:240,merge_review:48,compose:3,product_review:16,product_repair:6,revision_plan:3};

// An explicit allowlist keeps unrelated histories and host control fields out of
// prompts, while retaining supplied source/evidence context in repairs/writing.
export const V3_CONTEXT_FIELDS=['source_sections','source_excerpts','section_index','section_inventory','section_coverage','segment_id','segment_summaries','figure_checks','figures','visual_observations','evidence_context','evidence_index','source_selection','coverage','paper_title','unresolved','checked_section_ids','table_statistics','open_gaps','gap_resolutions'];
export const V3_INPUT_FIELDS:Partial<Record<PhaseName,string[]>>={
  chunk_extract:['source_sections','skill_guidance','segment_id','instruction','paper_title','previous_packet'],
  packet_repair:['source_sections','skill_guidance','packet','errors','review','instruction','repair_context'],
  figure_review:['figures','packet','source_sections','instruction','purpose'],
  merge_review:['packet','segment_summaries','section_index','source_sections','figure_checks','instruction','previous_review'],
};

export const packetRepairPolicy=' When repair_context is supplied, inspect its round/max_rounds, errors, unchanged_errors, diagnostics, strategy and previous_attempt before choosing a correction. The supplied packet is the current repair target; unchanged_errors identify a previous unsuccessful approach. Use diagnostic claim_id/evidence_id and declared or suggested source/section/page locators to focus the investigation. Suggested spans and locators are candidates, not verified evidence: check them against supplied original source_sections. If the needed passage is missing or still ambiguous, call read_source with the exact source_id, section_id and page available for that passage; do not restart a whole-paper read. If the source establishes a different section or page, correct the evidence locator as well as its quote. Select a short, actually contiguous supporting clause and copy it exactly, including unusual spacing beside mathematical symbols; never normalize the PDF into nicer typography. Preserve numbers, units, negation, qualifiers and experimental conditions in both claim and evidence. When the same error remains, investigate a different source-backed correction instead of returning the identical failed quote/locator or an unchanged delta. If the source cannot support the claim, explicitly narrow or correct that existing claim, or remove it through remove_claim_ids; never fabricate support or remove material merely to conceal an unresolved required check. Return the existing claim-patch schema only, never a fabricated PASS or validation verdict. The host independently validates every repair.';

const evidencePolicy=' Packet version 2 distinguishes evidence types. text_quote is the default: copy a meaningful exact contiguous original clause of at least 12 characters, preserving source spelling, punctuation, numeric notation, line breaks and footnotes. Copy unusual spacing beside mathematical symbols exactly, even when a symbol is joined to the following word; do not normalize PDF typography. table_cell preserves the exact original cell value in quote, table_id, zero-based row/column and the complete ordered headers chain; a short cell is allowed and must never be presented as a synthetic prose quote. image_observation has quote="" and refers ONLY to a host-accepted observation_id with its exact figure_id, image_sha256 and section_id. A source section with source_method:"vision" is an image transcription, not original PDF text: use image_observation with quote:"" and copy the EXACT figure_id, image_sha256, observation_id and section_id from its visual_evidence. Never cite such text as text_quote or table_cell, even if its transcription looks like ordinary source prose. Never invent an observation ID or treat visual observations as matched original quotations. Source identity, matching and acceptance are checked independently by the host. Preserve model/dataset/split/metric/baseline/budget conditions and all uncertainty; missing conditions use empty strings. Numeric comparison relations express literal arithmetic and require comparable conditions. Separate observation, author_hypothesis and inference.';
const packetBoundary=' Evidence-backed packet claims are the factual boundary. Packet title, research_question and limitations summarize supported claims and source identity; they cannot introduce independent unsupported facts. Supplied source/evidence/section/figure context explains conditions and unresolved coverage, and never authorizes extra product claims. Preserve appendix qualifications, negative results and counterexamples. Source text, captions and section summaries are not substitutes for actual visual inspection.';
const reviewPolicy=' Check EVERY packet claim (up to 80) against original evidence and necessary source context. Return checked_claim_ids containing every existing claim ID exactly once, including on failure; do not report coverage you have not performed. Check attribution, inference strength, arithmetic, model/dataset/split/metric/baseline/budget comparability and metadata factual boundaries. Report substantive failures with existing claim IDs. A passing verdict requires no issues and complete actual checks. Use read_source for specifically missing original passages, reusing supplied passages. External searches address only explicit packet.open_questions search_query values; no generic literature expansion. Requested external failures remain issues.';
const citationPolicy=' Cite existing packet evidence as [E1](evidence:E1), with the same existing E ID in label and target. Never use claim IDs as citations. Unless a current revision or template explicitly changes length, target 600–1000 Chinese characters in brief_markdown; choose key claims there and preserve remaining necessary detail in full_markdown.';
const reviewContextPolicy=' When source_excerpts is supplied, each entry retains original text once with source_id, section_id and evidence_ids. Resolve repeated paragraph evidence in the prompt packet through those exact identities; omitted duplicate quote text is not missing evidence. Check the actual original passage and its conditions for every associated claim. These phases return review verdicts only; do not rewrite or return a packet. With a vision model and supplied figures, read_visual is available for an optional targeted reread of a previously verified main-source image. Use only supplied figure IDs and preserve the exact returned identities and hashes. You may rely on supplied figure_checks without rereading every figure; only figure_review requires all images read in its own session. Do not claim a fresh image inspection unless you actually called read_visual in this session. A caption or an unreadable value does not establish an exact visual fact.';
export const V3_PHASE_INSTRUCTIONS:Record<PhaseName,string>={
  extract:'Read all supplied original source_sections and selected skill guidance. Extract a version 2 packet with 1–80 nonredundant supported claims. Retain material contributions, concrete mechanisms, formulas, qualified experiments, limitations, appendix counterexamples and transferable lessons without compressing away necessary conditions. Copy the supplied paper_title/source title. Use previous_packet and instruction for factual revisions. Default open_questions to []; add only necessary explicitly requested external checks. Native skill execution is not requested.'+evidencePolicy+packetBoundary,
  chunk_extract:'Extract only the supplied segment into {packet,checked_section_ids,unresolved}. Read EVERY supplied source section, including appendices, captions, formulas and table header metadata; acknowledge ALL supplied section IDs exactly once in checked_section_ids, including supplied reference sections (references are indexed, not silently dropped). Do not claim unseen coverage. The packet MUST be version 2 with 1–20 claims. Capture material findings, exact experiment conditions, appendix counterexamples and negative results even when they complicate the main narrative. Preserve table row header chains and associations between formulas/captions and their passages. Use segment_id and paper_title for context. unresolved means actual source ambiguity or missing coverage that prevents a supported claim; retain every genuine gap rather than guessing. Pending figure checks are scheduled later and do not themselves constitute an unresolved source fact at extraction. Missing conditions explicitly not supplied by the paper may be retained as qualified limitations with empty condition fields. Routine future work and work outside this segment do not belong in unresolved. The host retains unresolved for the full merge checks. Use read_source only for targeted necessary context. Default open_questions to [] except necessary explicitly requested external checks.'+evidencePolicy+packetBoundary,
  packet_repair:'Repair only listed host validation errors or independent review issues using supplied original source_sections, typed evidence and figure/section context. Return ONLY {claims,remove_claim_ids}: replacements and removals must use distinct EXISTING claim IDs; retain unlisted claims and packet metadata. Keep every necessary finding and its conditions; removal must not hide a failed required check or discard checked segment material. Never replace a table cell with a synthetic quote or an unread image with a guessed observation. New facts inside replacements require real evidence.'+packetRepairPolicy+evidencePolicy+packetBoundary,
  fact_review:'Independently review the packet; source_sections are original passages, with figure_checks and section context where supplied.'+reviewPolicy+evidencePolicy+packetBoundary,
  figure_review:'Call read_visual for EVERY supplied figure_id (metadata id is the figure_id when that field is absent), including page:N PDF figures, during THIS phase before submitting any result. Inspect the actual image content returned by the tool, not just its caption, path, hash or another review. Return exactly one observation for every supplied figure, with the EXACT figure_id, image_sha256 and section_id returned by read_visual. No invented or omitted figures, hashes, identities or read coverage. Record what the image actually shows, readability as a boolean, precise conditions, uncertainties and relevant existing claim_ids (empty if none). Unreadable numbers stay unresolved: do not guess values. If essential labels, numbers or relationships cannot be read, mark readable=false and state why in uncertainties. A passing review requires every image actually read and readable and no substantive issues. Uncertainties on a passing review may concern only nonessential approximate numbers; preserve their approximate nature without inventing precision. Essential unknown conditions or unresolved claim conflicts require failure. Conflicts with packet claims are issues naming existing claim IDs. Do not create host observation IDs or evidence quotes.'+evidencePolicy+packetBoundary,
  merge_review:'Independently review the combined version 2 packet using original source_sections and targeted read_source inspections, segment_summaries, section_index and figure_checks. Check ALL claims, up to 80; check cross-segment contradictions, condition preservation, appendix counterexamples, formulas/tables and missing material from segment summaries. The host already assigned globally unique claim/evidence IDs; never rename or compress the packet. Summary-only compression must not discard checked material. Missing material or unresolved required section/figure checks must remain substantive issues linked to a relevant existing claim; do not fabricate a passing coverage verdict.'+reviewPolicy+evidencePolicy+packetBoundary+reviewContextPolicy,
  compose:'Write both brief_markdown and full_markdown using the validated packet and supplied templates. Use supplied evidence, section and figure context to preserve precise conditions and limitations. Both views together must retain necessary checked material from all segments; mention required unresolved coverage honestly. Apply instruction as presentation requirements and never claim native completion.'+citationPolicy+evidencePolicy+packetBoundary,
  product_review:'Independently review BOTH product views against packet, templates and supplied source/evidence/section/figure context. Check necessary coverage across both views, preserved numerical and experimental conditions, inference labels, citation identity, and honest distinctions between quotations, table cells and visual observations. Inspect missing original passages with read_source as needed. Return coverage, template, numeric_conditions and inference_labels booleans; passed requires all four true and no issues. issues contains only actionable substantive failures, never positive commentary or optional polish. Do not rewrite the product.'+citationPolicy+evidencePolicy+packetBoundary+reviewContextPolicy,
  product_repair:'Return ONLY targeted edits {field,old_text,new_text} for substantive review errors in the frozen product. Each old_text must match exactly once in its original field; edits must not overlap. Retain unaffected text, citations and conditions. Use packet and supplied evidence/figure/section context to correct attribution and uncertainty. Do not modify the packet, create evidence or emit replacement whole documents.'+citationPolicy+evidencePolicy+packetBoundary,
  revision_plan:'Classify the instruction as presentation for changes entirely within the verified packet, or facts for new information, evidence/condition changes, comparisons or factual corrections. Choose facts if uncertain. Consider supplied section/figure coverage and previous_packet_compatible when present. Return scope and a short reason only.',
};

export function v3PhaseInstructions(phase:PhaseName,input:Record<string,any>):string{
  if(['fact_review','merge_review','packet_repair'].includes(phase))return V3_PHASE_INSTRUCTIONS[phase]+' When table_statistics is supplied, its row sums, means and relative changes were computed by the host from the original numeric table cells. Use those exact aggregates instead of mental arithmetic. Check the retained header paths and experimental conditions before comparing rows. A relative change of means and a mean of relative changes are different statistics; do not conflate them. Do not report a numerical source inconsistency or introduce a correction unless the actual retained cells and host arithmetic support it. Correctly attributed source reports are not errors merely because additional scheduled figure checks have not yet occurred.';
  if(phase==='chunk_extract')return V3_PHASE_INSTRUCTIONS[phase]+' The complete original text assigned to THIS segment and its table metadata are already supplied in input.source_sections. Read them directly and submit the extraction now; this stage has no source-fetch or skill-file tools. The supplied skill_guidance supplies the reading principles for this stage. Target 4–8 concise, nonredundant claims when sufficient, preserving all material conditions and counterexamples; use more only when needed for distinct material findings. Prefer one short, exact supporting clause per claim; avoid copying entire multi-sentence paragraphs when one clause supports the statement. numeric_comparison belongs on a CLAIM, never inside an evidence object. Omit numeric_comparison for a reported relative gain without a supplied same-condition baseline value; retain that gain accurately in the supported statement and conditions. The following independent fact review and full-paper merge retain access to the complete original paper for context from other segments. Do not invent missing context or mistake scheduled work on another segment for a missing source. Emit the required top-level checked_section_ids and unresolved fields BEFORE packet, with exact supplied section IDs; do not omit those two fields.';
  if(phase==='figure_review'&&input.purpose==='page_transcription')return V3_PHASE_INSTRUCTIONS[phase]+' This phase has purpose:"page_transcription" for a PDF page with missing extracted text. The packet may have no claims; claim_ids may be empty and no input packet schema is required. After actually reading every supplied page through read_visual, faithfully transcribe all visible substantive text into observation, including headings, table headers and rows with their relationships, and mathematics wherever readable. Preserve visible qualifiers, conditions, footnotes and negative results. Do not summarize away substantive content, infer hidden text, correct the paper or guess symbols/numbers. If essential content is unreadable, set readable=false, passed=false and explain the gaps in uncertainties. This is a supplemental image observation; the original PDF text remains unchanged.';
  if(phase==='figure_review')return V3_PHASE_INSTRUCTIONS[phase]+' All packet claims are supplied so you can identify the relevant ones; this single figure need not independently establish every claim in the paper. A schematic omitting an experimental detail is not a contradiction when the source prose supplies it. Check real disagreements in axes, legends, models, metrics and conditions. A domain category such as math/code/chat may summarize several named benchmarks; do not call that categorization a conflict by itself. Report only genuinely related claim IDs. Never promote an unreadable or estimated plot value into an exact paper fact.';
  return V3_PHASE_INSTRUCTIONS[phase];
}

export function validateVisionEvidence(evidence:any,input:Record<string,any>):void{
  for(const section of input.source_sections??[]){
    if(section.source_method!=='vision'||section.id!==evidence.section_id||(section.source_id??'main')!==evidence.source_id)continue;
    const visual=section.visual_evidence;
    if(evidence.evidence_type!=='image_observation'||evidence.quote!==''||!visual||['figure_id','image_sha256','observation_id','section_id'].some(key=>evidence[key]!==visual[key]))throw new Error('Vision-derived sections require image_observation evidence with the exact supplied visual_evidence identity');
  }
}

export interface VisualIdentity {figure_id:string;source_id:string;section_id:string;image_sha256:string}
export type VisualReads=ReadonlyMap<string,VisualIdentity>;

export function suppliedFigures(input:Record<string,any>):Map<string,any>{
  if(!Array.isArray(input.figures))throw new Error('Figure review requires supplied figures');
  const figures=new Map<string,any>();
  for(const figure of input.figures){
    const id=figure?.figure_id??figure?.id;
    if(typeof id!=='string'||!id.trim()||id.length>200||typeof figure?.section_id!=='string'||!figure.section_id.trim()||figure.section_id.length>200)throw new Error('Figure metadata requires bounded figure and section identities');
    if((figure.id!==undefined&&figure.figure_id!==undefined&&figure.id!==figure.figure_id)||figures.has(id))throw new Error('Duplicate or conflicting supplied figure identities');
    if(figure.source_id!==undefined&&figure.source_id!=='main')throw new Error('Visual figures must belong to the main source');
    figures.set(id,figure);
  }
  return figures;
}

/** Validate bytes against host identity before returning actual SDK image blocks. */
export function acceptVisualRead(figureId:string,result:any,figures:ReadonlyMap<string,any>,reads:Map<string,VisualIdentity>){
  const supplied=figures.get(figureId);
  if(!supplied)throw new Error('read_visual must address a supplied figure');
  if(!result||result.figure_id!==figureId||result.source_id!=='main'||result.section_id!==supplied.section_id)throw new Error('read_visual returned mismatched figure/source/section identity');
  if(typeof result.data!=='string'||!result.data.length||result.data.length>Math.ceil(5*1024*1024/3)*4||!['image/png','image/jpeg','image/webp'].includes(result.mime_type))throw new Error('read_visual requires a bounded actual image payload');
  const bytes=Buffer.from(result.data,'base64');
  if(!bytes.length||bytes.length>5*1024*1024||bytes.toString('base64')!==result.data)throw new Error('read_visual returned invalid image base64');
  const hash=createHash('sha256').update(bytes).digest('hex');
  if((result.sha256??result.image_sha256)!==hash||(result.image_sha256!==undefined&&result.image_sha256!==hash))throw new Error('read_visual image SHA-256 does not match its actual bytes');
  if((supplied.sha256!==undefined&&supplied.sha256!==hash)||(supplied.image_sha256!==undefined&&supplied.image_sha256!==hash)||(supplied.page!==undefined&&supplied.page!==result.page))throw new Error('read_visual differs from supplied image identity');
  if(reads.has(figureId)&&reads.get(figureId)!.image_sha256!==hash)throw new Error('Figure image changed during review');
  const identity={figure_id:figureId,source_id:'main',section_id:result.section_id,image_sha256:hash};
  const metadata={...identity,sha256:hash,mime_type:result.mime_type,...(typeof result.path==='string'?{path:result.path}:{}),...(typeof result.caption==='string'?{caption:result.caption}:{}),...(result.page!==undefined?{page:result.page}:{})};
  reads.set(figureId,identity);
  return {content:[{type:'text' as const,text:JSON.stringify(metadata)},{type:'image' as const,data:result.data as string,mimeType:result.mime_type as string}],details:{}};
}

export function validateChunkCoverage(value:any,input:Record<string,any>):void{
  if(!Array.isArray(input.source_sections)||!input.source_sections.length)throw new Error('Chunk extraction requires supplied source_sections');
  const ids=new Set<string>();
  for(const section of input.source_sections){
    if(typeof section?.id!=='string'||!section.id.trim())throw new Error('Supplied chunk sections require valid identities');
    ids.add(section.id);
  }
  if(value.checked_section_ids.length!==ids.size||value.checked_section_ids.some((id:string)=>!ids.has(id)))throw new Error('Chunk extraction must acknowledge all supplied section IDs exactly once');
  if(typeof input.paper_title==='string'&&value.packet.paper_title!==input.paper_title)throw new Error('Chunk packet must preserve the supplied paper title');
}

export function validateFigureReview(value:any,input:Record<string,any>,reads:VisualReads=new Map()):void{
  const figures=suppliedFigures(input);
  const ids=new Set((input.packet?.claims??[]).map((claim:any)=>claim.id));
  const seen=new Set<string>();
  for(const observation of value.observations){
    const id=observation.figure_id,read=reads.get(id);
    if(!figures.has(id)||seen.has(id))throw new Error('Figure review must cover each supplied figure exactly once');
    if(!read||read.figure_id!==id||read.source_id!=='main'||read.section_id!==figures.get(id).section_id||read.section_id!==observation.section_id||read.image_sha256!==observation.image_sha256)throw new Error('Figure observation requires the exact identity/hash from an actual read_visual call');
    if(observation.claim_ids.some((id:string)=>!ids.has(id)))throw new Error('Figure observation refers to an unknown claim');
    if(!observation.readable&&!observation.uncertainties.length)throw new Error('Unreadable figures must retain explicit uncertainties');
    seen.add(id);
  }
  if(seen.size!==figures.size)throw new Error('Figure review must read and cover every supplied figure');
  if(value.issues.some((issue:any)=>!ids.has(issue.claim_id)))throw new Error('Figure issue refers to an unknown claim');
  if(value.passed&&(value.issues.length||value.observations.some((observation:any)=>!observation.readable)))throw new Error('Passing figure review requires readable images and no issues');
}

// Object key order is not evidence identity; ordered header chains are.
export function evidenceIdentity(evidence:Record<string,unknown>):string{
  return JSON.stringify(Object.keys(evidence).sort().map(key=>[key,evidence[key]]));
}
