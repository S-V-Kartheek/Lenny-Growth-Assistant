import type { MessageRecord } from "../api/types";

/**
 * The wire contract has no boolean `refused` on a persisted message — the
 * signal is `grounding.reason`, set only by the two refusal paths in
 * `KnowledgeQASkill` (`insufficient_evidence`, `no_valid_citation_after_retry`).
 * Centralised here so the chat view and any future view agree on what counts
 * as a refusal instead of each re-deriving it slightly differently.
 */
const REFUSAL_REASONS = new Set(["insufficient_evidence", "no_valid_citation_after_retry"]);

export function isRefusal(message: MessageRecord): boolean {
  const reason = message.grounding?.reason;
  return typeof reason === "string" && REFUSAL_REASONS.has(reason);
}
