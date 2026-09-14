import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SourceCards } from "./SourceCards";
import type { Source } from "../api/types";

function source(overrides: Partial<Source>): Source {
  return {
    index: 1,
    label: "S1",
    cited: false,
    retrieval_method: "hybrid",
    chunk_id: "c1",
    episode_slug: "ep",
    episode_title: "Episode Title",
    guest: "Guest Name",
    speakers: ["Guest Name"],
    timestamp: "00:05:30",
    start_seconds: 330,
    citation_url: "https://www.youtube.com/watch?v=abc123&t=325s",
    publish_date: "2024-01-01",
    source_commit: "abc123",
    source_path: "x",
    score: 0.9,
    excerpt: "an excerpt",
    ...overrides,
  };
}

describe("SourceCards", () => {
  it("renders nothing when there are no sources", () => {
    const { container } = render(<SourceCards sources={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("visually and textually distinguishes a cited source from an uncited one", () => {
    render(<SourceCards sources={[source({ cited: true, index: 1 }), source({ cited: false, index: 2, label: "S2", chunk_id: "c2" })]} />);
    const cited = document.getElementById("source-1")!;
    const uncited = document.getElementById("source-2")!;
    expect(cited.className).toContain("source-card--cited");
    expect(uncited.className).toContain("source-card--uncited");
    expect(screen.getByText("cited")).toBeInTheDocument();
    expect(screen.getByText("not cited")).toBeInTheDocument();
  });

  it("links each card to the episode's exact timestamp", () => {
    render(<SourceCards sources={[source({})]} />);
    const link = screen.getByRole("link", { name: /watch on youtube/i });
    expect(link).toHaveAttribute("href", "https://www.youtube.com/watch?v=abc123&t=325s");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("gives each card an id matching its [S#] marker for citation deep-linking", () => {
    render(<SourceCards sources={[source({ index: 7, label: "S7" })]} />);
    expect(document.getElementById("source-7")).not.toBeNull();
  });
});
