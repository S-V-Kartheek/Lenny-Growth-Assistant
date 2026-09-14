import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ArtifactRecord } from "../api/types";
import { artifactDocumentUrl } from "../api/client";

type Tab = "preview" | "source";

function download(artifact: ArtifactRecord) {
  const ext = artifact.kind === "html" ? "html" : "md";
  const blob = new Blob([artifact.content], {
    type: artifact.kind === "html" ? "text/html" : "text/markdown",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${artifact.title || "artifact"}.${ext}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export function ArtifactViewer({ artifact }: { artifact: ArtifactRecord }) {
  const [tab, setTab] = useState<Tab>("preview");
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(artifact.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard permission denied or unavailable — the source tab still
      // lets a user select and copy manually, so this is not fatal.
    }
  };

  return (
    <div className="artifact-viewer">
      <div className="artifact-viewer__header">
        <h2 className="artifact-viewer__title">{artifact.title}</h2>
        <span className="badge badge--muted">{artifact.kind} · v{artifact.version}</span>
      </div>

      <div className="artifact-viewer__tabs" role="tablist" aria-label="Artifact view">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "preview"}
          className={tab === "preview" ? "tab tab--active" : "tab"}
          onClick={() => setTab("preview")}
        >
          Preview
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "source"}
          className={tab === "source" ? "tab tab--active" : "tab"}
          onClick={() => setTab("source")}
        >
          Source
        </button>
        <div className="artifact-viewer__actions">
          <button type="button" onClick={copy} aria-label="Copy artifact content">
            {copied ? "Copied ✓" : "Copy"}
          </button>
          <button type="button" onClick={() => download(artifact)} aria-label="Download artifact">
            Download
          </button>
        </div>
      </div>

      <div className="artifact-viewer__body" role="tabpanel">
        {tab === "source" && <pre className="artifact-viewer__source">{artifact.content}</pre>}
        {tab === "preview" && artifact.kind === "html" && artifact.document_url && (
          // The sandbox string is read verbatim from the API response
          // (ARTIFACT_SANDBOX) and never constructed here — see
          // docs/design.md §5 and backend/app/agent/sanitize.py. It never
          // contains allow-same-origin or allow-scripts.
          <iframe
            className="artifact-viewer__frame"
            title={`Generated document: ${artifact.title}`}
            src={artifactDocumentUrl(artifact.id)}
            sandbox={artifact.sandbox ?? ""}
          />
        )}
        {tab === "preview" && artifact.kind === "markdown" && (
          <div className="artifact-viewer__markdown">
            {/* No rehype-raw: this renderer has no code path that turns
                markdown text into live HTML, regardless of its content —
                defense-in-depth independent of server-side escaping. */}
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{artifact.content}</ReactMarkdown>
          </div>
        )}
      </div>
    </div>
  );
}
