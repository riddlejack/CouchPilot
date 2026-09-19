"""Netflix provider adapter — search_ready state machine only.

Profile selection requires a visually confirmed ``profile_picker`` with the
configured profile name highlighted. ``profile_index`` remains audit metadata
only — this slice never emits Up/Down profile counting.

``search_ready`` stops at search results. It never selects a result row and
sets ``may_start_playback=False`` on every transition.
"""

from __future__ import annotations

from typing import Any

from home_media.errors import SafetyBlockedError
from home_media.providers.base import (
    ProviderObservation,
    ProviderState,
    TransitionSpec,
)

NETFLIX_BUNDLE_ID = "com.netflix.Netflix"

# Continuable post-launch screens for search_ready. Playing / title_detail /
# blank / error / unknown are stop/handoff outcomes — never a reason to Select.
LAUNCH_ACCEPTED_POST_STATES = [
    ProviderState.PROFILE_PICKER,
    ProviderState.HOME,
    ProviderState.SEARCH_NAV,
    ProviderState.SEARCH_KEYBOARD,
    ProviderState.SEARCH_RESULTS,
]


def is_select_action(action: dict[str, Any]) -> bool:
    """True for select / select_profile style actions."""
    action_type = str(action.get("type", "")).lower()
    return action_type in {"select", "select_profile"}


def assert_transition_allowed(spec: TransitionSpec, current_state: ProviderState) -> None:
    """Raise SafetyBlockedError for forbidden Select / profile-select cases."""
    has_select = any(is_select_action(a) for a in spec.actions)
    if has_select and current_state == ProviderState.UNKNOWN:
        raise SafetyBlockedError(
            "Select is forbidden while provider state is UNKNOWN",
            reason="select_from_unknown",
        )

    is_profile = spec.name.startswith("select_profile") or any(
        str(a.get("type", "")).lower() == "select_profile" for a in spec.actions
    )
    if is_profile and current_state != ProviderState.PROFILE_PICKER:
        raise SafetyBlockedError(
            "Profile select is only allowed from PROFILE_PICKER",
            reason="profile_select_outside_picker",
        )

    if current_state not in spec.allowed_from:
        raise SafetyBlockedError(
            f"Transition '{spec.name}' not allowed from {current_state.value}",
            reason="transition_not_allowed_from_state",
        )


class NetflixAdapter:
    """ProviderAdapter for com.netflix.Netflix search_ready flows."""

    provider_id: str = "netflix"
    app_bundle_id: str = NETFLIX_BUNDLE_ID

    def classify(self, obs: ProviderObservation) -> ProviderState:
        # DRM / blank frames are unobservable — never invent menu state from them.
        if obs.screenshot_state == ProviderState.BLANK_OR_PROTECTED.value:
            return ProviderState.BLANK_OR_PROTECTED
        # Prefer visual classifier state when present.
        if obs.screenshot_state:
            try:
                visual_state = ProviderState(obs.screenshot_state)
                if visual_state != ProviderState.UNKNOWN:
                    return visual_state
            except ValueError:
                pass
        if obs.user_confirmed_state is not None:
            return obs.user_confirmed_state
        if obs.now_playing_title:
            return ProviderState.PLAYING
        if obs.keyboard_focus:
            if obs.keyboard_text:
                return ProviderState.SEARCH_RESULTS
            return ProviderState.SEARCH_KEYBOARD
        if obs.current_app and obs.current_app != self.app_bundle_id:
            return ProviderState.UNKNOWN
        if obs.current_app == self.app_bundle_id and obs.confidence < 0.3:
            return ProviderState.UNKNOWN
        return ProviderState.UNKNOWN

    def transitions(self) -> list[TransitionSpec]:
        return self.plan_search_ready("", profile_index=1, profile_name=None)

    def plan_search_ready(
        self,
        title: str,
        profile_index: int,
        *,
        profile_name: str | None = None,
    ) -> list[TransitionSpec]:
        if profile_index < 1:
            raise SafetyBlockedError(
                "profile_index must be 1-based (>= 1)",
                reason="invalid_profile_index",
            )

        specs: list[TransitionSpec] = [
            TransitionSpec(
                name="launch_app",
                allowed_from=[
                    ProviderState.UNKNOWN,
                    ProviderState.APPLE_HOME,
                    ProviderState.HOME,
                    ProviderState.ERROR_OR_MODAL,
                ],
                actions=[{"type": "launch_app", "app_bundle_id": self.app_bundle_id}],
                expected_to=ProviderState.HOME,
                accepted_post_states=list(LAUNCH_ACCEPTED_POST_STATES),
                timeout_s=35.0,
                may_start_playback=False,
                requires_observation=True,
            ),
            # Profile select ONLY from PROFILE_PICKER with visually matching name.
            # Index is audit metadata only — never emit Down/Up to count profiles.
            TransitionSpec(
                name=f"select_profile_{profile_index}",
                allowed_from=[ProviderState.PROFILE_PICKER],
                actions=[
                    {
                        "type": "select_profile",
                        "profile_index": profile_index,
                        "profile_name": profile_name,
                        "press": "select",
                    }
                ],
                expected_to=ProviderState.HOME,
                accepted_post_states=[
                    ProviderState.HOME,
                    ProviderState.SEARCH_NAV,
                    ProviderState.SEARCH_KEYBOARD,
                ],
                timeout_s=25.0,
                may_start_playback=False,
                requires_observation=True,
            ),
            TransitionSpec(
                name="home_to_search_keyboard",
                allowed_from=[ProviderState.HOME],
                actions=[
                    {"type": "press_key", "key": "up"},
                    {"type": "press_key", "key": "left"},
                    {"type": "press_key", "key": "down"},
                ],
                expected_to=ProviderState.SEARCH_KEYBOARD,
                accepted_post_states=[
                    ProviderState.SEARCH_KEYBOARD,
                    ProviderState.SEARCH_RESULTS,
                    ProviderState.SEARCH_NAV,
                ],
                timeout_s=25.0,
                may_start_playback=False,
                requires_observation=True,
            ),
            TransitionSpec(
                name="keyboard_set_query",
                allowed_from=[ProviderState.SEARCH_KEYBOARD, ProviderState.SEARCH_RESULTS],
                actions=[{"type": "keyboard_set", "text": title}],
                expected_to=ProviderState.SEARCH_KEYBOARD,
                accepted_post_states=[
                    ProviderState.SEARCH_KEYBOARD,
                    ProviderState.SEARCH_RESULTS,
                ],
                timeout_s=25.0,
                may_start_playback=False,
                requires_observation=True,
            ),
            TransitionSpec(
                name="keyboard_readback",
                allowed_from=[ProviderState.SEARCH_KEYBOARD, ProviderState.SEARCH_RESULTS],
                actions=[{"type": "keyboard_readback", "expected_text": title}],
                expected_to=ProviderState.SEARCH_RESULTS,
                accepted_post_states=[
                    ProviderState.SEARCH_RESULTS,
                    ProviderState.SEARCH_KEYBOARD,
                ],
                timeout_s=15.0,
                may_start_playback=False,
                requires_observation=True,
            ),
        ]
        # Explicitly never select a search result for search_ready.
        return specs
