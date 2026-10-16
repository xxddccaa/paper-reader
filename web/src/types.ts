// HTTP resource shapes generated from the backend OpenAPI contract.
import type { components } from "./generated-api";
type Schemas = components["schemas"];
export type User = Schemas["UserOut"];
export type Workspace = Schemas["WorkspaceOut"];
export type Member = Schemas["MemberOut"];
export type Skill = Schemas["SkillOut"];
export type ModelThinkingFormat = Schemas["ModelOut"]["thinking_format"];
export type ModelConnection = Schemas["ModelOut"];
export type TemplateVersion = Schemas["TemplateVersionOut"];
export type Template = Schemas["TemplateOut"];
export type Paper = Schemas["PaperOut"];
export type WorkflowMode = Schemas["ReadingDetail"]["workflow_mode"];
export interface SourceSelection {
  format?: string;
  reason?: string;
  requested_id?: string;
  resolved_id?: string;
}
export interface SectionCoverage {
  total?: number;
  processed?: number;
  complete?: boolean;
  sections?: {
    id: string;
    title: string;
    kind: string;
    status: string;
    segment_ids: string[];
  }[];
  segments?: { id: string; section_ids: string[]; status: string }[];
  unresolved?: unknown[];
}
export interface FigureChecks {
  total?: number;
  verified?: number;
  complete?: boolean;
  items?: {
    figure_id: string;
    section_id: string;
    caption: string;
    status: string;
    image_sha256?: string;
    observation?: string;
    conditions?: string;
    uncertainties: string[];
    claim_ids: string[];
  }[];
  unresolved?: unknown[];
}
export interface WorkflowSummaries {
  source_selection?: SourceSelection;
  section_coverage?: SectionCoverage;
  figure_checks?: FigureChecks;
}
export interface SourceInventorySummary extends WorkflowSummaries {
  section_inventory?: { id: string; title: string; kind: string }[];
  figures?: { id: string; section_id: string; caption: string }[];
}
export interface StageRecord extends WorkflowSummaries {
  name: string;
  status: "pending" | "running" | "succeeded" | "failed" | "reused" | "skipped";
  input_hash?: string;
  output_hash?: string;
  attempt?: number;
  reused_from?: string;
  started_at?: string;
  completed_at?: string;
  duration_ms?: number;
  usage?: Record<string, unknown>;
  error?: string;
  summary?: SourceInventorySummary & SectionCoverage & FigureChecks;
}
// Public, redacted task projection from docs/pi-agent-v1.md. Checkpoints and
// private tool messages are deliberately absent from the browser contract.
export type AgentTask = Schemas["AgentTaskOut"];
// Additive nested shapes are defined in docs/workflow-v2.md and workflow-v3.md.
export type Job = Omit<Schemas["JobOut"], "stage_records"> & {
  stage_records?: StageRecord[];
};
export type Reading = Omit<Schemas["ReadingListOut"], "latest_job"> & {
  latest_job: Job | null;
};
export type ReadingCreated = Schemas["ReadingCreated"];
export type TemplateBody = Schemas["TemplateIn"];
// These nested artifact/quote records are currently generic lists in OpenAPI;
// their minimum structure is documented in docs/contracts.md.
export interface EvidenceDetails {
  evidence_type?: "text_quote" | "table_cell" | "image_observation";
  selection?: "section";
  verification_method?:
    | "quote_match"
    | "section_lookup"
    | "table_cell_match"
    | "visual_observation";
  table_id?: string;
  row?: number;
  column?: number;
  headers?: string[];
  figure_id?: string;
  image_sha256?: string;
  observation_id?: string;
  observation?: string;
  conditions?: string;
  uncertainties?: string[];
}
export interface Evidence extends EvidenceDetails {
  id: string;
  source_id: string;
  source_title: string;
  source_url?: string;
  quote: string;
  section?: string;
  section_id?: string;
  page?: number;
  kind: "paper" | "external" | "inference";
  verified: boolean;
}
export interface Artifact {
  name: string;
  path: string;
  mime_type: string;
}
export type Revision = Omit<
  Schemas["RevisionOut"],
  "evidence_index" | "artifacts" | "reading_packet"
> & {
  evidence_index: Evidence[];
  artifacts: Artifact[];
  reading_packet?: ReadingPacket | Record<string, never>;
};
export type ReadingDetail = Omit<
  Schemas["ReadingDetail"],
  "revisions" | "jobs"
> & {
  revisions: Revision[];
  jobs: Job[];
};
export interface PacketEvidence extends EvidenceDetails {
  id: string;
  source_id: string;
  section_id: string;
  quote?: string;
  page?: number;
}
export interface ReadingClaim {
  id: string;
  kind:
    | "contribution"
    | "method"
    | "result"
    | "lesson"
    | "limitation"
    | "related_work";
  statement: string;
  basis: "observation" | "author_hypothesis" | "inference";
  conditions: {
    model: string;
    dataset: string;
    split: string;
    metric: string;
    baseline: string;
    budget: string;
  };
  evidence: PacketEvidence[];
  numeric_comparison?: {
    value: number;
    baseline_value: number;
    relation: "higher" | "lower" | "equal";
    unit: string;
  } | null;
}
export interface ReadingPacket {
  version: 1 | 2;
  paper_title: string;
  research_question: string;
  claims: ReadingClaim[];
  open_questions: { question: string; search_query: string; reason: string }[];
  limitations: string[];
}
