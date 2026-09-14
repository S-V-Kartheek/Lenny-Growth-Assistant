import type { ErrorBody } from "../api/types";

export function ErrorBanner({ error, onRetry }: { error: ErrorBody; onRetry?: () => void }) {
  return (
    <div className="error-banner" role="alert">
      <div className="error-banner__heading">
        <span aria-hidden="true">⚠️</span> Something went wrong
      </div>
      <p className="error-banner__message">{error.message}</p>
      {error.remediation && <p className="error-banner__remediation">{error.remediation}</p>}
      {error.request_id && <p className="error-banner__request-id">Request ID: {error.request_id}</p>}
      {onRetry && (
        <button type="button" className="error-banner__retry" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}
