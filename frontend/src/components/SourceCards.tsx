import type { Source } from "../api/types";

/**
 * Cited vs. uncited/near-miss are visually distinguished by border style and
 * an explicit text badge — never by color alone (docs/design.md §6).
 */
export function SourceCards({ sources, heading }: { sources: Source[]; heading?: string }) {
  if (sources.length === 0) return null;
  return (
    <section className="sources" aria-label={heading ?? "Sources"}>
      {heading && <h3 className="sources__heading">{heading}</h3>}
      <ol className="sources__list">
        {sources.map((s) => (
          <li
            key={s.chunk_id}
            id={`source-${s.index}`}
            className={"source-card" + (s.cited ? " source-card--cited" : " source-card--uncited")}
          >
            <div className="source-card__top">
              <span className="source-card__label">{s.label}</span>
              {s.cited ? (
                <span className="source-card__badge">cited</span>
              ) : (
                <span className="source-card__badge source-card__badge--muted">not cited</span>
              )}
            </div>
            <div className="source-card__episode">{s.episode_title}</div>
            <div className="source-card__meta">
              {s.guest && <span>{s.guest}</span>}
              {s.timestamp && <span className="source-card__timestamp">{s.timestamp}</span>}
            </div>
            <p className="source-card__excerpt">{s.excerpt}</p>
            {s.citation_url && (
              <a
                className="source-card__link"
                href={s.citation_url}
                target="_blank"
                rel="noopener noreferrer"
              >
                Watch on YouTube ↗
              </a>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}
