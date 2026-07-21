"""Prepare-content request/result models and pure keyboard query helpers."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from home_media.content.router import ContentGoal, ContentRoute

_WS_RE = re.compile(r"\s+")


class TerminalStatus(StrEnum):
    QUERY_VERIFIED = "query_verified"
    SEARCH_OPEN_UNVERIFIED = "search_open_unverified"
    KEYBOARD_NOT_FOCUSED = "keyboard_not_focused"
    QUERY_MISMATCH = "query_mismatch"
    TITLE_DETAIL_UNVERIFIED = "title_detail_unverified"
    HANDOFF = "handoff"
    FAILED = "failed"
    UNSUPPORTED_GOAL = "unsupported_goal"


class StageStatus(StrEnum):
    PENDING = "pending"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class PrepareStage(BaseModel):
    name: str
    status: StageStatus | str
    latency_ms: int | None = None
    evidence: list[str] = Field(default_factory=list)


class PrepareContentRequest(BaseModel):
    room_key: str
    title: str
    provider: str | None = None
    goal: ContentGoal = ContentGoal.SEARCH_READY
    wake: bool = True
    dry_run: bool = False
    url: str | None = None
    profile_index: int | None = None
    idempotency_key: str | None = None
    # Notes for callers: same key + different title/provider/goal should conflict
    # at the service layer (lead wires IdempotencyConflictError).
    idempotency_notes: str | None = Field(
        default=None,
        description="Caller-facing note; service owns conflict enforcement",
    )


class PrepareContentResult(BaseModel):
    room_key: str
    title: str
    normalized_title: str
    provider: str | None = None
    goal: ContentGoal
    route_used: ContentRoute | None = None
    stages: list[PrepareStage] = Field(default_factory=list)
    observed_states: list[str] = Field(default_factory=list)
    selected_result: bool = False
    playback_started: bool = False
    verification_status: str = "unverified"
    terminal_status: TerminalStatus = TerminalStatus.FAILED
    warnings: list[str] = Field(default_factory=list)
    physical_tv_state_known: bool = False
    idempotency_key: str | None = None
    idempotency_notes: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


def normalize_query(text: str) -> str:
    """Normalize keyboard text for readback comparison (casefold, collapse whitespace)."""
    return _WS_RE.sub(" ", text.casefold().strip())


def queries_match(expected: str, observed: str | None) -> bool:
    """True when keyboard readback matches the intended query after normalization."""
    if observed is None:
        return False
    return normalize_query(expected) == normalize_query(observed)


def query_mismatch_detail(expected: str, observed: str | None) -> dict[str, str | None]:
    """Structured evidence for a query readback mismatch."""
    return {
        "expected_normalized": normalize_query(expected),
        "observed_normalized": normalize_query(observed) if observed is not None else None,
        "expected_raw": expected,
        "observed_raw": observed,
    }
