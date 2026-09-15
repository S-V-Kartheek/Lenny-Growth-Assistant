import { useState } from "react";
import type { KeyboardEvent } from "react";
import type { SessionSummary } from "../api/types";

interface Props {
  sessions: SessionSummary[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
}

export function SessionSidebar({
  sessions,
  activeId,
  onSelect,
  onCreate,
  onRename,
  onDelete,
}: Props) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  const startEditing = (s: SessionSummary) => {
    setEditingId(s.id);
    setDraft(s.title || "New chat");
  };

  const commitEdit = () => {
    const title = draft.trim();
    if (editingId && title) onRename(editingId, title);
    setEditingId(null);
  };

  const onEditKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commitEdit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      setEditingId(null);
    }
  };

  const handleDelete = (s: SessionSummary) => {
    if (window.confirm(`Delete "${s.title || "New chat"}"? This can't be undone.`)) {
      onDelete(s.id);
    }
  };

  return (
    <nav className="sidebar" aria-label="Sessions">
      <button type="button" className="sidebar__new" onClick={onCreate}>
        <span aria-hidden="true">＋</span> New session
      </button>
      {sessions.length > 0 && <div className="sidebar__section-label">Recent</div>}
      <ul className="sidebar__list">
        {sessions.map((s) => (
          <li key={s.id}>
            {editingId === s.id ? (
              <input
                className="sidebar__item-input"
                value={draft}
                autoFocus
                maxLength={200}
                onChange={(e) => setDraft(e.target.value)}
                onBlur={commitEdit}
                onKeyDown={onEditKeyDown}
                aria-label="Rename session"
              />
            ) : (
              <div
                className={
                  "sidebar__item" + (s.id === activeId ? " sidebar__item--active" : "")
                }
              >
                <button
                  type="button"
                  className="sidebar__item-select"
                  aria-current={s.id === activeId ? "true" : undefined}
                  onClick={() => onSelect(s.id)}
                >
                  <span className="sidebar__item-title">{s.title || "New chat"}</span>
                </button>
                <span className="sidebar__item-count">{s.message_count}</span>
                <span className="sidebar__item-actions">
                  <button
                    type="button"
                    className="sidebar__item-action"
                    aria-label={`Rename "${s.title || "New chat"}"`}
                    title="Rename"
                    onClick={() => startEditing(s)}
                  >
                    ✎
                  </button>
                  <button
                    type="button"
                    className="sidebar__item-action sidebar__item-action--danger"
                    aria-label={`Delete "${s.title || "New chat"}"`}
                    title="Delete"
                    onClick={() => handleDelete(s)}
                  >
                    🗑
                  </button>
                </span>
              </div>
            )}
          </li>
        ))}
      </ul>
    </nav>
  );
}
