import { describe, expect, it } from "vitest";
import { isRefusal } from "./grounding";
import type { MessageRecord } from "../api/types";

function message(grounding: MessageRecord["grounding"]): MessageRecord {
  return {
    id: "m1",
    session_id: "s1",
    role: "assistant",
    content: "content",
    intent: "knowledge_qa",
    provider: "ollama",
    model: "qwen2.5:7b-instruct",
    latency_ms: 100,
    token_usage: null,
    grounding,
    sources: [],
    error: null,
    created_at: new Date().toISOString(),
  };
}

describe("isRefusal", () => {
  it("is true for the confidence-gate refusal reason", () => {
    expect(isRefusal(message({ reason: "insufficient_evidence" }))).toBe(true);
  });

  it("is true for the no-valid-citation-after-retry refusal reason", () => {
    expect(isRefusal(message({ reason: "no_valid_citation_after_retry" }))).toBe(true);
  });

  it("is false for a normal grounded answer with no reason set", () => {
    expect(isRefusal(message({ retrieval_confidence: 0.7 }))).toBe(false);
  });

  it("is false when grounding is null", () => {
    expect(isRefusal(message(null))).toBe(false);
  });
});
