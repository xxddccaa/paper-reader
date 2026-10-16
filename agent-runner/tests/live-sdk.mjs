// Explicit opt-in paid network smoke: never used by production or default tests.
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Type} from '@sinclair/typebox';
import {modelSession} from '../dist/session.js';
if(!process.env.OPENROUTER_API_KEY)throw new Error('OPENROUTER_API_KEY is required');
const dir=await mkdtemp(join(tmpdir(),'paper-reader-live-sdk-'));
const ctrl=new AbortController();const timeout=setTimeout(()=>ctrl.abort(),45000);
let calls=0;const usage=[];
try{
 await modelSession({type:'run',run_id:'live-sdk-smoke',kind:'read',skill_id:'ljg-paper',skill_root:dir,work_dir:dir,
 model:{api_key:process.env.OPENROUTER_API_KEY,base_url:'https://openrouter.ai/api/v1',model:'qwen/qwen3-coder',protocol:'openai-completions',vision:false,context_window:262144,max_tokens:512},
 paper:{id:'smoke',title:'Protocol smoke',source_type:'pdf'},template:{brief_markdown:'Brief',full_markdown:'Full',instructions:''},limits:{timeout_seconds:45,max_tool_calls:2,max_related_papers:0}},
 'You are verifying the SDK tool protocol. Call verify_sdk exactly once with value 7, then say done. No other task.',
 'Execute the protocol verification.',[{name:'verify_sdk',label:'Verify SDK',description:'Verify an integer',parameters:Type.Object({value:Type.Integer()}),execute:async(_id,args)=>{if(args.value!==7)throw new Error('Unexpected tool argument');calls++;return {content:[{type:'text',text:'Verified 7'}],details:{}};}}],event=>{if(event.type==='usage')usage.push(event.usage);},ctrl.signal);
 if(calls!==1)throw new Error('Expected one real tool call');
 process.stdout.write(JSON.stringify({ok:true,model:'qwen/qwen3-coder',sdk:'pi 1.1.0',tool_calls:calls,usage})+'\n');
}finally{clearTimeout(timeout);await rm(dir,{recursive:true,force:true});}
