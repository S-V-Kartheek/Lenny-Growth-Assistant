import type { StreamState } from "../lib/chatReducer";
import { PhaseIndicator } from "./PhaseIndicator";
import { SourceCards } from "./SourceCards";
import { ErrorBanner } from "./ErrorBanner";

/**
 * The in-flight turn: phase status first, source cards as soon as retrieval
 * finishes (PRD 2.1 — sources appear before the answer text, not after),
 * then the answer growing token-by-token. Once `result` lands, the parent
 * swaps this out for a settled `MessageBubble`/`RefusalBanner`.
 */
export function StreamingTurn({ stream, onRetry }: { stream: StreamState; onRetry?: () => void }) {
  if (!stream.active && !stream.error) return null;

  return (
    <div className="streaming-turn">
      <PhaseIndicator phase={stream.phase} />
      <SourceCards sources={stream.sources} heading="Sources found" />
      {stream.text && (
        <div className="message message--assistant">
          <div className="message__role">Assistant</div>
          <div className="message__content">
            <p>
              {stream.text}
              {stream.active && <span className="typing-caret" aria-hidden="true" />}
            </p>
          </div>
        </div>
      )}
      {stream.error && <ErrorBanner error={stream.error} onRetry={onRetry} />}
    </div>
  );
}
