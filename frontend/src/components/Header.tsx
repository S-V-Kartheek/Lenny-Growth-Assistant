import type { ProviderStatus } from "../hooks/useProvider";

export function Header({ provider }: { provider: ProviderStatus }) {
  return (
    <header className="app-header">
      <div className="app-header__title">
        <span aria-hidden="true">🎙️</span> Lenny Growth Assistant
      </div>
      <div className="app-header__provider" aria-live="polite">
        {provider.state === "loading" && <span className="badge badge--muted">Connecting…</span>}
        {provider.state === "error" && (
          <span className="badge badge--error" role="status">
            API unreachable
          </span>
        )}
        {provider.state === "ready" && (
          <span className="badge badge--provider" title="Active model provider">
            {provider.info.provider} · {provider.info.model}
            {provider.info.fallback_provider && (
              <span className="badge__fallback"> (fallback: {provider.info.fallback_provider})</span>
            )}
          </span>
        )}
      </div>
    </header>
  );
}
