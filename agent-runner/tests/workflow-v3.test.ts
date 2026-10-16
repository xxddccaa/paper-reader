import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createServer} from 'node:http';
import {once} from 'node:events';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Value} from '@sinclair/typebox/value';
import {runPhase,validatePhaseResult,phaseSchema} from '../src/phases.js';
import {modelSession} from '../src/session.js';
import {decodeStructuredArguments,structuredWireSchema} from '../src/structured-arguments.js';
import {assertRunSpec,PHASE_NAMES,WORKFLOW_V3,type RunSpec,type PhaseName} from '../src/protocol.js';
import {packet2EvidenceSchema,V3_PHASE_OUTPUT_CAPS,acceptVisualRead,suppliedFigures,type VisualIdentity} from '../src/workflow-v3.js';

const context={workflowVersion:WORKFLOW_V3};
const quote={id:'E1',source_id:'main',section_id:'s1',quote:'The measured effect depends on the evaluation conditions.'};
const cell={id:'E2',source_id:'main',section_id:'table-s',quote:'0.8',evidence_type:'table_cell',table_id:'t1',row:1,column:2,headers:['Evaluation','Score']};
const imageData='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=';
const imageHash=createHash('sha256').update(Buffer.from(imageData,'base64')).digest('hex');
const visual={id:'E3',source_id:'main',section_id:'s1',quote:'',evidence_type:'image_observation',figure_id:'fig:1',image_sha256:imageHash,observation_id:'V1'};
const claim={id:'C1',kind:'result',statement:'结果依赖评估条件。',basis:'observation',conditions:{model:'',dataset:'',split:'',metric:'',baseline:'',budget:''},evidence:[quote]};
const packet={version:2,paper_title:'Synthetic source fixture',research_question:'Which conditions constrain the measured effect?',claims:[claim],open_questions:[],limitations:[]};
const sections=[{id:'s1',source_id:'main',text:quote.quote,kind:'paragraph',section:'Result'},{id:'appendix',source_id:'main',text:'A different condition produces a negative result.',kind:'appendix',section:'Appendix'}];
const figures=[{id:'fig:1',section_id:'s1',caption:'A synthetic plot.'},{figure_id:'page:2',section_id:'appendix',caption:'A synthetic PDF page.',page:2}];
const observation=(figure:any)=>({figure_id:figure.id??figure.figure_id,image_sha256:imageHash,section_id:figure.section_id,observation:'Visible relationships depend on the labeled conditions.',readable:true,conditions:'The conditions printed in the fixture.',uncertainties:[],claim_ids:['C1']});
const figureReport={observations:figures.map(observation),issues:[],passed:true};
const imageResult=(figure:any)=>({figure_id:figure.id??figure.figure_id,source_id:'main',section_id:figure.section_id,caption:figure.caption,...(figure.page?{page:figure.page}:{}),path:'source/figure.png',type:'image',mime_type:'image/png',data:imageData,sha256:imageHash});
const product={brief_markdown:'结果依赖条件。[E1](evidence:E1)',full_markdown:'完整结果依赖评估条件。[E1](evidence:E1)'};
const template={brief_markdown:'# Brief',full_markdown:'# Full',instructions:'Preserve uncertainty.'};
const outputs:Record<PhaseName,any>={extract:packet,chunk_extract:{packet,checked_section_ids:['s1','appendix'],unresolved:[]},packet_repair:{claims:[claim],remove_claim_ids:[]},fact_review:{passed:true,issues:[],checked_claim_ids:['C1'],suggestions:[]},merge_review:{passed:true,issues:[],checked_claim_ids:['C1'],suggestions:[]},figure_review:figureReport,compose:product,product_review:{passed:true,issues:[],checks:{coverage:true,template:true,numeric_conditions:true,inference_labels:true}},product_repair:{edits:[{field:'brief_markdown',old_text:'结果依赖条件。',new_text:'结果受条件约束。'}]},revision_plan:{scope:'presentation',reason:'Only presentation changes.'}};
const makeSpec=(base:string,dir:string,phase:PhaseName,input:Record<string,unknown>):RunSpec=>({type:'run',run_id:'v3-fixture',kind:'read',skill_id:'ljg-paper',skill_root:dir,work_dir:dir,workflow_mode:'guided',workflow_version:WORKFLOW_V3,source_rules_version:'source-structured-v3',phase:{name:phase,input},model:{api_key:'fixture-only-key',protocol:'openai-completions',base_url:base,model:'offline-fixture',vision:true,context_window:128000,max_tokens:16000,allow_private_models:true},paper:{id:'paper',title:packet.paper_title,source_type:'pdf'},template,limits:{timeout_seconds:10,max_tool_calls:80,max_related_papers:3}});
const payload=(body:any)=>{const content=body.messages.find((message:any)=>message.role==='user').content;return JSON.parse(typeof content==='string'?content:content.filter((part:any)=>part.type==='text').map((part:any)=>part.text).join(''));};
function sendTool(res:any,name:string,args:any,index=0,protocol='openai-completions',finish='tool_calls'){
  res.writeHead(200,{'Content-Type':'text/event-stream'});
  if(protocol==='anthropic-messages'){
    const event=(value:any)=>res.write(`event: ${value.type}\ndata: ${JSON.stringify(value)}\n\n`);
    event({type:'message_start',message:{id:`msg_${index}`,type:'message',role:'assistant',content:[],model:'offline-fixture',stop_reason:null,stop_sequence:null,usage:{input_tokens:10,output_tokens:0}}});
    event({type:'content_block_start',index:0,content_block:{type:'tool_use',id:`call_${index}`,name,input:{}}});
    event({type:'content_block_delta',index:0,delta:{type:'input_json_delta',partial_json:JSON.stringify(args)}});
    event({type:'content_block_stop',index:0});
    event({type:'message_delta',delta:{stop_reason:'tool_use',stop_sequence:null},usage:{output_tokens:5}});
    event({type:'message_stop'});res.end();return;
  }
  const base={id:`fixture_${index}`,object:'chat.completion.chunk',created:1,model:'offline-fixture'};
  res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{tool_calls:[{index:0,id:`call_${index}`,type:'function',function:{name,arguments:typeof args==='string'?args:JSON.stringify(args)}}]},finish_reason:null}]})+'\n\n');
  res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:finish}],usage:{prompt_tokens:10,completion_tokens:5,total_tokens:15}})+'\n\n');res.end('data: [DONE]\n\n');
}
async function fixture(handler:(body:any,res:any,index:number)=>void,run:(base:string,dir:string,bodies:any[])=>Promise<void>){
  const dir=await mkdtemp(join(tmpdir(),'runner-v3-'));const bodies:any[]=[],errors:unknown[]=[];
  const server=createServer((req,res)=>{void(async()=>{let data='';for await(const chunk of req)data+=chunk;const body=JSON.parse(data);bodies.push(body);handler(body,res,bodies.length-1);})().catch(error=>{errors.push(error);res.writeHead(500);res.end();});});
  server.listen(0,'127.0.0.1');await once(server,'listening');
  try{await run(`http://127.0.0.1:${(server.address() as any).port}/v1`,dir,bodies);assert.deepEqual(errors,[]);}
  finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
}

test('v3 transport decodes nested JSON containers without scalar coercion or quote changes',async()=>{
  const strict=phaseSchema('chunk_extract',WORKFLOW_V3);
  const nested={...packet,claims:JSON.stringify([{...claim,conditions:JSON.stringify(claim.conditions),evidence:[{...quote,quote:'The source literally contains {"json": true} in this quotation.'}]}])};
  const wire={checked_section_ids:JSON.stringify(['s1','appendix']),unresolved:'[]',packet:JSON.stringify(nested)};
  assert.equal(Value.Check(structuredWireSchema(strict),wire),true);
  const parsed=decodeStructuredArguments(strict,wire);
  assert.equal(Value.Check(strict,parsed.value),true);
  assert.equal(parsed.value.packet.claims[0].evidence[0].quote,'The source literally contains {"json": true} in this quotation.');
  assert.ok(parsed.decoded.includes('/packet/claims/0/conditions'));
  const review=phaseSchema('fact_review',WORKFLOW_V3);
  const forged=decodeStructuredArguments(review,{passed:'true',issues:'[]',checked_claim_ids:'["C1"]',suggestions:'[]'});
  assert.equal(Value.Check(review,forged.value),false);
  assert.throws(()=>decodeStructuredArguments(strict,{...wire,packet:'{broken'}),/Invalid JSON/);
  await fixture((_body,res)=>sendTool(res,'submit_phase_result',wire),async(base,dir,bodies)=>{
    const events:any[]=[],rpcNames:string[]=[];
    const output=await runPhase(makeSpec(base,dir,'chunk_extract',{source_sections:sections,skill_guidance:'Fixture principles'}),async(name)=>{rpcNames.push(name);return {accepted:true};},event=>events.push(event),new AbortController().signal);
    assert.deepEqual(output,parsed.value);assert.equal(bodies.length,1);
    assert.deepEqual(rpcNames,['host_phase_submission']);
    assert.ok(events.some(event=>event.type==='progress'&&event.message.includes('JSON')));
  });
});

test('packet2 discriminates typed evidence, preserving short/empty table cells and exact image identities',()=>{
  for(const item of [quote,{...quote,evidence_type:'text_quote'},cell,{...cell,quote:'',headers:['','Score']},visual])assert.equal(Value.Check(packet2EvidenceSchema,item),true);
  const {quote:paragraphText,...paragraphLocator}=quote;
  const {quote:cellText,headers:cellHeaders,...cellLocator}=cell;
  for(const item of [paragraphLocator,{...paragraphLocator,selection:'section'},cellLocator])assert.equal(Value.Check(packet2EvidenceSchema,item),true,'existing optional paragraph/cell locators remain accepted');
  for(const item of [{...quote,quote:'0.8'},{...quote,quote:' '.repeat(12)},{...cell,row:-1},{...cell,column:1.5},{...cell,headers:'Score'},{...visual,quote:'A fabricated source quotation.'},{...visual,source_id:'external'},{...visual,image_sha256:'unknown'},{...visual,verified:true},{...quote,evidence_type:'unknown'}])assert.equal(Value.Check(packet2EvidenceSchema,item),false);
  const typed={...packet,claims:[{...claim,evidence:[quote,cell,visual]}]};
  validatePhaseResult('extract',typed,{},context);
  assert.throws(()=>validatePhaseResult('extract',typed,{}),/Invalid/);
  const changedHeader={...packet,claims:[{...claim,evidence:[cell]},{...claim,id:'C2',evidence:[{...cell,headers:['Other header']}]}]};
  assert.throws(()=>validatePhaseResult('extract',changedHeader,{},context),/identical/);
  const changedHash={...packet,claims:[{...claim,evidence:[visual]},{...claim,id:'C2',evidence:[{...visual,image_sha256:'a'.repeat(64)}]}]};
  assert.throws(()=>validatePhaseResult('extract',changedHash,{},context),/identical/);
  assert.throws(()=>validatePhaseResult('extract',{...packet,claims:[{...claim,numeric_comparison:{value:1,baseline_value:2,relation:'higher',unit:''}}]}, {},context),/relation/);
  assert.throws(()=>validatePhaseResult('packet_repair',{claims:[{...claim,id:'C2',evidence:[{...cell,headers:['Changed']}]}],remove_claim_ids:[]},{packet:{...packet,claims:[{...claim,evidence:[cell]},{...claim,id:'C2',evidence:[cell]}]}},context),/identical/);
});

test('merge pending questions cannot disappear or close without retained support',()=>{
  const input={packet,open_gaps:[{id:'G1',question:'Pending appendix'}],figure_checks:{items:[]}};
  const base={passed:true,issues:[],checked_claim_ids:['C1'],suggestions:[]};
  assert.throws(()=>validatePhaseResult('merge_review',base,input,context),/every pending/);
  const resolution={id:'G1',status:'resolved',explanation:'The retained appendix evidence answers the question.',evidence_ids:['E1'],figure_ids:[]};
  validatePhaseResult('merge_review',{...base,gap_resolutions:[resolution]},input,context);
  for(const bad of [{...resolution,evidence_ids:[]},{...resolution,status:'unresolved'},{...resolution,figure_ids:['missing']},{...resolution,id:'G2'}])assert.throws(()=>validatePhaseResult('merge_review',{...base,gap_resolutions:[bad]},input,context));
});

test('chunk extraction caps 20 claims and acknowledges every supplied section without invented coverage',()=>{
  const claims=Array.from({length:20},(_,index)=>({...claim,id:`C${index+1}`}));
  const input={source_sections:[...sections,{id:'refs',source_id:'main',text:'Indexed reference.',kind:'references'}],paper_title:packet.paper_title};
  const output={packet:{...packet,claims},checked_section_ids:['s1','appendix','refs'],unresolved:['An essential experimental condition is not stated.']};
  validatePhaseResult('chunk_extract',output,input,context);
  for(const ids of [['s1','refs'],['s1','appendix','invented'],['s1','appendix','refs','refs']])assert.throws(()=>validatePhaseResult('chunk_extract',{...output,checked_section_ids:ids},input,context),/Invalid|all supplied/);
  assert.throws(()=>validatePhaseResult('chunk_extract',{...output,packet:{...packet,claims:[...claims,{...claim,id:'C21'}]}},input,context),/Invalid/);
  assert.throws(()=>validatePhaseResult('chunk_extract',{...output,packet:{...packet,version:1}},input,context),/Invalid/);
  assert.throws(()=>validatePhaseResult('chunk_extract',{...output,packet:{...packet,paper_title:'Different title'}},input,context),/paper title/);
  validatePhaseResult('chunk_extract',output,{...input,source_sections:[...input.source_sections,{...sections[0],source_offset:100}]},context);
});

test('merge/factual reviews check all 80 claims and typed repairs stay within existing IDs',()=>{
  const claims=Array.from({length:80},(_,index)=>({...claim,id:`C${index+1}`}));
  const input={packet:{...packet,claims}};
  validatePhaseResult('extract',input.packet,{},context);
  for(const phase of ['fact_review','merge_review'] as const){
    const review={passed:true,issues:[],checked_claim_ids:claims.map(claim=>claim.id),suggestions:[]};
    validatePhaseResult(phase,review,input,context);
    for(const checked of [review.checked_claim_ids.slice(0,20),[...review.checked_claim_ids.slice(1),'C81'],[...review.checked_claim_ids.slice(1),'C2']])assert.throws(()=>validatePhaseResult(phase,{...review,passed:false,checked_claim_ids:checked},input,context),/Invalid|every packet claim/);
    assert.throws(()=>validatePhaseResult(phase,{...review,issues:[{claim_id:'C81',message:'Unknown claim.'}]},input,context),/unknown/);
    assert.throws(()=>validatePhaseResult(phase,{...review,issues:[{claim_id:'C1',message:'Conflict.'}]},input,context),/cannot contain issues/);
  }
  validatePhaseResult('packet_repair',{claims,remove_claim_ids:[]},input,context);
  assert.throws(()=>validatePhaseResult('extract',{...packet,claims:[...claims,{...claim,id:'C81'}]}, {},context),/Invalid/);
  assert.throws(()=>validatePhaseResult('packet_repair',{claims:[{...claim,id:'C81'}],remove_claim_ids:[]},input,context),/existing/);
});

test('visual ledger rejects missing bytes, forged hashes/identities and unread observations',()=>{
  const input={figures,packet},reads=new Map<string,VisualIdentity>();
  const expected=suppliedFigures(input);
  for(const result of [{...imageResult(figures[0]),data:undefined},{...imageResult(figures[0]),data:'not base64'},{...imageResult(figures[0]),sha256:'f'.repeat(64)},{...imageResult(figures[0]),figure_id:'invented'},{...imageResult(figures[0]),source_id:'external'},{...imageResult(figures[0]),section_id:'wrong'},{...imageResult(figures[0]),mime_type:'text/plain'}])assert.throws(()=>acceptVisualRead('fig:1',result,expected,reads),/read_visual/);
  assert.equal(reads.size,0);
  assert.throws(()=>validatePhaseResult('figure_review',figureReport,input,{...context,visualReads:reads}),/actual read_visual/);
  for(const figure of figures)acceptVisualRead(figure.id??figure.figure_id!,imageResult(figure),expected,reads);
  validatePhaseResult('figure_review',figureReport,input,{...context,visualReads:reads});
  const withObservation=(patch:any)=>({...figureReport,observations:[{...figureReport.observations[0],...patch},figureReport.observations[1]]});
  for(const patch of [{section_id:'wrong'},{image_sha256:'b'.repeat(64)},{figure_id:'unknown'},{claim_ids:['C9']},{readable:false},{readable:false,uncertainties:['Essential labels cannot be read.']}])assert.throws(()=>validatePhaseResult('figure_review',withObservation(patch),input,{...context,visualReads:reads}),/identity|supplied|unknown|uncertainties|readable/);
  validatePhaseResult('figure_review',withObservation({uncertainties:['The nonessential position is approximate.']}),input,{...context,visualReads:reads});
  validatePhaseResult('figure_review',{...withObservation({readable:false,uncertainties:['Essential labels are unreadable.']}),passed:false},input,{...context,visualReads:reads});
  assert.throws(()=>suppliedFigures({figures:[figures[0],figures[0]]}),/Duplicate/);
});

test('v3 phase prompts forward evidence/figure/section context through repairs and product with frozen host caps',async()=>{
  await fixture((body,res,index)=>sendTool(res,'submit_phase_result',outputs[payload(body).phase as PhaseName],index),async(base,dir,bodies)=>{
    const repair_context={round:2,max_rounds:3,errors:['E1: quote mismatch'],unchanged_errors:['E1: quote mismatch'],diagnostics:[{evidence_id:'E1',declared:{source_id:'main',section_id:'s1'}}]};
    const common={packet,product,template,source_sections:sections,skill_guidance:'Pinned reading principles.',segment_id:'segment_1',paper_title:packet.paper_title,instruction:'Preserve all material conditions.',segment_summaries:[{segment_id:'segment_1',findings:['A negative appendix finding.']}],section_index:[{id:'appendix',kind:'appendix'}],section_coverage:{complete:true},figure_checks:{items:figureReport.observations,complete:true},evidence_context:{marker:'TYPED_EVIDENCE'},visual_observations:[{id:'V1',...figureReport.observations[0]}],errors:[],review:{issues:[]},private_history:'DO_NOT_FORWARD',rules_version:'UNTRUSTED_OVERRIDE'};
    for(const phase of PHASE_NAMES.filter(phase=>phase!=='figure_review')){
      const events:any[]=[];
      const output=await runPhase(makeSpec(base,dir,phase,{...common,repair_context}),async(name)=>{assert.equal(name,'host_phase_submission');return {accepted:true};},event=>events.push(event),new AbortController().signal);
      assert.deepEqual(output,outputs[phase]);
      const body=bodies.at(-1),prompt=payload(body);
      assert.equal(body.max_tokens,V3_PHASE_OUTPUT_CAPS[phase]);assert.equal(prompt.rules_version,WORKFLOW_V3);assert.equal(prompt.workflow_version,WORKFLOW_V3);assert.equal(prompt.source_rules_version,'source-structured-v3');
      for(const key of ['source_sections','figure_checks','section_index','section_coverage','segment_summaries','evidence_context','visual_observations'])assert.deepEqual(prompt.input[key],(common as any)[key],`${phase}/${key}`);
      assert.equal(prompt.input.private_history,undefined);assert.equal(prompt.input.rules_version,undefined);
      assert.deepEqual(prompt.input.repair_context,phase==='packet_repair'?repair_context:undefined,'repair feedback belongs only in the repair session');
      const system=body.messages.find((message:any)=>message.role==='system').content;
      assert.doesNotMatch(system,/DSpark|2607\.05147|BitFit|Synthetic source fixture/);
      if(phase==='merge_review')assert.match(system,/cross-segment contradictions/);
      if(phase==='chunk_extract')assert.match(system,/1–20 claims/);
      if(phase!=='revision_plan')assert.match(system,/source_method:"vision"/);
      const trace=events.filter(event=>event.type==='model_output');assert.equal(trace.length,1);
      assert.deepEqual(Object.keys(trace[0]).sort(),['content','phase','stop_reason','type']);assert.equal(trace[0].phase,phase);
      assert.deepEqual(trace[0].content.find((part:any)=>part.type==='toolCall').arguments,outputs[phase]);
      assert.ok(events.indexOf(trace[0])<events.findIndex(event=>event.type==='usage'));
      assert.doesNotMatch(JSON.stringify(trace),/fixture-only-key|Pinned reading principles|TYPED_EVIDENCE/);
    }
  });
});

test('figure review uses real OpenAI and Anthropic image blocks with separate exact metadata',async()=>{
  for(const protocol of ['openai-completions','anthropic-messages'] as const){
    await fixture((_body,res,index)=>sendTool(res,index<2?'read_visual':'submit_phase_result',index<2?{figure_id:figures[index].id??figures[index].figure_id}:figureReport,index,protocol),async(base,dir,bodies)=>{
      const calls:string[]=[],spec=makeSpec(base,dir,'figure_review',{figures,packet,source_sections:sections});spec.model.protocol=protocol;
      const result=await runPhase(spec,async(name,args)=>{calls.push(name);if(name==='host_phase_submission')return {accepted:true};assert.equal(name,'read_visual');return imageResult(figures.find(figure=>(figure.id??figure.figure_id)===args.figure_id)!);},()=>{},new AbortController().signal);
      assert.deepEqual(result,figureReport);assert.deepEqual(calls,['read_visual','read_visual','host_phase_submission']);assert.equal(bodies.length,3);
      const body=bodies[2],serialized=JSON.stringify(body.messages);
      assert.match(serialized,/image_sha256/);assert.match(serialized,/fig:1/);assert.match(serialized,/page:2/);
      if(protocol==='openai-completions'){
        const parts=body.messages.flatMap((message:any)=>Array.isArray(message.content)?message.content:[]);
        const images=parts.filter((part:any)=>part.type==='image_url');assert.equal(images.length,2);
        assert.equal(images[0].image_url.url,`data:image/png;base64,${imageData}`);
        for(const message of body.messages.filter((message:any)=>message.role==='tool'))assert.ok(!JSON.stringify(message.content).includes(imageData));
      }else{
        const results=body.messages.flatMap((message:any)=>Array.isArray(message.content)?message.content:[]).filter((part:any)=>part.type==='tool_result');
        const parts=results.flatMap((result:any)=>result.content),images=parts.filter((part:any)=>part.type==='image');
        assert.equal(images.length,2);assert.deepEqual(images[0].source,{type:'base64',media_type:'image/png',data:imageData});
        assert.ok(parts.filter((part:any)=>part.type==='text').every((part:any)=>!part.text.includes(imageData)));
      }
    });
  }
});

test('merge/product reviews optionally reread one whitelisted image through both SDK transports',async()=>{
  for(const phase of ['merge_review','product_review'] as const)for(const protocol of ['openai-completions','anthropic-messages'] as const){
    await fixture((_body,res,index)=>sendTool(res,index===0?'read_visual':'submit_phase_result',index===0?{figure_id:'fig:1'}:outputs[phase],index,protocol),async(base,dir,bodies)=>{
      const hostCalls:string[]=[],input={figures,packet,product,source_sections:sections,figure_checks:{items:figureReport.observations.map(item=>({...item,status:'verified'})),complete:true}};
      const spec=makeSpec(base,dir,phase,input);spec.model.protocol=protocol;spec.limits.max_tool_calls=2;
      const output=await runPhase(spec,async(name,args)=>{hostCalls.push(name);if(name==='host_phase_submission')return {accepted:true};assert.equal(name,'read_visual');assert.deepEqual(args,{figure_id:'fig:1'});return imageResult(figures[0]);},()=>{},new AbortController().signal);
      assert.deepEqual(output,outputs[phase]);assert.equal(bodies.length,2);assert.deepEqual(hostCalls,['read_visual','host_phase_submission']);
      const messages=bodies[1].messages;
      const parts=messages.flatMap((message:any)=>Array.isArray(message.content)?message.content:[]);
      const images=protocol==='openai-completions'?parts.filter((part:any)=>part.type==='image_url'):parts.filter((part:any)=>part.type==='tool_result').flatMap((part:any)=>part.content).filter((part:any)=>part.type==='image');
      assert.equal(images.length,1,'optional rereading must not require the second supplied figure');
      if(protocol==='openai-completions'){
        assert.equal(images[0].image_url.url,`data:image/png;base64,${imageData}`);
        assert.ok(messages.filter((message:any)=>message.role==='tool').every((message:any)=>!JSON.stringify(message.content).includes(imageData)));
      }else{
        assert.deepEqual(images[0].source,{type:'base64',media_type:'image/png',data:imageData});
        assert.ok(parts.filter((part:any)=>part.type==='tool_result').flatMap((part:any)=>part.content).filter((part:any)=>part.type==='text').every((part:any)=>!part.text.includes(imageData)));
      }
      assert.match(JSON.stringify(messages),new RegExp(imageHash));
      assert.deepEqual(input.figures,figures,'binding prior hashes must not modify the host input');
    });
  }
});

test('merge/product reviews retain source_excerpts once and can submit without rereading any images',async()=>{
  const {quote:originalText,...locator}=quote;
  const compactPacket={...packet,claims:[{...claim,evidence:[locator]},{...claim,id:'C2',evidence:[{...locator,id:'E9'}]}]};
  const source_excerpts=[{source_id:'main',section_id:'s1',text:originalText,evidence_ids:['E1','E9']}];
  for(const phase of ['merge_review','product_review'] as const){
    const expected=phase==='merge_review'?{...outputs.merge_review,checked_claim_ids:['C1','C2']}:outputs.product_review;
    const wire=phase==='merge_review'?{...expected,issues:'[]',checked_claim_ids:'["C1","C2"]',suggestions:'[]'}:{...expected,issues:'[]',checks:JSON.stringify(expected.checks)};
    await fixture((_body,res,index)=>sendTool(res,'submit_phase_result',wire,index),async(base,dir,bodies)=>{
      const input={packet:compactPacket,product,figures,figure_checks:{complete:true,items:figureReport.observations},source_excerpts,table_statistics:[{table_id:'t1',mean:0.8}]};
      const spec=makeSpec(base,dir,phase,input);spec.limits.max_tool_calls=1;
      const hostCalls:string[]=[],events:any[]=[];
      assert.deepEqual(await runPhase(spec,async(name)=>{hostCalls.push(name);return {accepted:true};},event=>events.push(event),new AbortController().signal),expected);
      assert.deepEqual(hostCalls,['host_phase_submission']);assert.equal(bodies.length,1);
      const body=bodies[0],prompt=payload(body),system=body.messages.find((message:any)=>message.role==='system').content;
      assert.deepEqual(prompt.input.source_excerpts,source_excerpts);assert.deepEqual(prompt.input.packet,compactPacket);
      assert.equal(prompt.input.source_sections,undefined);assert.equal(JSON.stringify(prompt.input).split(originalText).length-1,1);
      assert.ok(body.tools.some((tool:any)=>tool.function.name==='read_visual'));
      assert.match(system,/resolve repeated paragraph evidence/i);assert.match(system,/review verdicts only/);
      if(phase==='merge_review')assert.match(system,/row sums, means and relative changes were computed by the host/);
      assert.ok(events.some(event=>event.type==='progress'&&event.message.includes('JSON')),'existing wire normalization still applies');
    });
  }
});

test('optional visual rereads reject unknown IDs, wrong identities, byte hashes and changes since verification',async()=>{
  const changedData=Buffer.concat([Buffer.from(imageData,'base64'),Buffer.from([0])]).toString('base64');
  const changedHash=createHash('sha256').update(Buffer.from(changedData,'base64')).digest('hex');
  for(const phase of ['merge_review','product_review'] as const){
    const responses=[{...imageResult(figures[0]),section_id:'wrong'},{...imageResult(figures[0]),sha256:'a'.repeat(64)},{...imageResult(figures[0]),data:changedData,sha256:changedHash},imageResult(figures[0])];
    await fixture((_body,res,index)=>sendTool(res,index<5?'read_visual':'submit_phase_result',index<5?{figure_id:index===0?'not-supplied':'fig:1'}:outputs[phase],index),async(base,dir,bodies)=>{
      let reads=0;const spec=makeSpec(base,dir,phase,{figures,packet,product,source_sections:sections,figure_checks:{items:figureReport.observations,complete:true}});
      const output=await runPhase(spec,async(name,args)=>{if(name==='host_phase_submission')return {accepted:true};assert.equal(name,'read_visual');assert.equal(args.figure_id,'fig:1');return responses[reads++];},()=>{},new AbortController().signal);
      assert.deepEqual(output,outputs[phase]);assert.equal(reads,4,'unknown IDs never reach the host');assert.equal(bodies.length,6);
      assert.match(JSON.stringify(bodies[1].messages),/must address a supplied figure/);
      assert.match(JSON.stringify(bodies[2].messages),/mismatched figure\/source\/section identity/);
      assert.match(JSON.stringify(bodies[3].messages),/SHA-256 does not match/);
      assert.match(JSON.stringify(bodies[4].messages),/differs from supplied image identity/);
      const images=bodies[5].messages.flatMap((message:any)=>Array.isArray(message.content)?message.content:[]).filter((part:any)=>part.type==='image_url');
      assert.equal(images.length,1,'invalid image reads must never yield multimodal content');
    });
  }
});

test('optional read_visual is unavailable without vision, without supplied figures, or in legacy product review',async()=>{
  for(const phase of ['merge_review','product_review'] as const)for(const disabled of ['vision','figures','legacy']){
    if(phase==='merge_review'&&disabled==='legacy')continue;
    await fixture((_body,res,index)=>sendTool(res,index===0?'read_visual':'submit_phase_result',index===0?{figure_id:'fig:1'}:outputs[phase],index),async(base,dir,bodies)=>{
      const spec=makeSpec(base,dir,phase,{packet,product,source_sections:sections,...(disabled!=='figures'?{figures}:{}),figure_checks:{items:figureReport.observations},source_excerpts:[{source_id:'main',section_id:'s1',text:quote.quote,evidence_ids:['E1']}]});
      if(disabled==='vision')spec.model.vision=false;
      if(disabled==='legacy')spec.workflow_version='guided-v2.3';
      const hostCalls:string[]=[];
      assert.deepEqual(await runPhase(spec,async(name)=>{hostCalls.push(name);assert.equal(name,'host_phase_submission');return {accepted:true};},()=>{},new AbortController().signal),outputs[phase]);
      assert.deepEqual(hostCalls,['host_phase_submission']);assert.ok(bodies.every(body=>body.tools.every((tool:any)=>tool.function.name!=='read_visual')));
      if(disabled==='legacy'){
        const prompt=payload(bodies[0]),system=bodies[0].messages.find((message:any)=>message.role==='system').content;
        assert.equal(prompt.input.source_excerpts,undefined);assert.equal(prompt.input.figures,undefined);
        assert.equal(createHash('sha256').update(system.split('\n\n<cwd>')[0]).digest('hex'),'94419512b906ac377cfecc95c97fe66633ec4a238231037ce77d4d1e46080ddd');
      }
    });
  }
});

test('figure review charges failed submissions and cannot accept unread, omitted or forged images',async()=>{
  const attempts=[['submit_phase_result',figureReport],['read_visual',{figure_id:'fig:1'}],['submit_phase_result',{...figureReport,observations:[figureReport.observations[0]]}],['read_visual',{figure_id:'page:2'}],['submit_phase_result',{...figureReport,observations:[{...figureReport.observations[0],image_sha256:'a'.repeat(64)},figureReport.observations[1]]}],['submit_phase_result',figureReport]];
  await fixture((_body,res,index)=>{assert.ok(index<attempts.length);sendTool(res,attempts[index][0] as string,attempts[index][1],index);},async(base,dir,bodies)=>{
    let charges=0;const spec=makeSpec(base,dir,'figure_review',{figures,packet,source_sections:sections});
    const result=await runPhase(spec,async(name,args)=>{if(name==='host_phase_submission'){charges++;return {accepted:true};}return imageResult(figures.find(figure=>(figure.id??figure.figure_id)===args.figure_id)!);},()=>{},new AbortController().signal);
    assert.deepEqual(result,figureReport);assert.equal(charges,4);assert.equal(bodies.length,6);
    assert.match(JSON.stringify(bodies[1].messages),/actual read_visual/);
    assert.match(JSON.stringify(bodies[3].messages),/every supplied figure/);
    assert.match(JSON.stringify(bodies[5].messages),/exact identity/);
  });
});

test('page transcription reads the image with no claims and later chunks cannot present it as original text',async()=>{
  const page=figures[1];
  const output={observations:[{...observation(page),claim_ids:[],observation:'Visible table: Condition | Value\nA | 0.8\nFormula: x² + y². Essential qualifier: only under condition A.'}],passed:true,issues:[]};
  await fixture((_body,res,index)=>sendTool(res,index===0?'read_visual':'submit_phase_result',index===0?{figure_id:'page:2'}:output,index),async(base,dir,bodies)=>{
    const spec=makeSpec(base,dir,'figure_review',{purpose:'page_transcription',figures:[page],packet:{claims:[]},source_sections:[]});
    assert.deepEqual(await runPhase(spec,async(name)=>name==='host_phase_submission'?{accepted:true}:imageResult(page),()=>{},new AbortController().signal),output);
    assert.equal(payload(bodies[0]).input.purpose,'page_transcription');
    const system=bodies[0].messages.find((message:any)=>message.role==='system').content;
    assert.match(system,/faithfully transcribe all visible substantive text/);assert.match(system,/table headers and rows/);assert.match(system,/original PDF text remains unchanged/);
  });
  const visual_evidence={figure_id:'page:2',image_sha256:imageHash,observation_id:'V2',section_id:'appendix'};
  const input={source_sections:[{...sections[1],text:output.observations[0].observation,source_method:'vision',visual_evidence}]};
  const good={packet:{...packet,claims:[{...claim,evidence:[{...visual,...visual_evidence}]}]},checked_section_ids:['appendix'],unresolved:[]};
  validatePhaseResult('chunk_extract',good,input,context);
  for(const evidence of [{...quote,section_id:'appendix'},{...cell,section_id:'appendix'},{...visual,...visual_evidence,observation_id:'forged'}])assert.throws(()=>validatePhaseResult('chunk_extract',{...good,packet:{...packet,claims:[{...claim,evidence:[evidence]}]}},input,context),/Vision-derived sections/);
});

test('version, source-rule, vision and budget gates fail before sending any provider request',async()=>{
  const spec=makeSpec('http://127.0.0.1:1/v1','/tmp','figure_review',{figures,packet});
  for(const version of [undefined,'guided-v2.3','guided-v2.2']){
    assert.throws(()=>assertRunSpec({...spec,workflow_version:version}),/v3 phases require/);
    assert.throws(()=>phaseSchema('chunk_extract',version),/v3 phases require/);
    assertRunSpec({...spec,workflow_version:version,phase:{name:'compose',input:{packet:{...packet,version:1},template}}});
  }
  assertRunSpec({...spec,workflow_mode:'native',phase:undefined,workflow_version:undefined,source_rules_version:undefined});
  assert.throws(()=>assertRunSpec({...spec,source_rules_version:7}),/source_rules_version/);
  await assert.rejects(runPhase({...spec,model:{...spec.model,vision:false}},async()=>{throw new Error('No host call expected');},()=>{},new AbortController().signal),/vision model/);
  await assert.rejects(runPhase({...spec,limits:{...spec.limits,max_tool_calls:2}},async()=>{throw new Error('No host call expected');},()=>{},new AbortController().signal),/cannot read every/);
});

test('v3 retains raw truncated assistant content before usage without executing its incomplete call',async()=>{
  await fixture((_body,res,index)=>sendTool(res,'submit_phase_result','{"brief_markdown":"unfinished fixture',index,'openai-completions','length'),async(base,dir,bodies)=>{
    const events:any[]=[],spec=makeSpec(base,dir,'compose',{packet,template});let hostCalls=0;
    await assert.rejects(runPhase(spec,async()=>{hostCalls++;return {accepted:true};},event=>events.push(event),new AbortController().signal),/truncated at the output token limit/);
    assert.equal(hostCalls,0);assert.equal(bodies.length,1);
    const trace=events.find(event=>event.type==='model_output');assert.ok(trace);assert.equal(trace.stop_reason,'length');assert.equal(trace.phase,'compose');
    assert.match(JSON.stringify(trace.content),/unfinished fixture/);assert.deepEqual(Object.keys(trace).sort(),['content','phase','stop_reason','type']);
    assert.ok(events.indexOf(trace)<events.findIndex(event=>event.type==='usage'));
  });
});

test('raw model-output events are absent for native sessions even with an explicit version field',async()=>{
  await fixture((_body,res,index)=>sendTool(res,'unknown_fixture',{},index),async(base,dir)=>{
    const spec=makeSpec(base,dir,'compose',{});spec.workflow_mode='native';delete spec.phase;
    const events:any[]=[],abort=new AbortController();
    await assert.rejects(modelSession(spec,'Fixture only.','Do not persist inputs.',[],event=>{events.push(event);if(event.type==='usage')abort.abort();},abort.signal),/Cancelled/);
    assert.equal(events.some(event=>event.type==='model_output'),false);
  });
});
