import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ArtifactViewer } from "./ArtifactViewer";
import type { ArtifactRecord } from "../api/types";

function htmlArtifact(overrides: Partial<ArtifactRecord> = {}): ArtifactRecord {
  return {
    id: "a1",
    session_id: "s1",
    message_id: "m1",
    kind: "html",
    title: "Onboarding Checklist",
    content: "<h1>Onboarding Checklist</h1>",
    version: 1,
    sanitization: {},
    sandbox: "allow-popups allow-popups-to-escape-sandbox",
    document_url: "/api/artifacts/a1/document",
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

describe("ArtifactViewer", () => {
  beforeEach(() => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      configurable: true,
    });
    globalThis.URL.createObjectURL = vi.fn().mockReturnValue("blob:mock");
    globalThis.URL.revokeObjectURL = vi.fn();
  });

  it("renders the iframe with the exact sandbox string from the API, never adding allow-same-origin or allow-scripts", () => {
    render(<ArtifactViewer artifact={htmlArtifact()} />);
    const frame = screen.getByTitle(/generated document/i);
    expect(frame).toHaveAttribute("sandbox", "allow-popups allow-popups-to-escape-sandbox");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-same-origin");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-scripts");
  });

  it("falls back to an empty sandbox attribute rather than omitting it if the API ever sent null", () => {
    render(<ArtifactViewer artifact={htmlArtifact({ sandbox: null })} />);
    const frame = screen.getByTitle(/generated document/i);
    expect(frame).toHaveAttribute("sandbox", "");
  });

  it("switches to the source tab and shows the raw sanitised content", async () => {
    const user = userEvent.setup();
    render(<ArtifactViewer artifact={htmlArtifact({ content: "<h1>Raw</h1>" })} />);
    await user.click(screen.getByRole("tab", { name: "Source" }));
    expect(screen.getByText("<h1>Raw</h1>")).toBeInTheDocument();
  });

  it("copies the artifact content to the clipboard and confirms it", async () => {
    render(<ArtifactViewer artifact={htmlArtifact({ content: "copy me" })} />);
    // fireEvent, not userEvent, for this one click: userEvent.setup() installs
    // its own clipboard stub that shadows the one this test configured above.
    fireEvent.click(screen.getByRole("button", { name: /copy artifact content/i }));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("copy me");
    expect(await screen.findByText(/copied/i)).toBeInTheDocument();
  });

  it("renders a Markdown artifact through the client-side renderer, not the iframe", () => {
    render(
      <ArtifactViewer
        artifact={htmlArtifact({ kind: "markdown", content: "# Heading\n\nBody text", document_url: null, sandbox: null })}
      />,
    );
    expect(screen.queryByTitle(/generated document/i)).toBeNull();
    expect(screen.getByRole("heading", { name: "Heading" })).toBeInTheDocument();
  });
});
