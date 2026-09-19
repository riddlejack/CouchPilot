"""Provider UI state-machine contracts for search_ready navigation."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from home_media.models import PowerState


class ProviderState(StrEnum):
    UNKNOWN = "unknown"
    APPLE_HOME = "apple_home"
    PROFILE_PICKER = "profile_picker"
    HOME = "home"
    SEARCH_NAV = "search_nav"
    SEARCH_KEYBOARD = "search_keyboard"
    SEARCH_RESULTS = "search_results"
    TITLE_DETAIL = "title_detail"
    PLAYING = "playing"
    ERROR_OR_MODAL = "error_or_modal"
    BLANK_OR_PROTECTED = "blank_or_protected"


class ProviderObservation(BaseModel):
    current_app: str | None = None
    power: PowerState = PowerState.UNKNOWN
    keyboard_focus: bool | None = None
    keyboard_text: str | None = None
    now_playing_title: str | None = None
    screenshot_state: str | None = None
    user_confirmed_state: ProviderState | None = None
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)
    screenshot_anchors: list[str] = Field(default_factory=list)
    highlighted_profile_name: str | None = None
    classifier_name: str | None = None


class TransitionSpec(BaseModel):
    """One guarded UI transition with explicit allowed source states."""

    name: str
    allowed_from: list[ProviderState]
    actions: list[dict[str, Any]] = Field(default_factory=list)
    expected_to: ProviderState
    accepted_post_states: list[ProviderState] | None = None
    timeout_s: float = 5.0
    may_start_playback: bool = False
    requires_observation: bool = True

    def resolved_accepted_post_states(self) -> list[ProviderState]:
        if self.accepted_post_states:
            return list(self.accepted_post_states)
        return [self.expected_to]


@runtime_checkable
class ProviderAdapter(Protocol):
    provider_id: str
    app_bundle_id: str

    def classify(self, obs: ProviderObservation) -> ProviderState: ...

    def transitions(self) -> list[TransitionSpec]: ...

    def plan_search_ready(self, title: str, profile_index: int) -> list[TransitionSpec]: ...
