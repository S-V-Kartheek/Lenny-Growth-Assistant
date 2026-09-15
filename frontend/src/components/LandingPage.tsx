import { Logo } from "./Logo";

const FEATURES = [
  {
    icon: "◎",
    title: "Grounded answers",
    body: "Every claim links to the exact moment in a Lenny's Podcast episode — click a citation and hear the operator say it.",
  },
  {
    icon: "◇",
    title: "Honest refusals",
    body: "When the transcripts don't support an answer, it says so. No fluent nonsense dressed up as expertise.",
  },
  {
    icon: "▣",
    title: "Ship 30 essays & artifacts",
    body: "Generate structured essays, checklists, and landing pages — all grounded in the same cited source material.",
  },
] as const;

const STEPS = [
  { num: "01", title: "Ask", body: "Pose a real product or growth question in plain language." },
  { num: "02", title: "Retrieve", body: "Hybrid search scans 40+ full episodes for the most relevant passages." },
  { num: "03", title: "Answer", body: "Get a cited response with source cards you can verify in seconds." },
] as const;

const STATS = [
  { value: "40+", label: "Full episodes indexed" },
  { value: "4.4k", label: "Searchable passages" },
  { value: "100%", label: "Citation-linked sources" },
] as const;

export function LandingPage({ onEnter }: { onEnter: () => void }) {
  return (
    <div className="landing">
      <div className="landing__bg" aria-hidden="true">
        <div className="landing__orb landing__orb--1" />
        <div className="landing__orb landing__orb--2" />
        <div className="landing__grid" />
      </div>

      <nav className="landing__nav">
        <Logo size="md" />
        <button type="button" className="landing__nav-cta" onClick={onEnter}>
          Open assistant
        </button>
      </nav>

      <main className="landing__main">
        <section className="landing__hero">
          <div className="landing__hero-copy">
            <p className="landing__eyebrow">Built on Lenny's Podcast transcripts</p>
            <h1 className="landing__headline">
              Product & growth wisdom,
              <span className="landing__headline-accent"> cited to the second</span>
            </h1>
            <p className="landing__subhead">
              Ask operators how they actually think about activation, pricing, hiring, and
              growth. Get answers backed by what they said — with deep links to the exact
              moment in each episode.
            </p>
            <div className="landing__actions">
              <button type="button" className="landing__btn landing__btn--primary" onClick={onEnter}>
                Start asking questions
                <span className="landing__btn-arrow" aria-hidden="true">→</span>
              </button>
              <a href="#how-it-works" className="landing__btn landing__btn--ghost">
                How it works
              </a>
            </div>

            <div className="landing__stats">
              {STATS.map(({ value, label }) => (
                <div key={label} className="landing__stat">
                  <span className="landing__stat-value">{value}</span>
                  <span className="landing__stat-label">{label}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="landing__preview" aria-label="Example conversation preview">
          <div className="landing__preview-window">
            <div className="landing__preview-chrome">
              <span className="landing__preview-dot" />
              <span className="landing__preview-dot" />
              <span className="landing__preview-dot" />
              <span className="landing__preview-title">Assistant</span>
            </div>
            <div className="landing__preview-body">
              <div className="landing__preview-q">
                How do I improve activation for a B2B SaaS product?
              </div>
              <div className="landing__preview-sources">
                <span className="landing__preview-tag">3 sources found</span>
              </div>
              <div className="landing__preview-a">
                <p>
                  Focus on reducing time-to-value in the first session. Elena Verna suggests
                  treating activation as a{" "}
                  <span className="landing__preview-cite">habit loop</span>
                  <sup className="landing__preview-sup">1</sup>, not a one-time event…
                </p>
              </div>
              <div className="landing__preview-card">
                <span className="landing__preview-card-badge">Cited</span>
                <strong>Elena Verna on growth loops</strong>
                <span className="landing__preview-card-meta">42:18 · Lenny's Podcast</span>
              </div>
            </div>
          </div>
          </div>
        </section>

        <section className="landing__features" id="features">
          <h2 className="landing__section-title">Why this assistant is different</h2>
          <p className="landing__section-desc">
            Generic AI chat invents plausible advice. This one only speaks when the
            transcripts support it — and shows you the proof.
          </p>
          <div className="landing__feature-grid">
            {FEATURES.map(({ icon, title, body }) => (
              <article key={title} className="landing__feature">
                <span className="landing__feature-icon" aria-hidden="true">
                  {icon}
                </span>
                <h3>{title}</h3>
                <p>{body}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="landing__steps" id="how-it-works">
          <h2 className="landing__section-title">How it works</h2>
          <div className="landing__step-list">
            {STEPS.map(({ num, title, body }) => (
              <article key={num} className="landing__step">
                <span className="landing__step-num">{num}</span>
                <div>
                  <h3>{title}</h3>
                  <p>{body}</p>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section className="landing__cta">
          <div className="landing__cta-inner">
            <h2>Ready to ask something real?</h2>
            <p>
              No signup. Runs locally with Ollama, or switch to a cloud provider in one
              click.
            </p>
            <button type="button" className="landing__btn landing__btn--primary landing__btn--lg" onClick={onEnter}>
              Open the assistant
            </button>
          </div>
        </section>
      </main>

      <footer className="landing__footer">
        <Logo size="sm" />
        <p>
          Grounded in{" "}
          <a href="https://www.youtube.com/@LennysPodcast" target="_blank" rel="noopener noreferrer">
            Lenny's Podcast
          </a>
          . Citations deep-link to the source.
        </p>
      </footer>
    </div>
  );
}
