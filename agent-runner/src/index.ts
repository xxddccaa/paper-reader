import {createInterface} from 'node:readline';
import {assertRunSpec,safeError,type RunSpec} from './protocol.js';
import {WorkerRpc} from './rpc.js';
import {runReading} from './runner.js';
import {runPhase} from './phases.js';
import {runChat} from './chat.js';
import {runPiAgent} from './persistent-agent.js';
const emit=(event:Record<string,unknown>)=>process.stdout.write(JSON.stringify(event)+'\n');
const rpc=new WorkerRpc(emit);
const controller=new AbortController();
let active=false;let spec:RunSpec|undefined;let timer:NodeJS.Timeout|undefined;
const lines=createInterface({input:process.stdin,crlfDelay:Infinity});
const stop=()=>{controller.abort();rpc.close('Cancelled');};
process.on('SIGTERM',stop);process.on('SIGINT',stop);
lines.on('line',line=>{
  if(Buffer.byteLength(line)>20_000_000){emit({type:'error',message:'Protocol message exceeds limit'});stop();return;}
  let message:any;try{message=JSON.parse(line);}catch{emit({type:'error',message:'Invalid JSONL input'});stop();return;}
  if(message.type==='tool_response'){rpc.receive(message);return;}
  if(message.type==='cancel'){stop();return;}
  if(active||message.type!=='run'){emit({type:'error',message:'Expected one run request'});stop();return;}
  active=true;
  try{assertRunSpec(message);spec=message;}catch(error){emit({type:'error',message:safeError(error)});process.exitCode=1;lines.close();return;}
  // The persistent engine protects each request and returns waiting when the
  // host's remaining allocation expires; a five-minute phase timer would kill
  // its entire logical task and discard that distinction.
  if(spec.execution_engine!=='pi-agent-v1')timer=setTimeout(stop,Math.min(spec.limits.timeout_seconds,spec.phase||spec.kind==='chat'?300:Infinity)*1000);
  const execute=spec.execution_engine==='pi-agent-v1'?runPiAgent:spec.kind==='chat'?runChat:spec.phase?runPhase:runReading;
  void execute(spec,rpc.call,emit,controller.signal).then(result=>emit({type:'result',result})).catch(error=>{
    emit({type:'error',message:safeError(error,[spec!.model.api_key])});process.exitCode=1;
  }).finally(()=>{if(timer)clearTimeout(timer);rpc.close();lines.close();process.stdin.pause();});
});
lines.on('close',()=>{if(active&&!controller.signal.aborted)rpc.close('Worker input closed');});
