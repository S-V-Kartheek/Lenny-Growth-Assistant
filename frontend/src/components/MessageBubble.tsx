import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { AnchorHTMLAttributes } from "react";
import type { MessageRecord } from "../api/types";
import { CITATION_HREF, linkifyCitations } from "../lib/citations";

function CitationAwareLink(props: AnchorHTMLAttributes<HTMLAnchorElement>) {
  const href = props.href ?? "";
  if (CITATION_HREF.test(href)) {
    return (
      <a
        {...props}
        className="citation-badge"
        onClick={(e) => {
          e.preventDefault();
          const id = href.slice(1);
          const target = document.getElementById(id);
          if (target) {
            target.scrollIntoView({ behavior: "smooth", block: "center" });
            target.setAttribute("tabindex", "-1");
            target.focus({ preventScroll: true });
          }
        }}
      />
    );
  }
  return <a {...props} target="_blank" rel="noopener noreferrer" />;
}

export function MessageBubble({ message }: { message: MessageRecord }) {
  const isUser = message.role === "user";
  return (
    <div
      className={"message" + (isUser ? " message--user" : " message--assistant")}
      role="article"
    >
      <div className="message__avatar" aria-hidden="true">
        {isUser ? "🧑" : "🎙️"}
      </div>
      <div className="message__body">
        <div className="message__role">{isUser ? "You" : "Assistant"}</div>
        <div className="message__content">
          {isUser ? (
            <p>{message.content}</p>
          ) : (
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: CitationAwareLink }}>
              {linkifyCitations(message.content)}
            </ReactMarkdown>
          )}
        </div>
        {!isUser && (message.provider || message.latency_ms != null) && (
          <div className="message__footer">
            {message.provider && (
              <span>
                {message.provider} · {message.model}
              </span>
            )}
            {message.latency_ms != null && <span>{Math.round(message.latency_ms)} ms</span>}
          </div>
        )}
      </div>
    </div>
  );
}
