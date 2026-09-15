import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { MessageBubble } from "./MessageBubble";
import type { MessageRecord } from "../api/types";

function assistantMessage(content: string): MessageRecord {
  return {
    id: "m1",
    session_id: "s1",
    role: "assistant",
    content,
    intent: "knowledge_qa",
    provider: "ollama",
    model: "qwen2.5:7b-instruct",
    latency_ms: 1500,
    token_usage: null,
    grounding: {},
    sources: [],
    error: null,
    created_at: new Date().toISOString(),
  };
}

describe("MessageBubble", () => {
  it("renders every [S#] marker as a real link into its source card, not inert text", () => {
    render(<MessageBubble message={assistantMessage("Activation matters [S1] a lot [S2].")} />);
    const link1 = screen.getByRole("link", { name: "[S1]" });
    const link2 = screen.getByRole("link", { name: "[S2]" });
    expect(link1).toHaveAttribute("href", "#source-1");
    expect(link2).toHaveAttribute("href", "#source-2");
  });

  it("scrolls to and focuses the matching source card when a citation is clicked", async () => {
    const anchor = document.createElement("div");
    anchor.id = "source-1";
    document.body.appendChild(anchor);
    anchor.scrollIntoView = () => {};

    const user = userEvent.setup();
    render(<MessageBubble message={assistantMessage("See [S1].")} />);
    await user.click(screen.getByRole("link", { name: "[S1]" }));
    expect(document.activeElement).toBe(anchor);

    anchor.remove();
  });

  it("renders plain user messages as text, not through the markdown/citation pipeline", () => {
    render(
      <MessageBubble
        message={{ ...assistantMessage("How do I improve activation? [S1]"), role: "user" }}
      />,
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("How do I improve activation? [S1]")).toBeInTheDocument();
  });

  it("shows provider, model and latency in the footer for an assistant turn", () => {
    render(<MessageBubble message={assistantMessage("An answer.")} />);
    expect(screen.getByText(/ollama/)).toBeInTheDocument();
    expect(screen.getByText("1500 ms")).toBeInTheDocument();
  });
});
