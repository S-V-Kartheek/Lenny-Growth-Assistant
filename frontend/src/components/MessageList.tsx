import type { MessageRecord } from "../api/types";
import { isRefusal } from "../lib/grounding";
import { MessageBubble } from "./MessageBubble";
import { RefusalBanner } from "./RefusalBanner";
import { SourceCards } from "./SourceCards";

const EXAMPLES = [
  "How do I improve activation for a B2B SaaS product?",
  "What does the podcast say about pricing power?",
  "How should an early-stage team think about hiring its first PM?",
];

export function MessageList({
  messages,
  onExampleClick,
}: {
  messages: MessageRecord[];
  onExampleClick: (text: string) => void;
}) {
  if (messages.length === 0) {
    return (
      <div className="empty-state">
        <p>Ask a real product or growth question and get an answer grounded in what operators actually said, with citations.</p>
        <ul className="empty-state__examples">
          {EXAMPLES.map((ex) => (
            <li key={ex}>
              <button type="button" onClick={() => onExampleClick(ex)}>
                {ex}
              </button>
            </li>
          ))}
        </ul>
      </div>
    );
  }

  return (
    <div className="message-list">
      {messages.map((m) => {
        if (m.role === "user") return <MessageBubble key={m.id} message={m} />;
        if (isRefusal(m)) {
          return (
            <div key={m.id} className="message-list__turn">
              <SourceCards sources={m.sources} heading="Closest matches (not used)" />
              <RefusalBanner content={m.content} grounding={m.grounding} />
            </div>
          );
        }
        return (
          <div key={m.id} className="message-list__turn">
            <SourceCards sources={m.sources} heading="Sources" />
            <MessageBubble message={m} />
          </div>
        );
      })}
    </div>
  );
}
