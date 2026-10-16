import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {Type, type TSchema} from '@sinclair/typebox';
import type {ToolDefinition} from '@earendil-works/pi-coding-agent';
import {modelSession} from './session.js';
import {SYSTEM,DEEP_ADAPTATION,LJG_ADAPTATION,PRODUCT_INSTRUCTIONS} from './adapter.js';
import type {RunSpec,Emit,ToolCall,Product} from './protocol.js';
import {NATIVE_QUALITY_PROMPT,NATIVE_READABILITY_PROMPT,REVIEW_CONTEXT_POLICY,READER_RECORD_FIELDS,nativeReviewPolicyKey} from './native-review.js';
const obj=(fields:Record<string,TSchema>)=>Type.Object(fields,{additionalProperties:false});
const str=()=>Type.String();
const optionalNumber=()=>Type.Optional(Type.Number());
const jsonRecord=Type.Record(Type.String(),Type.Unknown());
const evidenceSchema=obj({id:str(),source_id:str(),source_title:str(),source_url:Type.Optional(str()),quote:str(),section:Type.Optional(str()),page:optionalNumber(),kind:Type.Union([Type.Literal('paper'),Type.Literal('external'),Type.Literal('inference')]),verified:Type.Boolean()});
const productSchema=obj({brief_markdown:str(),full_markdown:str(),evidence_index:Type.Array(evidenceSchema),verification:jsonRecord,artifacts:Type.Array(obj({name:str(),path:str(),mime_type:str()})),provenance:jsonRecord});
const reviewSchema=obj({passed:Type.Boolean(),issues:Type.Array(str()),checks:jsonRecord});
const readabilitySchema=obj({passed:Type.Boolean(),issues:Type.Array(str()),checks:jsonRecord,reader_record:obj(Object.fromEntries(READER_RECORD_FIELDS.map(key=>[key,Type.String({minLength:1,maxLength:8000})])))});
const patchSchema=obj({path:str(),expected_sha256:Type.String({pattern:'^[a-f0-9]{64}$'}),edits:Type.Array(obj({old_text:Type.String({minLength:1}),new_text:str()}),{minItems:1,maxItems:20})});
const makeTool=(name:string,description:string,parameters:TSchema,fn:(args:any)=>Promise<any>):ToolDefinition=>({name,label:name,description,parameters,executionMode:'sequential',execute:async(_id,args)=>{const result=await fn(args);return {content:result?.data&&result?.mime_type?.startsWith('image/')?[{type:'image' as const,data:result.data,mimeType:result.mime_type}]:[{type:'text' as const,text:JSON.stringify(result)}],details:{}};}});
const textFrom=(artifact:any):string=>typeof artifact==='string'?artifact:String(artifact.content??artifact.text??'');
const hash=(text:string)=>createHash('sha256').update(text).digest('hex');
export function verifyNativeGates(skill:string, executions:Map<string,any>, review:any) {
  if(!review?.passed)throw new Error('Independent native quality/readability review has not passed');
  for(const script of skill==='deeppapernote'?['resolve_paper.py','collect_metadata.py','build_identity_contract.py','fetch_pdf.py','extract_source_text.py','extract_evidence.py','extract_pdf_assets.py','plan_figures.py','plan_figure_table_decisions.py','build_synthesis_bundle.py','lint_grounding.py','lint_note.py','write_obsidian_note.py']:['validate_note.ts']) {
    if(executions.get(script)?.exit_code!==0)throw new Error(`Native step has not succeeded: ${script}`);
  }
}
export function productIssues(product:Product):string[] {
  const issues:string[]=[];
  if(!product.brief_markdown?.trim()||!product.full_markdown?.trim())issues.push('Both template outputs are required');
  if(!Array.isArray(product.evidence_index)||!product.evidence_index.length)issues.push('Evidence index is required');
  const ids=new Set(product.evidence_index?.map(e=>e.id));
  if(ids.size!==product.evidence_index?.length)issues.push('Duplicate evidence IDs');
  for(const evidence of product.evidence_index??[])if(evidence.kind!=='inference'&&!evidence.quote.trim())issues.push(`Missing source quote: ${evidence.id}`);
  for(const md of [product.brief_markdown,product.full_markdown]){
    const citations=[...(md??'').matchAll(/\[([^\]]+)\]\(evidence:([^)]+)\)/g)].map(match=>match[2]);
    const bare=[...(md??'').matchAll(/\[(E\d+)\](?!\()/g)].map(match=>match[1]);
    if(!citations.length&&!bare.length)issues.push('Each output must cite evidence');
    for(const id of [...citations,...bare])if(!ids.has(id))issues.push(`Unknown citation: ${id}`);
  }
  return [...new Set(issues)];
}
class ToolBudgetExhausted extends Error {
  constructor(){super('Task tool budget exhausted');this.name='ToolBudgetExhausted';}
}
export async function runReading(spec:RunSpec, rpc:ToolCall, emit:Emit, signal:AbortSignal):Promise<Product> {
  const task=new AbortController();
  const cancel=()=>task.abort();
  if(signal.aborted)cancel();else signal.addEventListener('abort',cancel,{once:true});
  try{return await executeReading(spec,rpc,emit,task);}
  catch(error){
    // Pi may surface an aborted inner prompt as Cancelled. Preserve the actual
    // task-level stop reason without exposing it as a recoverable tool failure.
    if(task.signal.reason instanceof ToolBudgetExhausted)throw task.signal.reason;
    throw error;
  }finally{signal.removeEventListener('abort',cancel);}
}
async function executeReading(spec:RunSpec, rpc:ToolCall, emit:Emit, task:AbortController):Promise<Product> {
  const signal=task.signal;
  const runStartedAt=new Date().toISOString();
  let calls=0;
  let currentStage='prepare_source';
  const executions=new Map<string,any>();
  const call:ToolCall=async(name,args)=>{
    if(signal.aborted)throw new Error('Cancelled');
    if(++calls>spec.limits.max_tool_calls){
      const error=new ToolBudgetExhausted();
      task.abort(error);
      throw error;
    }
    emit({type:'progress',stage:currentStage,message:`调用工具：${name}`});
    const result=await rpc(name,args,signal);
    if(name==='run_skill_step')executions.set(String(args.script).replace(/^scripts\//,''),result);
    return result;
  };
  async function readWhole(path:string):Promise<string>{
    let content='',offset=0;
    for(let page=0;page<30;page++){
      const part=await call('read_artifact',{path,offset,limit:40000});
      content+=textFrom(part);
      if(part.next_offset===null||part.next_offset===undefined)return content;
      if(typeof part.next_offset!=='number'||part.next_offset<=offset)throw new Error('Invalid artifact pagination');
      offset=part.next_offset;
    }
    throw new Error('Artifact exceeds complete-review limit');
  }
  const tools:ToolDefinition[]=[
    makeTool('read_source','Read verified original text in character chunks. offset and limit count CHARACTERS, not tokens or pages. Default limit is 16000; maximum is 32000. Prefer large chunks for whole-paper reading and follow the returned next_offset until null. Reuse text already seen in this session instead of repeating tiny overlapping reads; never claim coverage of unread ranges.',obj({source_id:Type.Optional(str()),section_id:Type.Optional(str()),page:optionalNumber(),offset:Type.Optional(Type.Number({description:'Character offset, not a token or page number. Start at 0 and continue from the returned next_offset for the same source/section/page selection.'})),limit:Type.Optional(Type.Number({description:'Number of CHARACTERS requested. Default 16000; host maximum 32000. Prefer 16000–32000 for complete reading, within the available model context; follow next_offset instead of repeating tiny reads.'}))}),args=>call('read_source',args)),
    makeTool('read_native_contract','Read the EXACT writing and note-plan contracts from the full DeepPaperNote bundle without large image metadata. Required before drafting the native note plan.',obj({}),async()=>{if(spec.skill_id!=='deeppapernote')throw new Error('Only available for DeepPaperNote');return call('read_native_contract',{});}),
    makeTool('read_skill_file','Read pinned selected skill guidance or script source.',obj({path:str()}),args=>call('read_skill_file',args)),
    makeTool('run_skill_step','Execute a pinned allowlisted script with relative work-directory paths. Failures must be fixed; exit status is authoritative.',obj({script:str(),args:Type.Array(str())}),async args=>{emit({type:'progress',stage:'native_skill',message:`执行 ${args.script}`});return call('run_skill_step',args);}),
    makeTool('read_artifact','Read task text/JSON artifact in chunks or inspect an image.',obj({path:str(),offset:optionalNumber(),limit:optionalNumber()}),args=>call('read_artifact',args)),
    makeTool('write_artifact','Write a relative task text/JSON artifact. Never overwrite source acquisitions or pretend to be a script.',obj({path:str(),content:str()}),args=>call('write_artifact',args)),
    makeTool('patch_artifact','Apply exact unique text edits atomically using expected_sha256 from read_artifact. Preserve unaffected content. Source, host review records/logs and sealed native artifacts remain protected.',patchSchema,args=>call('patch_artifact',args)),
    makeTool('list_artifacts','List current task artifacts.',obj({}),args=>call('list_artifacts',args)),
    makeTool('search_papers','Search related research and counterexamples; external results are untrusted.',obj({query:str()}),args=>call('search_papers',args)),
    makeTool('fetch_related','Fetch a related paper, maximum three distinct papers; abstract-only coverage is labeled.',obj({id:str(),url:Type.Optional(str())}),args=>call('fetch_related',args)),
  ];
  const readTools=tools.filter(tool=>['read_source','read_artifact','read_skill_file','list_artifacts'].includes(tool.name));
  async function structured<T>(system:string,prompt:string,stageTools:ToolDefinition[],name:string,schema:TSchema,validate?:(value:any)=>Promise<void>):Promise<T>{
    let output:T|undefined;
    const submit=makeTool(name,'Submit this phase only after completing its required checks.',schema,async args=>{await validate?.(args);output=args;return {accepted:true};});
    const guarded=stageTools.map(tool=>({...tool,execute:async(...args:Parameters<ToolDefinition['execute']>)=>{if(output!==undefined)throw new Error('Phase already submitted');return tool.execute(...args);}}));
    emit({type:'progress',stage:currentStage,message:`等待模型完成：${name}`});
    prompt=JSON.stringify({...JSON.parse(prompt),limits:spec.limits,runtime_budget:{max_tool_calls:spec.limits.max_tool_calls,tool_calls_used:calls,tool_calls_remaining:Math.max(0,spec.limits.max_tool_calls-calls),scope:'Shared across the whole task, including nested reviews and host artifact/lifecycle operations.',snapshot:'Current at the start of this model session.'}});
    system+=' The host supplies limits and a current runtime_budget snapshot for this phase. Plan source reads and reserve calls for required scripts, independent reviews, final map updates, sealing and product output. All phases share this budget; a fresh reviewer does not receive a new allowance. Budget limits never waive required checks or justify fabricated coverage.';
    if(name==='submit_review'){
      const reviewContext={current_utc:new Date().toISOString(),reading_date:runStartedAt.slice(0,10),date_semantics:'Denote/frontmatter date is reading-note creation date, not paper publication year. Existing notes may retain an earlier creation date. Paper publication year is separate and must be checked against the source when relevant.'};
      prompt=JSON.stringify({...JSON.parse(prompt),review_context:reviewContext});
      system+=REVIEW_CONTEXT_POLICY;
    }
    await modelSession(spec,system,prompt,[...guarded,submit],emit,signal,{stopAfterTool:name});
    if(output===undefined)throw new Error(`Agent finished without required ${name} submission`);
    return output;
  }
  emit({type:'progress',stage:'prepare_source',message:'获取原文并建立可追溯来源',progress:5});
  const source=await call('prepare_source',{});
  const guidance=textFrom(await call('read_skill_file',{path:'SKILL.md'}));
  const registry=JSON.parse(await readFile(resolve(dirname(fileURLToPath(import.meta.url)),'../../skills/registry.json'),'utf8'));
  const selected=registry.find((item:any)=>item.id===spec.skill_id);
  if(!selected?.available)throw new Error('Selected skill is unavailable');
  const nativeStatus=await call('host_native_status',{});
  const nativeReused=spec.kind==='revise'||nativeStatus.sealed===true;
  let native:any;
  if(spec.kind==='read'&&!nativeStatus.sealed){
    currentStage='native_skill';
    emit({type:'progress',stage:'native_skill',message:`运行 ${selected.name} 的阅读与修订流程`,progress:15});
    let approved:any;
    const policyKey=nativeReviewPolicyKey(spec,selected.commit);
    const reviewTool=makeTool('review_native','Run independent content/readability reviews, reusing only host-verified matching cache entries. The host records actual ljg reader answers; do not rewrite that protected section.',obj({note_path:str(),map_path:Type.Optional(str())}),async args=>{
      const cached=await call('host_native_review_lookup',{note_path:args.note_path,...(args.map_path?{map_path:args.map_path}:{}),policy_key:policyKey});
      const passed=(report:any)=>report?.passed===true&&Array.isArray(report.issues)&&report.issues.length===0;
      let quality=passed(cached.quality)?cached.quality:undefined;
      let readability=passed(cached.readability)?cached.readability:undefined;
      if(spec.skill_id==='ljg-paper'&&readability&&!READER_RECORD_FIELDS.every(key=>typeof readability.reader_record?.[key]==='string'&&readability.reader_record[key].trim()))readability=undefined;
      const reused={quality:!!quality,readability:!!readability};
      const note=quality&&readability?undefined:await readWhole(args.note_path);
      const noteHash=note===undefined?cached.note_sha256:hash(note);
      if(typeof noteHash!=='string'||!/^[a-f0-9]{64}$/.test(noteHash))throw new Error('Native review lookup omitted the current note hash');
      if(cached.note_sha256&&noteHash!==cached.note_sha256)throw new Error('Native note changed during review lookup');
      let mapHash=cached.map_sha256;
      if(!quality){
        const map=args.map_path?await readWhole(args.map_path):'';
        if(args.map_path){mapHash=hash(map);if(cached.map_sha256&&mapHash!==cached.map_sha256)throw new Error('Native map changed during review lookup');}
        currentStage='native_quality';
        emit({type:'progress',stage:currentStage,message:'独立上下文核查原生笔记质量'});
        quality=await structured<any>(SYSTEM+NATIVE_QUALITY_PROMPT,JSON.stringify({task:'Native content quality review',phase_contract:{evaluates:'content_and_evidence',downstream_not_required:['independent_readability_records','native_validator_execution','formal_save','host_seal']},note,map}),readTools,'submit_review',reviewSchema);
        quality.passed=quality.passed&&quality.issues.length===0;
      }else emit({type:'progress',stage:'native_quality',message:'复用与正文、来源、研究记录及审阅规则匹配的质量审阅'});
      if(!quality.passed){
        approved=undefined;
        await call('host_native_attest',{note_path:args.note_path,note_sha256:noteHash,...(args.map_path?{map_path:args.map_path,map_sha256:mapHash}:{}),policy_key:policyKey,quality,readability:readability??{passed:false,issues:['Not executed because quality review failed'],checks:{executed:false}}});
        currentStage='native_skill';return {passed:false,quality,reused};
      }
      if(!readability){
        currentStage='native_readability';
        emit({type:'progress',stage:currentStage,message:'独立上下文检查原生笔记可读性'});
        readability=await structured<any>(SYSTEM+NATIVE_READABILITY_PROMPT,JSON.stringify({task:'Independent readability review',phase_contract:{evaluates:'final_note_only',creates:'independent_readability_evidence',downstream_not_required:['map_review_records','native_validator_execution','formal_save','host_seal']},note,reading_criteria:spec.skill_id==='ljg-paper'?textFrom(await call('read_skill_file',{path:'ReadingGuide.md'})).split('## 独立阅读检查')[1]?.split('## 写入与验证')[0]:'Check fluency, clear actors/actions, mechanism flow and explanatory continuity. Report actual reading obstacles only.'}),[],'submit_review',spec.skill_id==='ljg-paper'?readabilitySchema:reviewSchema);
        readability.passed=readability.passed&&readability.issues.length===0;
      }else emit({type:'progress',stage:'native_readability',message:'复用与正文、来源及审阅规则匹配的真实阅读记录'});
      const attestation=await call('host_native_attest',{note_path:args.note_path,note_sha256:noteHash,...(args.map_path?{map_path:args.map_path,map_sha256:mapHash}:{}),policy_key:policyKey,quality,readability});
      approved={passed:readability.passed,note_hash:noteHash,note_path:args.note_path,...(args.map_path?{map_path:args.map_path,map_hash:attestation.map_sha256??mapHash}:{}),quality,readability,reused,reader_record_written:attestation.reader_record_written===true};
      currentStage='native_skill';return approved;
    });
    native=await structured<any>(SYSTEM+'\n'+(spec.skill_id==='deeppapernote'?DEEP_ADAPTATION:LJG_ADAPTATION),JSON.stringify({now:new Date().toISOString(),task:'Produce and verify the native skill reading, then finish_native.',paper:spec.paper,source,skill_guidance:guidance}),[...tools,reviewTool],'finish_native',obj({note_path:str(),map_path:Type.Optional(str())}),async args=>{
      verifyNativeGates(spec.skill_id,executions,approved);
      const note=await readWhole(args.note_path);
      if(approved.note_path!==args.note_path||approved.note_hash!==hash(note))throw new Error('Final note changed after independent review; rerun review_native');
      if(spec.skill_id==='deeppapernote')for(const path of ['native/grounding.json','native/lint.json']){
        const gate=JSON.parse(await readWhole(path));
        const flags=Object.entries(gate).filter(([key])=>key.startsWith('passes_'));
        if(!flags.length||flags.some(([,value])=>value!==true))throw new Error(`Native gates are incomplete in ${path}`);
        if(path==='native/lint.json'&&gate.note_sha256!==hash(note))throw new Error('Native lint refers to an outdated note');
      }
      await call('host_native_seal',{note_path:args.note_path,...(args.map_path?{map_path:args.map_path}:{})});
    });
    native.reviews=approved;
    native.text=await readWhole(native.note_path);
  }else if(spec.kind==='read'){
    if(typeof nativeStatus.note_path!=='string'||nativeStatus.reviews?.passed!==true)throw new Error('Verified native checkpoint is incomplete');
    native={note_path:nativeStatus.note_path,map_path:nativeStatus.map_path,reviews:nativeStatus.reviews,reused:true,text:await readWhole(nativeStatus.note_path)};
    emit({type:'progress',stage:'native_recovery',message:'已验证原生检查点，复用笔记与独立评审',progress:55});
  }else{
    if(!spec.previous_revision)throw new Error('Revision requires previous result and evidence');
    await call('host_native_seal',{});
    native={reused:true,previous_revision:spec.previous_revision};
    emit({type:'progress',stage:'revision',message:'复用原文和证据，按修改要求回查',progress:35});
  }
  currentStage='product';
  emit({type:'progress',stage:'product',message:'生成模板解读并核查相关论文',progress:65});
  const productTools=tools.filter(tool=>!['run_skill_step','write_artifact','patch_artifact','read_native_contract'].includes(tool.name));
  productTools.push(makeTool('write_artifact','Write a product artifact only. Original source and native artifacts are sealed.',obj({path:str(),content:str()}),async args=>{
    if(!args.path.startsWith('product/')||args.path.split('/').includes('..'))throw new Error('Product writes must stay in product/');
    return call('write_artifact',args);
  }));
  productTools.push(makeTool('patch_artifact','Patch a product artifact only with the expected current SHA-256; native material remains sealed.',patchSchema,async args=>{
    if(!args.path.startsWith('product/')||args.path.split('/').includes('..'))throw new Error('Product patches must stay in product/');
    return call('patch_artifact',args);
  }));
  const productPrompt=JSON.stringify({task:'Write product outputs and submit_result',paper:spec.paper,source,native,template:spec.template,instruction:spec.instruction,limits:spec.limits});
  let product=await structured<Product>(SYSTEM+'\n'+PRODUCT_INSTRUCTIONS,productPrompt,productTools,'submit_result',productSchema);
  let review:any;
  for(let attempt=0;attempt<2;attempt++){
    currentStage='verification';
    emit({type:'progress',stage:'verification',message:'独立核对关键结论、数字、模板与证据',progress:85});
    const localIssues=productIssues(product);
    review=await structured<any>(SYSTEM+' Independently check product brief/full against original evidence and template. Verify key numeric claims, percentage points versus relative percentages, baseline comparability, inference labels, citations and concise transferable conditions. Use source tools to recheck factual claims. Preserve accurate qualitative findings when the source provides no exact numbers. Put optional polish under checks.suggestions; issues are substantive evidence, factual, template or comprehension failures. Return honest issues; no invented confidence scores.',JSON.stringify({product,template:spec.template,source,local_issues:localIssues}),readTools,'submit_review',reviewSchema);
    review.issues=[...localIssues,...review.issues];review.passed=review.passed&&review.issues.length===0;
    if(review.passed||attempt===1)break;
    currentStage='product_repair';
    product=await structured<Product>(SYSTEM+'\n'+PRODUCT_INSTRUCTIONS,JSON.stringify({task:'One targeted product repair, address every review issue',original:product,review,template:spec.template,instruction:spec.instruction}),productTools,'submit_result',productSchema);
  }
  await call('write_artifact',{path:'product/review.json',content:JSON.stringify(review,null,2)});
  product.evidence_index=product.evidence_index.map(e=>({...e,verified:false}));
  product.verification={...product.verification,product_review:review,native_skill:spec.kind==='read'?{completed:true,reused_from_checkpoint:nativeReused,review:native.reviews}:{reused_from_previous:true},passed:review.passed};
  product.provenance={...product.provenance,agent:'pi',agent_version:'1.1.0',skill_id:spec.skill_id,skill_commit:selected.commit,adapter_version:selected.adapter_version,native_reused:nativeReused,native_reuse_reason:spec.kind==='revise'?'revision':nativeReused?'checkpoint_recovery':'none',template_version_id:spec.template.version_id,tool_calls:calls};
  const listed=await call('list_artifacts',{});
  const files=Array.isArray(listed)?listed:(listed.files??listed.artifacts??[]);
  const paths=new Map<string,any>();
  for(const item of files){const path=typeof item==='string'?item:item.path;if(typeof path==='string'&&!path.startsWith('/')&&!path.split('/').includes('..'))paths.set(path,{name:typeof item==='string'?path:(item.name??path),path,mime_type:item.mime_type??(path.endsWith('.json')?'application/json':path.endsWith('.md')?'text/markdown':'application/octet-stream')});}
  product.artifacts=[...paths.values()];
  return product;
}
