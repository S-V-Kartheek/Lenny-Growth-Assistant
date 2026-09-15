import { useCallback, useEffect, useState } from "react";
import { createSession, deleteSession, listSessions, renameSession } from "../api/client";
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

  const rename = useCallback(async (id: string, title: string) => {
    const updated = await renameSession(id, title);
    // The rename endpoint's RETURNING clause doesn't recompute message_count
    // (it's a plain UPDATE, not the list query's join+count), so it always
    // comes back 0 -- keep the count this list already knew instead of
    // clobbering it with a wrong one.
    setSessions((prev) =>
      prev.map((s) => (s.id === id ? { ...updated, message_count: s.message_count } : s)),
    );
  }, []);

  const remove = useCallback(
    async (id: string) => {
      await deleteSession(id);
      setSessions((prev) => {
        const remaining = prev.filter((s) => s.id !== id);
        if (id === activeId) {
          // Deleting the open session needs somewhere to land: the next most
          // recent session, or a fresh one if that was the last one left --
          // never an activeId pointing at a session that no longer exists.
          if (remaining.length > 0) {
            setActiveId(remaining[0].id);
          } else {
            createSession().then((created) => {
              setSessions((cur) => [created, ...cur]);
              setActiveId(created.id);
            });
          }
        }
        return remaining;
      });
    },
    [activeId],
  );

  return { sessions, activeId, setActiveId, loading, newSession, refresh, rename, remove };
}
