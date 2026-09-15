/**
 * Wire types, kept in lockstep with backend/app/schemas/*.py and the SkillResult
 * shapes in backend/app/agent/contracts.py. This file has no logic — it exists
 * so a backend field rename is a compile error here, not a silent `undefined`
 * in a card somewhere.
 */

export type Intent = "knowledge_qa" | "ship30_essay" | "artifact";

export type Phase = "routing" | "retrieving" | "generating" | "validating" | "done";

export interface SessionSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

export interface SessionListResponse {
  sessions: SessionSummary[];
}

export interface Source {
  index: number;
  label: string;
  cited: boolean;
  retrieval_method: string;
  chunk_id: string;
  episode_slug: string;
  episode_title: string;
  guest: string | null;
  speakers: string[];
  timestamp: string | null;
  start_seconds: number | null;
  citation_url: string | null;
  publish_date: string | null;
  source_commit: string;
  source_path: string;
  score: number;
  excerpt: string;
}

export interface RetrievalInfo {
  method: string;
  confidence: number;
  query: string;
  degraded_reason: string | null;
}

export interface Grounding {
  source_count?: number;
  cited_indices?: number[];
  invalid_markers?: string[];
  uncited_paragraphs?: number;
  reason?: string;
  retrieval_confidence?: number;
  retrieval_method?: string;
  degraded_reason?: string | null;
  threshold?: number;
  fallback_from?: string | null;
  [key: string]: unknown;
}

export interface ArtifactPayload {
  kind: "markdown" | "html";
  title: string;
  content: string;
  sanitization?: Record<string, unknown>;
}

export interface SkillResultPayload {
  content: string;
  intent: Intent;
  refused: boolean;
  sources: Source[];
  grounding: Grounding;
  artifact: ArtifactPayload | null;
  provider: string | null;
  model: string | null;
  usage: Record<string, number>;
  latency_ms: number;
}

export interface MessageRecord {
  id: string;
  session_id: string;
  role: "user" | "assistant";
  content: string;
  intent: Intent | null;
  provider: string | null;
  model: string | null;
  latency_ms: number | null;
  token_usage: Record<string, unknown> | null;
  grounding: Grounding | null;
  sources: Source[];
  error: Partial<ErrorBody> | null;
  created_at: string;
}

export interface SessionHistoryResponse {
  session: SessionSummary;
  messages: MessageRecord[];
}

export interface ProviderInfo {
  provider: string;
  model: string;
  context_tokens: number;
  fallback_provider: string | null;
}

export interface ArtifactRecord {
  id: string;
  session_id: string;
  message_id: string | null;
  kind: "markdown" | "html";
  title: string;
  content: string;
  version: number;
  sanitization: Record<string, unknown>;
  sandbox: string | null;
  document_url: string | null;
  created_at: string;
}

export interface ArtifactListResponseLike {
  artifacts: ArtifactRecord[];
}

export interface ArtifactSavedData {
  id: string;
  kind: "markdown" | "html";
  title: string;
  version: number;
  document_url: string | null;
}

export interface ErrorBody {
  code: string;
  message: string;
  remediation: string | null;
  details: Record<string, unknown> | null;
  request_id: string | null;
}

/** One parsed SSE frame from POST /api/sessions/{id}/messages. */
export type ChatStreamEvent =
  | { kind: "phase"; phase: Phase }
  | { kind: "routing"; data: { intent: Intent; method: string; confidence: number } }
  | { kind: "sources"; data: { sources: Source[]; retrieval: RetrievalInfo } }
  | { kind: "delta"; text: string }
  | { kind: "result"; result: SkillResultPayload }
  | { kind: "artifact_saved"; data: ArtifactSavedData }
  | { kind: "error"; data: ErrorBody };
