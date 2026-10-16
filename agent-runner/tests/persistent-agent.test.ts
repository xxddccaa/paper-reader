import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer,type ServerResponse} from 'node:http';
import {once} from 'node:events';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {agentTaskOverview,boundAgentContext,checkpointMessages,pendingAgentCalls,runPiAgent,strictAgentArguments} from '../src/persistent-agent.js';
import {assertRunSpec,type AgentToolSpec,type RunSpec,type ToolCall} from '../src/protocol.js';

const save:AgentToolSpec={name:'save_findings',description:'Save bounded findings',parameters:{type:'object',additionalProperties:false,required:['n'],properties:{n:{type:'integer',minimum:1}}}};
const finish:AgentToolSpec={name:'submit_final',description:'Submit the saved version after verification',parameters:{type:'object',additionalProperties:false,required:['document_version','review_ids'],properties:{document_version:{type:'integer',minimum:1},review_ids:{type:'array',items:{type:'string'}}}}};
const finalArgs={document_version:1,review_ids:['host-review']};
const usage={input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
const assistant=(content:any[],stopReason='toolUse'):any=>({role:'assistant',content,stopReason,api:'openai-completions',provider:'paper-reader-tenant',model:'fixture',timestamp:1,usage});
function respond(res:ServerResponse,index:number,name?:string,args?:unknown,stop?:string){
  res.writeHead(200,{'Content-Type':'text/event-stream'});
  const base={id:`fixture_${index}`,object:'chat.completion.chunk',created:1,model:'fixture'};
  const delta=name?{tool_calls:[{index:0,id:'provider-reuses-this-id',type:'function',function:{name,arguments:JSON.stringify(args)}}]}:{content:'A complete ordinary response.'};
  res.write(`data: ${JSON.stringify({...base,choices:[{index:0,delta,finish_reason:null}]})}\n\n`);
  res.write(`data: ${JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:stop??(name?'tool_calls':'stop')}],usage:{prompt_tokens:10,completion_tokens:5,total_tokens:15}})}\n\n`);
  res.end('data: [DONE]\n\n');
}
function spec(base:string,dir:string):RunSpec{
  return {type:'run',run_id:'persistent-fixture',kind:'read',execution_engine:'pi-agent-v1',skill_id:'ljg-paper',skill_root:dir,work_dir:dir,workflow_mode:'guided',agent:{operation:'run',goal:'Read and verify this paper completely',state:{goal:'Read paper'},messages:[],tools:[save,finish]},model:{api_key:'fixture-only-key',protocol:'openai-completions',base_url:base,model:'fixture',vision:false,context_window:64000,max_tokens:2000,allow_private_models:true},paper:{id:'paper',title:'Persistent reading fixture',source_type:'pdf'},template:{brief_markdown:'# Brief',full_markdown:'# Full',instructions:''},limits:{timeout_seconds:10,max_tool_calls:20,max_related_papers:3}};
}
async function fixture(handler:(body:any,res:ServerResponse,index:number)=>void,run:(base:string,dir:string,bodies:any[])=>Promise<void>){
  const dir=await mkdtemp(join(tmpdir(),'pi-persistent-')),bodies:any[]=[],errors:unknown[]=[];
  const server=createServer((req,res)=>{void(async()=>{let data='';for await(const chunk of req)data+=chunk;const body=JSON.parse(data);bodies.push(body);handler(body,res,bodies.length-1);})().catch(error=>{errors.push(error);res.writeHead(500);res.end();});});
  server.listen(0,'127.0.0.1');await once(server,'listening');
  try{await run(`http://127.0.0.1:${(server.address() as any).port}/v1`,dir,bodies);assert.deepEqual(errors,[]);}
  finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
}
function host(){
  const calls:Array<{name:string,args:any}>=[],attempts=new Map<string,string>(),effects=new Map<string,any>();let saved:any[]=[];
  const rpc:ToolCall=async(name,args)=>{
    calls.push({name,args:structuredClone(args)});
    if(name==='host_agent_attempt'){
      assert.match(String(args.call_id),/^[a-f0-9-]{36}$/);attempts.set(String(args.call_id),String(args.name));return {accepted:true,remaining_seconds:100,remaining_tool_calls:20-attempts.size};
    }
    if(name==='host_agent_checkpoint'){
      saved=structuredClone(args.messages as any[]);assert.deepEqual(checkpointMessages(saved),saved);return {remaining_seconds:100};
    }
    if(name==='host_agent_observation')return {accepted:true};
    assert.equal(name,'host_agent_action');assert.equal(attempts.get(String(args.call_id)),args.name);
    if(effects.has(String(args.call_id)))return effects.get(String(args.call_id));
    const result=args.name==='submit_final'?{accepted:true,submission_id:'host-sealed-version',document_version:1}:{accepted:true,saved:args.args};
    effects.set(String(args.call_id),result);return result;
  };
  return {rpc,calls,attempts,effects,get saved(){return saved;}};
}
const feedback=(body:any)=>body.messages.filter((message:any)=>message.role==='tool').map((message:any)=>typeof message.content==='string'?message.content:JSON.stringify(message.content)).join('\n');

test('persistent spec bypasses legacy phase/chat selector but reserves internal host names',()=>{
  const value=spec('http://127.0.0.1:1/v1','/tmp');assertRunSpec(value);
  assertRunSpec({...value,kind:'chat',chat:undefined});
  assert.throws(()=>assertRunSpec({...value,agent:{...value.agent!,tools:[{...save,name:'host_agent_checkpoint'}]}}),/reserved/);
  assert.throws(()=>assertRunSpec({...value,execution_engine:undefined}),/Agent context requires/);
});

test('raw dynamic tool arguments cannot exploit SDK scalar coercion or unknown fields',()=>{
  assert.deepEqual(strictAgentArguments(save,{n:2}),{n:2});
  for(const value of [{n:'2'},{n:2,extra:true},{n:null},{n:0}])assert.throws(()=>strictAgentArguments(save,value));
});

test('task overview does not expose originals, draft histories, arbitrary state or full base product',()=>{
  const result=agentTaskOverview({goal:'Read paper',document_version:7,draft_history:['PRIVATE'.repeat(100000)],evidence:{x:'SECRET_ORIGINAL'},drafts:{a:{}},findings:{c:{}},unknown:'UNTRUSTED',remaining_work:['Inspect appendix']});
  assert.equal(result.document_version,7);assert.equal(result.drafts_count,1);assert.equal(result.findings_count,1);
  assert.ok(JSON.stringify(result).length<1000);assert.doesNotMatch(JSON.stringify(result),/SECRET_ORIGINAL|PRIVATE|UNTRUSTED/);
});

test('checkpoints omit failed/truncated proposals, malformed containers and image bytes while retaining complete pending calls',()=>{
  const good=assistant([{type:'toolCall',id:'saved',name:'save_findings',arguments:{n:1}}]);
  const bad=assistant([{type:'toolCall',id:'truncated',name:'save_findings',arguments:{n:2}}],'length');
  const malformed=assistant([{type:'toolCall',id:'malformed',name:'save_findings',arguments:'{"n":1}'}]);
  const result=checkpointMessages([good,bad,malformed,{role:'toolResult',toolCallId:'truncated',toolName:'save_findings',content:[{type:'text',text:'error'}]},assistant([{type:'toolCall',id:'visual',name:'read_visual',arguments:{figure_id:'f1'}}]),{role:'toolResult',toolCallId:'visual',toolName:'read_visual',content:[{type:'image',data:'SECRET_BINARY',mimeType:'image/png'}],isError:false,timestamp:1}]);
  assert.deepEqual(pendingAgentCalls(result).map(call=>call.id),['saved']);
  assert.doesNotMatch(JSON.stringify(result),/SECRET_BINARY|truncated|malformed/);assert.match(JSON.stringify(result),/reread/);
});

test('real Pi loop reserves invalid proposals before validation and chooses changed valid action in same session',async()=>{
  await fixture((body,res,index)=>{
    if(index===0)respond(res,index,'save_findings',{n:'2'});
    else if(index===1){assert.match(feedback(body),/invalid field types/);respond(res,index,'save_findings',{n:2});}
    else respond(res,index,'submit_final',finalArgs);
  },async(base,dir,bodies)=>{
    const h=host();const result=await runPiAgent(spec(base,dir),h.rpc,()=>{},new AbortController().signal);
    assert.equal(result.status,'completed');assert.equal(result.submission.submission_id,'host-sealed-version');
    assert.equal(h.attempts.size,3);assert.equal(h.effects.size,2);assert.equal(bodies.length,3);
    assert.equal(bodies[1].messages.filter((m:any)=>m.role==='user').length,1,'tool recovery keeps original task in same session');
    const action=h.calls.findIndex(c=>c.name==='host_agent_action'&&c.args.name==='save_findings');
    const checkpoint=h.calls.slice(0,action).filter(c=>c.name==='host_agent_checkpoint').at(-1)!;
    assert.ok(pendingAgentCalls(checkpoint.args.messages).length,'proposal is durable before effect');
    assert.equal(pendingAgentCalls(h.saved).length,0,'accepted final reply is checkpointed');
  });
});

test('truncated response never executes, accepted siblings survive and Agent sees scope recovery feedback',async()=>{
  await fixture((body,res,index)=>{
    if(index===0)respond(res,index,'save_findings',{n:1});
    else if(index===1)respond(res,index,'save_findings',{n:99},'length');
    else{assert.match(JSON.stringify(body.messages),/truncated/);assert.match(feedback(body),/saved/);respond(res,index,'submit_final',finalArgs);}
  },async(base,dir)=>{
    const h=host();const result=await runPiAgent(spec(base,dir),h.rpc,()=>{},new AbortController().signal);
    assert.equal(result.status,'completed');assert.equal(h.attempts.size,3);
    assert.deepEqual([...h.effects.values()].filter(v=>v.saved).map(v=>v.saved),[{n:1}]);
    assert.ok(h.saved.every(message=>message.stopReason!=='length'));
  });
});

test('request deadline covers a live partial stream and recovers without executing partial arguments',async()=>{
  await fixture((_body,res,index)=>{
    if(index===0){res.writeHead(200,{'Content-Type':'text/event-stream'});res.write('data: '+JSON.stringify({id:'partial',object:'chat.completion.chunk',created:1,model:'fixture',choices:[{index:0,delta:{tool_calls:[{index:0,id:'partial-call',type:'function',function:{name:'save_findings',arguments:'{"n":'}}]},finish_reason:null}]})+'\n\n');}
    else respond(res,index,'submit_final',finalArgs);
  },async(base,dir,bodies)=>{
    const h=host();const result=await runPiAgent(spec(base,dir),h.rpc,()=>{},new AbortController().signal,{requestTimeoutMs:150});
    assert.equal(result.status,'completed');assert.equal(h.effects.size,1);assert.equal(bodies.length,2);
    assert.match(JSON.stringify(bodies[1].messages),/deadline/);assert.ok(!h.saved.some(m=>m.stopReason==='aborted'));
  });
});

test('crash after host write replays identical saved UUID once, then continues accepted task',async()=>{
  await fixture((_body,res,index)=>respond(res,index,index===0?'save_findings':'submit_final',index===0?{n:1}:finalArgs),async(base,dir)=>{
    const h=host();let crash=true;
    const flaky:ToolCall=async(name,args,signal)=>{const result=await h.rpc(name,args,signal);if(name==='host_agent_action'&&args.name==='save_findings'&&crash){crash=false;throw new Error('Worker disconnected');}return result;};
    await assert.rejects(runPiAgent(spec(base,dir),flaky,()=>{},new AbortController().signal),/Worker disconnected/);
    const pending=pendingAgentCalls(h.saved);assert.equal(pending.length,1);assert.equal(h.effects.size,1);
    const resume=spec(base,dir);resume.agent!.messages=h.saved;
    const result=await runPiAgent(resume,h.rpc,()=>{},new AbortController().signal);
    assert.equal(result.status,'completed');assert.equal(h.effects.size,2);
    const saves=h.calls.filter(c=>c.name==='host_agent_action'&&c.args.name==='save_findings');assert.equal(saves.length,2);assert.equal(saves[0].args.call_id,saves[1].args.call_id);assert.equal(h.attempts.size,2);
  });
});

test('fresh reviewer has only read-only dynamic tools and cannot overwrite main checkpoint or manufacture host action',async()=>{
  await fixture((body,res,index)=>{
    const names=body.tools.map((tool:any)=>tool.function.name);assert.deepEqual(names.sort(),['read_source','submit_review']);
    respond(res,index,index===0?'read_source':'submit_review',index===0?{n:1}:{passed:true,issues:[]});
  },async(base,dir)=>{
    const h=host(),value=spec(base,dir);value.agent={...value.agent!,operation:'review',messages:[{role:'user',content:'MAIN_PRIVATE_HISTORY',timestamp:1}],review_input:{kind:'fact',source:'Host material'},tools:[save,finish,{...save,name:'read_source'}]};
    const result=await runPiAgent(value,h.rpc,()=>{},new AbortController().signal);
    assert.deepEqual(result,{passed:true,issues:[]});assert.equal(h.attempts.size,2);
    assert.ok(!h.calls.some(c=>c.name==='host_agent_checkpoint'));assert.ok(!h.calls.some(c=>c.name==='host_agent_action'&&c.args.name==='submit_review'));
  });
});

test('host budget exhaustion returns resumable waiting; cancellation remains cancellation',async()=>{
  await fixture((_body,res,index)=>respond(res,index,'save_findings',{n:1}),async(base,dir)=>{
    const h=host();const rpc:ToolCall=async(name,args,signal)=>{if(name==='host_agent_attempt')throw new Error('Agent tool budget exhausted');return h.rpc(name,args,signal);};
    const result=await runPiAgent(spec(base,dir),rpc,()=>{},new AbortController().signal);
    assert.equal(result.status,'waiting');assert.match(result.reason,/budget exhausted/);assert.equal(h.effects.size,0);
    const abort=new AbortController();abort.abort();await assert.rejects(runPiAgent(spec(base,dir),h.rpc,()=>{},abort.signal),/Cancelled/);
  });
});

test('unchanged no-progress actions end with work saved, never fake completion',async()=>{
  await fixture((_body,res,index)=>respond(res,index,'save_findings',{n:1}),async(base,dir)=>{
    const h=host();const result=await runPiAgent(spec(base,dir),h.rpc,()=>{},new AbortController().signal,{maxNoProgress:3});
    assert.equal(result.status,'waiting');assert.match(result.reason,/没有产生新进展/);assert.ok(h.saved.length>0);
  });
});

test('actual images remain in model context, and insufficient image context fails explicitly',()=>{
  const pixels='REAL_BASE64'.repeat(100000);
  const context:any={messages:[{role:'system',content:'Inspect actual pixels.',timestamp:1},{role:'user',content:'Check Figure 1',timestamp:1},assistant([{type:'toolCall',id:'image',name:'read_visual',arguments:{figure_id:'f1'}}]),{role:'toolResult',toolCallId:'image',toolName:'read_visual',content:[{type:'image',data:pixels,mimeType:'image/png'}],isError:false,timestamp:1}]};
  const bounded=boundAgentContext(context,64000,2000);assert.equal((bounded.messages.at(-1)!.content as any[])[0].data,pixels);
  assert.throws(()=>boundAgentContext(context,16000,2000),/actual visual input/);
});

test('safe idle Pi compaction checkpoints summary and resumes the same main task',async()=>{
  let ordinary=0,compactions=0;
  await fixture((body,res,index)=>{
    if(!body.tools?.length){compactions++;respond(res,index);return;}
    ordinary++;
    respond(res,index,ordinary<=5?'read_source':'submit_final',ordinary<=5?{n:ordinary}:finalArgs);
  },async(base,dir)=>{
    const h=host(),value=spec(base,dir);value.model.context_window=24000;value.agent!.tools=[{...save,name:'read_source'},finish];
    const rpc:ToolCall=async(name,args,signal)=>{
      const result=await h.rpc(name,args,signal);
      if(name==='host_agent_action'&&args.name==='read_source')return {...result,text:'original source statement '.repeat(900)};
      return result;
    };
    const result=await runPiAgent(value,rpc,()=>{},new AbortController().signal);
    assert.equal(result.status,'completed');assert.ok(compactions>=1,'Pi SDK summary request actually ran');
    assert.ok(h.calls.some(c=>c.name==='host_agent_checkpoint'&&c.args.summary?.includes('A complete ordinary response.')),'SDK summary is durably checkpointed');
    assert.ok(h.saved.every(message=>message.role!=='compactionSummary'),'summary is stored separately from replayable normal messages');
  });
});

test('visual reviewer cannot approve an image until the next actual model request receives its pixels',async()=>{
  const pixels='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9WQAAAAASUVORK5CYII=';
  await fixture((body,res,index)=>{
    if(index===0){
      res.writeHead(200,{'Content-Type':'text/event-stream'});
      const base={id:'review-batch',object:'chat.completion.chunk',created:1,model:'fixture'};
      const tool_calls=[{name:'read_visual',args:{figure_id:'f1'}},{name:'submit_review',args:{passed:true,issues:[]}}].map((tool,i)=>({index:i,id:'call_'+i,type:'function',function:{name:tool.name,arguments:JSON.stringify(tool.args)}}));
      res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{tool_calls},finish_reason:null}]})+'\n\n');
      res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:'tool_calls'}]})+'\n\n');res.end('data: [DONE]\n\n');
    }else{
      assert.match(feedback(body),/not yet been presented/);
      assert.ok(JSON.stringify(body.messages).includes('data:image/png;base64,'+pixels),'actual provider input includes returned image bytes');
      respond(res,index,'submit_review',{passed:true,issues:[]});
    }
  },async(base,dir,bodies)=>{
    const value=spec(base,dir),h=host();value.model.vision=true;value.agent={...value.agent!,operation:'review',review_input:{kind:'visual',required_visual_ids:['f1']},tools:[{name:'read_visual',description:'Read actual image',parameters:{type:'object',additionalProperties:false,required:['figure_id'],properties:{figure_id:{type:'string'}}}}]};
    const rpc:ToolCall=async(name,args,signal)=>{const result=await h.rpc(name,args,signal);return name==='host_agent_action'&&args.name==='read_visual'?{type:'image',data:pixels,mime_type:'image/png',figure_id:'f1',sha256:'host-verified'}:result;};
    const result=await runPiAgent(value,rpc,()=>{},new AbortController().signal);
    assert.deepEqual(result,{passed:true,issues:[]});assert.equal(bodies.length,2);assert.equal(h.attempts.size,3);
  });
});

test('provider mislabelled tool_calls cannot execute SDK-repaired incomplete JSON',async()=>{
  await fixture((body,res,index)=>{
    if(index===0){
      res.writeHead(200,{'Content-Type':'text/event-stream'});
      const base={id:'invalid-wire',object:'chat.completion.chunk',created:1,model:'fixture'};
      res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{tool_calls:[{index:0,id:'bad',type:'function',function:{name:'save_findings',arguments:'{"n":123'}}]},finish_reason:null}]})+'\n\n');
      res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:'tool_calls'}]})+'\n\n');res.end('data: [DONE]\n\n');
    }else{assert.match(feedback(body),/incomplete or malformed JSON/);respond(res,index,'submit_final',finalArgs);}
  },async(base,dir)=>{
    const h=host();const result=await runPiAgent(spec(base,dir),h.rpc,()=>{},new AbortController().signal);
    assert.equal(result.status,'completed');assert.equal(h.attempts.size,2);assert.equal(h.effects.size,1);
    assert.ok(!h.calls.some(c=>c.name==='host_agent_action'&&c.args.name==='save_findings'));
    assert.ok(!pendingAgentCalls(h.saved).length);
  });
});

test('invented tool attempts are charged and corrected in the same session without executing',async()=>{
  await fixture((body,res,index)=>{
    if(index===0)respond(res,index,'invented_tool',{n:1});
    else{assert.match(feedback(body),/invented_tool not found/);respond(res,index,'submit_final',finalArgs);}
  },async(base,dir,bodies)=>{
    const h=host();
    const rpc:ToolCall=async(name,args,signal)=>{
      const result=await h.rpc(name,args,signal);
      if(name==='host_agent_attempt'&&args.name==='invented_tool')throw new Error('Tool is not permitted in this Agent session');
      return result;
    };
    const result=await runPiAgent(spec(base,dir),rpc,()=>{},new AbortController().signal);
    assert.equal(result.status,'completed');assert.equal(bodies.length,2);
    assert.equal(h.attempts.size,2);assert.equal(h.effects.size,1);
    assert.ok(!h.calls.some(call=>call.name==='host_agent_action'&&call.args.name==='invented_tool'));
  });
});

test('all unseen images in the latest batch survive bounding even with a trailing text result',()=>{
  const context:any={messages:[{role:'system',content:'Inspect both images.',timestamp:1},{role:'user',content:'Compare figures',timestamp:1},
    assistant(['one','two','text'].map(id=>({type:'toolCall',id,name:id==='text'?'read_source':'read_visual',arguments:{}}))),
    ...['one','two'].map(id=>({role:'toolResult',toolCallId:id,toolName:'read_visual',content:[{type:'image',data:'PIXELS_'+id,mimeType:'image/png'}],isError:false,timestamp:1})),
    {role:'toolResult',toolCallId:'text',toolName:'read_source',content:[{type:'text',text:'Small source result'}],isError:false,timestamp:1}]};
  const bounded=boundAgentContext(context,64000,2000);
  assert.match(JSON.stringify(bounded),/PIXELS_one/);assert.match(JSON.stringify(bounded),/PIXELS_two/);
  assert.throws(()=>boundAgentContext(context,30000,2000),/actual visual input/,'cannot omit the first unseen figure to fit the second');
});

test('fresh advisory subtask has no main history or writes and shares host attempt accounting',async()=>{
  await fixture((body,res,index)=>{
    assert.deepEqual(body.tools.map((tool:any)=>tool.function.name).sort(),['read_source','submit_subtask']);
    assert.doesNotMatch(JSON.stringify(body.messages),/MAIN_PRIVATE_HISTORY|MAIN_SUMMARY/);
    respond(res,index,index===0?'read_source':'submit_subtask',index===0?{n:1}:{summary:'Original source locator and uncertainty retained',remaining_work:['Check appendix']});
  },async(base,dir)=>{
    const h=host(),value=spec(base,dir);
    value.agent={...value.agent!,operation:'subtask',summary:'MAIN_SUMMARY',messages:[{role:'user',content:'MAIN_PRIVATE_HISTORY',timestamp:1}],review_input:{goal:'Inspect appendix'},tools:[save,finish,{...save,name:'read_source'}]};
    const result=await runPiAgent(value,h.rpc,()=>{},new AbortController().signal);
    assert.deepEqual(result,{summary:'Original source locator and uncertainty retained',remaining_work:['Check appendix']});
    assert.equal(h.attempts.size,2);assert.equal(h.effects.size,1);
    assert.ok(!h.calls.some(call=>call.name==='host_agent_checkpoint'));
  });
});

test('cancellation during partial arguments preserves earlier completed work and never executes partial proposal',async()=>{
  const cancellation=new AbortController();
  await fixture((_body,res,index)=>{
    if(index===0){respond(res,index,'save_findings',{n:1});return;}
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    res.write('data: '+JSON.stringify({id:'partial',object:'chat.completion.chunk',created:1,model:'fixture',choices:[{index:0,delta:{tool_calls:[{index:0,id:'cancelled-call',type:'function',function:{name:'save_findings',arguments:'{"n":999'}}]},finish_reason:null}]})+'\n\n');
    setTimeout(()=>cancellation.abort(),25);
  },async(base,dir,bodies)=>{
    const h=host();await assert.rejects(runPiAgent(spec(base,dir),h.rpc,()=>{},cancellation.signal),/Cancelled/);
    assert.equal(bodies.length,2);assert.equal(h.effects.size,1);
    assert.deepEqual([...h.effects.values()].map(result=>result.saved),[{n:1}]);
    assert.equal(pendingAgentCalls(h.saved).length,0);assert.doesNotMatch(JSON.stringify(h.saved),/999|cancelled-call/);
  });
});

test('pending compaction waits until an actual model response has inspected the new image',async()=>{
  const pixels='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9WQAAAAASUVORK5CYII=';
  let ordinary=0,compactions=0;
  await fixture((body,res,index)=>{
    if(!body.tools?.length){compactions++;respond(res,index);return;}
    ordinary++;
    if(ordinary<=4)respond(res,index,'read_source',{n:ordinary});
    else if(ordinary===5)respond(res,index,'read_visual',{n:1});
    else{
      assert.ok(JSON.stringify(body.messages).includes('data:image/png;base64,'+pixels),'unseen original pixels must reach the model before compaction');
      respond(res,index,'submit_final',finalArgs);
    }
  },async(base,dir)=>{
    const h=host(),value=spec(base,dir);value.model.vision=true;
    value.agent!.tools=[{...save,name:'read_source'},{...save,name:'read_visual'},finish];
    const rpc:ToolCall=async(name,args,signal)=>{
      const result=await h.rpc(name,args,signal);
      if(name==='host_agent_action'&&args.name==='read_source')return {...result,text:'a'.repeat(10000)};
      if(name==='host_agent_action'&&args.name==='read_visual')return {type:'image',data:pixels,mime_type:'image/png'};
      return result;
    };
    const result=await runPiAgent(value,rpc,()=>{},new AbortController().signal);
    assert.equal(result.status,'completed');assert.equal(ordinary,6);assert.equal(compactions,0);
  });
});

test('Anthropic incomplete streamed JSON remains unexecutable even when provider claims tool_use',async()=>{
  await fixture((_body,res,index)=>{
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    const sse=(type:string,data:any)=>res.write(`event: ${type}\ndata: ${JSON.stringify({type,...data})}\n\n`);
    sse('message_start',{message:{id:'anthropic_'+index,type:'message',role:'assistant',content:[],model:'fixture',stop_reason:null,stop_sequence:null,usage:{input_tokens:10,output_tokens:0}}});
    sse('content_block_start',{index:0,content_block:{type:'tool_use',id:'anthropic_call',name:index===0?'save_findings':'submit_final',input:{}}});
    sse('content_block_delta',{index:0,delta:{type:'input_json_delta',partial_json:index===0?'{"n":123':JSON.stringify(finalArgs)}});
    sse('content_block_stop',{index:0});
    sse('message_delta',{delta:{stop_reason:'tool_use',stop_sequence:null},usage:{output_tokens:5}});
    sse('message_stop',{});res.end();
  },async(base,dir,bodies)=>{
    const h=host(),value=spec(base,dir);value.model.protocol='anthropic-messages';
    const result=await runPiAgent(value,h.rpc,()=>{},new AbortController().signal);
    assert.equal(result.status,'completed');assert.equal(bodies.length,2);assert.equal(h.attempts.size,2);assert.equal(h.effects.size,1);
    assert.match(JSON.stringify(bodies[1].messages),/incomplete or malformed JSON/);
    assert.ok(!h.calls.some(call=>call.name==='host_agent_action'&&call.args.name==='save_findings'));
  });
});
