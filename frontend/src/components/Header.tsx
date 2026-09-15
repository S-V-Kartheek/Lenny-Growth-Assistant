import type { ProviderStatus } from "../hooks/useProvider";
import { Logo } from "./Logo";

// Only these two are surfaced as a one-click toggle: Ollama is the fully
// local demo path, Gemini the cloud path for a hosted deployment (Render/
// Vercel, no Ollama host available). Anthropic/OpenAI stay configurable via
// LLM_PROVIDER for anyone who wants them, but a two-way switch is what the
// UI needs -- more options here would just be more ways to pick an
// unconfigured provider by accident.
const TOGGLE_PROVIDERS = [
  { id: "ollama", label: "Ollama" },
  { id: "gemini", label: "Gemini" },
] as const;

export function Header({
  provider,
  switching,
  onSwitchProvider,
  onBack,
}: {
  provider: ProviderStatus;
  switching: boolean;
  onSwitchProvider: (provider: string) => void;
  onBack?: () => void;
}) {
  const activeProvider = provider.state === "ready" ? provider.info.provider : null;

  return (
    <header className="app-header">
      <div className="app-header__title">
        {onBack ? (
          <button type="button" className="app-header__home" onClick={onBack} aria-label="Back to home">
            <Logo size="sm" />
          </button>
        ) : (
          <Logo size="sm" />
        )}
      </div>
      <div className="app-header__controls">
        <div
          className="provider-toggle"
          role="group"
          aria-label="Active model provider"
        >
          {TOGGLE_PROVIDERS.map(({ id, label }) => (
            <button
              key={id}
              type="button"
              className={`provider-toggle__option${
                activeProvider === id ? " provider-toggle__option--active" : ""
              }`}
              aria-pressed={activeProvider === id}
              disabled={switching || activeProvider === id}
              onClick={() => onSwitchProvider(id)}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="app-header__provider" aria-live="polite">
          {provider.state === "loading" && (
            <span className="badge badge--muted">Connecting…</span>
          )}
          {provider.state === "error" && (
            <span className="badge badge--error" role="status">
              API unreachable
            </span>
          )}
          {provider.state === "ready" && (
            <span className="badge badge--provider" title="Active model provider">
              {switching ? "Switching…" : `${provider.info.provider} · ${provider.info.model}`}
              {provider.info.fallback_provider && !switching && (
                <span className="badge__fallback"> (fallback: {provider.info.fallback_provider})</span>
              )}
            </span>
          )}
        </div>
      </div>
    </header>
  );
}
