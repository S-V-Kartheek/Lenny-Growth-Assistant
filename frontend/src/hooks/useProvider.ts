import { useEffect, useState } from "react";
import { getProvider } from "../api/client";
import type { ProviderInfo } from "../api/types";

export type ProviderStatus =
  | { state: "loading" }
  | { state: "ready"; info: ProviderInfo }
  | { state: "error"; message: string };

/** PRD 2.5: the header must reflect the active provider/model without a code change. */
export function useProvider(): ProviderStatus {
  const [status, setStatus] = useState<ProviderStatus>({ state: "loading" });

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

  return status;
}
