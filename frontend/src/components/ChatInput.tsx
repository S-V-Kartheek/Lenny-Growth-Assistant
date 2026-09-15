import { useRef, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";

interface Props {
  disabled: boolean;
  onSend: (content: string) => void;
}

export function ChatInput({ disabled, onSend }: Props) {
  const [value, setValue] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  const submit = () => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue("");
    // Focus stays in the composer so a follow-up question is one keystroke
    // away (docs/design.md §6) instead of being stolen by the new message.
    ref.current?.focus();
  };

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    submit();
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <>
      <form className="chat-input" onSubmit={handleSubmit}>
        <label htmlFor="chat-input-box" className="visually-hidden">
          Ask a product or growth question
        </label>
        <div className="chat-input__composer">
          <textarea
            id="chat-input-box"
            ref={ref}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask a product or growth question…"
            rows={2}
            disabled={disabled}
          />
          <button type="submit" disabled={disabled || !value.trim()} aria-label={disabled ? "Working" : "Send"}>
            {disabled ? (
              <span className="phase-indicator__spinner" aria-hidden="true" />
            ) : (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M4 12L20 4L14 20L11 13L4 12Z"
                  fill="currentColor"
                  stroke="currentColor"
                  strokeWidth="1.2"
                  strokeLinejoin="round"
                />
              </svg>
            )}
          </button>
        </div>
      </form>
      <p className="chat-input__hint">
        <kbd>Enter</kbd> to send · <kbd>Shift</kbd>+<kbd>Enter</kbd> for a new line
      </p>
    </>
  );
}
