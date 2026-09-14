"""Artifact generation: a checklist, one-pager or landing page from the chat.

Two decisions shape this skill.

**The format is decided before generation, not after.** A model asked to "make
something" produces Markdown wrapped in an apology, or HTML with a preamble.
Choosing `markdown` or `html` up front from explicit cues in the request means
the prompt asks for exactly one thing, and the output can be parsed with one
contract instead of sniffed.

**The skill does not touch the database.** It returns the sanitised artifact on
its `SkillResult`, and `app.services.chat` persists it after the assistant
message exists -- which is the only moment `message_id` is known. That keeps the
skill runnable in a test with no PostgreSQL, and keeps persistence in the layer
that already owns the transaction.

Grounding is deliberately *softer* here than for Q&A and essays, and the reason
is worth stating: a launch checklist is a work product derived from the
conversation, not a claim about what someone said on a podcast. Requiring a
citation on every checklist item would produce worse checklists, not more honest
ones. So when retrieval clears the confidence threshold the passages are
supplied and citations are requested; when it does not, the artifact is still
built from the conversation, and the result records `grounded: false` so the UI
can say which kind of artifact this is rather than implying evidence that is
not there.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.agent.citations import validate_and_repair
from app.agent.contracts import Intent, Phase, Skill, SkillContext, SkillEvent, SkillResult
from app.agent.prompts import build_passage_block, estimate_tokens
from app.agent.sanitize import (
    ARTIFACT_SANDBOX,
    SanitizationReport,
    build_artifact_document,
    extract_title,
    sanitise_html,
    sanitise_markdown,
)
from app.agent.sources import build_retrieval_query, sources_payload
from app.config import Settings
from app.errors import AppError, ArtifactRejected
from app.llm.base import ChatMessage
from app.llm.registry import LLMGateway
from app.retrieval.retriever import Retriever

log = logging.getLogger("app.agent.skills.artifact")

# Cues that name a *format*. Note what is deliberately absent: "checklist",
# "one-pager", "brief" and "template" name a document *type*, which is
# orthogonal to format -- a one-pager can be either. Counting them as Markdown
# votes made "make an HTML one-pager" tie and fall back to Markdown, which is
# the opposite of what was asked for. Markdown remains the default because it
# is the safer and more useful output for most requests; HTML is opted into.
_HTML_CUES = re.compile(
    r"\b(html|css|web ?page|landing page|website|styled|mock ?-?up|wireframe)\b",
    re.IGNORECASE,
)
_MARKDOWN_CUES = re.compile(r"\b(markdown|\.?md)\b", re.IGNORECASE)

_FENCE = re.compile(r"```(?:html|markdown|md)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)


def choose_kind(message: str) -> str:
    """`html` only when a format cue asks for it; `markdown` otherwise.

    An explicit "markdown" always wins, even alongside an HTML cue ("a markdown
    version of that landing page"): rendering untrusted HTML is the higher-risk
    path, so anything ambiguous must not select it.
    """
    text = message or ""
    if _MARKDOWN_CUES.search(text):
        return "markdown"
    return "html" if _HTML_CUES.search(text) else "markdown"


MARKDOWN_SYSTEM = """You produce a single, finished Markdown work product for a \
product or growth team -- a checklist, a one-pager, a brief, a template.

Rules:
- Output ONLY the Markdown. No preamble, no "here is your document", no code \
fences around the whole thing.
- Start with a single `# ` title.
- Make it usable: concrete steps, real numbers, checkboxes (`- [ ] `) for \
checklists, short sections with `## ` headings.
- No filler sections ("Introduction", "Conclusion"), no lorem ipsum, no \
placeholders like [Your Company] unless the user asked for a template.
- Do not embed raw HTML."""

HTML_SYSTEM = """You produce a single, finished HTML page for a product or \
growth team -- a one-pager, a landing page, a printable brief.

Rules:
- Output ONLY the HTML. No preamble, no explanation, no code fences.
- Put all styling in one `<style>` element in the head. Do not link to external \
stylesheets, fonts, scripts or images.
- Use semantic structure: headings, sections, lists, tables.
- No JavaScript of any kind, no event handler attributes, no forms, no iframes. \
They are stripped before the page is stored, so including them only loses you \
the markup that would have been there.
- Use system fonts and CSS you write yourself. Make it look deliberate: real \
spacing, a restrained palette, readable line length."""

_GROUNDED_NOTE = """
The passages below are from Lenny's Podcast transcripts. Where a point comes \
from one of them, cite it inline with a marker like [S1]. Do not invent marker \
numbers, and do not cite anything you were not given."""


@dataclass(slots=True)
class GeneratedArtifact:
    """The sanitised artifact, ready to persist and serve."""

    kind: str
    title: str
    content: str
    raw_content: str
    sanitization: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """The client-facing shape. `raw_content` is deliberately absent."""
        return {
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "sanitization": self.sanitization,
            "sandbox": ARTIFACT_SANDBOX if self.kind == "html" else None,
        }

    def as_record(self) -> dict[str, Any]:
        """The persistence shape, including the raw output kept for debugging."""
        return {**self.as_dict(), "raw_content": self.raw_content}


class ArtifactSkill(Skill):
    intent = Intent.ARTIFACT
    name = "Artifact generator"
    description = (
        "Produces a Markdown document or a sanitised HTML/CSS page -- a checklist, "
        "one-pager or landing page -- from the conversation, rendered in an "
        "isolated sandboxed viewer."
    )
    examples = (
        "Create an HTML landing page for this.",
        "Give me a launch checklist.",
        "Turn this into a one-pager I can share.",
    )

    def __init__(self, settings: Settings, gateway: LLMGateway, retriever: Retriever) -> None:
        self._settings = settings
        self._gateway = gateway
        self._retriever = retriever

    async def run(self, ctx: SkillContext) -> AsyncIterator[SkillEvent]:
        started = time.perf_counter()
        kind = choose_kind(ctx.message)

        yield SkillEvent(kind="phase", phase=Phase.RETRIEVING)
        query = build_retrieval_query(ctx.message, ctx.history, drop_format_terms=True)
        retrieval = await self._retriever.retrieve(query)
        grounded = (
            not retrieval.is_empty
            and retrieval.confidence >= self._settings.retrieval_min_confidence
        )

        block, used = ("", 0)
        sources: list[dict[str, Any]] = []
        if grounded:
            block, used = build_passage_block(retrieval.chunks, self._passage_budget())
            sources = sources_payload(retrieval.chunks[:used], retrieval)
            yield SkillEvent(
                kind="sources",
                data={
                    "sources": sources,
                    "retrieval": {
                        "method": retrieval.method,
                        "confidence": round(retrieval.confidence, 4),
                        "query": retrieval.query,
                        "degraded_reason": retrieval.degraded_reason,
                    },
                },
            )

        yield SkillEvent(kind="phase", phase=Phase.GENERATING)
        raw, usage = await self._generate(ctx, kind, block, used)
        if not raw.strip():
            yield self._failed(
                "The model returned an empty artifact.",
                kind,
                started,
                sources,
            )
            return

        yield SkillEvent(kind="phase", phase=Phase.VALIDATING)
        try:
            artifact, cited = self._finalise(raw, kind, used)
        except ArtifactRejected as exc:
            # Rejection is a *successful* security outcome, not a crash: the user
            # is told plainly, and the raw output never reaches a browser.
            log.error("artifact_rejected", extra={"kind": kind, "details": exc.details})
            yield self._failed(
                "The generated page did not pass safety checks, so it was discarded. "
                "Ask again, or ask for Markdown instead.",
                kind,
                started,
                sources,
                reason="failed_sanitisation",
            )
            return

        for source in sources:
            source["cited"] = source["index"] in cited

        summary = (
            f"Created a {kind} artifact: **{artifact.title}**. "
            f"It opens in the artifact panel."
        )
        yield SkillEvent(kind="delta", text=summary)
        yield SkillEvent(kind="artifact", data=artifact.as_dict())
        yield SkillEvent(
            kind="result",
            result=SkillResult(
                content=summary,
                intent=self.intent,
                refused=False,
                sources=sources,
                grounding={
                    "grounded": grounded and bool(cited),
                    "cited_indices": cited,
                    "source_count": used,
                    "retrieval_confidence": round(retrieval.confidence, 4),
                    "retrieval_method": retrieval.method,
                    "reason": None if grounded else "conversation_only",
                },
                artifact=artifact.as_record(),
                provider=self._gateway.last_provider.name,
                model=self._gateway.last_provider.model,
                usage=usage,
                latency_ms=(time.perf_counter() - started) * 1000,
            ),
        )

    # ------------------------------------------------------------ generation --
    async def _generate(
        self, ctx: SkillContext, kind: str, block: str, used: int
    ) -> tuple[str, dict[str, int]]:
        system = HTML_SYSTEM if kind == "html" else MARKDOWN_SYSTEM
        if used:
            system = f"{system}\n{_GROUNDED_NOTE}"

        context = ""
        if ctx.history:
            # The conversation is the artifact's brief. Only the last few turns:
            # a checklist should reflect what was just discussed, not the whole
            # session, and an over-long brief crowds out the passages.
            recent = ctx.history[-4:]
            context = "Conversation so far:\n" + "\n\n".join(
                f"{m.role}: {m.content[:1200]}" for m in recent
            )
        passages = f"Transcript passages:\n\n{block}\n\n" if used else ""
        cite = f" Cite using [S1]-[S{used}] where a point comes from a passage." if used else ""

        try:
            completion = await self._gateway.complete(
                [
                    ChatMessage(role="system", content=system),
                    ChatMessage(
                        role="user",
                        content=(
                            f"{passages}{context}\n\n---\nRequest: {ctx.message}\n\n"
                            f"Produce the {kind} artifact now.{cite}"
                        ).strip(),
                    ),
                ],
                temperature=0.5,
            )
        except AppError as exc:
            log.warning("artifact_generation_failed", extra={"error_type": type(exc).__name__})
            return "", {}
        return completion.text, completion.usage.as_dict()

    def _finalise(self, raw: str, kind: str, used: int) -> tuple[GeneratedArtifact, list[int]]:
        """Unwrap, validate citations, sanitise, and package."""
        body = _unwrap(raw)
        cleaned, citation_report = validate_and_repair(body, used)
        cited = citation_report.cited_indices

        if kind == "html":
            title = extract_title(cleaned, fallback="Artifact")
            html_body, css, report = sanitise_html(cleaned)
            content = build_artifact_document(title, html_body, css)
        else:
            content, report = sanitise_markdown(cleaned)
            title = _markdown_title(content)

        if report.modified:
            log.info(
                "artifact_sanitised",
                extra={
                    "kind": kind,
                    "removed_tags": report.removed_tags[:10],
                    "removed_css": report.removed_css[:10],
                    "blocked_urls": len(report.blocked_urls),
                },
            )
        return (
            GeneratedArtifact(
                kind=kind,
                title=title,
                content=content,
                # Retained for debugging only. `content` is the sole field ever
                # served to a viewer -- see app.api.artifacts.
                raw_content=raw,
                sanitization=report.as_dict(),
            ),
            cited,
        )

    def _failed(
        self,
        message: str,
        kind: str,
        started: float,
        sources: list[dict[str, Any]],
        *,
        reason: str = "generation_failed",
    ) -> SkillEvent:
        return SkillEvent(
            kind="result",
            result=SkillResult(
                content=message,
                intent=self.intent,
                refused=True,
                sources=sources,
                grounding={"grounded": False, "reason": reason, "kind": kind},
                provider=self._gateway.last_provider.name,
                model=self._gateway.last_provider.model,
                latency_ms=(time.perf_counter() - started) * 1000,
            ),
        )

    def _passage_budget(self) -> int:
        context = self._gateway.primary.context_tokens
        return max(
            512,
            context
            - self._settings.llm_max_output_tokens
            - estimate_tokens(HTML_SYSTEM)
            - 1024,
        )


# Prose that can surround a fenced artifact without the fence being a code
# sample: "Here you go!", "Let me know if you want changes." Anything longer, or
# anything containing a heading, is a document in its own right.
_MAX_WRAPPER_CHARS = 200


def _unwrap(raw: str) -> str:
    """Take the fenced block when the model wrapped its output, else the text.

    The discriminator is what lies *outside* the fence, not the fence's share of
    the characters: a one-line artifact inside a fence is still the artifact,
    while a Markdown guide that happens to contain a `bash` sample must not be
    reduced to that sample. So a fence is unwrapped only when everything around
    it is a short, heading-free pleasantry.
    """
    text = (raw or "").strip()
    matches = list(_FENCE.finditer(text))
    if len(matches) != 1:
        return text
    outside = (text[: matches[0].start()] + text[matches[0].end() :]).strip()
    if len(outside) > _MAX_WRAPPER_CHARS or re.search(r"^#{1,6}\s", outside, re.MULTILINE):
        return text
    return matches[0].group(1).strip()


_MD_TITLE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _markdown_title(content: str) -> str:
    if match := _MD_TITLE.search(content or ""):
        return " ".join(match.group(1).split()).strip("*_# ")[:200]
    return "Artifact"


def report_from_dict(payload: dict[str, Any]) -> SanitizationReport:
    """Rebuild a report from its persisted JSON, for the API layer."""
    return SanitizationReport(
        kind=payload.get("kind", "html"),
        engine=payload.get("engine", "nh3"),
        removed_tags=list(payload.get("removed_tags", [])),
        removed_attributes=list(payload.get("removed_attributes", [])),
        removed_css=list(payload.get("removed_css", [])),
        blocked_urls=list(payload.get("blocked_urls", [])),
        verified=bool(payload.get("verified", False)),
    )
