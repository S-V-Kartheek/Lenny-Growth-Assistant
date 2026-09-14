import { useEffect, useRef } from "react";
import type { ArtifactRecord } from "../api/types";
import { ArtifactViewer } from "./ArtifactViewer";

/**
 * Empty when there is no artifact yet, so a plain Q&A session does not carry
 * dead panel width (docs/design.md §2). Focus moves to the heading only on
 * the first artifact a session produces, not on every later version, so an
 * ongoing conversation is not repeatedly yanked sideways (docs/design.md §6).
 */
export function ArtifactPanel({
  artifact,
  loading,
  isFirstArtifact,
}: {
  artifact: ArtifactRecord | null;
  loading: boolean;
  isFirstArtifact: boolean;
}) {
  const headingRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (artifact && isFirstArtifact) {
      headingRef.current?.focus();
    }
  }, [artifact, isFirstArtifact]);

  if (!artifact && !loading) return null;

  return (
    <aside className="artifact-panel" aria-label="Generated artifact">
      <div ref={headingRef} tabIndex={-1} className="visually-hidden">
        Artifact panel
      </div>
      {loading && !artifact && <p className="artifact-panel__loading">Loading artifact…</p>}
      {artifact && <ArtifactViewer artifact={artifact} />}
    </aside>
  );
}
