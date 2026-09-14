import { useEffect, useState } from "react";
import { getArtifact } from "../api/client";
import type { ArtifactRecord } from "../api/types";

export function useArtifact(artifactId: string | null) {
  const [artifact, setArtifact] = useState<ArtifactRecord | null>(null);
  // Derived rather than a separate `setLoading(true)` at the top of the
  // effect: "loading" is exactly "we have an id but not yet its artifact,"
  // so it does not need its own state to fall out of sync with `artifact`.
  const loading = artifactId != null && artifact?.id !== artifactId;

  useEffect(() => {
    if (!artifactId) {
      setArtifact(null);
      return;
    }
    let cancelled = false;
    getArtifact(artifactId).then((row) => {
      if (!cancelled) setArtifact(row);
    });
    return () => {
      cancelled = true;
    };
  }, [artifactId]);

  return { artifact, loading };
}
