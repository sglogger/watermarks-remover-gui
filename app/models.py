"""Request and response shapes for this GUI's own API.

Reports coming back from the engine are passed through as opaque objects. The
frontend renders them tolerantly, so an upstream report change degrades the
display instead of breaking the app.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TextScanRequest(BaseModel):
    text: str = Field(default="", description="Text pasted into the textarea")
    format: str = Field(default="text", description="text | markdown | html")
    options: dict[str, Any] = Field(default_factory=dict)


class CleanRequest(BaseModel):
    ids: list[str] = Field(default_factory=list, description="Scan ids to clean")
    options: dict[str, Any] = Field(default_factory=dict)


class LoginRequest(BaseModel):
    token: str = ""


class ScanItem(BaseModel):
    id: str | None = None
    name: str
    ok: bool = True
    kind: str = "unknown"
    size: int = 0
    suspicious: bool = False
    highlightable: bool = False
    #: Original text, sent back only for highlightable files below the inline
    #: size limit, so the browser can render the marked-up view.
    text: str | None = None
    highlight: dict[str, Any] | None = None
    report: Any = None
    error: str | None = None


class ScanResponse(BaseModel):
    items: list[ScanItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CleanItem(BaseModel):
    id: str | None = None
    name: str
    ok: bool = True
    cleaned_name: str | None = None
    download_url: str | None = None
    text: str | None = None
    report: Any = None
    #: Result of re-inspecting the cleaned bytes. `verified` is the reliable
    #: signal; `remaining_hits` is only filled in for reports that carry a
    #: count, and `remaining_findings` names what the engine still objects to.
    remaining_hits: int | None = None
    remaining_findings: list[str] = Field(default_factory=list)
    verified: bool | None = None
    error: str | None = None


class CleanResponse(BaseModel):
    items: list[CleanItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
