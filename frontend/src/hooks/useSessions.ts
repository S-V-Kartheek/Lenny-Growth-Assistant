import { useCallback, useEffect, useState } from "react";
import { createSession, listSessions } from "../api/client";
import type { SessionSummary } from "../api/types";

export function useSessions() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    const { sessions: rows } = await listSessions();
    setSessions(rows);
    return rows;
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const rows = await refresh();
        if (cancelled) return;
        if (rows.length > 0) {
          setActiveId(rows[0].id);
        } else {
          const created = await createSession();
          if (!cancelled) {
            setSessions([created]);
            setActiveId(created.id);
          }
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const newSession = useCallback(async () => {
    const created = await createSession();
    setSessions((prev) => [created, ...prev]);
    setActiveId(created.id);
    return created;
  }, []);

  return { sessions, activeId, setActiveId, loading, newSession, refresh };
}
