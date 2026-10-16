import {randomUUID} from 'node:crypto';
import type {Emit, ToolCall} from './protocol.js';
export class WorkerRpc {
  private pending = new Map<string, {resolve:(value:any)=>void;reject:(error:Error)=>void;cleanup:()=>void}>();
  constructor(private emit: Emit) {}
  call: ToolCall = (name,args,signal) => new Promise((resolve,reject) => {
    if (signal?.aborted) { reject(new Error('Cancelled')); return; }
    const id=randomUUID();
    const abort=()=>{this.pending.delete(id);reject(new Error('Cancelled'));};
    signal?.addEventListener('abort',abort,{once:true});
    this.pending.set(id,{resolve,reject,cleanup:()=>signal?.removeEventListener('abort',abort)});
    this.emit({type:'tool_request',id,name,args});
  });
  receive(message: any) {
    if (message.type !== 'tool_response' || typeof message.id !== 'string') return;
    const request=this.pending.get(message.id); if(!request)return;
    this.pending.delete(message.id);request.cleanup();
    if (message.error) request.reject(new Error(String(message.error))); else request.resolve(message.result);
  }
  close(reason='Worker disconnected') {for(const request of this.pending.values()){request.cleanup();request.reject(new Error(reason));}this.pending.clear();}
}
