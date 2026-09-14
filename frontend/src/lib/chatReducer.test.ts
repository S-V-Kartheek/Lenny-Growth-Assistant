import { describe, expect, it } from "vitest";
import { applyStreamEvent, initialStreamState, startStream } from "./chatReducer";
import type { ChatStreamEvent } from "../api/types";

describe("startStream", () => {
  it("marks the stream active with the routing phase", () => {
    const state = startStream();
    expect(state.active).toBe(true);
    expect(state.phase).toBe("routing");
    expect(state.text).toBe("");
  });
});

describe("applyStreamEvent", () => {
  it("advances phase on a phase event", () => {
    const state = applyStreamEvent(startStream(), { kind: "phase", phase: "retrieving" });
    expect(state.phase).toBe("retrieving");
  });

  it("stores sources and retrieval info as soon as they arrive, before any text", () => {
    const event: ChatStreamEvent = {
      kind: "sources",
      data: {
        sources: [
          {
            index: 1,
            label: "S1",
            cited: false,
            retrieval_method: "hybrid",
            chunk_id: "c1",
            episode_slug: "ep",
            episode_title: "Episode",
            guest: "Guest",
            speakers: ["Guest"],
            timestamp: "00:01:00",
            start_seconds: 60,
            citation_url: "https://youtube.com/watch?v=abc&t=55s",
            publish_date: null,
            source_commit: "abc123",
            source_path: "x",
            score: 0.9,
            excerpt: "excerpt",
          },
        ],
        retrieval: { method: "hybrid", confidence: 0.6, query: "q", degraded_reason: null },
      },
    };
    const state = applyStreamEvent(startStream(), event);
    expect(state.sources).toHaveLength(1);
    expect(state.retrieval?.confidence).toBe(0.6);
    expect(state.text).toBe("");
  });

  it("accumulates delta text across multiple events, in order", () => {
    let state = startStream();
    state = applyStreamEvent(state, { kind: "delta", text: "Hello " });
    state = applyStreamEvent(state, { kind: "delta", text: "world" });
    expect(state.text).toBe("Hello world");
  });

  it("sets result and phase=done on a result event", () => {
    const state = applyStreamEvent(startStream(), {
      kind: "result",
      result: {
        content: "answer",
        intent: "knowledge_qa",
        refused: false,
        sources: [],
        grounding: {},
        artifact: null,
        provider: "ollama",
        model: "qwen2.5:7b-instruct",
        usage: {},
        latency_ms: 1200,
      },
    });
    expect(state.phase).toBe("done");
    expect(state.result?.content).toBe("answer");
  });

  it("records an artifact_saved event without disturbing other fields", () => {
    let state = startStream();
    state = applyStreamEvent(state, { kind: "delta", text: "partial" });
    state = applyStreamEvent(state, {
      kind: "artifact_saved",
      data: { id: "a1", kind: "html", title: "Checklist", version: 1, document_url: "/api/artifacts/a1/document" },
    });
    expect(state.artifactSaved?.id).toBe("a1");
    expect(state.text).toBe("partial");
  });

  it("marks the stream inactive and records the error on an error event", () => {
    const state = applyStreamEvent(startStream(), {
      kind: "error",
      data: { code: "provider_unavailable", message: "Ollama is down", remediation: "Run ollama serve", details: null, request_id: "r1" },
    });
    expect(state.active).toBe(false);
    expect(state.error?.code).toBe("provider_unavailable");
  });

  it("is a no-op for the initial idle state passed a routing event", () => {
    const state = applyStreamEvent(initialStreamState, { kind: "routing", data: { intent: "knowledge_qa", method: "rules", confidence: 1 } });
    expect(state).toEqual(initialStreamState);
  });
});
