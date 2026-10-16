import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createServer} from 'node:http';
import {once} from 'node:events';
import {mkdtemp,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {runReading} from '../src/runner.js';
import {nativeReviewPolicyKey,READER_RECORD_FIELDS} from '../src/native-review.js';
import type {RunSpec} from '../src/protocol.js';

const specFor=(base:string,dir:string):RunSpec=>({type:'run',run_id:'native-cache-fixture',kind:'read',skill_id:'ljg-paper',skill_root:dir,work_dir:dir,model:{api_key:'secret-one',base_url:base,model:'fixture-model',protocol:'openai-completions',vision:false,context_window:128000,max_tokens:1000,allow_private_models:true},paper:{id:'paper',title:'Fixture',source_type:'pdf'},template:{brief_markdown:'# Brief',full_markdown:'# Full',instructions:''},limits:{timeout_seconds:10,max_tool_calls:80,max_related_papers:3}});
const hash=(text:string)=>createHash('sha256').update(text).digest('hex');
const record=Object.fromEntries(READER_RECORD_FIELDS.map(key=>[key,`Synthetic protocol fixture reader answer for ${key}`]));
const quality={passed:true,issues:[],checks:{grounded:true}};
const readability={passed:true,issues:[],checks:{clear:true},reader_record:record};

test('native review policy includes public model, skill and rules while excluding secrets',()=>{
  const spec=specFor('http://127.0.0.1:1/v1','/tmp');const key=nativeReviewPolicyKey(spec,'commit-one');
  assert.match(key,/^[a-f0-9]{64}$/);
  assert.equal(nativeReviewPolicyKey({...spec,model:{...spec.model,api_key:'different-secret'}},'commit-one'),key);
  assert.equal(nativeReviewPolicyKey({...spec,model:{...spec.model,thinking_format:'default'}},'commit-one'),key);
  for(const change of [{model:'other'},{base_url:'http://127.0.0.1:2/v1'},{context_window:64000},{max_tokens:2000},{vision:true},{thinking_format:'qwen-chat-template' as const}])assert.notEqual(nativeReviewPolicyKey({...spec,model:{...spec.model,...change}},'commit-one'),key);
  assert.notEqual(nativeReviewPolicyKey(spec,'commit-two'),key);
  assert.notEqual(nativeReviewPolicyKey(spec,'commit-one','changed-review-rules'),key);
});

test('host reader-record-only update reuses both reviews, retains final map hash and still requires original validator',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'native-review-cache-'));
  const note='A native note with the factual method and conditional effects.';let map='## Research\nA factual map.\n\n## 独立阅读检查记录\nPending actual review.';
  let nativeRequests=0,qualityRequests=0,readabilityRequests=0,lookups=0,attestations=0,artifactReads=0,seals=0;
  const policyKeys:string[]=[];let savedReports:any;
  const server=createServer(async(req,res)=>{
    let data='';for await(const chunk of req)data+=chunk;const body=JSON.parse(data);
    const names=body.tools.map((tool:any)=>tool.function.name);let call:any;
    if(names.includes('review_native')){
      nativeRequests++;
      assert.ok(names.includes('patch_artifact'));assert.ok(!names.includes('host_native_review_lookup'));
      if(nativeRequests<=2)call={name:'review_native',arguments:JSON.stringify({note_path:'native/note.md',map_path:'native/paper-map.md'})};
      else if(nativeRequests===3){
        const replies=body.messages.filter((message:any)=>message.role==='tool').map((message:any)=>JSON.parse(message.content));
        assert.equal(replies.at(-1).map_hash,hash(map));assert.deepEqual(replies.at(-1).reused,{quality:true,readability:true});
        call={name:'finish_native',arguments:JSON.stringify({note_path:'native/note.md',map_path:'native/paper-map.md'})};
      }
    }else{
      const content=body.messages.find((message:any)=>message.role==='user').content;
      const payload=JSON.parse(typeof content==='string'?content:content.map((item:any)=>item.text??'').join(''));
      if(payload.task==='Native content quality review'){qualityRequests++;call={name:'submit_review',arguments:JSON.stringify(quality)};}
      else{readabilityRequests++;call={name:'submit_review',arguments:JSON.stringify(readability)};}
    }
    res.writeHead(200,{'Content-Type':'text/event-stream'});const base={id:'native-review-fixture',object:'chat.completion.chunk',created:1,model:'fixture-model'};
    res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:call?{tool_calls:[{index:0,id:`call-${nativeRequests}-${qualityRequests}-${readabilityRequests}`,type:'function',function:call}]}:{content:'The original validator is still required.'},finish_reason:null}]})+'\n\n');
    res.write('data: '+JSON.stringify({...base,choices:[{index:0,delta:{},finish_reason:call?'tool_calls':'stop'}]})+'\n\n');res.end('data: [DONE]\n\n');
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');const port=(server.address() as any).port;
  try{
    await assert.rejects(runReading(specFor(`http://127.0.0.1:${port}/v1`,dir),async(name,args)=>{
      if(name==='prepare_source')return {source_manifest:{source_id:'main'},sections:[]};
      if(name==='read_skill_file')return {content:args.path==='ReadingGuide.md'?'## 独立阅读检查\nRead and reconstruct.\n## 写入与验证':'Use the selected reading skill.'};
      if(name==='host_native_status')return {sealed:false};
      if(name==='host_native_review_lookup'){lookups++;policyKeys.push(String(args.policy_key));return {note_sha256:hash(note),map_sha256:hash(map),...(savedReports??{})};}
      if(name==='read_artifact'){artifactReads++;return {content:args.path==='native/note.md'?note:map,next_offset:null,sha256:hash(args.path==='native/note.md'?note:map)};}
      if(name==='host_native_attest'){
        attestations++;assert.equal(args.note_sha256,hash(note));assert.equal(args.map_sha256,hash(map));assert.equal(args.policy_key,policyKeys[0]);
        assert.deepEqual((args.readability as any).reader_record,record);savedReports={quality:args.quality,readability:args.readability};
        map='## Research\nA factual map.\n\n## 独立阅读检查记录\nHOST_GENERATED_ACTUAL_READER_RECORD';
        return {recorded:true,passed:true,map_sha256:hash(map),reader_record_written:true};
      }
      if(name==='host_native_seal'){seals++;throw new Error('Cannot seal without original validator');}
      throw new Error(`Unexpected host call: ${name}`);
    },()=>{},new AbortController().signal),/without required finish_native/);
    assert.equal(qualityRequests,1);assert.equal(readabilityRequests,1);assert.equal(nativeRequests,4);
    assert.equal(lookups,2);assert.equal(attestations,2);assert.equal(artifactReads,2,'cached second review need not reread whole files');assert.equal(seals,0);
    assert.equal(policyKeys[0],policyKeys[1]);
  }finally{server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()));await rm(dir,{recursive:true,force:true});}
});
