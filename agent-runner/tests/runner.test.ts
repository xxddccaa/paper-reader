import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {once} from 'node:events';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Type} from '@sinclair/typebox';
import {modelSession} from '../src/session.js';
import {WorkerRpc} from '../src/rpc.js';
import {safeError,assertRunSpec,type RunSpec} from '../src/protocol.js';
import {productIssues,verifyNativeGates} from '../src/runner.js';
import {READER_RECORD_FIELDS} from '../src/native-review.js';

const runSpec=(base:string,dir:string):RunSpec=>({type:'run',run_id:'test',kind:'read',skill_id:'ljg-paper',skill_root:dir,work_dir:dir,
  model:{api_key:'fixture-secret',base_url:base,model:'fixture-model',protocol:'openai-completions',allow_private_models:true,vision:false,context_window:32000,max_tokens:1000},
  paper:{id:'test',title:'Fixture',source_type:'pdf'},template:{brief_markdown:'# Brief',full_markdown:'# Full',instructions:''},limits:{timeout_seconds:20,max_tool_calls:10,max_related_papers:3}});

test('RPC correlates out-of-order tool responses and cancels pending work',async()=>{
  const emitted:any[]=[];const rpc=new WorkerRpc(event=>emitted.push(event));
  const one=rpc.call('read_source',{page:1});const two=rpc.call('read_source',{page:2});
  rpc.receive({type:'tool_response',id:emitted[1].id,result:{page:2}});rpc.receive({type:'tool_response',id:emitted[0].id,result:{page:1}});
  assert.deepEqual(await one,{page:1});assert.deepEqual(await two,{page:2});
  const abort=new AbortController();const pending=rpc.call('search_papers',{},abort.signal);abort.abort();await assert.rejects(pending,/Cancelled/);
});
test('native gate failures are not promoted to completed skill checks',()=>{
  assert.throws(()=>verifyNativeGates('ljg-paper',new Map([['validate_note.ts',{exit_code:1}]]),{passed:true}),/has not succeeded/);
  assert.throws(()=>verifyNativeGates('ljg-paper',new Map([['validate_note.ts',{exit_code:0}]]),{passed:false}),/review/);
  verifyNativeGates('ljg-paper',new Map([['validate_note.ts',{exit_code:0}]]),{passed:true});
});
test('product citations must exist and each template output needs evidence',()=>{
  const product:any={brief_markdown:'Result [E1](evidence:E1)',full_markdown:'Unknown [E2]',evidence_index:[{id:'E1',quote:'exact',kind:'paper'}]};
  assert.deepEqual(productIssues(product),['Unknown citation: E2']);
  product.full_markdown='Result [E1]';assert.deepEqual(productIssues(product),[]);
});
test('invalid run and credential redaction',()=>{
  assert.throws(()=>assertRunSpec({type:'run'}),/Invalid/);
  assert.equal(safeError(new Error('oops fixture-secret Bearer confidential'),['fixture-secret']),'oops [REDACTED] Bearer [REDACTED]');
});
test('real Pi SDK talks OpenAI protocol, executes only explicit custom tool, and reports usage',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'paper-runner-test-'));let requests=0;let executed=0;const events:any[]=[];
  const server=createServer(async(req,res)=>{
    let body='';for await(const chunk of req)body+=chunk;
    const parsed=JSON.parse(body);requests++;
    assert.equal(req.headers.authorization,'Bearer fixture-secret');
    assert.deepEqual(parsed.tools.map((t:any)=>t.function.name),['verify_fixture']);
    assert.equal(parsed.model,'fixture-model');
    assert.equal(parsed.max_tokens,1000);
    const toolReply=parsed.messages.some((m:any)=>m.role==='tool');
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    const base={id:'chatcmpl-fixture',object:'chat.completion.chunk',created:1,model:'fixture-model'};
    const delta=toolReply?{content:'Done'}:{tool_calls:[{index:0,id:'fixture-call',type:'function',function:{name:'verify_fixture',arguments:'{"source":"main"}'}}]};
    res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta,finish_reason:null}]})+'\n\n');
    res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:toolReply?'stop':'tool_calls'}],usage:{prompt_tokens:10,completion_tokens:5,total_tokens:15}})+'\n\n');res.end('data: [DONE]\n\n');
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');const address=server.address() as any;
  try{await modelSession(runSpec(`http://127.0.0.1:${address.port}/v1`,dir),'Only verify the fixture.','Call verify_fixture, then finish.',[{
    name:'verify_fixture',label:'Verify',description:'Explicit offline protocol fixture tool',parameters:Type.Object({source:Type.String()}),
    execute:async(_id,args)=>{assert.equal((args as any).source,'main');executed++;return {content:[{type:'text',text:'verified fixture'}],details:{}};}
  }],event=>events.push(event),new AbortController().signal);
  assert.equal(requests,2);assert.equal(executed,1);assert.ok(events.some(event=>event.type==='usage'&&event.usage.input_tokens===10));}
  finally{server.closeAllConnections();await new Promise<void>(r=>server.close(()=>r()));await rm(dir,{recursive:true,force:true});}
});
test('SDK cancellation aborts a pending model request',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'paper-runner-abort-'));const control=new AbortController();
  const server=createServer((_req,res)=>{res.writeHead(200,{'Content-Type':'text/event-stream'});res.flushHeaders();setTimeout(()=>control.abort(),30);});
  server.listen(0,'127.0.0.1');await once(server,'listening');const address=server.address() as any;
  try{await assert.rejects(modelSession(runSpec(`http://127.0.0.1:${address.port}/v1`,dir),'Check cancellation.','Wait.',[],()=>{},control.signal),/Cancelled/);}
  finally{server.closeAllConnections();await new Promise<void>(r=>server.close(()=>r()));await rm(dir,{recursive:true,force:true});}
});

test('real Pi SDK selects Qwen chat-template thinking only from the explicit setting',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'paper-runner-qwen-format-'));const bodies:any[]=[];
  const server=createServer(async(req,res)=>{
    let body='';for await(const chunk of req)body+=chunk;bodies.push(JSON.parse(body));
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    const base={id:'qwen-format-fixture',object:'chat.completion.chunk',created:1,model:'fixture-model'};
    res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{content:'Done'},finish_reason:null}]})+'\n\n');
    res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:'stop'}],usage:{prompt_tokens:10,completion_tokens:2,total_tokens:12}})+'\n\n');res.end('data: [DONE]\n\n');
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
  try{
    for(const format of [undefined,'default','qwen-chat-template'] as const){
      const spec=runSpec(`http://127.0.0.1:${port}/v1`,dir);spec.model.thinking_format=format;
      spec.model.model=format==='qwen-chat-template'?'anonymous-model':'Qwen3.8-27B';
      await modelSession(spec,'Return a short answer.','Say done.',[],()=>{},new AbortController().signal);
      const sent=bodies.at(-1);assert.equal(sent.model,spec.model.model);assert.equal(sent.max_tokens,1000);
      assert.equal(sent.reasoning_effort,undefined);assert.equal(sent.enable_thinking,undefined);assert.equal(sent.thinking,undefined);
      if(format==='qwen-chat-template')assert.deepEqual(sent.chat_template_kwargs,{enable_thinking:false,preserve_thinking:true});
      else assert.equal(sent.chat_template_kwargs,undefined,'model name must not select Qwen compatibility');
    }
    assert.equal(bodies.length,3);
  }finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
});

test('Anthropic default wire protocol has no Qwen thinking parameters',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'paper-runner-anthropic-default-'));let sent:any;
  const server=createServer(async(req,res)=>{
    let body='';for await(const chunk of req)body+=chunk;sent=JSON.parse(body);
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    const event=(value:any)=>res.write(`event: ${value.type}\ndata: ${JSON.stringify(value)}\n\n`);
    event({type:'message_start',message:{id:'msg_fixture',type:'message',role:'assistant',content:[],model:'fixture-model',stop_reason:null,stop_sequence:null,usage:{input_tokens:10,output_tokens:0}}});
    event({type:'content_block_start',index:0,content_block:{type:'text',text:''}});
    event({type:'content_block_delta',index:0,delta:{type:'text_delta',text:'Done'}});
    event({type:'content_block_stop',index:0});
    event({type:'message_delta',delta:{stop_reason:'end_turn',stop_sequence:null},usage:{output_tokens:2}});
    event({type:'message_stop'});res.end();
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
  try{
    const spec=runSpec(`http://127.0.0.1:${port}`,dir);spec.model.protocol='anthropic-messages';spec.model.thinking_format='default';
    await modelSession(spec,'Answer briefly.','Say done.',[],()=>{},new AbortController().signal);
    assert.equal(sent.max_tokens,1000);assert.equal(sent.chat_template_kwargs,undefined);assert.equal(sent.enable_thinking,undefined);assert.equal(sent.thinking,undefined);assert.equal(sent.reasoning_effort,undefined);
  }finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
});

test('unsupported thinking settings fail before a model request',async()=>{
  for(const [format,protocol] of [['invented','openai-completions'],['qwen-chat-template','anthropic-messages'],[null,'openai-completions']] as const){
    const spec=runSpec('http://127.0.0.1:1/v1','/tmp');spec.model.thinking_format=format as any;spec.model.protocol=protocol;
    assert.throws(()=>assertRunSpec(spec),/thinking_format/);
    await assert.rejects(modelSession(spec,'Unused','Unused',[],()=>{},new AbortController().signal),/thinking_format/);
  }
});

test('SDK length stops fail explicitly with usage and no truncated-tool continuation',async()=>{
  const {runReading}=await import('../src/runner.js');
  for(const truncatedTool of [false,true]){
    const dir=await mkdtemp(join(tmpdir(),'paper-runner-output-limit-'));let requests=0;const events:any[]=[];const hostCalls:string[]=[];
    const server=createServer(async(req,res)=>{
      for await(const _chunk of req){}requests++;
      res.writeHead(200,{'Content-Type':'text/event-stream'});
      const base={id:'length-fixture',object:'chat.completion.chunk',created:1,model:'fixture-model'};
      const delta=truncatedTool?{tool_calls:[{index:0,id:'truncated',type:'function',function:{name:'write_artifact',arguments:'{"path":"native/note.md","content":"incomplete'}}]}:{reasoning_content:'INTERNAL_REASONING_FIXTURE'};
      res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta,finish_reason:null}]})+'\n\n');
      res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:'length'}],usage:{prompt_tokens:10,completion_tokens:256,total_tokens:266}})+'\n\n');res.end('data: [DONE]\n\n');
    });
    server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
    const control=new AbortController();const failsafe=setTimeout(()=>control.abort(),3000);
    try{
      const spec=runSpec(`http://127.0.0.1:${port}/v1`,dir);spec.model.max_tokens=256;spec.model.thinking_format='qwen-chat-template';
      await assert.rejects(runReading(spec,async name=>{
        hostCalls.push(name);
        if(name==='prepare_source')return {source_manifest:{source_id:'main'},sections:[]};
        if(name==='read_skill_file')return {content:'Read the paper.'};
        if(name==='host_native_status')return {sealed:false};
        throw new Error('No truncated tool may execute');
      },event=>events.push(event),control.signal),error=>{
        assert.match(String(error),/truncated at the output token limit.*stop_reason=length, configured max_tokens=256/);
        assert.doesNotMatch(String(error),/without required|INTERNAL_REASONING_FIXTURE|Cancelled/);return true;
      });
      assert.equal(control.signal.aborted,false);assert.equal(requests,1,'length must not start a retry or follow-up request');
      assert.deepEqual(hostCalls,['prepare_source','read_skill_file','host_native_status']);
      const usage=events.filter(event=>event.type==='usage').reduce((sum,event)=>sum+event.usage.total_tokens,0);
      assert.equal(usage,266);assert.ok(events.some(event=>event.type==='usage'&&event.usage.output_tokens===256));
    }finally{clearTimeout(failsafe);server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
  }
});

test('all vendored skill source bytes match pinned provenance',async()=>{
  const {readFile}=await import('node:fs/promises');const {createHash}=await import('node:crypto');
  for(const id of ['deeppapernote','ljg-paper']){
    const manifest=JSON.parse(await readFile(new URL(`../../skills/${id}-provenance.json`,import.meta.url),'utf8'));
    assert.match(manifest.commit,/^[a-f0-9]{40}$/);
    for(const [path,sha] of Object.entries(manifest.files_sha256)){
      const bytes=await readFile(new URL(`../../skills/${id}/${path}`,import.meta.url));
      assert.equal(createHash('sha256').update(bytes).digest('hex'),sha,`${id}/${path}`);
    }
  }
});

test('bundled ljg validator runs with Node and rejects an incomplete native note',async()=>{
  const {writeFile}=await import('node:fs/promises');const {spawnSync}=await import('node:child_process');
  const dir=await mkdtemp(join(tmpdir(),'paper-runner-validator-'));
  try{
    const note=join(dir,'20261012T120000--paper-fixture__paper.md');const map=join(dir,'paper-map.md');
    await writeFile(note,'# Insufficient note\n');await writeFile(map,'# Missing required review record\n');
    const result=spawnSync(process.execPath,[new URL('../dist/validate-ljg.js',import.meta.url).pathname,note,'--map',map],{encoding:'utf8'});
    assert.equal(result.status,1);const report=JSON.parse(result.stdout);assert.equal(report.ok,false);assert.ok(report.errors.length>0);
  }finally{await rm(dir,{recursive:true,force:true});}
});

test('model cannot call host attestation or complete native phase with forged checks',async()=>{
  const {runReading}=await import('../src/runner.js');
  const dir=await mkdtemp(join(tmpdir(),'paper-runner-forgery-'));let requests=0;const hostCalls:string[]=[];
  const server=createServer(async(req,res)=>{
    let body='';for await(const chunk of req)body+=chunk;
    const parsed=JSON.parse(body);requests++;
    const names=parsed.tools.map((tool:any)=>tool.function.name);
    assert.ok(!names.includes('host_native_attest'));assert.ok(!names.includes('host_native_seal'));
    const call=requests===1?{name:'host_native_attest',arguments:JSON.stringify({quality:{passed:true},readability:{passed:true}})}:
      requests===2?{name:'finish_native',arguments:JSON.stringify({note_path:'native/forged.md'})}:undefined;
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    const base={id:'forgery-test',object:'chat.completion.chunk',created:1,model:'fixture-model'};
    res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:call?{tool_calls:[{index:0,id:`forged-${requests}`,type:'function',function:call}]}:{content:'Could not bypass required checks'},finish_reason:null}]})+'\n\n');
    res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:call?'tool_calls':'stop'}]})+'\n\n');res.end('data: [DONE]\n\n');
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');const address=server.address() as any;
  try{
    const spec=runSpec(`http://127.0.0.1:${address.port}/v1`,dir);
    await assert.rejects(runReading(spec,async(name)=>{
      hostCalls.push(name);
      if(name==='prepare_source')return {source_manifest:{source_id:'main'},sections:[]};
      if(name==='read_skill_file')return {content:'Native checks and independent reviews are mandatory.'};
      if(name==='host_native_status')return {sealed:false};
      throw new Error('Unexpected host operation');
    },()=>{},new AbortController().signal),/without required finish_native/);
    assert.equal(requests,3);assert.deepEqual(hostCalls,['prepare_source','read_skill_file','host_native_status']);
  }finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
});

test('a sealed first-read checkpoint resumes product generation without rerunning native tools',async()=>{
  const {runReading}=await import('../src/runner.js');
  const dir=await mkdtemp(join(tmpdir(),'paper-runner-recovery-'));const hostCalls:string[]=[];let requests=0;
  const product={brief_markdown:'简述 [E1]',full_markdown:'详细解释 [E1]',evidence_index:[{id:'E1',source_id:'main',source_title:'Fixture',quote:'This is a complete original evidence quote.',kind:'paper',verified:false}],verification:{},artifacts:[],provenance:{}};
  const server=createServer(async(req,res)=>{
    let body='';for await(const chunk of req)body+=chunk;const parsed=JSON.parse(body);requests++;
    const names=parsed.tools.map((tool:any)=>tool.function.name);
    assert.ok(!names.includes('run_skill_step'));assert.ok(!names.includes('finish_native'));assert.ok(!names.includes('host_native_status'));
    const submitted=parsed.messages.some((message:any)=>message.role==='tool');
    const name=names.includes('submit_result')?'submit_result':'submit_review';
    if(name==='submit_review'){
      const user=parsed.messages.find((message:any)=>message.role==='user');
      const text=typeof user.content==='string'?user.content:user.content.filter((part:any)=>part.type==='text').map((part:any)=>part.text).join('');
      const context=JSON.parse(text).review_context;
      assert.match(context.current_utc,/^\d{4}-\d{2}-\d{2}T/);
      assert.equal(context.reading_date,new Date().toISOString().slice(0,10));
    }
    const value=name==='submit_result'?product:{passed:true,issues:[],checks:{citations:'checked in fixture'}};
    const base={id:'recovery-test',object:'chat.completion.chunk',created:1,model:'fixture-model'};
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:submitted?{content:'Done'}:{tool_calls:[{index:0,id:`submission-${requests}`,type:'function',function:{name,arguments:JSON.stringify(value)}}]},finish_reason:null}]})+'\n\n');
    res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:submitted?'stop':'tool_calls'}]})+'\n\n');res.end('data: [DONE]\n\n');
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
  try{
    const result=await runReading(runSpec(`http://127.0.0.1:${port}/v1`,dir),async(name,args)=>{
      hostCalls.push(name);
      if(name==='prepare_source')return {source_manifest:{source_id:'main'},sections:[]};
      if(name==='read_skill_file')return {content:'Native workflow'};
      if(name==='host_native_status')return {sealed:true,note_path:'native/note.md',map_path:'native/map.md',reviews:{passed:true,quality:{passed:true},readability:{passed:true}}};
      if(name==='read_artifact'){assert.equal(args.path,'native/note.md');return {content:'Previously checked native note.',next_offset:null};}
      if(name==='write_artifact'){assert.equal(args.path,'product/review.json');return {path:args.path};}
      if(name==='list_artifacts')return {files:[]};
      throw new Error(`Unexpected operation: ${name}`);
    },()=>{},new AbortController().signal);
    assert.equal(result.provenance.native_reused,true);assert.equal(result.provenance.native_reuse_reason,'checkpoint_recovery');
    assert.equal((result.verification.native_skill as any).reused_from_checkpoint,true);
    assert.equal(hostCalls.filter(name=>name==='run_skill_step'||name==='host_native_attest').length,0);
    assert.equal(requests,2);
  }finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
});

test('provider errors expose a short diagnostic without returning upstream bodies',()=>{
  const raw='400: {"message":"Provider returned error","code":400,"metadata":{"raw":"The input is too long: the request payload exceeds the maximum message size accepted by the service. private-correlation-data"}}';
  const safe=safeError(new Error(raw));
  assert.match(safe,/context or payload limit/);assert.match(safe,/HTTP 400/);assert.ok(!safe.includes('private-correlation-data'));assert.ok(!safe.includes('metadata'));
  assert.equal(safeError(new Error('401: {"error":"secret upstream response"}')),'Model authentication failed (HTTP 401). Check the configured API key.');
});

test('runner image artifacts become SDK image content without base64 in textual tool results',async()=>{
  const {runReading}=await import('../src/runner.js');
  const dir=await mkdtemp(join(tmpdir(),'paper-runner-image-'));let requests=0;
  const imageData='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==';
  const server=createServer(async(req,res)=>{
    let body='';for await(const chunk of req)body+=chunk;const parsed=JSON.parse(body);requests++;
    if(requests===2){
      const tools=parsed.messages.filter((message:any)=>message.role==='tool');
      assert.ok(tools.length);assert.ok(tools.every((message:any)=>!JSON.stringify(message.content).includes(imageData)));
      const images=parsed.messages.flatMap((message:any)=>Array.isArray(message.content)?message.content:[]).filter((part:any)=>part.type==='image_url');
      assert.equal(images.length,1);assert.equal(images[0].image_url.url,`data:image/png;base64,${imageData}`);
    }
    const base={id:'image-test',object:'chat.completion.chunk',created:1,model:'fixture-model'};
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    const delta=requests===1?{tool_calls:[{index:0,id:'image-read',type:'function',function:{name:'read_artifact',arguments:'{"path":"native/figure.png"}'}}]}:{content:'Image inspected; no native completion claimed.'};
    res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta,finish_reason:null}]})+'\n\n');
    res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:requests===1?'tool_calls':'stop'}]})+'\n\n');res.end('data: [DONE]\n\n');
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
  try{
    const spec=runSpec(`http://127.0.0.1:${port}/v1`,dir);spec.model.vision=true;
    await assert.rejects(runReading(spec,async(name)=>{
      if(name==='prepare_source')return {source_manifest:{source_id:'main'},sections:[]};
      if(name==='read_skill_file')return {content:'Inspect figure with provided image tool.'};
      if(name==='host_native_status')return {sealed:false};
      if(name==='read_artifact')return {data:imageData,mime_type:'image/png',type:'image'};
      throw new Error('Unexpected host operation');
    },()=>{},new AbortController().signal),/without required finish_native/);
    assert.equal(requests,2);
  }finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
});

test('native/review protocol exposes shared budget and character pagination while preserving review gates',async()=>{
  const {runReading}=await import('../src/runner.js');
  for(const factualFailure of [false,true]){
    const dir=await mkdtemp(join(tmpdir(),'paper-runner-review-order-'));let qualityCalls=0,readabilityCalls=0;const attestations:any[]=[];
    const poisonWorkflow='WORKFLOW_SENTINEL: finished independent records and validator evidence are required for whole-task delivery.';
    const creationDate=new Date().toISOString().slice(0,10);
    const note=`---\ndate: ${creationDate}\n---\nThis paper was published in 2022. The method changes only biases. Results describe the comparison conditions and limitations.`;
    const map='独立阅读检查记录：（待真实独立阅读检查后填写）。验证脚本尚未执行。';
    const server=createServer(async(req,res)=>{
      let body='';for await(const chunk of req)body+=chunk;const parsed=JSON.parse(body);
      const names=parsed.tools.map((tool:any)=>tool.function.name);
      const priorTool=parsed.messages.some((message:any)=>message.role==='tool');
      let call:any;
      if(!priorTool){
        const user=parsed.messages.find((message:any)=>message.role==='user');
        const userText=typeof user.content==='string'?user.content:user.content.filter((part:any)=>part.type==='text').map((part:any)=>part.text).join('');
        const payload=JSON.parse(userText);
        assert.deepEqual(payload.limits,{timeout_seconds:20,max_tool_calls:10,max_related_papers:3});
        assert.equal(payload.runtime_budget.max_tool_calls,10);
        assert.equal(payload.runtime_budget.tool_calls_remaining,10-payload.runtime_budget.tool_calls_used);
        assert.match(payload.runtime_budget.scope,/Shared across the whole task/);
        const sourceTool=parsed.tools.find((tool:any)=>tool.function.name==='read_source')?.function;
        if(sourceTool){
          assert.match(sourceTool.description,/CHARACTERS/);
          assert.match(sourceTool.description,/next_offset/);
          assert.match(sourceTool.parameters.properties.offset.description,/Character offset/);
          assert.match(sourceTool.parameters.properties.limit.description,/Default 16000; host maximum 32000/);
        }
        if(names.includes('review_native')){
          assert.equal(payload.runtime_budget.tool_calls_used,3);
          const nativeSystem=JSON.stringify(parsed.messages.filter((message:any)=>message.role==='system'||message.role==='developer'));
          assert.match(nativeSystem,/read back the SAVED note using read_artifact/);
          assert.match(nativeSystem,/preserve literal spaces, punctuation and backticks exactly/);
          assert.match(nativeSystem,/including equation fences/);
          assert.match(nativeSystem,/Each frontstage 名称 must itself be an actual body substring/);
          call={name:'review_native',arguments:JSON.stringify({note_path:'native/note.md',map_path:'native/map.md'})};
        }else{
          const systemText=JSON.stringify(parsed.messages.filter((message:any)=>message.role==='system'||message.role==='developer'));
          assert.match(systemText,/Within this review session, reuse source passages/);
          assert.match(systemText,/report only source coverage you actually inspected/);
          assert.equal(payload.review_context.reading_date,creationDate);
          assert.ok(Math.abs(Date.now()-Date.parse(payload.review_context.current_utc))<60000);
          assert.ok(payload.review_context.date_semantics.includes('not paper publication year'));
          assert.ok(!userText.includes('WORKFLOW_SENTINEL'),'whole-skill completion instructions must not leak into reviewer context');
          if(payload.task==='Native content quality review'){
            assert.equal(payload.runtime_budget.tool_calls_used,6);
            qualityCalls++;assert.equal(payload.map,map);assert.equal(payload.phase_contract.evaluates,'content_and_evidence');
            call={name:'submit_review',arguments:JSON.stringify({passed:!factualFailure,issues:factualFailure?['The claimed baseline comparison contradicts the source.']:[],checks:{comparison:factualFailure?'unsupported':'supported',suggestions:['An additional worked example would be optional.']}})};
          }else{
            assert.equal(payload.runtime_budget.tool_calls_used,7);
            readabilityCalls++;assert.equal(payload.note,note);assert.equal(payload.map,undefined);assert.equal(payload.phase_contract.evaluates,'final_note_only');
            assert.ok(!payload.reading_criteria.includes('STORAGE_SENTINEL'));
            call={name:'submit_review',arguments:JSON.stringify({passed:true,issues:[],checks:{retelling:'The note explains the method and qualified result.'},reader_record:Object.fromEntries(READER_RECORD_FIELDS.map(key=>[key,`Actual reader answer: ${key}`]))})};
          }
        }
      }
      const base={id:'review-order',object:'chat.completion.chunk',created:1,model:'fixture-model'};
      res.writeHead(200,{'Content-Type':'text/event-stream'});
      res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:call?{tool_calls:[{index:0,id:'review-step',type:'function',function:call}]}:{content:'Review phase finished; no whole-task completion claimed.'},finish_reason:null}]})+'\n\n');
      res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:call?'tool_calls':'stop'}]})+'\n\n');res.end('data: [DONE]\n\n');
    });
    server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
    try{
      await assert.rejects(runReading(runSpec(`http://127.0.0.1:${port}/v1`,dir),async(name,args)=>{
        if(name==='prepare_source')return {source_manifest:{source_id:'main'},sections:[]};
        if(name==='read_skill_file')return {content:args.path==='ReadingGuide.md'?'## 独立阅读检查\nRead the final note and report actual understanding.\n## 写入与验证\nSTORAGE_SENTINEL':poisonWorkflow};
        if(name==='host_native_status')return {sealed:false};
        if(name==='host_native_review_lookup')return {};
        if(name==='read_artifact')return {content:args.path==='native/map.md'?map:note,next_offset:null};
        if(name==='host_native_attest'){attestations.push(args);return {recorded:true};}
        throw new Error('Unexpected host operation');
      },()=>{},new AbortController().signal),/without required finish_native/);
      assert.equal(qualityCalls,1);assert.equal(readabilityCalls,factualFailure?0:1);assert.equal(attestations.length,1);
      assert.equal(attestations[0].quality.passed,!factualFailure);assert.equal(attestations[0].readability.passed,!factualFailure);
    }finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
  }
});

test('hard tool-budget exhaustion aborts both native and nested review model loops',async()=>{
  const {runReading}=await import('../src/runner.js');
  const dir=await mkdtemp(join(tmpdir(),'paper-runner-hard-budget-'));const hostCalls:string[]=[];let requests=0;
  const server=createServer(async(req,res)=>{
    let body='';for await(const chunk of req)body+=chunk;const parsed=JSON.parse(body);requests++;
    const native=parsed.tools.some((tool:any)=>tool.function.name==='review_native');
    // Deliberately never stop or obey budget hints. Only the host abort can end
    // this fixture; a recoverable tool error would keep generating requests.
    const call=native?{name:'review_native',arguments:'{"note_path":"native/note.md"}'}:{name:'read_source',arguments:'{}'};
    const base={id:'budget-test',object:'chat.completion.chunk',created:1,model:'fixture-model'};
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{tool_calls:[{index:0,id:`budget-call-${requests}`,type:'function',function:call}]},finish_reason:null}]})+'\n\n');
    res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:'tool_calls'}]})+'\n\n');res.end('data: [DONE]\n\n');
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
  const control=new AbortController();const failsafe=setTimeout(()=>control.abort(),3000);
  try{
    const spec=runSpec(`http://127.0.0.1:${port}/v1`,dir);spec.limits.max_tool_calls=6;
    const started=Date.now();
    await assert.rejects(runReading(spec,async(name)=>{
      hostCalls.push(name);
      if(name==='prepare_source')return {source_manifest:{source_id:'main'},sections:[]};
      if(name==='read_skill_file')return {content:'Read and independently review the note.'};
      if(name==='host_native_status')return {sealed:false};
      if(name==='host_native_review_lookup')return {};
      if(name==='read_artifact')return {content:'An existing native note.',next_offset:null};
      if(name==='read_source')return {text:'Original source evidence.'};
      throw new Error('Unexpected host operation');
    },()=>{},control.signal),{message:'Task tool budget exhausted'});
    assert.ok(Date.now()-started<2000,'budget abort must not wait for the external timeout');
    assert.equal(control.signal.aborted,false,'task budget is distinct from caller cancellation');
    assert.equal(requests,3,'no provider request after the denied tool call');
    assert.deepEqual(hostCalls,['prepare_source','read_skill_file','host_native_status','host_native_review_lookup','read_artifact','read_source']);
  }finally{clearTimeout(failsafe);server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
});

test('external cancellation remains Cancelled instead of a budget diagnostic',async()=>{
  const {runReading}=await import('../src/runner.js');const control=new AbortController();control.abort();
  let calls=0;
  await assert.rejects(runReading(runSpec('http://127.0.0.1:1/v1','/tmp'),async()=>{calls++;return {};},()=>{},control.signal),{message:'Cancelled'});
  assert.equal(calls,0);
});
