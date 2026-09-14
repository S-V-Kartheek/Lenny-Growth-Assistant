import { describe, expect, it } from "vitest";
import { parseSseFrame, readSseStream } from "./sse";

describe("parseSseFrame", () => {
  it("parses a well-formed event/data frame", () => {
    const frame = 'event: phase\ndata: {"kind":"phase","phase":"retrieving"}';
    expect(parseSseFrame(frame)).toEqual({ kind: "phase", phase: "retrieving" });
  });

  it("returns null for a frame with no data line", () => {
    expect(parseSseFrame("event: phase\n")).toBeNull();
  });

  it("returns null for a data line that is not valid JSON", () => {
    expect(parseSseFrame("event: phase\ndata: not-json")).toBeNull();
  });

  it("returns null for an empty data line rather than throwing", () => {
    expect(parseSseFrame("event: phase\ndata: ")).toBeNull();
  });
});

function streamFromChunks(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let i = 0;
  return new ReadableStream({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(encoder.encode(chunks[i]));
        i += 1;
      } else {
        controller.close();
      }
    },
  });
}

describe("readSseStream", () => {
  it("yields one event per frame, even when a frame arrives split across chunks", async () => {
    const chunks = [
      'event: phase\ndata: {"kind":"phase","phase":"routing"}\n\n',
      'event: delta\ndata: {"kind":"delta","text":"Hel',
      'lo"}\n\n',
    ];
    const events = [];
    for await (const event of readSseStream(streamFromChunks(chunks))) {
      events.push(event);
    }
    expect(events).toEqual([
      { kind: "phase", phase: "routing" },
      { kind: "delta", text: "Hello" },
    ]);
  });

  it("parses real sse_starlette output, which uses CRLF between frames, not bare \\n\\n", async () => {
    // Regression test: a version of readSseStream that searched only for a
    // literal "\n\n" boundary never matched CRLF-terminated frames, so no
    // event was parsed until the stream closed. Found via live Playwright
    // verification against the real API, not by a unit test -- this fixture
    // reproduces the exact bytes the backend sends.
    const chunks = [
      'event: phase\r\ndata: {"kind":"phase","phase":"routing"}\r\n\r\n',
      'event: phase\r\ndata: {"kind":"phase","phase":"retrieving"}\r\n\r\n',
      'event: delta\r\ndata: {"kind":"delta","text":"Hi"}\r\n\r\n',
    ];
    const events = [];
    for await (const event of readSseStream(streamFromChunks(chunks))) {
      events.push(event);
    }
    expect(events).toEqual([
      { kind: "phase", phase: "routing" },
      { kind: "phase", phase: "retrieving" },
      { kind: "delta", text: "Hi" },
    ]);
  });

  it("yields a trailing frame that never had a closing blank line", async () => {
    const chunks = ['event: phase\ndata: {"kind":"phase","phase":"done"}'];
    const events = [];
    for await (const event of readSseStream(streamFromChunks(chunks))) {
      events.push(event);
    }
    expect(events).toEqual([{ kind: "phase", phase: "done" }]);
  });
});
