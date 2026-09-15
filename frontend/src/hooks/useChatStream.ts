import { useCallback, useRef, useState } from "react";
import { ApiError, streamMessage } from "../api/client";
import type { MessageRecord } from "../api/types";
import { applyStreamEvent, initialStreamState, startStream, type StreamState } from "../lib/chatReducer";

/**
 * Drives one session's live turn against POST /api/sessions/{id}/messages.
 * `messages` holds everything already settled (loaded history plus finished
 * turns); `stream` holds the in-flight turn's incremental state so the UI can
 * render phase/source/delta progress before a `result` event ever arrives.
 */
export function useChatStream(sessionId: string | null) {
  const [messages, setMessages] = useState<MessageRecord[]>([]);
  const [stream, setStream] = useState<StreamState>(initialStreamState);
  const abortRef = useRef<AbortController | null>(null);

  const setHistory = useCallback((history: MessageRecord[]) => {
    setMessages(history);
  }, []);

  const send = useCallback(
    async (content: string) => {
      if (!sessionId || stream.active) return;

      const optimisticUser: MessageRecord = {
        id: `pending-${Date.now()}`,
        session_id: sessionId,
        role: "user",
        content,
        intent: null,
        provider: null,
        model: null,
        latency_ms: null,
        token_usage: null,
        grounding: null,
        sources: [],
        error: null,
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, optimisticUser]);
      setStream(startStream());

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        for await (const event of streamMessage(sessionId, content, controller.signal)) {
          setStream((prev) => applyStreamEvent(prev, event));
          if (event.kind === "result") {
            const result = event.result;
            const finalised: MessageRecord = {
              id: `local-${Date.now()}`,
              session_id: sessionId,
              role: "assistant",
              content: result.content,
              intent: result.intent,
              provider: result.provider,
              model: result.model,
              latency_ms: result.latency_ms,
              token_usage: result.usage,
              grounding: result.grounding,
              sources: result.sources,
              error: null,
              created_at: new Date().toISOString(),
            };
            setMessages((prev) => [...prev, finalised]);
          }
        }
      } catch (err) {
        if (controller.signal.aborted) return;
        const body =
          err instanceof ApiError
            ? { code: err.code, message: err.message, remediation: err.remediation, details: null, request_id: null }
            : {
                code: "internal_error",
                message: err instanceof Error ? err.message : "The stream failed unexpectedly.",
                remediation: null,
                details: null,
                request_id: null,
              };
        setStream((prev) => ({ ...prev, error: body, active: false }));
        return;
      }
      setStream((prev) => ({ ...prev, active: false }));
    },
    [sessionId, stream.active],
  );

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    setStream((prev) => ({ ...prev, active: false }));
  }, []);

  const resetStream = useCallback(() => setStream(initialStreamState), []);

  return { messages, setHistory, stream, send, cancel, resetStream };
}
