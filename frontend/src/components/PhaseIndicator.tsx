import type { Phase } from "../api/types";

const PHASE_LABEL: Record<Phase, string> = {
  routing: "Working out what you're asking…",
  retrieving: "Searching the transcripts…",
  generating: "Writing the answer…",
  validating: "Checking citations…",
  done: "Answer ready.",
};

/**
 * A local 7B model can take 30+ seconds before the first token. This is the
 * component that keeps that from reading as "hung" (docs/design.md §1, §3;
 * PRD 1.6 "Latency") — it is the only place phase text is announced, kept
 * separate from the streaming answer text itself so a screen reader gets one
 * clean announcement per phase, not one per token.
 */
export function PhaseIndicator({ phase }: { phase: Phase | null }) {
  if (!phase) return null;
  return (
    <div className="phase-indicator" aria-live="polite" role="status">
      {phase !== "done" && <span className="phase-indicator__spinner" aria-hidden="true" />}
      {PHASE_LABEL[phase]}
    </div>
  );
}
