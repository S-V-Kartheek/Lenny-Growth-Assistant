"""Wire models for artifacts.

`raw_content` has no field here, and that is the point: the unsanitised model
output cannot be returned by accident because there is nowhere on the contract
for it to go. See `app.services.artifacts` for the same rule at the row level.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ArtifactRecord(BaseModel):
    id: str
    session_id: str
    message_id: str | None = None
    kind: str = Field(description="markdown | html")
    title: str
    content: str = Field(description="Sanitised content. The only content ever served.")
    version: int = 1
    sanitization: dict[str, Any] = Field(
        default_factory=dict,
        description="What sanitisation removed, and the isolation policy applied.",
    )
    sandbox: str | None = Field(
        default=None,
        description=(
            "The exact `sandbox` attribute an embedding iframe must use. Never "
            "contains allow-same-origin or allow-scripts."
        ),
    )
    document_url: str | None = Field(
        default=None, description="Where to point the sandboxed iframe (HTML artifacts only)."
    )
    created_at: datetime


class ArtifactListResponse(BaseModel):
    artifacts: list[ArtifactRecord]
