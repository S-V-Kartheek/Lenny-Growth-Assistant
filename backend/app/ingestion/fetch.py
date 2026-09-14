"""Acquire the transcript corpus from a pinned upstream revision.

Why a pinned commit rather than `git clone main`
------------------------------------------------
Citations must stay meaningful over time. If the corpus silently moved, an
answer's quoted text could no longer exist at the cited source. Ingestion
therefore downloads one immutable tarball (`CORPUS_COMMIT`) and records that SHA
on every episode row, so any citation can be traced back to exact bytes.

Refreshing the corpus is an explicit, reviewable act: bump CORPUS_COMMIT and
re-run ingestion. Episodes whose content hash is unchanged are skipped.
"""

from __future__ import annotations

import io
import logging
import tarfile
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger("app.ingestion.fetch")

TARBALL_URL = "https://codeload.github.com/{repo}/tar.gz/{commit}"
DOWNLOAD_TIMEOUT = httpx.Timeout(120.0, connect=15.0)


@dataclass(slots=True)
class RawTranscript:
    slug: str
    source_path: str
    text: str


class CorpusUnavailable(RuntimeError):
    """The corpus could not be downloaded and no cached copy exists."""


def corpus_dir(cache_dir: str | Path, commit: str) -> Path:
    return Path(cache_dir) / commit[:12]


def ensure_corpus(
    *, repo: str, commit: str, cache_dir: str | Path, force: bool = False
) -> Path:
    """Return a local directory holding the episodes/ tree for `commit`."""
    target = corpus_dir(cache_dir, commit)
    marker = target / ".complete"
    if marker.exists() and not force:
        log.info("corpus_cache_hit", extra={"commit": commit, "path": str(target)})
        return target

    url = TARBALL_URL.format(repo=repo, commit=commit)
    log.info("corpus_download_start", extra={"repo": repo, "commit": commit})
    try:
        with httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            payload = response.content
    except httpx.HTTPError as exc:
        if marker.exists():
            log.warning("corpus_download_failed_using_cache", extra={"commit": commit})
            return target
        raise CorpusUnavailable(
            f"Could not download {repo}@{commit[:12]}: {exc}. "
            "Check network access, or point CORPUS_CACHE_DIR at a pre-downloaded copy."
        ) from exc

    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            # Only transcripts are needed, and path traversal is refused.
            parts = Path(member.name).parts[1:]  # strip "<repo>-<sha>/"
            if len(parts) != 3 or parts[0] != "episodes" or parts[2] != "transcript.md":
                continue
            if any(p in ("..", "") for p in parts):
                continue
            destination = target.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            destination.write_bytes(extracted.read())

    marker.write_text(commit, encoding="utf-8")
    count = len(list(target.glob("episodes/*/transcript.md")))
    log.info("corpus_download_complete", extra={"commit": commit, "episodes": count})
    return target


def iter_transcripts(root: Path) -> list[RawTranscript]:
    out: list[RawTranscript] = []
    for path in sorted(root.glob("episodes/*/transcript.md")):
        out.append(
            RawTranscript(
                slug=path.parent.name,
                source_path=f"episodes/{path.parent.name}/transcript.md",
                text=path.read_text(encoding="utf-8", errors="replace"),
            )
        )
    return out
