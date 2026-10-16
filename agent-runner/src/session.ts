import {createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager, type ToolDefinition} from '@earendil-works/pi-coding-agent';
import {InMemoryCredentialStore,InMemoryModelsStore,normalizeContext,type TranscriptContext} from '@earendil-works/pi-ai';
import {assertThinkingFormat,isV3Workflow,safeError,type RunSpec,type Emit,type ChatHistoryEntry} from './protocol.js';
import {createModelTransport} from './transport.js';

/** A fresh, ephemeral SDK context. All discovery and file-backed state are disabled. */
export async function modelSession(spec:RunSpec, systemPrompt:string, prompt:string, customTools:ToolDefinition[], emit:Emit, signal:AbortSignal, options:{stopAfterTool?:string;history?:ChatHistoryEntry[];onTextDelta?:(text:string)=>void;beforeToolAttempt?:(name:string)=>Promise<void>;transformContext?:(context:TranscriptContext)=>TranscriptContext;completionNudge?:string;onCompletionNudge?:(attempt:number)=>void}={}) {
  assertThinkingFormat(spec.model);
  const qwenChatTemplate=spec.model.thinking_format==='qwen-chat-template';
  // Some compatible servers buffer structured tool arguments until a complete
  // JSON object exists. The host phase deadline governs that wait; a shorter
  // transport idle timeout must not abort a still-authorized generation.
  const streamTimeout=spec.kind==='chat'||isV3Workflow(spec.workflow_version)?Math.min(300,spec.limits.timeout_seconds)*1000+1000:120000;
  const transport=createModelTransport(spec.model.base_url,spec.model.allow_private_models===true,undefined,streamTimeout);
  const settings=SettingsManager.inMemory({compaction:{enabled:false},retry:{enabled:true,maxRetries:2,baseDelayMs:1000}});
  const runtime=await ModelRuntime.create({credentials:new InMemoryCredentialStore(),modelsStore:new InMemoryModelsStore(),modelsPath:null,allowModelNetwork:false,refreshOnCreate:false});
  const stream=runtime.streamSimple.bind(runtime);
  runtime.streamSimple=(model,context,streamOptions)=>stream(model,options.transformContext?options.transformContext(normalizeContext(context)):context,{...streamOptions,fetch:transport.fetch});
  const provider='paper-reader-tenant';
  runtime.registerProvider(provider,{baseUrl:spec.model.base_url,api:spec.model.protocol,authHeader:true,models:[{
    id:spec.model.model,name:spec.model.model,api:spec.model.protocol,reasoning:qwenChatTemplate,input:spec.model.vision?['text','image']:['text'],
    contextWindow:spec.model.context_window,maxTokens:spec.model.max_tokens,cost:{input:0,output:0,cacheRead:0,cacheWrite:0},
    ...(spec.model.protocol==='openai-completions'?{compat:{supportsDeveloperRole:false,supportsStore:false,supportsUsageInStreaming:true,maxTokensField:'max_tokens' as const,...(qwenChatTemplate?{thinkingFormat:'qwen-chat-template' as const}:{})}}:{})
  }]});
  await runtime.setRuntimeApiKey(provider,spec.model.api_key);
  const model=runtime.getModel(provider,spec.model.model);if(!model)throw new Error('Configured model is unavailable');
  const loader=new DefaultResourceLoader({cwd:spec.work_dir,agentDir:spec.work_dir+'/.isolated-agent',settingsManager:settings,
    noExtensions:true,noSkills:true,noPromptTemplates:true,noThemes:true,noContextFiles:true,systemPrompt});
  await loader.reload();
  const sessionTools=customTools.map(tool=>tool.name===options.stopAfterTool?{...tool,execute:async(...args:Parameters<ToolDefinition['execute']>)=>{
    const result=await tool.execute(...args);
    return {...result,terminate:result.isError!==true};
  }}:tool);
  const sessionManager=SessionManager.inMemory(spec.work_dir);
  for(const entry of options.history??[]){
    // Restore only host-authorized visible messages, never persisted SDK tool
    // calls, model credentials, hidden reasoning, or another session's policy.
    if(!entry||!['user','assistant'].includes(entry.role)||typeof entry.content!=='string')throw new Error('Invalid model session history');
    if(entry.role==='user')sessionManager.appendMessage({role:'user',content:entry.content,timestamp:Date.now()});
    else sessionManager.appendMessage({role:'assistant',content:[{type:'text',text:entry.content}],api:spec.model.protocol,provider,model:spec.model.model,
      stopReason:'stop',timestamp:Date.now(),usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});
  }
  const {session}=await createAgentSession({cwd:spec.work_dir,agentDir:spec.work_dir+'/.isolated-agent',modelRuntime:runtime,model,
    thinkingLevel:'off',sessionManager,settingsManager:settings,resourceLoader:loader,
    tools:sessionTools.map(tool=>tool.name),customTools:sessionTools});
  const abort=()=>{void session.abort();};signal.addEventListener('abort',abort,{once:true});
  let modelFailure:string|undefined;
  let outputLimitFailure:string|undefined;
  let submitted=false;
  let lastAssistantStop:string|undefined;
  session.subscribe(event=>{
    if(event.type==='message_update'&&event.assistantMessageEvent.type==='text_delta'&&!signal.aborted&&!submitted){
      options.onTextDelta?.(event.assistantMessageEvent.delta);
    }
    if(event.type==='tool_execution_end'&&event.toolName===options.stopAfterTool&&!event.isError){
      // Stop only after the tool result has been validated and finalized. Never
      // abort from inside execute(), which would race its awaited host mutation.
      submitted=true;void session.abort();
    }
    if(event.type==='message_end' && event.message.role==='assistant') {
      const message=event.message;
      lastAssistantStop=message.stopReason;
      if(spec.kind!=='chat'&&spec.workflow_mode==='guided'&&isV3Workflow(spec.workflow_version))emit({type:'model_output',phase:spec.phase?.name,content:message.content,stop_reason:message.stopReason,...(message.errorMessage?{error:safeError(new Error(message.errorMessage),[spec.model.api_key])}:{})});
      if(message.stopReason==='error')modelFailure=message.errorMessage||'Model call failed';
      else modelFailure=undefined;
      const u=message.usage;
      if(u)emit({type:'usage',usage:{input_tokens:u.input,output_tokens:u.output,cache_read_tokens:u.cacheRead,cache_write_tokens:u.cacheWrite,total_tokens:u.totalTokens}});
      if(message.stopReason==='length'){
        outputLimitFailure=`Model response was truncated at the output token limit (stop_reason=length, configured max_tokens=${spec.model.max_tokens}). Reduce the requested output or adjust the model output limit before retrying.`;
        // A truncated tool call must never trigger an SDK continuation loop.
        // Record usage before stopping; preserve this reason if abort surfaces as Cancelled.
        void session.abort();
      }
    }
  });
  // Agent.subscribe awaits listeners before SDK argument validation. The higher
  // level session subscription above is synchronous. Chat accounting uses this
  // boundary so malformed proposals cannot evade submission/call budgets.
  const unsubscribeAttempts=options.beforeToolAttempt?session.agent.subscribe(async event=>{
    if(event.type==='tool_execution_start'&&!signal.aborted&&!submitted&&!outputLimitFailure)await options.beforeToolAttempt!(event.toolName);
  }):undefined;
  const checkOutcome=()=>{if(signal.aborted)throw new Error('Cancelled');if(outputLimitFailure)throw new Error(outputLimitFailure);if(modelFailure)throw new Error(modelFailure);};
  try {
    checkOutcome();await session.prompt(prompt);checkOutcome();
    // Ordinary prose may end normally before the required structured
    // submission. Recover within the same ephemeral session and outer deadline;
    // never convert provider failures, truncation or cancellation into retries.
    if(options.completionNudge&&options.stopAfterTool){
      for(let attempt=1;attempt<=2&&!submitted&&lastAssistantStop==='stop';attempt++){
        options.onCompletionNudge?.(attempt);checkOutcome();
        await session.prompt(options.completionNudge);checkOutcome();
      }
    }
  }
  catch(error){if(signal.aborted)throw new Error('Cancelled');if(outputLimitFailure)throw new Error(outputLimitFailure);if(submitted&&!modelFailure)return;throw error;}
  finally{signal.removeEventListener('abort',abort);unsubscribeAttempts?.();session.dispose();await transport.close();}
}
