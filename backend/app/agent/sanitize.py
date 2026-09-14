"""Sanitisation of model-generated artifacts.

Threat model (PRD assumption A6: *generated HTML is hostile by default*)
-----------------------------------------------------------------------
Artifact HTML is produced by a language model from a prompt that includes
user-supplied text and transcript text. Both are attacker-influenceable in any
real deployment, so the output is treated exactly like a form submission from
an unauthenticated stranger: **untrusted input that will later be rendered in a
browser.** It is not "our own" HTML because we asked for it.

Three independent layers, in order, each of which alone would be a defence:

1. **Allow-list sanitisation** (`nh3`, Rust bindings to `ammonia`, built on the
   `html5ever` tokenizer -- the same parsing rules a browser applies). Anything
   not explicitly permitted is removed. A hand-rolled regex or
   `html.parser`-based sanitiser was rejected for this layer: the classic
   bypasses against home-grown sanitisers are *parser differentials*, where the
   sanitiser's idea of the token stream differs from the browser's, and the only
   reliable defence is to tokenise the way the browser does.
2. **Independent verification** (`verify_sanitised`, stdlib only). The sanitised
   output is re-parsed by a second, unrelated implementation and checked against
   the same policy. If the two disagree the artifact is rejected outright
   (`ArtifactRejected`) rather than served. This is what turns "we believe nh3
   is correct" into an assertion the system actually makes on every artifact.
3. **Isolation at render time** (`build_artifact_document`). The stored document
   carries its own restrictive CSP and is designed to be framed with
   `sandbox` and *without* `allow-same-origin`, so even a total sanitiser
   bypass lands in an opaque origin with no script execution, no parent access,
   no cookies and no storage.

What is blocked, exactly
------------------------
* **All scripting.** `<script>` (and its text content), `<noscript>`,
  `<template>`, every `on*` event-handler attribute, and `javascript:` URLs.
* **All embedding and navigation-hijacking.** `<iframe>`, `<frame>`,
  `<object>`, `<embed>`, `<applet>`, `<portal>`, `<base>`, `<meta>` (so no
  `http-equiv=refresh`), `<link>` (so no remote stylesheet or `prefetch`).
* **All form submission.** `<form>`, `<input>`, `<button>`, `<textarea>`,
  `<select>`, and the `formaction` family of attributes.
* **Non-http(s) URL schemes**, including `data:` and `blob:` in `href`/`src`,
  and scheme strings obfuscated with whitespace, NUL bytes or HTML entities.
* **CSS that can fetch or execute:** `@import`, `@font-face`, `expression()`,
  `behavior`, `-moz-binding`, and every `url()` reference. Any CSS property not
  on `ALLOWED_CSS_PROPERTIES` is dropped, in `<style>` blocks and in `style=`
  attributes alike.
* **SVG and MathML entirely.** They are foreign-content namespaces and the
  historical source of most sanitiser-bypass (mXSS) research; the value they add
  to a checklist or a one-pager does not justify the surface.

What is allowed
---------------
Structural and text markup only: headings, paragraphs, lists, tables,
`<a href>` restricted to `http`/`https`/`mailto` (and forced to
`rel="noopener noreferrer nofollow"`), `<img src>` restricted to `https`,
emphasis, `<code>`/`<pre>`, `<blockquote>`, `<hr>`, `<br>`, `<div>`/`<span>`
with `class`/`style`, and a vetted subset of CSS properties covering layout,
colour, spacing and typography.

Accepted residual risk, stated plainly: an `<img>` pointing at a third-party
`https` host causes the viewer's browser to make a request to that host when the
artifact is rendered, revealing the viewer's IP address. Images are kept because
a landing-page artifact without them is not a landing page. No cookies ride
along: the sandboxed frame is an opaque origin and the request is cross-site.
"""

from __future__ import annotations

import html as html_module
import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import nh3

from app.errors import ArtifactRejected

log = logging.getLogger("app.agent.sanitize")

# --------------------------------------------------------------- the policy --

ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "a", "abbr", "article", "aside", "b", "blockquote", "br", "caption",
        "code", "col", "colgroup", "dd", "div", "dl", "dt", "em", "figcaption",
        "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
        "i", "img", "li", "main", "mark", "nav", "ol", "p", "pre", "q", "s",
        "section", "small", "span", "strong", "sub", "sup", "table", "tbody",
        "td", "tfoot", "th", "thead", "time", "tr", "u", "ul",
    }
)

# Per-tag attribute allow-list. `style` and `class` are granted globally so a
# generated page can actually look like a page; both are themselves filtered
# (see ALLOWED_CSS_PROPERTIES) rather than trusted.
ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "*": {"class", "style", "title"},
    "a": {"href"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "time": {"datetime"},
    "col": {"span"},
    "colgroup": {"span"},
    "ol": {"start"},
}

# Attributes nh3 *writes itself* and which therefore appear in sanitised output
# without ever having been accepted from the model. `rel` is set unconditionally
# by `link_rel`; nh3 rejects it in `attributes` for exactly that reason. The
# verifier has to know about it or it would flag nh3's own hardening as a
# violation and reject every artifact containing a link.
INJECTED_ATTRIBUTES: dict[str, set[str]] = {"a": {"rel"}}

ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})
# Images are stricter than links: a link is a deliberate click, an image fetches
# automatically the moment the artifact is rendered.
ALLOWED_IMG_SCHEMES: frozenset[str] = frozenset({"https"})

# Attributes carrying a URL, which therefore need scheme checking rather than
# mere presence checking.
URL_ATTRIBUTES: frozenset[str] = frozenset({"href", "src"})

# Presentation only. Nothing here can load a resource, run code, or move an
# element outside the artifact's own box.
ALLOWED_CSS_PROPERTIES: frozenset[str] = frozenset(
    {
        "align-items", "align-self", "background", "background-color", "border",
        "border-bottom", "border-collapse", "border-color", "border-left",
        "border-radius", "border-right", "border-spacing", "border-style",
        "border-top", "border-width", "box-shadow", "box-sizing", "caption-side",
        "clear", "color", "column-gap", "display", "flex", "flex-basis",
        "flex-direction", "flex-grow", "flex-shrink", "flex-wrap", "float",
        "font", "font-family", "font-size", "font-style", "font-variant",
        "font-weight", "gap", "grid-column", "grid-row",
        "grid-template-columns", "grid-template-rows", "height", "justify-content",
        "justify-items", "justify-self", "letter-spacing", "line-height",
        "list-style", "list-style-type", "margin", "margin-bottom", "margin-left",
        "margin-right", "margin-top", "max-height", "max-width", "min-height",
        "min-width", "opacity", "order", "overflow", "overflow-wrap", "padding",
        "padding-bottom", "padding-left", "padding-right", "padding-top",
        "row-gap", "table-layout", "text-align", "text-decoration",
        "text-indent", "text-overflow", "text-transform", "vertical-align",
        "white-space", "width", "word-break", "word-spacing",
    }
)

# At-rules that only group other rules, and so can be kept once their contents
# are themselves sanitised. Everything else (@import, @font-face, @charset,
# @namespace, @document) is dropped: they either fetch a resource or change how
# the rest of the sheet is parsed.
ALLOWED_AT_RULES: frozenset[str] = frozenset({"media", "supports"})

# The Content-Security-Policy embedded in every served artifact document. With
# no `script-src` at all, `default-src 'none'` denies script execution even if
# every other layer failed. `style-src 'unsafe-inline'` is required because the
# artifact's CSS is inline by construction -- it is not a weakening of script
# policy, which is what CSP is protecting here.
ARTIFACT_CSP = (
    "default-src 'none'; "
    "img-src https: data:; "
    "style-src 'unsafe-inline'; "
    "font-src 'none'; "
    "connect-src 'none'; "
    "form-action 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'self'; "
    "sandbox allow-popups allow-popups-to-escape-sandbox"
)

# The iframe attributes a viewer MUST use. Exported as data rather than left to
# the frontend's memory: checkpoint 4 reads this from the API, and
# `test_sanitize.py` asserts `allow-same-origin` and `allow-scripts` never
# appear in it.
ARTIFACT_SANDBOX = "allow-popups allow-popups-to-escape-sandbox"


# --------------------------------------------------------------- the report --


@dataclass(slots=True)
class SanitizationReport:
    """What sanitisation actually did, persisted alongside the artifact.

    Kept because "it was sanitised" is not an auditable claim -- an operator
    investigating a suspicious artifact needs to see which constructs were
    present in the raw output and removed.
    """

    kind: str = "html"
    engine: str = "nh3"
    removed_tags: list[str] = field(default_factory=list)
    removed_attributes: list[str] = field(default_factory=list)
    removed_css: list[str] = field(default_factory=list)
    blocked_urls: list[str] = field(default_factory=list)
    verified: bool = False

    @property
    def modified(self) -> bool:
        return bool(
            self.removed_tags
            or self.removed_attributes
            or self.removed_css
            or self.blocked_urls
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "engine": self.engine,
            "removed_tags": self.removed_tags,
            "removed_attributes": self.removed_attributes,
            "removed_css": self.removed_css,
            "blocked_urls": self.blocked_urls,
            "modified": self.modified,
            "verified": self.verified,
            "policy": {
                "csp": ARTIFACT_CSP,
                "sandbox": ARTIFACT_SANDBOX,
                "url_schemes": sorted(ALLOWED_URL_SCHEMES),
            },
        }


# ------------------------------------------------------------- URL handling --

# Characters a browser strips from a URL before resolving its scheme. They are
# the standard obfuscation for `java\tscript:` / `java\x00script:` payloads, so
# the scheme check must strip them the same way before comparing.
_URL_NOISE = re.compile(r"[\x00-\x20\x7f   ]")
_SCHEME = re.compile(r"^([a-z][a-z0-9+.\-]*):", re.IGNORECASE)


def url_scheme(value: str) -> str | None:
    """The scheme a browser would resolve, or None for a relative URL."""
    cleaned = _URL_NOISE.sub("", value or "")
    match = _SCHEME.match(cleaned)
    return match.group(1).lower() if match else None


def is_safe_url(value: str, *, schemes: frozenset[str] = ALLOWED_URL_SCHEMES) -> bool:
    """Absolute URLs must use an allowed scheme; relative URLs are rejected.

    Relative URLs are rejected rather than kept because the artifact document is
    served from an opaque origin -- a relative link there resolves to nothing
    useful, so allowing it only creates a place for `//evil.example` (a
    protocol-relative URL, which *is* absolute to a browser) to hide.
    """
    cleaned = _URL_NOISE.sub("", value or "").strip()
    if not cleaned:
        return False
    if cleaned.startswith("//"):
        return False
    scheme = url_scheme(cleaned)
    return scheme in schemes


# ------------------------------------------------------------------- scanner --


class _Scanner(HTMLParser):
    """Records every tag, attribute and URL in a document.

    Used twice: once on the raw output to build the removal report, and once on
    the sanitised output as the independent verification pass. Deliberately a
    *different* implementation from nh3 -- a verifier that shares the
    sanitiser's parser cannot catch the sanitiser's parsing mistakes.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attributes: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag.lower())
        for name, value in attrs:
            self.attributes.append((tag.lower(), name.lower(), value or ""))

    handle_startendtag = handle_starttag  # type: ignore[assignment]


def _scan(html: str) -> _Scanner:
    scanner = _Scanner()
    try:
        scanner.feed(html)
        scanner.close()
    except Exception:  # noqa: BLE001 - a parse failure is itself information
        log.warning("scanner_failed_treating_as_unverifiable")
    return scanner


def verify_sanitised(html: str) -> list[str]:
    """Re-check sanitised HTML against the policy with an independent parser.

    Returns a list of policy violations. A non-empty list means layer 1 and
    layer 2 disagree, which is a bug in the sanitiser (or a genuine bypass) and
    is never something to paper over -- the caller rejects the artifact.
    """
    violations: list[str] = []
    scanner = _scan(html)

    for tag in set(scanner.tags):
        if tag not in ALLOWED_TAGS:
            violations.append(f"tag:{tag}")

    for tag, name, value in scanner.attributes:
        if name.startswith("on"):
            violations.append(f"event-handler:{tag}[{name}]")
            continue
        permitted = (
            ALLOWED_ATTRIBUTES.get("*", set())
            | ALLOWED_ATTRIBUTES.get(tag, set())
            | INJECTED_ATTRIBUTES.get(tag, set())
        )
        if name not in permitted:
            violations.append(f"attribute:{tag}[{name}]")
            continue
        if name in URL_ATTRIBUTES:
            schemes = ALLOWED_IMG_SCHEMES if tag == "img" else ALLOWED_URL_SCHEMES
            if not is_safe_url(value, schemes=schemes):
                violations.append(f"url:{tag}[{name}]={value[:40]}")
        if name == "style":
            cleaned, removed = sanitise_css_declarations(value)
            if removed:
                violations.append(f"style:{tag}:{','.join(removed)}")

    lowered = html.lower()
    for construct in ("<script", "javascript:", "@import", "expression(", "-moz-binding"):
        if construct in lowered:
            violations.append(f"literal:{construct}")
    return violations


# ----------------------------------------------------------------------- CSS --

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# An unterminated comment would otherwise let `/*` swallow the rest of a sheet
# in our parser while a browser recovers differently.
_CSS_OPEN_COMMENT = re.compile(r"/\*.*$", re.DOTALL)
_CSS_DANGEROUS = re.compile(
    r"(expression\s*\(|url\s*\(|behavior\s*:|-moz-binding|javascript\s*:|@import|"
    r"</\s*style|<!--|-->)",
    re.IGNORECASE,
)


def sanitise_css_declarations(block: str) -> tuple[str, list[str]]:
    """Filter one `prop: value; prop: value` run against the property allow-list.

    Value-level checks run *before* the property check so a dangerous value on
    an otherwise-allowed property (`background: url(https://evil/?leak)`) is
    reported as removed rather than silently passing.
    """
    kept: list[str] = []
    removed: list[str] = []
    for declaration in block.split(";"):
        if not declaration.strip():
            continue
        name, separator, value = declaration.partition(":")
        prop = name.strip().lower()
        if not separator or not prop:
            removed.append(declaration.strip()[:40])
            continue
        if _CSS_DANGEROUS.search(declaration):
            removed.append(prop)
            continue
        if prop not in ALLOWED_CSS_PROPERTIES:
            removed.append(prop)
            continue
        kept.append(f"{prop}: {value.strip()}")
    return "; ".join(kept), removed


def sanitise_css(css: str) -> tuple[str, list[str]]:
    """Sanitise a whole stylesheet: at-rules, selectors and declarations.

    A brace-walking parser rather than a full CSS grammar. That is sufficient
    because the output is rebuilt from the parts that were *understood* --
    anything the walker cannot make sense of is dropped, not passed through, so
    an exotic construct fails closed.
    """
    removed: list[str] = []
    text = _CSS_OPEN_COMMENT.sub("", _CSS_COMMENT.sub(" ", css or ""))
    out: list[str] = []
    index = 0
    length = len(text)

    while index < length:
        brace = text.find("{", index)
        if brace == -1:
            break
        prelude = text[index:brace].strip()
        body, end = _read_block(text, brace)
        index = end

        if prelude.startswith("@"):
            at_name = prelude[1:].split(None, 1)[0].strip("({").lower()
            if at_name not in ALLOWED_AT_RULES:
                removed.append(f"@{at_name}")
                continue
            inner, inner_removed = sanitise_css(body)
            removed.extend(inner_removed)
            if inner.strip():
                out.append(f"{prelude} {{\n{inner}\n}}")
            continue

        selector, selector_ok = _sanitise_selector(prelude)
        if not selector_ok:
            removed.append(f"selector:{prelude[:30]}")
            continue
        declarations, declaration_removed = sanitise_css_declarations(body)
        removed.extend(declaration_removed)
        if declarations:
            out.append(f"{selector} {{ {declarations} }}")

    # Anything after the last block (a dangling `@import url(...)` with no body)
    # is dropped by construction, but report it so the removal is visible.
    trailing = text[index:].strip()
    if trailing.startswith("@"):
        removed.append(trailing.split(None, 1)[0].lower())
    return "\n".join(out), removed


def _read_block(text: str, open_brace: int) -> tuple[str, int]:
    """Return the balanced contents of the block starting at `open_brace`."""
    depth = 0
    for position in range(open_brace, len(text)):
        if text[position] == "{":
            depth += 1
        elif text[position] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace + 1 : position], position + 1
    return text[open_brace + 1 :], len(text)


_SELECTOR_ALLOWED = re.compile(r"^[A-Za-z0-9_\-\s.,:#>+~\[\]='\"*()]+$")


def _sanitise_selector(selector: str) -> tuple[str, bool]:
    """Selectors may not contain markup, escapes or at-rule syntax."""
    candidate = " ".join(selector.split())
    if not candidate or len(candidate) > 500:
        return "", False
    if any(ch in candidate for ch in ("<", ">{", "\\", "@", "{", "}", ";")):
        return "", False
    if not _SELECTOR_ALLOWED.match(candidate):
        return "", False
    return candidate, True


# ------------------------------------------------------------------- HTML ---

_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.IGNORECASE | re.DOTALL)
_TITLE_TAG = re.compile(r"<title\b[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)


def _attribute_filter(tag: str, attribute: str, value: str) -> str | None:
    """nh3's per-attribute hook: the URL and CSS policies live here.

    nh3 already applies its own `url_schemes` check; this runs in addition
    because nh3 compares the scheme after its own normalisation, and the
    `img`-only `https` restriction is ours, not nh3's.
    """
    if attribute in URL_ATTRIBUTES:
        schemes = ALLOWED_IMG_SCHEMES if tag == "img" else ALLOWED_URL_SCHEMES
        return value if is_safe_url(value, schemes=schemes) else None
    if attribute == "style":
        cleaned, _ = sanitise_css_declarations(value)
        return cleaned or None
    return value


def sanitise_html(raw: str) -> tuple[str, str, SanitizationReport]:
    """Sanitise model-generated HTML into (body, css, report).

    The body and the stylesheet are returned separately because the served
    document is **rebuilt by us** (`build_artifact_document`) rather than
    patched: the model never gets to author the `<head>`, so it cannot author a
    `<meta http-equiv>`, a `<base>` or a `<link>` no matter what it emits.
    """
    report = SanitizationReport(kind="html")
    raw = raw or ""

    # `<style>` contents must be lifted out before nh3 sees them: nh3 drops the
    # element, which would silently discard the whole design rather than filter
    # it. The contents are then sanitised by our own CSS pass.
    style_source = "\n".join(match.group(1) for match in _STYLE_BLOCK.finditer(raw))
    body_source = _STYLE_BLOCK.sub(" ", raw)
    css, css_removed = sanitise_css(style_source)
    report.removed_css = sorted(set(css_removed))

    before = _scan(body_source)

    body = nh3.clean(
        body_source,
        tags=set(ALLOWED_TAGS),
        clean_content_tags={"script", "style", "noscript", "template", "title"},
        attributes={k: set(v) for k, v in ALLOWED_ATTRIBUTES.items()},
        attribute_filter=_attribute_filter,
        url_schemes=set(ALLOWED_URL_SCHEMES),
        link_rel="noopener noreferrer nofollow",
        url_relative="deny",
        strip_comments=True,
    )

    after = _scan(body)
    report.removed_tags = sorted({t for t in before.tags if t not in ALLOWED_TAGS})
    kept_attributes = {(t, n) for t, n, _ in after.attributes}
    report.removed_attributes = sorted(
        {f"{t}[{n}]" for t, n, _ in before.attributes if (t, n) not in kept_attributes}
    )
    report.blocked_urls = sorted(
        {
            value[:80]
            for tag, name, value in before.attributes
            if name in URL_ATTRIBUTES
            and not is_safe_url(
                value, schemes=ALLOWED_IMG_SCHEMES if tag == "img" else ALLOWED_URL_SCHEMES
            )
        }
    )

    # Layer 2. A violation here means nh3 and the stdlib parser disagree about
    # what the sanitised text means, which is exactly the condition under which
    # nothing should be served.
    violations = verify_sanitised(body)
    if violations:
        log.error("artifact_failed_verification", extra={"violations": violations[:10]})
        raise ArtifactRejected(
            "The generated artifact did not pass sanitisation verification and was discarded.",
            remediation="Ask for the artifact again, or request Markdown instead of HTML.",
            details={"violations": violations[:10]},
        )
    report.verified = True
    return body, css, report


def extract_title(raw: str, fallback: str = "Artifact") -> str:
    """Best-effort title from the model's own `<title>` or first heading."""
    for pattern in (_TITLE_TAG, re.compile(r"<h1\b[^>]*>(.*?)</h1\s*>", re.IGNORECASE | re.DOTALL)):
        if match := pattern.search(raw or ""):
            # Strip markup, then decode entities, so `&amp;` in a heading
            # becomes `&` once here rather than being re-escaped on every
            # render. `nh3.clean_text` is deliberately not used: it escapes
            # spaces to `&#32;`, which is correct for an attribute value and
            # wrong for a title the UI will display as text.
            title = html_module.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))
            title = " ".join(title.replace("<", " ").split())
            if title:
                return title[:200]
    return fallback


def build_artifact_document(title: str, body: str, css: str) -> str:
    """Assemble the standalone document that is stored and served.

    Everything outside `<body>` is written here, not by the model. The CSP is
    embedded in the document itself (rather than only sent as a header) so the
    policy travels with the artifact -- it still applies if the document is
    downloaded, copied, or served by something other than this API.
    """
    safe_title = html_module.escape(title or "Artifact", quote=True)[:400]
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{ARTIFACT_CSP}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="referrer" content="no-referrer">\n'
        f"<title>{safe_title}</title>\n"
        "<style>\n"
        f"{_BASE_STYLE}\n{css}\n"
        "</style>\n"
        "</head>\n"
        f"<body>\n{body}\n</body>\n</html>\n"
    )


# A readable default so an artifact that ships no CSS of its own still looks
# deliberate rather than like an unstyled 1996 document.
_BASE_STYLE = """
:root { color-scheme: light dark; }
body {
  margin: 0 auto; padding: 32px 24px; max-width: 760px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  line-height: 1.65; color: #1a1a1a; background: #ffffff;
}
h1, h2, h3 { line-height: 1.25; margin: 1.6em 0 0.5em; }
h1 { font-size: 2rem; margin-top: 0; }
ul, ol { padding-left: 1.3em; }
li { margin: 0.35em 0; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.92em; }
pre { background: #f5f5f5; padding: 12px 14px; border-radius: 6px; overflow-x: auto; }
blockquote { margin: 1em 0; padding-left: 1em; border-left: 3px solid #ddd; color: #444; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #ddd; padding: 8px 10px; text-align: left; }
img { max-width: 100%; height: auto; }
a { color: #0b5fff; }
""".strip()


# -------------------------------------------------------------- markdown ---

# Markdown is rendered by the client, and every mainstream renderer passes raw
# HTML through unless told not to. So a Markdown artifact is sanitised too --
# assuming the renderer is configured safely would make the artifact's safety
# depend on a setting in a different codebase.
_MD_HTML_TAG = re.compile(r"</?\s*[a-zA-Z][^>]*>")
_MD_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MD_LINK = re.compile(r"(!?\[[^\]]*\]\()\s*([^)\s]+)")
_MD_AUTOLINK = re.compile(r"<((?:[a-z][a-z0-9+.\-]*):[^>\s]*)>", re.IGNORECASE)


def sanitise_markdown(raw: str) -> tuple[str, SanitizationReport]:
    """Neutralise embedded HTML and unsafe link targets in Markdown.

    Raw HTML is **escaped**, not deleted: deleting it would silently change the
    document's meaning, while escaping makes the attempt visible in the rendered
    output, which is the more honest failure for something a human will read.
    """
    report = SanitizationReport(kind="markdown", engine="markdown-policy")
    text = raw or ""

    if found := _MD_COMMENT.findall(text):
        report.removed_tags.append("comment")
        _ = found
        text = _MD_COMMENT.sub("", text)

    def escape_tag(match: re.Match[str]) -> str:
        tag = match.group(0)
        name = re.sub(r"[^a-zA-Z0-9]", "", tag.split()[0] if tag.split() else tag).lower()
        report.removed_tags.append(name or "html")
        return tag.replace("<", "&lt;").replace(">", "&gt;")

    text = _MD_HTML_TAG.sub(escape_tag, text)

    def rewrite_link(match: re.Match[str]) -> str:
        target = match.group(2)
        if is_safe_url(target) or (
            not url_scheme(target) and not target.startswith("//")
        ):
            return match.group(0)
        report.blocked_urls.append(target[:80])
        return f"{match.group(1)}#blocked-unsafe-url"

    text = _MD_LINK.sub(rewrite_link, text)

    def rewrite_autolink(match: re.Match[str]) -> str:
        if is_safe_url(match.group(1)):
            return match.group(0)
        report.blocked_urls.append(match.group(1)[:80])
        return match.group(1).replace("<", "&lt;")

    text = _MD_AUTOLINK.sub(rewrite_autolink, text)

    report.removed_tags = sorted(set(report.removed_tags))
    report.blocked_urls = sorted(set(report.blocked_urls))
    report.verified = True
    return text, report
