/**
 * Rewrites `[S3]`-style citation markers into Markdown links pointing at the
 * matching source card's anchor (`#source-3`), so every marker the model
 * writes is a real link to a real card (docs/design.md §1, §4) rather than
 * inert bracketed text — instead of a bespoke inline-citation renderer, this
 * reuses the Markdown link renderer already in place for artifact previews.
 */
const CITATION_MARKER = /\[S(\d+)\]/g;

export function linkifyCitations(content: string): string {
  return content.replace(CITATION_MARKER, (full, index: string) => `[${full}](#source-${index} "Jump to source ${index}")`);
}

export const CITATION_HREF = /^#source-\d+$/;
