import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RefusalBanner } from "./RefusalBanner";
import { ErrorBanner } from "./ErrorBanner";

describe("RefusalBanner", () => {
  it("labels itself explicitly as a refusal, not a normal answer", () => {
    render(<RefusalBanner content="No supporting material." grounding={{ retrieval_confidence: 0.3, threshold: 0.48 }} />);
    expect(screen.getByText(/no supporting material found/i)).toBeInTheDocument();
    expect(screen.getByText(/0.30/)).toBeInTheDocument();
    expect(screen.getByText(/threshold 0.48/)).toBeInTheDocument();
  });

  it("uses role=status, not role=alert (a refusal is not a system error)", () => {
    render(<RefusalBanner content="text" grounding={null} />);
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("ErrorBanner", () => {
  it("uses role=alert and shows the server's remediation text verbatim", () => {
    render(
      <ErrorBanner
        error={{
          code: "provider_unavailable",
          message: "The configured model backend is unreachable.",
          remediation: "Run `ollama serve`.",
          details: null,
          request_id: "req-1",
        }}
      />,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("The configured model backend is unreachable.")).toBeInTheDocument();
    expect(screen.getByText("Run `ollama serve`.")).toBeInTheDocument();
  });

  it("renders a retry action only when a retry handler is supplied", () => {
    const { rerender } = render(
      <ErrorBanner error={{ code: "internal_error", message: "x", remediation: null, details: null, request_id: null }} />,
    );
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();

    rerender(
      <ErrorBanner
        error={{ code: "internal_error", message: "x", remediation: null, details: null, request_id: null }}
        onRetry={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
});
