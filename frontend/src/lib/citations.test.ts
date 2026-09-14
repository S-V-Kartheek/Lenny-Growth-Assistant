import { describe, expect, it } from "vitest";
import { CITATION_HREF, linkifyCitations } from "./citations";

describe("linkifyCitations", () => {
  it("turns a citation marker into a markdown link pointing at its source anchor", () => {
    const out = linkifyCitations("Activation matters [S1] a lot.");
    expect(out).toBe('Activation matters [[S1]](#source-1 "Jump to source 1") a lot.');
  });

  it("handles multiple distinct markers in one string", () => {
    const out = linkifyCitations("See [S2] and also [S10].");
    expect(out).toContain("#source-2");
    expect(out).toContain("#source-10");
  });

  it("leaves text with no markers untouched", () => {
    const out = linkifyCitations("No citations here.");
    expect(out).toBe("No citations here.");
  });
});

describe("CITATION_HREF", () => {
  it("matches a source anchor", () => {
    expect(CITATION_HREF.test("#source-3")).toBe(true);
  });

  it("does not match an external link", () => {
    expect(CITATION_HREF.test("https://youtube.com/watch?v=abc")).toBe(false);
  });
});
