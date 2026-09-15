export function Logo({ size = "md" }: { size?: "sm" | "md" | "lg" }) {
  const dim = size === "sm" ? 28 : size === "lg" ? 40 : 32;
  const fontSize = size === "sm" ? "0.95rem" : size === "lg" ? "1.15rem" : "1.02rem";

  return (
    <span className="logo" style={{ fontSize }}>
      <svg
        className="logo__mark"
        width={dim}
        height={dim}
        viewBox="0 0 32 32"
        fill="none"
        aria-hidden="true"
      >
        <rect width="32" height="32" rx="8" fill="currentColor" />
        <path
          d="M10 12.5c0-1.1.9-2 2-2h8c1.1 0 2 .9 2 2v7c0 1.1-.9 2-2 2h-8c-1.1 0-2-.9-2-2v-7z"
          fill="var(--bg-panel)"
          opacity="0.95"
        />
        <path
          d="M14 14.5h4M14 17h2.5"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
        />
        <circle cx="22" cy="10" r="3" fill="var(--accent-soft)" />
      </svg>
      <span className="logo__text">Lenny Growth Assistant</span>
    </span>
  );
}
