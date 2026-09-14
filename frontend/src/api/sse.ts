import type { ChatStreamEvent } from "./types";

/**
 * Parses one `text/event-stream` frame (the bytes between blank lines) into a
 * typed event. Kept as a pure function, separate from the fetch/stream
 * plumbing in `client.ts`, so the wire-format parsing can be unit tested
 * without a real network stream or a server.
 *
 * The backend (`app/api/sessions.py`) always sends `event: <kind>` and
 * `data: <json>` on the same frame, so a frame with no recognisable `data:`
 * line is a malformed frame, not a valid empty event -- returned as null so
 * the caller can skip it rather than throw mid-stream.
 */
export function parseSseFrame(frame: string): ChatStreamEvent | null {
  const dataLine = frame
    .split("\n")
    .find((line) => line.startsWith("data:"));
  if (!dataLine) return null;
  const json = dataLine.slice("data:".length).trim();
  if (!json) return null;
  try {
    const payload = JSON.parse(json) as Record<string, unknown>;
    return payload as unknown as ChatStreamEvent;
  } catch {
    return null;
  }
}

/**
 * Reads a fetch Response body as SSE frames, yielding one parsed event per
 * frame. `EventSource` cannot be used here because it only supports GET —
 * this endpoint is a POST that streams its response, so the stream is read
 * directly with the fetch Streams API instead.
 */
export async function* readSseStream(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<ChatStreamEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      // sse_starlette writes CRLF line endings ("\r\n\r\n" between frames),
      // not bare "\n\n" -- normalising here means the boundary search below
      // (and parseSseFrame's per-line split) do not need to know that. A
      // version of this that searched for a literal "\n\n" boundary never
      // matched, so no event was ever parsed until the stream closed, at
      // which point the whole buffer was mistaken for a single frame.
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const event = parseSseFrame(frame);
        if (event) yield event;
        boundary = buffer.indexOf("\n\n");
      }
    }
    if (buffer.trim()) {
      const event = parseSseFrame(buffer);
      if (event) yield event;
    }
  } finally {
    reader.releaseLock();
  }
}
