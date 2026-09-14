import ReactMarkdown from "react-markdown";
import type { Grounding } from "../api/types";

/**
 * A refusal is the system working correctly (PRD 2.2), never rendered as a
 * normal answer. Deliberately not error-red — see docs/design.md §3 — so a
 * user does not learn to distrust an honest "I don't know."
 */
export function RefusalBanner({ content, grounding }: { content: string; grounding: Grounding | null }) {
  return (
    <div className="refusal" role="status">
      <div className="refusal__heading">
        <span aria-hidden="true">🚫</span> No supporting material found
      </div>
      <div className="refusal__body">
        <ReactMarkdown>{content}</ReactMarkdown>
      </div>
      {grounding?.retrieval_confidence != null && (
        <div className="refusal__meta">
          Retrieval confidence {Number(grounding.retrieval_confidence).toFixed(2)}
          {grounding.threshold != null ? ` (threshold ${grounding.threshold})` : ""}
        </div>
      )}
    </div>
  );
}
