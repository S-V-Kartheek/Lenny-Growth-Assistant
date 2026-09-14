import type { SessionSummary } from "../api/types";

interface Props {
  sessions: SessionSummary[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
}

export function SessionSidebar({ sessions, activeId, onSelect, onCreate }: Props) {
  return (
    <nav className="sidebar" aria-label="Sessions">
      <button type="button" className="sidebar__new" onClick={onCreate}>
        + New session
      </button>
      <ul className="sidebar__list">
        {sessions.map((s) => (
          <li key={s.id}>
            <button
              type="button"
              className={
                "sidebar__item" + (s.id === activeId ? " sidebar__item--active" : "")
              }
              aria-current={s.id === activeId ? "true" : undefined}
              onClick={() => onSelect(s.id)}
            >
              <span className="sidebar__item-title">{s.title || "New chat"}</span>
              <span className="sidebar__item-count">{s.message_count}</span>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
