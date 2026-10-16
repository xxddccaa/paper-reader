import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {once} from 'node:events';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
import {Type} from '@sinclair/typebox';
import {runPhase,PHASE_OUTPUT_CAPS,validatePhaseResult} from '../src/phases.js';
import {modelSession} from '../src/session.js';
import {assertRunSpec,WORKFLOW_V3,type RunSpec,type PhaseName,type LegacyPhaseName} from '../src/protocol.js';

const evidence={id:'E1',source_id:'main',section_id:'p1',quote:'Only the bias parameters are updated.',page:1};
const claim={id:'C1',kind:'method',statement:'只更新偏置项。',basis:'observation',conditions:{model:'BERT',dataset:'',split:'',metric:'',baseline:'',budget:''},evidence:[evidence]};
const packet={version:1,paper_title:'Fixture',research_question:'哪些参数需要更新？',claims:[claim],open_questions:[],limitations:['效果依赖任务与数据规模。']};
const product={brief_markdown:'只更新偏置项。[E1](evidence:E1)',full_markdown:'方法仅更新偏置项。[E1](evidence:E1)'};
const template={brief_markdown:'# Brief',full_markdown:'# Full',instructions:'Write clearly.'};
const source_sections=[{id:'p1',source_id:'main',text:evidence.quote,page:1,section:'Method'}];
const phaseOutput:Record<LegacyPhaseName,any>={extract:packet,packet_repair:{claims:[claim],remove_claim_ids:[]},fact_review:{passed:true,issues:[],checked_claim_ids:['C1'],suggestions:[]},compose:product,product_review:{passed:true,issues:[],checks:{coverage:true,template:true,numeric_conditions:true,inference_labels:true}},product_repair:{edits:[{field:'brief_markdown',old_text:'只更新偏置项。',new_text:'训练仅更新偏置项。'}]},revision_plan:{scope:'presentation',reason:'只要求调整展示结构。'}};
// Preserve unaffected legacy prompts; extraction and repair changes are intentional.
const legacyPromptHashes:Record<Exclude<LegacyPhaseName,'extract'|'packet_repair'>,string>={fact_review:'de3ca81d70aa0aa044c4cf8aa501db0689eb539ecca14b4f2f513dcfb3ad7a49',compose:'e2bd7a8e635eb55c691e6cc2f9023f6914e9bef5a67d4f50176c92913b11b57a',product_review:'94419512b906ac377cfecc95c97fe66633ec4a238231037ce77d4d1e46080ddd',product_repair:'281d2c1f28116de374c7f27dc6fded31838761f168f80a708001030d5e981236',revision_plan:'a51b2fed1574fb1457639e227f0cda75df917491452a8eec228cf1f28d5a8e38'};
const specFor=(base:string,dir:string,name:PhaseName,input:Record<string,unknown>):RunSpec=>({type:'run',run_id:'phase-test',kind:'read',skill_id:'ljg-paper',skill_root:dir,work_dir:dir,workflow_mode:'guided',phase:{name,input},model:{api_key:'fixture-secret',base_url:base,model:'fixture-model',protocol:'openai-completions',thinking_format:'qwen-chat-template',allow_private_models:true,vision:false,context_window:128000,max_tokens:12000},paper:{id:'paper',title:'Fixture',source_type:'pdf'},template,limits:{timeout_seconds:10,max_tool_calls:80,max_related_papers:3}});
const userPayload=(body:any)=>{
  const content=body.messages.find((message:any)=>message.role==='user').content;
  return JSON.parse(typeof content==='string'?content:content.filter((item:any)=>item.type==='text').map((item:any)=>item.text).join(''));
};
function sendTool(res:any,name:string,args:any,id='call',finish='tool_calls'){
  const base={id:'phase-fixture',object:'chat.completion.chunk',created:1,model:'fixture-model'};
  res.writeHead(200,{'Content-Type':'text/event-stream'});
  res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{tool_calls:[{index:0,id,type:'function',function:{name,arguments:JSON.stringify(args)}}]},finish_reason:null}]})+'\n\n');
  res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:finish}],usage:{prompt_tokens:20,completion_tokens:30,total_tokens:50}})+'\n\n');res.end('data: [DONE]\n\n');
}

test('all guided phases use their own schemas/caps and stop after one valid SDK submission',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'guided-phases-'));const bodies:any[]=[];
  const server=createServer(async(req,res)=>{
    let data='';for await(const chunk of req)data+=chunk;const body=JSON.parse(data);bodies.push(body);
    const payload=userPayload(body);sendTool(res,'submit_phase_result',phaseOutput[payload.phase as LegacyPhaseName]);
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
  try{
    for(const version of [undefined,'guided-v2.3'])for(const name of Object.keys(phaseOutput) as LegacyPhaseName[]){
      const spec=specFor(`http://127.0.0.1:${port}/v1`,dir,name,{packet,previous_packet:packet,previous_packet_compatible:true,product,template,source_sections,skill_guidance:'SELECTED_SKILL_PRINCIPLES',instruction:'清楚说明方法。',errors:['C1 needs correction'],review:{issues:[]},previous_review:{marker:'PREVIOUS_REVIEW'},private_history:'DO_NOT_COPY_HISTORY'});
      spec.workflow_version=version;
      const events:any[]=[];const before=bodies.length;let submissionCharges=0;
      const output=await runPhase(spec,async(name,args)=>{assert.equal(name,'host_phase_submission');assert.deepEqual(args,{});submissionCharges++;return {accepted:true,remaining_tool_calls:79};},event=>events.push(event),new AbortController().signal);
      assert.deepEqual(output,phaseOutput[name]);assert.equal(bodies.length,before+1,'no post-submission model round-trip');
      assert.equal(submissionCharges,1,'host charges the executed submission exactly once');
      const body=bodies.at(-1);const prompt=userPayload(body);const names=body.tools.map((item:any)=>item.function.name);
      assert.equal(body.max_tokens,PHASE_OUTPUT_CAPS[name]);assert.deepEqual(body.chat_template_kwargs,{enable_thinking:false,preserve_thinking:true});
      assert.equal(prompt.workflow_mode,'guided');assert.equal(prompt.native_status,'not_requested');assert.equal(prompt.phase,name);
      assert.equal(prompt.rules_version,'guided-v2.3');
      const systemText=body.messages.find((message:any)=>message.role==='system').content;
      if(name!=='extract'&&name!=='packet_repair')assert.equal(createHash('sha256').update(systemText.split('\n\n<cwd>')[0]).digest('hex'),legacyPromptHashes[name]);
      assert.equal(events.some(event=>event.type==='model_output'),false);
      assert.equal(prompt.input.private_history,undefined);
      assert.ok(!names.some((name:string)=>['run_skill_step','write_artifact','patch_artifact','review_native','host_native_seal','host_phase_submission','finish_native','search_papers','fetch_related'].includes(name)));
      if(name==='compose'||name==='product_repair'||name==='revision_plan'){
        assert.deepEqual(names,['submit_phase_result']);assert.equal(prompt.input.source_sections,undefined);assert.equal(prompt.input.skill_guidance,undefined);
      }
      if(name==='extract'){
        assert.equal(prompt.input.skill_guidance,'SELECTED_SKILL_PRINCIPLES');
        const system=JSON.stringify(body.messages.filter((message:any)=>message.role==='system'||message.role==='developer'));
        assert.match(system,/6–10 high-value, nonredundant claims/);
        assert.match(system,/one short exact contiguous evidence clause/);
        assert.match(system,/Preserve PDF spellings, line breaks, hyphenation, ligatures and footnote markers/);
        assert.match(system,/Default open_questions to \[\]/);
        assert.match(system,/necessary explicitly requested external check/);
        assert.match(system,/value=1 and baseline_value=2 means relation=lower/);
      }
      if(name==='fact_review'||name==='product_review')assert.match(JSON.stringify(body.messages),/metadata.*evidence-backed claims|evidence-backed claims.*metadata/);
      if(name==='compose'){
        assert.match(JSON.stringify(body.messages),/both the visible label and the evidence target must use the SAME existing E ID/);
        assert.match(JSON.stringify(body.messages),/explicit current revision instruction about length overrides the default brief target/);
        assert.match(JSON.stringify(body.messages),/do not force every packet claim or comparison into the brief/);
      }
      if(name==='product_review')assert.match(JSON.stringify(body.messages),/issues array contains ONLY actionable substantive failures/);
      if(name==='fact_review'||name==='product_review')assert.equal(prompt.input.previous_review.marker,'PREVIOUS_REVIEW');
      if(name==='revision_plan')assert.equal(prompt.input.previous_packet_compatible,true);
      assert.equal(events.filter(event=>event.type==='usage').reduce((sum,event)=>sum+event.usage.total_tokens,0),50);
    }
  }finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
});

test('legacy and v3 repair sessions receive prior failures and can reread an exact locator before submitting a corrected delta',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'adaptive-packet-repair-'));const bodies:any[]=[];
  const locator={source_id:'main',section_id:'p5',page:5};
  const sourceQuote='For 𝛾input tokens, only the bias parameters are updated.';
  const failedClaim={...claim,evidence:[{...evidence,quote:'For 𝛾 input tokens, only the bias parameters are updated.'}]};
  const replacement={...claim,evidence:[{...evidence,...locator,quote:sourceQuote}]};
  const delta={claims:[replacement],remove_claim_ids:[]};
  const error='E1: exact quote was not found in the declared source/section/page';
  const repair_context={policy_version:'evidence-repair-v1',round:2,max_rounds:3,errors:[error],unchanged_errors:[error],strategy:'Inspect the source before changing the remaining reference.',previous_attempt:{round:1,changed_claim_ids:['C1'],remaining_errors:[error]},diagnostics:[{claim_id:'C1',evidence_id:'E1',declared:{source_id:'main',section_id:'p1',page:1},suggested_locators:[locator],candidate_excerpt:sourceQuote}],host_extension:{opaque:['preserve future diagnostic fields']}};
  const server=createServer(async(req,res)=>{
    let data='';for await(const chunk of req)data+=chunk;const body=JSON.parse(data);bodies.push(body);
    sendTool(res,bodies.length%2===1?'read_source':'submit_phase_result',bodies.length%2===1?locator:delta,`call_${bodies.length}`);
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
  try{
    for(const version of [undefined,'guided-v2.3',WORKFLOW_V3]){
      const currentPacket={...packet,version:version===WORKFLOW_V3?2:1,claims:[failedClaim]};
      const input={packet:currentPacket,source_sections,skill_guidance:'Pinned reading principles.',errors:[error],repair_context,private_history:'DO_NOT_FORWARD'};
      const spec=specFor(`http://127.0.0.1:${port}/v1`,dir,'packet_repair',input);spec.workflow_version=version;
      const rpcCalls:string[]=[];const before=bodies.length;
      const result=await runPhase(spec,async(name,args)=>{
        rpcCalls.push(name);
        if(name==='read_source'){assert.deepEqual(args,locator);return {sections:[{id:locator.section_id,source_id:locator.source_id,page:locator.page,text:sourceQuote}]};}
        assert.equal(name,'host_phase_submission');return {accepted:true};
      },()=>{},new AbortController().signal);
      assert.deepEqual(result,delta);assert.deepEqual(rpcCalls,['read_source','host_phase_submission']);
      assert.equal(bodies.length,before+2,'targeted reread plus submission must remain available without extra calls');
      for(const body of bodies.slice(before)){
        const prompt=userPayload(body),names=body.tools.map((tool:any)=>tool.function.name);
        assert.deepEqual(prompt.input.repair_context,repair_context);assert.deepEqual(prompt.input.packet,currentPacket);
        assert.deepEqual(prompt.input.source_sections,source_sections);assert.equal(prompt.input.private_history,undefined);
        assert.ok(names.includes('read_source'));assert.ok(names.includes('submit_phase_result'));
        const system=body.messages.find((message:any)=>message.role==='system').content;
        for(const field of ['repair_context','unchanged_errors','previous_attempt','read_source','remove_claim_ids'])assert.ok(system.includes(field),`${version}: repair instructions must explain ${field}`);
      }
      assert.ok(JSON.stringify(bodies.at(-1).messages).includes(sourceQuote),'exact source typography must reach the model through the tool response');
    }
  }finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
});

test('guided fact review permits related retrieval only through explicit question search candidates',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'guided-related-'));let requests=0;const hostCalls:string[]=[];
  const query='bias-only tuning counterexamples';
  const calls=[['search_papers',{query:'unrequested generic search'}],['fetch_related',{id:'arxiv:unseen'}],['search_papers',{query}],['fetch_related',{id:'arxiv:related'}],['submit_phase_result',phaseOutput.fact_review]];
  const server=createServer(async(req,res)=>{
    let data='';for await(const chunk of req)data+=chunk;
    const body=JSON.parse(data);assert.equal(body.tools.find((tool:any)=>tool.function.name==='search_papers').function.parameters.properties.query.maxLength,500);
    const [name,args]=calls[requests++]??['submit_phase_result',phaseOutput.fact_review];sendTool(res,name as string,args);
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
  try{
    const spec=specFor(`http://127.0.0.1:${port}/v1`,dir,'fact_review',{packet:{...packet,open_questions:[{question:'何时效果下降？',search_query:query,reason:'核对适用范围'}]},source_sections});
    const result=await runPhase(spec,async(name)=>{hostCalls.push(name);return name==='host_phase_submission'?{accepted:true,remaining_tool_calls:77}:name==='search_papers'?{candidates:[{id:'arxiv:related',title:'Related'}]}:{source_id:'related',sections:[]};},()=>{},new AbortController().signal);
    assert.equal(result.passed,true);assert.equal(requests,5);assert.deepEqual(hostCalls,['search_papers','fetch_related','host_phase_submission']);
  }finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
});

test('guided schema rejects malformed metadata, invented coverage and ambiguous patches',()=>{
  const question={question:'Which conditions differ?',search_query:'q'.repeat(500),reason:'Check the comparison boundary.'};
  validatePhaseResult('extract',{...packet,open_questions:[question]},{});
  assert.throws(()=>validatePhaseResult('extract',{...packet,open_questions:[{...question,search_query:'q'.repeat(501)}]},{}),/Invalid/);
  assert.throws(()=>validatePhaseResult('extract',{...packet,paper_title:' '.repeat(4)},{}),/Invalid/);
  assert.throws(()=>validatePhaseResult('extract',{...packet,claims:[{...claim,evidence:[{...evidence,section_id:'x'.repeat(201)}]}]},{}),/Invalid/);
  assert.throws(()=>validatePhaseResult('extract',{...packet,claims:[{...claim,id:'C1000000'}]},{}),/Invalid/);
  assert.throws(()=>validatePhaseResult('extract',{...packet,claims:[{...claim,evidence:[{...evidence,quote:'            a'}]}]},{}),/meaningful/);
  assert.throws(()=>validatePhaseResult('extract',{...packet,claims:[claim,{...claim,id:'C2',evidence:[{...evidence,quote:'A different but nonmatching claim.'}]}]},{}),/identical/);
  assert.throws(()=>validatePhaseResult('fact_review',{...phaseOutput.fact_review,checked_claim_ids:[]},{packet}),/every packet claim/);
  assert.throws(()=>validatePhaseResult('product_review',{...phaseOutput.product_review,checks:{...phaseOutput.product_review.checks,coverage:false}},{}),/all checks/);
  assert.throws(()=>validatePhaseResult('packet_repair',{claims:[{...claim,id:'C9'}],remove_claim_ids:[]},{packet}),/existing/);
  assert.throws(()=>validatePhaseResult('product_repair',{edits:[{field:'brief_markdown',old_text:'repeat',new_text:'change'}]},{product:{brief_markdown:'repeat repeat'}}),/exactly once/);
  assert.throws(()=>validatePhaseResult('product_repair',{edits:[{field:'full_markdown',old_text:'abc',new_text:'A'},{field:'full_markdown',old_text:'bc',new_text:'B'}]},{product:{full_markdown:'abc'}}),/overlap/);
  validatePhaseResult('extract',{...packet,claims:[{...claim,numeric_comparison:{value:1,baseline_value:1,relation:'equal',unit:''}}]},{});
});

test('guided mode cannot silently fall back to native and respects model output cap',async()=>{
  const spec=specFor('http://127.0.0.1:1/v1','/tmp','compose',{packet,template});
  assert.throws(()=>assertRunSpec({...spec,phase:undefined}),/host-selected phase/);
  assert.throws(()=>assertRunSpec({...spec,workflow_mode:'native'}),/require guided/);
  const control=new AbortController();control.abort();
  await assert.rejects(runPhase(spec,async()=>({}),()=>{},control.signal),/Cancelled/);
});

test('stop-after-submit waits for the completed tool result and ignores failed submissions',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'submit-lifecycle-'));let requests=0,executions=0,completed=false;
  const server=createServer(async(req,res)=>{for await(const _chunk of req){}requests++;sendTool(res,'submit_phase_result',{valid:requests>1});});
  server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
  try{
    const spec=specFor(`http://127.0.0.1:${port}/v1`,dir,'revision_plan',{});
    await modelSession(spec,'Submit only valid work.','Submit.',[{name:'submit_phase_result',label:'Submit',description:'Fixture',parameters:Type.Object({valid:Type.Boolean()}),execute:async(_id,args,signal)=>{
      executions++;if(!(args as any).valid)throw new Error('Validation failed');
      await new Promise(resolve=>setTimeout(resolve,20));assert.equal(signal?.aborted,false,'submission host work must finish before intentional abort');completed=true;
      return {content:[{type:'text',text:'accepted'}],details:{}};
    }}],()=>{},new AbortController().signal,{stopAfterTool:'submit_phase_result'});
    assert.equal(completed,true);assert.equal(executions,2);assert.equal(requests,2);
  }finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
});

test('external cancellation wins a submit race and cannot become phase success',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'submit-cancel-'));const control=new AbortController();let requests=0;
  const server=createServer(async(req,res)=>{for await(const _chunk of req){}requests++;sendTool(res,'submit_phase_result',{});});
  server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
  try{
    await assert.rejects(modelSession(specFor(`http://127.0.0.1:${port}/v1`,dir,'revision_plan',{}),'Submit.','Submit.',[{name:'submit_phase_result',label:'Submit',description:'Fixture',parameters:Type.Object({}),execute:async()=>{control.abort();return {content:[{type:'text',text:'accepted'}],details:{}};}}],()=>{},control.signal,{stopAfterTool:'submit_phase_result'}),/Cancelled/);
    assert.equal(requests,1);
  }finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
});

test('two invalid exact-match submissions then a valid repair charge the host exactly three times',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'product-repair-feedback-'));let requests=0,submissionCharges=0;
  const invalid={edits:[{field:'brief_markdown',old_text:'Text absent from frozen product.',new_text:'Replacement'}]};
  const server=createServer(async(req,res)=>{
    let data='';for await(const chunk of req)data+=chunk;requests++;
    const body=JSON.parse(data);
    assert.equal(userPayload(body).limits.max_tool_calls,3);
    if(requests>1)assert.match(JSON.stringify(body.messages),/old_text must occur exactly once/);
    sendTool(res,'submit_phase_result',requests<=2?invalid:phaseOutput.product_repair);
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
  try{
    const spec=specFor(`http://127.0.0.1:${port}/v1`,dir,'product_repair',{packet,product,template,review:{issues:['Make the sentence explicit.']}});
    const result=await runPhase(spec,async(name,args)=>{assert.equal(name,'host_phase_submission');assert.deepEqual(args,{});submissionCharges++;return {accepted:true,remaining_tool_calls:80-submissionCharges};},()=>{},new AbortController().signal);
    assert.deepEqual(result,phaseOutput.product_repair);assert.equal(requests,3);assert.equal(submissionCharges,3);
  }finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
});

test('repeated invalid repair submissions stop at the phase or smaller remaining task budget',async()=>{
  for(const remainingBudget of [80,2]){
    const dir=await mkdtemp(join(tmpdir(),'product-repair-bounded-'));let requests=0,submissionCharges=0;
    const server=createServer(async(req,res)=>{
      for await(const _chunk of req){}requests++;
      sendTool(res,'submit_phase_result',{edits:[{field:'brief_markdown',old_text:'Never present',new_text:'Replacement'}]});
    });
    server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
    const control=new AbortController();const failsafe=setTimeout(()=>control.abort(),3000);
    try{
      const spec=specFor(`http://127.0.0.1:${port}/v1`,dir,'product_repair',{packet,product,template});spec.limits.max_tool_calls=remainingBudget;
      await assert.rejects(runPhase(spec,async(name)=>{assert.equal(name,'host_phase_submission');submissionCharges++;return {accepted:true,remaining_tool_calls:remainingBudget-submissionCharges};},()=>{},control.signal),/Workflow phase tool budget exhausted/);
      assert.equal(requests,Math.min(3,remainingBudget)+1,'no request after the first denied submission');
      assert.equal(submissionCharges,Math.min(3,remainingBudget));
      assert.equal(control.signal.aborted,false,'bounded stop must not wait for timeout');
    }finally{clearTimeout(failsafe);server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
  }
});

test('host submission budget rejection aborts the phase without a recoverable tool-error loop',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'phase-host-budget-'));let requests=0,charges=0;
  const server=createServer(async(req,res)=>{
    for await(const _chunk of req){}requests++;
    sendTool(res,'submit_phase_result',requests===1?{edits:[{field:'brief_markdown',old_text:'Absent',new_text:'Replacement'}]}:phaseOutput.product_repair);
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
  const control=new AbortController();const failsafe=setTimeout(()=>control.abort(),3000);
  try{
    const spec=specFor(`http://127.0.0.1:${port}/v1`,dir,'product_repair',{packet,product,template});
    await assert.rejects(runPhase(spec,async(name)=>{
      assert.equal(name,'host_phase_submission');charges++;
      if(charges===1)return {accepted:true,remaining_tool_calls:0};
      throw new Error('Task tool budget exhausted at host');
    },()=>{},control.signal),/Task tool budget exhausted at host/);
    assert.equal(requests,2);assert.equal(charges,2);assert.equal(control.signal.aborted,false);
  }finally{clearTimeout(failsafe);server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
});
