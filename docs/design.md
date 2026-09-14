# Design — UI principles, interaction states, accessibility

**Status:** written in checkpoint 4, alongside the frontend it governs. Not
retrofitted after the fact — the components in `frontend/src` are built to
satisfy the states and rules below, and the manual test plan
(`docs/manual-test-plan.md`) checks the built app against them.

---

## 1. What this UI has to be honest about

The PRD's core promise (1.3) is: *if it answers, the answer is supported; if
it cannot support an answer, it says so.* A UI can undermine that promise
without a single wrong line of code, just by rendering a refusal and a normal
answer the same way, or by looking "hung" during a 30-second local-model call
and training the user to distrust the spinner. Three rules follow directly:

1. **A refusal is never allowed to look like an answer.** Different container,
   different color role, an explicit label, and it never appears where a
   normal assistant bubble would be silently expected. See §3.
2. **Latency is shown, not hidden.** The backend already emits phase events
   (`routing → retrieving → generating → validating → done`) for exactly this
   reason (PRD 1.6, "Latency"); the UI's job is to surface every one of them,
   continuously, so a 45-second wait for a 7B model reads as progress, not a
   freeze.
3. **A citation marker is only ever decoration for a real source card.** No
   `[S3]` renders as plain text with nothing behind it — it is always a link
   into the source list, so "every citation marker resolves to a real source
   card" (PRD 2.1 acceptance) is visibly true, not just contractually true in
   the API.

## 2. Layout

```
┌─────────────────────────────────────────────────────────────┐
│ Header: app name · provider/model badge · session picker    │
├───────────────┬───────────────────────────────┬─────────────┤
│ Session list   │  Chat (messages, phase,       │  Artifact   │
│ (sidebar)      │  source cards, input)         │  panel      │
│                │                               │  (collapses │
│                │                               │  when empty)│
└───────────────┴───────────────────────────────┴─────────────┘
```

The artifact panel only appears once a turn produces one; it is not empty
chrome taking up width for a plain Q&A session. Below ~900px the three
columns stack vertically (sidebar collapses to a toggle) rather than
compressing horizontally into unreadable slivers.

## 3. Interaction states

| State | Trigger | What is shown |
|---|---|---|
| **Empty** | New session, no messages yet | A short prompt ("Ask a product or growth question…") and 2-3 example questions pulled from the skill registry's `examples`, so a new user sees what "grounded question" means concretely |
| **Routing** | `phase: routing` | A single-line status ("Working out what you're asking…") in the live region; no content area changes yet |
| **Retrieving** | `phase: retrieving` | Status updates to "Searching the transcripts…"; once the `sources` event lands, source cards render immediately, *before* any answer text — the PRD flow (2.1) says sources appear "as soon as retrieval finishes," not after generation |
| **Generating** | `phase: generating`, `delta` events arriving | Status "Writing the answer…"; assistant bubble grows token-by-token with a visible typing caret |
| **Validating** | `phase: validating` | Status "Checking citations…" — a real step (citation validation/repair), not decorative, so it is named accurately rather than folded silently into "generating" |
| **Done, answered** | `result` with `refused: false` | Final assistant bubble, citation markers linked to source cards, latency/provider footer |
| **Done, refused** | `result` with `refused: true` | **Refusal banner**, not a normal bubble: warm-neutral background (not error-red — a refusal is not a system failure), an icon, the label "No supporting material found," the model's explanation, and the near-miss source cards labeled "closest, but not used" |
| **Error** | `error` event, or a fetch/network failure | Error banner (error-red), the server's `message` + `remediation` verbatim (PRD: "stable error codes with remediation text"), and a retry action. Never silently swallowed |
| **Artifact ready** | `artifact_saved` event | Artifact panel opens (or updates in place if already open) to the new version, defaulting to **preview** |

Refusal vs. error are deliberately different visual languages: a refusal is
the system working correctly (PRD 2.2); an error is the system failing. Using
the same red for both would teach the user to distrust honest refusals.

## 4. Source cards

Each card shows: episode title, guest (if known), timestamp (`HH:MM:SS`), an
excerpt, and a deep link to `youtube.com/watch?v=<id>&t=<seconds>` that opens
in a new tab. Two visual states, driven by the `cited` field the backend
already computes per source:

- **Cited** — solid border, a small "cited" badge, `id="source-{index}"` so
  an inline `[S#]` marker can jump straight to it.
- **Not cited / near-miss** — dashed border, muted text, no "cited" badge.
  Used both for sources the model didn't end up citing and for the near-miss
  passages shown on refusal (PRD A2: showing what *was* found is what makes
  "I don't know" a useful answer instead of a dead end).

## 5. Artifact viewer

- HTML artifacts render in a sandboxed `<iframe>` pointed at
  `GET /api/artifacts/{id}/document`, using the exact `sandbox` string the API
  returns on the artifact record (`ARTIFACT_SANDBOX`) — the frontend never
  constructs or edits that string itself. Adding `allow-same-origin` or
  `allow-scripts` here would undo the entire isolation chain documented in
  `backend/app/agent/sanitize.py`, so the sandbox attribute is read from the
  API response and rendered as-is, never hand-written in a component.
- Markdown artifacts render client-side with `react-markdown` +
  `remark-gfm`, **without** `rehype-raw`. The server already HTML-escapes any
  literal `<tag>` a model wrote into Markdown (`sanitise_markdown`), so the
  markdown source contains no live HTML by the time it reaches the browser —
  but the client does not lean on that as its only defense: not enabling
  `rehype-raw` means this renderer has no code path that turns markdown text
  into live DOM nodes at all, regardless of what the string contains. Two
  independent reasons content can't execute, not one relying on the other.
- **Source / Preview toggle** — "Preview" is the rendered iframe/Markdown;
  "Source" is the raw sanitised text in a `<pre>`, so a user can see exactly
  what will be copied or downloaded.
- **Copy** copies the current tab's content (source or rendered text) to the
  clipboard, with a visible "Copied" confirmation (not just a silent no-op).
- **Download** saves the artifact with the right extension (`.html` /
  `.md`) and its title as the filename.

## 6. Accessibility

- **Keyboard.** Every interactive element (session list rows, chat input,
  send button, source cards, artifact tabs, copy/download) is a real
  `<button>`/`<a>`/`<input>`/`<textarea>`, reachable by Tab in visual order,
  with a visible focus ring (`:focus-visible`, never suppressed). No
  `div onClick`.
- **Live regions.** The phase status line is `aria-live="polite"` and updates
  on every phase transition. Streaming answer text is **not** itself wrapped
  in a chatty `aria-live` region — announcing every token would be unusable
  with a screen reader — instead the phase region announces "Answer ready"
  once the `result` event lands, and the full text is then reachable in
  normal reading order.
- **Focus management.** Submitting a message keeps focus in the input (a
  chat composer that steals focus after every send breaks the core loop of
  "ask a follow-up"). Opening the artifact panel moves focus to its heading
  only on the *first* artifact of a session, not on every subsequent
  version update, so an in-progress conversation isn't repeatedly yanked
  sideways.
- **Color is never the only signal.** Cited vs. uncited sources differ in
  border style and an explicit text badge, not color alone. Refusal vs.
  error differ in icon and heading text, not color alone.
- **Labels.** Every icon-only control (copy, download, cancel) has an
  `aria-label`; the artifact iframe has a `title` describing its content
  ("Generated landing page: {title}").
