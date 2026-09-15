import { readSseStream } from "./sse";
import type {
  ArtifactListResponseLike,
  ArtifactRecord,
  ChatStreamEvent,
  ErrorBody,
  ProviderInfo,
  SessionHistoryResponse,
  SessionListResponse,
  SessionSummary,
} from "./types";

export const API_BASE: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";

export class ApiError extends Error {
  code: string;
  remediation: string | null;
  status: number;

  constructor(status: number, body: ErrorBody) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
    this.remediation = body.remediation;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    let body: ErrorBody;
    try {
      const parsed = (await res.json()) as { error: ErrorBody };
      body = parsed.error;
    } catch {
      body = {
        code: "internal_error",
        message: `Request failed with status ${res.status}.`,
        remediation: null,
        details: null,
        request_id: null,
      };
    }
    throw new ApiError(res.status, body);
  }
  return (await res.json()) as T;
}

export function getProvider(): Promise<ProviderInfo> {
  return request<ProviderInfo>("/api/provider");
}

/** Switches the active model for every subsequent request (PRD 2.5's toggle). */
export function setProvider(provider: string): Promise<ProviderInfo> {
  return request<ProviderInfo>("/api/provider", {
    method: "POST",
    body: JSON.stringify({ provider }),
  });
}

export function listSessions(): Promise<SessionListResponse> {
  return request<SessionListResponse>("/api/sessions");
}

export function createSession(title?: string): Promise<SessionSummary> {
  return request<SessionSummary>("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ title: title ?? null }),
  });
}

export function getSessionHistory(sessionId: string): Promise<SessionHistoryResponse> {
  return request<SessionHistoryResponse>(`/api/sessions/${sessionId}`);
}

export function listSessionArtifacts(sessionId: string): Promise<ArtifactListResponseLike> {
  return request<ArtifactListResponseLike>(`/api/sessions/${sessionId}/artifacts`);
}

export function getArtifact(artifactId: string): Promise<ArtifactRecord> {
  return request<ArtifactRecord>(`/api/artifacts/${artifactId}`);
}

export function artifactDocumentUrl(artifactId: string): string {
  return `${API_BASE}/api/artifacts/${artifactId}/document`;
}

/**
 * Posts a message and streams the SSE response. Returns an async generator of
 * typed events; the caller drives it (usually inside a React effect) and can
 * abort mid-stream via `signal`.
 */
export async function* streamMessage(
  sessionId: string,
  content: string,
  signal?: AbortSignal,
): AsyncGenerator<ChatStreamEvent> {
  const res = await fetch(`${API_BASE}/api/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
    signal,
  });
  if (!res.ok || !res.body) {
    let body: ErrorBody;
    try {
      const parsed = (await res.json()) as { error: ErrorBody };
      body = parsed.error;
    } catch {
      body = {
        code: "internal_error",
        message: `The stream could not be started (status ${res.status}).`,
        remediation: null,
        details: null,
        request_id: null,
      };
    }
    throw new ApiError(res.status, body);
  }
  yield* readSseStream(res.body);
}
