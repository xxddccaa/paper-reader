import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer,type ServerResponse} from 'node:http';
import {once} from 'node:events';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Type} from '@sinclair/typebox';
import {Value} from '@sinclair/typebox/value';
import {decodeStructuredArguments,structuredArgumentErrors} from '../src/structured-arguments.js';
import {phaseSchema,runPhase,validatePhaseResult} from '../src/phases.js';
import {WORKFLOW_V3,type RunSpec} from '../src/protocol.js';

const quote='The measured effect depends on the evaluation conditions.';
const claim={id:'C1',kind:'result',statement:'结果依赖评估条件。',basis:'observation',conditions:{model:'',dataset:'',split:'',metric:'',baseline:'',budget:''},evidence:[{id:'E1',source_id:'main',section_id:'s1',quote}]};
const packet={version:2,paper_title:'Structured recovery fixture',research_question:'Which conditions constrain the measured effect?',claims:[claim],open_questions:[],limitations:[]};
const submission={checked_section_ids:['s1'],unresolved:[],packet};
const schema=phaseSchema('chunk_extract',WORKFLOW_V3);
// Same syntax failure as the retained DeepSeek attempt: one extra closing brace
// ends packet before open_questions. No live model or source is needed here.
const brokenPacket=JSON.stringify(packet).replace(',"open_questions":','},"open_questions":');

function send(res:ServerResponse,args:unknown,index:number,text?:string):void{
  res.writeHead(200,{'Content-Type':'text/event-stream'});
  const base={id:`fixture_${index}`,object:'chat.completion.chunk',created:1,model:'offline-fixture'};
  const delta=text===undefined?{tool_calls:[{index:0,id:`call_${index}`,type:'function',function:{name:'submit_phase_result',arguments:JSON.stringify(args)}}]}:{content:text};
  res.write(`data: ${JSON.stringify({...base,choices:[{index:0,delta,finish_reason:null}]})}\n\n`);
  res.write(`data: ${JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:text===undefined?'tool_calls':'stop'}],usage:{prompt_tokens:10,completion_tokens:5,total_tokens:15}})}\n\n`);
  res.end('data: [DONE]\n\n');
}
function spec(base:string,dir:string,budget=8):RunSpec{
  return {type:'run',run_id:'structured-recovery-fixture',kind:'read',skill_id:'ljg-paper',skill_root:dir,work_dir:dir,workflow_mode:'guided',workflow_version:WORKFLOW_V3,phase:{name:'chunk_extract',input:{source_sections:[{id:'s1',source_id:'main',text:quote}],skill_guidance:'Read the supplied original evidence.'}},model:{api_key:'fixture-only-key',protocol:'openai-completions',base_url:base,model:'offline-fixture',vision:false,context_window:128000,max_tokens:16000,allow_private_models:true},paper:{id:'paper',title:packet.paper_title,source_type:'pdf'},template:{brief_markdown:'# Brief',full_markdown:'# Full',instructions:''},limits:{timeout_seconds:10,max_tool_calls:budget,max_related_papers:3}};
}
async function fixture(handler:(body:any,res:ServerResponse,index:number)=>void,run:(base:string,dir:string,bodies:any[])=>Promise<void>){
  const dir=await mkdtemp(join(tmpdir(),'runner-structured-recovery-')),bodies:any[]=[],errors:unknown[]=[];
  const server=createServer((req,res)=>{void(async()=>{let data='';for await(const chunk of req)data+=chunk;const body=JSON.parse(data);bodies.push(body);handler(body,res,bodies.length-1);})().catch(error=>{errors.push(error);res.writeHead(500);res.end();});});
  server.listen(0,'127.0.0.1');await once(server,'listening');
  try{await run(`http://127.0.0.1:${(server.address() as any).port}/v1`,dir,bodies);assert.deepEqual(errors,[]);}
  finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
}
const feedback=(body:any)=>body.messages.filter((message:any)=>message.role==='tool').map((message:any)=>typeof message.content==='string'?message.content:JSON.stringify(message.content)).join('\n');

test('malformed nested packet returns bounded exact location, native structure and retained-content guidance',()=>{
  const bad={...submission,packet:brokenPacket};
  assert.throws(()=>decodeStructuredArguments(schema,bad),(error:Error)=>{
    const offset=brokenPacket.indexOf(',"open_questions"');
    assert.match(error.message,/Invalid JSON-encoded container at \/packet/);
    assert.ok(error.message.includes(`character offset ${offset} (zero-based), UTF-8 byte offset ${Buffer.byteLength(brokenPacket.slice(0,offset))}`));
    assert.match(error.message,/extra closing brace/);
    assert.match(error.message,/claims: array, open_questions: array, limitations: array/);
    assert.match(error.message,/normal object\/array, not a quoted JSON string/);
    assert.match(error.message,/preserve supported claims, exact evidence and coverage/);
    assert.ok(error.message.length<1600);
    assert.ok(!error.message.includes(packet.research_question));
    return true;
  });
  assert.equal(bad.packet,brokenPacket,'a rejected attempt is never silently repaired');
});

test('syntax diagnostics locate missing-offset V8 errors and escaped pointer paths without dumping long content',()=>{
  const cases=[['[1,]',3],['{"x":"bad\\q"}',10],['{"x":',5],['[true false]',6]] as const;
  for(const [value,offset] of cases){
    const shape=Type.Object({'a/b~c':value.startsWith('[')?Type.Array(Type.Number()):Type.Object({x:Type.String()})});
    assert.throws(()=>decodeStructuredArguments(shape,{'a/b~c':value}),new RegExp(`at /a~1b~0c: UTF-16 character offset ${offset} `));
  }
  const huge='{"x":"'+ '秘密'.repeat(50000)+'"},"extra":1}';
  assert.throws(()=>decodeStructuredArguments(Type.Object({packet:Type.Object({x:Type.String()})}),{packet:huge}),(error:Error)=>{assert.ok(error.message.length<1400);assert.ok(!error.message.includes('秘密'.repeat(100)));return true;});
});

test('union diagnostics select table evidence and never leak irrelevant image requirements',()=>{
  const table={id:'E1',source_id:'main',section_id:'s1',evidence_type:'table_cell',table_id:'t1',row:0,column:1,headers:'[7]'};
  const candidate={...submission,packet:{...packet,claims:[{...claim,evidence:[table]}]}};
  const decoded=decodeStructuredArguments(schema,candidate);
  const errors=structuredArgumentErrors(schema,decoded.value);
  assert.deepEqual(errors,['/packet/claims/0/evidence/0/headers/0: Expected string']);
  assert.throws(()=>validatePhaseResult('chunk_extract',decoded.value,{source_sections:[{id:'s1'}]},{workflowVersion:WORKFLOW_V3}),/headers\/0: Expected string/);
  const literal='{this is source text, not valid JSON}';
  const textEvidence={...claim.evidence[0],quote:literal,headers:'[broken'};
  const invalid={...submission,packet:{...packet,claims:[{...claim,evidence:[textEvidence]}]}};
  const untouched=decodeStructuredArguments(schema,invalid);
  assert.equal(untouched.value.packet.claims[0].evidence[0].quote,literal);
  assert.match(structuredArgumentErrors(schema,untouched.value).join(';'),/headers: Unexpected property/);
  assert.equal(Value.Check(schema,untouched.value),false);
  const stringAlternative=Type.Object({content:Type.Union([Type.Object({x:Type.String()}),Type.String()])});
  assert.deepEqual(decodeStructuredArguments(stringAlternative,{content:'{literal, not JSON}'}),{value:{content:'{literal, not JSON}'},decoded:[]});
});

test('real Pi HTTP loop uses precise failed submission feedback to accept corrected native packet in same context',async()=>{
  await fixture((body,res,index)=>{
    if(index===0)send(res,{...submission,packet:brokenPacket},index);
    else{
      const message=feedback(body);
      assert.match(message,/Invalid JSON-encoded container at \/packet/);
      assert.match(message,/UTF-8 byte offset/);
      assert.match(message,/open_questions: array/);
      assert.match(message,/not a quoted JSON string/);
      assert.equal(body.messages.filter((entry:any)=>entry.role==='user').length,1);
      send(res,submission,index);
    }
  },async(base,dir,bodies)=>{
    let charges=0;
    const result=await runPhase(spec(base,dir),async name=>{assert.equal(name,'host_phase_submission');charges++;return {accepted:true};},()=>{},new AbortController().signal);
    assert.deepEqual(result,submission);assert.equal(bodies.length,2);assert.equal(charges,2);
  });
});

test('ordinary stop after rejected packet gets bounded same-session completion recovery',async()=>{
  await fixture((body,res,index)=>{
    if(index===0)send(res,{...submission,packet:brokenPacket},index);
    else if(index===1)send(res,undefined,index,'I will correct the structure.');
    else{
      assert.match(feedback(body),/Invalid JSON-encoded container at \/packet/);
      assert.equal(body.messages.filter((entry:any)=>entry.role==='user').length,2);
      send(res,submission,index);
    }
  },async(base,dir,bodies)=>{
    const events:any[]=[];let charges=0;
    const result=await runPhase(spec(base,dir),async()=>{charges++;return {accepted:true};},event=>events.push(event),new AbortController().signal);
    assert.deepEqual(result,submission);assert.equal(bodies.length,3);assert.equal(charges,2);
    assert.equal(events.filter(event=>event.type==='progress'&&event.message.includes('1/2')).length,1);
  });
});

test('guided completion reminders stop after two attempts without inventing a phase result',async()=>{
  await fixture((_body,res,index)=>send(res,undefined,index,'I will submit the result.'),async(base,dir,bodies)=>{
    let charges=0;const events:any[]=[];
    await assert.rejects(runPhase(spec(base,dir),async()=>{charges++;return {accepted:true};},event=>events.push(event),new AbortController().signal),/without required chunk_extract submission/);
    assert.equal(bodies.length,3);assert.equal(charges,0);
    assert.equal(events.filter(event=>event.type==='progress'&&event.message.includes('继续同一会话')).length,2);
  });
});

for(const malformed of [{...submission,packet:brokenPacket},{...submission,packet:7}])test(`invalid ${typeof malformed.packet==='string'?'JSON-string':'SDK-schema'} submissions cannot escape shared stage tool budget`,async()=>{
  await fixture((_body,res,index)=>send(res,malformed,index),async(base,dir,bodies)=>{
    let charges=0;
    await assert.rejects(runPhase(spec(base,dir,3),async()=>{charges++;return {accepted:true};},()=>{},new AbortController().signal),/Workflow phase tool budget exhausted/);
    assert.equal(charges,3);assert.equal(bodies.length,4);
  });
});
