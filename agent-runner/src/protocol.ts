export const LEGACY_PHASE_NAMES=['extract','packet_repair','fact_review','compose','product_review','product_repair','revision_plan'] as const;
export const V3_PHASE_NAMES=['chunk_extract','figure_review','merge_review'] as const;
export const PHASE_NAMES=[...LEGACY_PHASE_NAMES,...V3_PHASE_NAMES] as const;
export type LegacyPhaseName=typeof LEGACY_PHASE_NAMES[number];
export type PhaseName=typeof PHASE_NAMES[number];
export const WORKFLOW_V3='guided-v3.0';
export const isV3Workflow=(version:unknown):boolean=>version===WORKFLOW_V3;
export const CHAT_OPERATIONS=['answer','review','compact'] as const;
export type ChatOperation=typeof CHAT_OPERATIONS[number];
export interface ChatHistoryEntry {role:'user'|'assistant';content:string}
export interface AgentToolSpec {name:string;description:string;parameters:Record<string,unknown>}
export interface PersistentAgentSpec {
  operation:'run'|'review'|'subtask'; goal:string; state:Record<string,unknown>;
  messages:unknown[]; summary?:string; tools:AgentToolSpec[]; review_input?:Record<string,unknown>;
}
export interface RunSpec {
  type: 'run'; run_id: string; kind: 'read' | 'revise' | 'chat'; skill_id: 'deeppapernote' | 'ljg-paper';
  skill_root: string; work_dir: string;
  model: { api_key: string; protocol: 'openai-completions' | 'anthropic-messages'; base_url: string; model: string; vision: boolean; context_window: number; max_tokens: number; allow_private_models?: boolean; thinking_format?: 'default' | 'qwen-chat-template' };
  paper: { id: string; title: string; source_type: string; source_url?: string; storage_path?: string };
  template: { brief_markdown: string; full_markdown: string; instructions: string; version_id?: string };
  instruction?: string; previous_revision?: Record<string, unknown> | null;
  workflow_mode?: 'guided' | 'native';
  workflow_version?: string;
  execution_engine?: 'legacy'|'pi-agent-v1';
  agent?: PersistentAgentSpec;
  source_rules_version?: string;
  phase?: {name:PhaseName;input:Record<string,unknown>};
  chat?: {operation:ChatOperation;input:Record<string,unknown>};
  limits: { timeout_seconds: number; max_tool_calls: number; max_related_papers: number };
}
export interface Evidence {id:string;source_id:string;source_title:string;source_url?:string;quote:string;section?:string;page?:number;kind:'paper'|'external'|'inference';verified:boolean}
export interface Product {brief_markdown:string;full_markdown:string;evidence_index:Evidence[];verification:Record<string,unknown>;artifacts:Array<{name:string;path:string;mime_type:string}>;provenance:Record<string,unknown>}
export type Emit = (event: Record<string, unknown>) => void;
export type ToolCall = (name: string, args: Record<string, unknown>, signal?: AbortSignal) => Promise<any>;
export function assertThinkingFormat(model: RunSpec['model']): void {
  if (model.thinking_format !== undefined && !['default', 'qwen-chat-template'].includes(model.thinking_format)) throw new Error('Unsupported model thinking_format');
  if (model.thinking_format === 'qwen-chat-template' && model.protocol !== 'openai-completions') throw new Error('qwen-chat-template thinking_format requires openai-completions protocol');
}
export function assertRunSpec(value: unknown): asserts value is RunSpec {
  const v = value as RunSpec;
  if (!v || v.type !== 'run' || !['read','revise','chat'].includes(v.kind) || !['deeppapernote','ljg-paper'].includes(v.skill_id)) throw new Error('Invalid runner request');
  if (!v.model?.api_key || !v.model.base_url || !v.model.model || !['openai-completions','anthropic-messages'].includes(v.model.protocol)) throw new Error('Model configuration is incomplete');
  assertThinkingFormat(v.model);
  if(v.workflow_mode!==undefined&&!['guided','native'].includes(v.workflow_mode))throw new Error('Invalid workflow_mode');
  if(v.workflow_version!==undefined&&(typeof v.workflow_version!=='string'||!v.workflow_version.trim()))throw new Error('Invalid workflow_version');
  if(v.source_rules_version!==undefined&&(typeof v.source_rules_version!=='string'||!v.source_rules_version.trim()))throw new Error('Invalid source_rules_version');
  if(v.execution_engine!==undefined&&!['legacy','pi-agent-v1'].includes(v.execution_engine))throw new Error('Invalid execution_engine');
  if(v.execution_engine==='pi-agent-v1'){
    const a=v.agent;
    if(!a||!['run','review','subtask'].includes(a.operation)||typeof a.goal!=='string'||!a.goal.trim()||!a.state||typeof a.state!=='object'||Array.isArray(a.state)||!Array.isArray(a.messages)||!Array.isArray(a.tools)||a.tools.length>100)throw new Error('Invalid persistent Agent context');
    if(a.summary!==undefined&&typeof a.summary!=='string')throw new Error('Invalid Agent summary');
    const names=new Set<string>();
    for(const tool of a.tools){
      if(!tool||!/^[_a-zA-Z][_a-zA-Z0-9]{0,63}$/.test(tool.name)||tool.name.startsWith('host_')||['submit_review','submit_subtask'].includes(tool.name)||names.has(tool.name)||typeof tool.description!=='string'||!tool.parameters||tool.parameters.type!=='object')throw new Error('Invalid or reserved Agent tool definition');
      names.add(tool.name);
    }
    if(v.phase)throw new Error('Persistent Agent does not accept a host-selected phase');
  }else if(v.agent)throw new Error('Agent context requires pi-agent-v1');
  if(v.phase&&(!PHASE_NAMES.includes(v.phase.name)||!v.phase.input||typeof v.phase.input!=='object'||Array.isArray(v.phase.input)))throw new Error('Invalid workflow phase');
  if(v.phase&&V3_PHASE_NAMES.some(name=>name===v.phase!.name)&&!isV3Workflow(v.workflow_version))throw new Error('Workflow v3 phases require workflow_version guided-v3.0');
  if(v.phase&&v.workflow_mode!=='guided')throw new Error('Workflow phases require guided mode');
  if(v.execution_engine==='pi-agent-v1'){
    if(v.chat&&(v.kind!=='chat'||!v.chat.input||typeof v.chat.input!=='object'||Array.isArray(v.chat.input)))throw new Error('Invalid Agent chat context');
  }else if(v.kind==='chat'){
    if(v.phase||!v.chat||!CHAT_OPERATIONS.includes(v.chat.operation)||!v.chat.input||typeof v.chat.input!=='object'||Array.isArray(v.chat.input))throw new Error('Chat requires a valid host-selected operation and no workflow phase');
  }else{
    if(v.chat)throw new Error('Chat operation requires kind chat');
    if(v.workflow_mode==='guided'&&!v.phase)throw new Error('Guided mode requires a host-selected phase');
  }
  if (!v.run_id || !v.work_dir?.startsWith('/') || !v.skill_root?.startsWith('/') || !v.paper?.id || !v.template?.brief_markdown || !v.template.full_markdown) throw new Error('Run context is incomplete');
  if (!v.limits || !Number.isFinite(v.limits.timeout_seconds) || v.limits.timeout_seconds < 1 || !Number.isInteger(v.limits.max_tool_calls) || v.limits.max_tool_calls < 1) throw new Error('Run limits are invalid');
}
export function safeError(error: unknown, secrets: string[] = []): string {
  let message = error instanceof Error ? error.message : String(error);
  for (const secret of secrets) if (secret) message = message.split(secret).join('[REDACTED]');
  const status=message.match(/^(?:HTTP\s*)?([45]\d\d)\b/i)?.[1]??message.match(/"(?:status|code)"\s*:\s*([45]\d\d)/)?.[1];
  const suffix=status?` (HTTP ${status})`:'';
  if(status==='413'||/input.{0,20}too long|maximum context|context(?: window| length)?.{0,40}(?:exceed|limit)|maximum message size|request payload.{0,50}(?:exceed|limit)|payload too large/i.test(message))
    return `Model request exceeds the provider's context or payload limit${suffix}. Reduce source/image payload or choose a model with a larger limit.`;
  if(status==='401')return 'Model authentication failed (HTTP 401). Check the configured API key.';
  if(status==='403')return 'Model access was denied (HTTP 403). Check model permissions and regional availability.';
  if(status==='402')return 'Model provider quota or payment limit reached (HTTP 402).';
  if(status==='429')return 'Model provider rate limit reached (HTTP 429). Retry later or reduce concurrency.';
  if(status==='408'||status==='504')return `Model provider request timed out${suffix}.`;
  if(status&&Number(status)>=500)return `Model provider is temporarily unavailable${suffix}.`;
  if(status||/provider returned error|"metadata"\s*:.*"raw"|^\s*data:\s*\{.*"error"/is.test(message))
    return `Model provider rejected the request${suffix}. Check the model configuration and request limits.`;
  return message.replace(/Bearer\s+[^\s"']+/gi,'Bearer [REDACTED]').slice(0,1000);
}
