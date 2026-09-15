import { useCallback, useEffect, useState } from "react";
import { getProvider, setProvider as setProviderRequest } from "../api/client";
import type { ProviderInfo } from "../api/types";

export type ProviderStatus =
  | { state: "loading" }
  | { state: "ready"; info: ProviderInfo }
  | { state: "error"; message: string };

/**
 * PRD 2.5: the header must reflect the active provider/model without a code
 * change, and offer a live toggle. `switching` covers the request in flight
 * so the toggle can disable itself rather than let a second click race the
 * first against the server-side gateway swap.
 */
export function useProvider(): {
  status: ProviderStatus;
  switching: boolean;
  switchProvider: (provider: string) => Promise<void>;
} {
  const [status, setStatus] = useState<ProviderStatus>({ state: "loading" });
  const [switching, setSwitching] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getProvider()
      .then((info) => {
        if (!cancelled) setStatus({ state: "ready", info });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setStatus({
            state: "error",
            message: err instanceof Error ? err.message : "Could not reach the API.",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const switchProvider = useCallback(async (provider: string) => {
    setSwitching(true);
    try {
      const info = await setProviderRequest(provider);
      setStatus({ state: "ready", info });
    } catch (err) {
      setStatus({
        state: "error",
        message: err instanceof Error ? err.message : "Could not switch model.",
      });
    } finally {
      setSwitching(false);
    }
  }, []);

  return { status, switching, switchProvider };
}
