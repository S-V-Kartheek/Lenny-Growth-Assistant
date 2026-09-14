import type {
  ArtifactSavedData,
  ChatStreamEvent,
  ErrorBody,
  Phase,
  RetrievalInfo,
  SkillResultPayload,
  Source,
} from "../api/types";

/**
 * State for the turn currently streaming in, separate from the persisted
 * message history. Kept as a pure reducer (no React) so the SSE→UI mapping —
 * the part most likely to have an off-by-one or a missed event kind — can be
 * unit tested directly against scripted event sequences, the same principle
 * the backend transcripts describe applying to the citation validator.
 */
export interface StreamState {
  active: boolean;
  phase: Phase | null;
  sources: Source[];
  retrieval: RetrievalInfo | null;
  text: string;
  result: SkillResultPayload | null;
  artifactSaved: ArtifactSavedData | null;
  error: ErrorBody | null;
}

export const initialStreamState: StreamState = {
  active: false,
  phase: null,
  sources: [],
  retrieval: null,
  text: "",
  result: null,
  artifactSaved: null,
  error: null,
};

export function startStream(): StreamState {
  return { ...initialStreamState, active: true, phase: "routing" };
}

export function applyStreamEvent(state: StreamState, event: ChatStreamEvent): StreamState {
  switch (event.kind) {
    case "phase":
      return { ...state, phase: event.phase };
    case "routing":
      return state;
    case "sources":
      return { ...state, sources: event.data.sources, retrieval: event.data.retrieval };
    case "delta":
      return { ...state, text: state.text + event.text };
    case "result":
      return { ...state, result: event.result, phase: "done" };
    case "artifact_saved":
      return { ...state, artifactSaved: event.data };
    case "error":
      return { ...state, error: event.data, active: false };
    default:
      return state;
  }
}
