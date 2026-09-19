"""Closed-loop Netflix title-open and resume-then-pause tests."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

import pytest

from home_media.computer_use import (
    ComputerUseAction,
    ComputerUseActionResult,
    ComputerUseState,
    ComputerUseTerminal,
    DeviceContext,
    MacroStepResult,
    ScreenSemantics,
    VerifiedMacro,
    VerifiedMacroResult,
)
from home_media.content.prepare import PrepareContentResult, StageStatus, TerminalStatus
from home_media.content.router import ContentGoal, ContentRoute
from home_media.providers.base import ProviderState
from home_media.service import ApplicationService
from home_media.vision_policy import (
    FocusedTargetKind,
    ScreenSurface,
    TitleMatch,
    VisionContext,
    VisionDecision,
    VisionRemoteAction,
)


def _state(
    sequence: int,
    provider_state: ProviderState,
    *,
    playback: str = "idle",
    now_playing_title: str | None = None,
    now_playing_series: str | None = None,
    png: bytes | None = b"frame",
    blank: bool = False,
    current_app: str | None = "com.netflix.Netflix",
    keyboard_focused: bool | None = None,
    keyboard_text: str | None = None,
) -> ComputerUseState:
    return ComputerUseState(
        captured_at=datetime.now(UTC),
        room_key="living_room",
        device_id="00000000-0000-4000-8000-000000000004",
        sequence=sequence,
        frame_sha256=f"sha-{sequence}",
        blank_or_protected=blank,
        semantics=ScreenSemantics(
            state=provider_state.value,
            confidence=0.92,
            anchors=[f"test.{provider_state.value}"],
        ),
        device=DeviceContext(
            current_app=current_app,
            keyboard_focused=keyboard_focused,
            keyboard_text=keyboard_text,
            playback_state=playback,
            now_playing_title=now_playing_title,
            now_playing_series=now_playing_series,
        ),
        png_bytes=png,
    )


def _decision(
    surface: str,
    action: str,
    *,
    focused: str | None,
    title_match: str = "exact",
    focused_kind_override: str | None = None,
) -> VisionDecision:
    if surface == "search" and action == "select":
        focused_kind = "title_result"
    elif surface == "title_detail" and action == "select":
        focused_kind = "playback_cta"
    else:
        focused_kind = "other"
    focused_kind = focused_kind_override or focused_kind
    return VisionDecision.model_validate(
        {
            "surface": ScreenSurface(surface),
            "confidence": 0.95,
            "title_match": TitleMatch(title_match),
            "focused_target": focused,
            "focused_kind": FocusedTargetKind(focused_kind),
            "visible_titles": ["Avatar: The Last Airbender"] if title_match == "exact" else [],
            "safe_to_select": action == "select",
            "playback_visible": surface == "playback",
            "next_action": VisionRemoteAction(action),
            "reason_code": "test_decision",
        }
    )


class _Policy:
    def __init__(self, *decisions: VisionDecision) -> None:
        self.decisions = deque(decisions)
        self.contexts: list[VisionContext] = []

    async def decide(
        self, _frames: list[bytes], context: VisionContext
    ) -> VisionDecision:
        self.contexts.append(context)
        return self.decisions.popleft()


class _NeverPolicy:
    def __init__(self) -> None:
        self.calls = 0

    async def decide(
        self, _frames: list[bytes], _context: VisionContext
    ) -> VisionDecision:
        self.calls += 1
        raise AssertionError("vision policy must not be called")


class _Controller:
    def __init__(self, *states: ComputerUseState) -> None:
        self.states = deque(states)
        self.actions: list[ComputerUseAction] = []
        self.current: ComputerUseState | None = None

    async def observe(
        self,
        _room_key: str,
        *,
        classify: bool = True,
    ) -> ComputerUseState:
        _ = classify
        if self.current is None:
            template = self.states[0] if self.states else _state(
                2, ProviderState.SEARCH_RESULTS
            )
            # Stability observations rebind the same pixels to a fresh
            # generation; they must not consume the next action outcome.
            self.current = template.model_copy(
                update={"sequence": 2, "frame_sha256": "sha-1"}
            )
        else:
            self.current = self.current.model_copy(
                update={"sequence": self.current.sequence + 1}
            )
        return self.current

    async def act(
        self,
        room_key: str,
        action: ComputerUseAction,
        *,
        max_state_age_s: float = 8.0,
        classify_after: bool = True,
    ) -> ComputerUseActionResult:
        _ = (max_state_age_s, classify_after)
        self.actions.append(action)
        after = self.states.popleft()
        self.current = after
        return ComputerUseActionResult(
            room_key=room_key,
            action=action,
            before_sequence=action.expected_sequence,
            before_frame_sha256=f"sha-{action.expected_sequence}",
            after=after,
            frame_changed=True,
            latency_ms=1,
        )

    async def read_device_context(self, _room_key: str) -> DeviceContext:
        if self.current is None:
            return DeviceContext()
        return self.current.device


def _action_names(controller: _Controller) -> list[str | None]:
    return [action.key or action.transport_action for action in controller.actions]


def _result(goal: ContentGoal) -> PrepareContentResult:
    return PrepareContentResult(
        room_key="living_room",
        title="Avatar: The Last Airbender",
        normalized_title="avatar: the last airbender",
        provider="netflix",
        goal=goal,
        route_used=ContentRoute.PROVIDER_STATE_MACHINE,
        terminal_status=TerminalStatus.QUERY_VERIFIED,
        verification_status="verified",
    )


def _failed_macro_result(
    macro: VerifiedMacro,
    final_state: ComputerUseState,
    *,
    actions_sent: list[str],
    reason: str = "unexpected_post_state",
) -> VerifiedMacroResult:
    return VerifiedMacroResult(
        room_key="living_room",
        provider=macro.provider,
        macro=macro.name,
        terminal=ComputerUseTerminal.VISION_REQUIRED,
        final_state=final_state,
        steps=[
            MacroStepResult(
                name=macro.steps[0].name,
                before_sequence=max(1, final_state.sequence - 1),
                after_sequence=final_state.sequence,
                terminal=ComputerUseTerminal.VISION_REQUIRED,
                reason=reason,
                actions_sent=actions_sent,
                before_state=(
                    macro.steps[0].allowed_from_states[0]
                    if macro.steps[0].allowed_from_states
                    else None
                ),
                after_state=final_state.semantics.state,
            )
        ],
        fallback_reason=reason,
    )


@pytest.mark.asyncio
async def test_resume_selects_exact_title_starts_playback_and_verifies_pause() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _Policy(
        _decision("search", "select", focused="Avatar: The Last Airbender"),
        _decision("title_detail", "select", focused="Resume"),
    )
    controller = _Controller(
        _state(2, ProviderState.TITLE_DETAIL),
        _state(3, ProviderState.PLAYING, playback="playing", png=None, blank=True),
        _state(4, ProviderState.PLAYING, playback="paused", png=None, blank=True),
    )
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        result = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(1, ProviderState.SEARCH_RESULTS),
            result=_result(ContentGoal.RESUME),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert _action_names(controller) == [
        "select",
        "select",
        "pause",
    ]
    assert controller.actions[0].model_observed_target == "Avatar: The Last Airbender"
    assert result.terminal_status == TerminalStatus.PLAYBACK_PAUSED_VERIFIED
    assert result.selected_result is True
    assert result.playback_started is True
    assert result.verification_status == "verified"


@pytest.mark.asyncio
async def test_resume_receipt_completes_after_final_model_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decision budget must not strand a successful playback CTA."""
    monkeypatch.setenv("HOME_MEDIA_VISION_MAX_STEPS", "1")
    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _Policy(
        _decision("title_detail", "select", focused="Resume S1: Ep. 1"),
    )
    controller = _Controller(
        _state(2, ProviderState.PLAYING, playback="playing", png=None, blank=True),
        _state(3, ProviderState.PLAYING, playback="paused", png=None, blank=True),
    )
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        result = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(1, ProviderState.TITLE_DETAIL),
            result=_result(ContentGoal.RESUME),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert _action_names(controller) == ["select", "pause"]
    assert result.terminal_status == TerminalStatus.PLAYBACK_PAUSED_VERIFIED
    assert result.selected_result is True
    assert result.playback_started is True
    assert result.verification_status == "verified"


@pytest.mark.asyncio
async def test_title_open_stops_on_exact_verified_detail_without_playback() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _Policy(
        _decision("search", "select", focused="Avatar: The Last Airbender"),
        _decision("title_detail", "none", focused="Avatar: The Last Airbender"),
    )
    controller = _Controller(_state(2, ProviderState.TITLE_DETAIL))
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        result = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.TITLE_OPEN,
            state=_state(1, ProviderState.SEARCH_RESULTS),
            result=_result(ContentGoal.TITLE_OPEN),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert _action_names(controller) == ["select"]
    assert result.terminal_status == TerminalStatus.TITLE_OPEN_VERIFIED
    assert result.selected_result is True
    assert result.playback_started is False


@pytest.mark.asyncio
async def test_resume_never_claims_success_when_pause_metadata_is_unverified() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    controller = _Controller(
        _state(2, ProviderState.PLAYING, playback="unknown", png=None, blank=True),
        _state(3, ProviderState.PLAYING, playback="unknown", png=None, blank=True),
    )
    service.computer_use = controller  # type: ignore[assignment]
    result = _result(ContentGoal.RESUME)
    result.playback_started = True
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(
                1,
                ProviderState.PLAYING,
                playback="playing",
                now_playing_title="Avatar: The Last Airbender",
                png=None,
                blank=True,
            ),
            result=result,
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert _action_names(controller) == ["pause", "pause"]
    assert outcome.terminal_status == TerminalStatus.HANDOFF
    assert outcome.verification_status == "unverified"


@pytest.mark.asyncio
async def test_resume_does_not_pause_unrelated_existing_playback() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    controller = _Controller()
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(
                1,
                ProviderState.PLAYING,
                playback="playing",
                png=None,
                blank=True,
            ),
            result=_result(ContentGoal.RESUME),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert controller.actions == []
    assert outcome.terminal_status == TerminalStatus.HANDOFF
    assert outcome.playback_started is False


@pytest.mark.asyncio
async def test_resume_does_not_treat_title_page_autoplay_preview_as_resume() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _Policy(_decision("title_detail", "right", focused="Episodes"))
    controller = _Controller(
        _state(2, ProviderState.PLAYING, playback="playing", png=None, blank=True)
    )
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(1, ProviderState.TITLE_DETAIL),
            result=_result(ContentGoal.RESUME),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert _action_names(controller) == ["right"]
    assert outcome.terminal_status == TerminalStatus.HANDOFF
    assert outcome.playback_started is False


@pytest.mark.asyncio
async def test_resume_rejects_unrelated_title_detail_select_even_from_fake_policy() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _Policy(
        _decision(
            "title_detail",
            "select",
            focused="More Like This",
            focused_kind_override="other",
        )
    )
    controller = _Controller()
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(1, ProviderState.TITLE_DETAIL),
            result=_result(ContentGoal.RESUME),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert controller.actions == []
    assert outcome.terminal_status == TerminalStatus.HANDOFF


@pytest.mark.asyncio
async def test_search_result_preview_is_not_claimed_as_resumed_playback() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _Policy(
        _decision("search", "select", focused="Avatar: The Last Airbender")
    )
    controller = _Controller(
        _state(
            2,
            ProviderState.TITLE_DETAIL,
            playback="playing",
            now_playing_title="Official Preview",
        )
    )
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(1, ProviderState.SEARCH_RESULTS),
            result=_result(ContentGoal.RESUME),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert _action_names(controller) == ["select"]
    assert outcome.terminal_status == TerminalStatus.HANDOFF
    assert outcome.playback_started is False


@pytest.mark.asyncio
async def test_search_result_blank_preview_is_not_claimed_as_resumed_playback() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _Policy(
        _decision("search", "select", focused="Avatar: The Last Airbender")
    )
    controller = _Controller(
        _state(
            2,
            ProviderState.BLANK_OR_PROTECTED,
            playback="playing",
            now_playing_title="Official Preview",
            png=None,
            blank=True,
        )
    )
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(1, ProviderState.SEARCH_RESULTS),
            result=_result(ContentGoal.RESUME),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert _action_names(controller) == ["select"]
    assert outcome.terminal_status == TerminalStatus.HANDOFF
    assert outcome.playback_started is False


@pytest.mark.asyncio
async def test_verified_playback_cta_allows_episode_metadata_then_pauses() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _Policy(
        _decision("search", "select", focused="Avatar: The Last Airbender"),
        _decision("title_detail", "select", focused="Resume"),
    )
    controller = _Controller(
        _state(2, ProviderState.TITLE_DETAIL),
        _state(
            3,
            ProviderState.PLAYING,
            playback="playing",
            now_playing_title="The Boy in the Iceberg",
            now_playing_series="Avatar: The Last Airbender",
            png=None,
            blank=True,
        ),
        _state(
            4,
            ProviderState.PLAYING,
            playback="paused",
            now_playing_title="The Boy in the Iceberg",
            now_playing_series="Avatar: The Last Airbender",
            png=None,
            blank=True,
        ),
    )
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(1, ProviderState.SEARCH_RESULTS),
            result=_result(ContentGoal.RESUME),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert _action_names(controller) == [
        "select",
        "select",
        "pause",
    ]
    assert outcome.terminal_status == TerminalStatus.PLAYBACK_PAUSED_VERIFIED


@pytest.mark.asyncio
async def test_netflix_visible_idle_after_pause_is_verified_as_paused() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _Policy(_decision("title_detail", "select", focused="Resume"))
    controller = _Controller(
        _state(2, ProviderState.PLAYING, playback="playing", png=None, blank=True),
        _state(3, ProviderState.PLAYING, playback="idle", png=b"paused-controls"),
    )
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(1, ProviderState.TITLE_DETAIL),
            result=_result(ContentGoal.RESUME),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert _action_names(controller) == ["select", "pause"]
    assert outcome.terminal_status == TerminalStatus.PLAYBACK_PAUSED_VERIFIED


@pytest.mark.asyncio
async def test_playback_cta_does_not_verify_title_page_preview_as_resume() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _Policy(_decision("title_detail", "select", focused="Resume"))
    controller = _Controller(
        _state(
            2,
            ProviderState.TITLE_DETAIL,
            playback="playing",
            now_playing_title="Official Preview",
        )
    )
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(1, ProviderState.TITLE_DETAIL),
            result=_result(ContentGoal.RESUME),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert _action_names(controller) == ["select"]
    assert outcome.terminal_status == TerminalStatus.HANDOFF
    assert outcome.playback_started is False


@pytest.mark.asyncio
async def test_delayed_select_outcome_is_never_selected_twice() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _Policy(
        _decision("search", "select", focused="Avatar: The Last Airbender"),
    )
    unchanged = _state(2, ProviderState.SEARCH_RESULTS).model_copy(
        update={"frame_sha256": "sha-1"}
    )
    controller = _Controller(unchanged)
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.TITLE_OPEN,
            state=_state(1, ProviderState.SEARCH_RESULTS),
            result=_result(ContentGoal.TITLE_OPEN),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert _action_names(controller) == ["select"]
    assert outcome.terminal_status == TerminalStatus.HANDOFF
    assert "not repeated" in outcome.warnings[-1]


@pytest.mark.asyncio
async def test_visual_search_proof_recovers_from_ocr_home_misclassification() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _Policy(
        _decision(
            "search",
            "none",
            focused="Sonic X",
            title_match="exact",
        )
    )
    controller = _Controller()
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.SEARCH_READY,
            state=_state(1, ProviderState.HOME),
            result=_result(ContentGoal.SEARCH_READY),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert controller.actions == []
    assert outcome.terminal_status == TerminalStatus.QUERY_VERIFIED
    assert outcome.selected_result is False


@pytest.mark.asyncio
async def test_capture_failure_without_pixels_never_sends_blind_menu() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _NeverPolicy()
    controller = _Controller()
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(
                1,
                ProviderState.BLANK_OR_PROTECTED,
                playback="idle",
                png=None,
                blank=True,
            ),
            result=_result(ContentGoal.RESUME),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert controller.actions == []
    assert policy.calls == 0
    assert outcome.terminal_status == TerminalStatus.HANDOFF
    assert "No current Apple TV frame" in outcome.warnings[-1]


@pytest.mark.asyncio
async def test_failed_profile_macro_select_is_never_repeated_by_vision() -> None:
    picker = _state(1, ProviderState.PROFILE_PICKER)

    class ProfileFailureController(_Controller):
        async def observe(  # type: ignore[override]
            self, _room_key: str, *, classify: bool = True
        ) -> ComputerUseState:
            _ = classify
            self.current = picker
            return picker

        async def run_macro(  # noqa: ANN202
            self,
            _room_key: str,
            macro: VerifiedMacro,
            *,
            initial_state: ComputerUseState | None = None,
        ):
            _ = initial_state
            select = macro.steps[0].actions[0].model_copy(
                update={"expected_sequence": picker.sequence}
            )
            self.actions.append(select)
            final = picker.model_copy(update={"sequence": 2})
            self.current = final
            return _failed_macro_result(
                macro,
                final,
                actions_sent=[select.label],
            )

    service = ApplicationService.from_config_path(use_fakes=True)
    service.registry.config.provider_prefs.setdefault("netflix", {})[
        "profile_name"
    ] = "primary"
    policy = _NeverPolicy()
    controller = ProfileFailureController()
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_search_ready(  # noqa: SLF001
            room_key="living_room",
            device_id="00000000-0000-4000-8000-000000000004",
            title="Avatar: The Last Airbender",
            profile_index=2,
            wake=True,
            goal=ContentGoal.TITLE_OPEN,
            idempotency_key=None,
            prior_warnings=[],
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert _action_names(controller) == ["select"]
    assert policy.calls == 0
    assert outcome.terminal_status == TerminalStatus.HANDOFF
    assert "not repeated" in outcome.warnings[-1]


@pytest.mark.asyncio
async def test_failed_metadata_refresh_cannot_count_stale_idle_twice() -> None:
    class RefreshFailureController(_Controller):
        async def read_device_context(self, _room_key: str) -> DeviceContext:
            raise RuntimeError("metadata unavailable")

    service = ApplicationService.from_config_path(use_fakes=True)
    controller = RefreshFailureController(
        _state(2, ProviderState.PLAYING, playback="idle", png=None, blank=True),
        _state(3, ProviderState.PLAYING, playback="idle", png=None, blank=True),
    )
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(
                1,
                ProviderState.PLAYING,
                playback="playing",
                now_playing_title="Avatar: The Last Airbender",
                png=None,
                blank=True,
            ),
            result=_result(ContentGoal.RESUME),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    pause_stages = [stage for stage in outcome.stages if stage.name.startswith("pause_after")]
    assert _action_names(controller) == ["pause", "pause"]
    assert len(pause_stages) == 2
    assert all(stage.status == StageStatus.FAILED for stage in pause_stages)
    assert all("stable_stopped_reads=1" in stage.evidence for stage in pause_stages)
    assert outcome.terminal_status == TerminalStatus.HANDOFF


@pytest.mark.asyncio
async def test_persistent_wrong_foreground_app_after_playback_exit_hands_off() -> None:
    wrong_app = _state(
        2,
        ProviderState.TITLE_DETAIL,
        playback="idle",
        current_app="com.apple.TVWatchList",
    )

    class WrongAppAfterExitController(_Controller):
        async def run_macro(  # noqa: ANN202
            self,
            _room_key: str,
            macro: VerifiedMacro,
            *,
            initial_state: ComputerUseState | None = None,
        ):
            assert initial_state is not None
            actions = [
                template.model_copy(update={"expected_sequence": initial_state.sequence})
                for template in macro.steps[0].actions
            ]
            self.actions.extend(actions)
            self.current = wrong_app
            return VerifiedMacroResult(
                room_key="living_room",
                provider=macro.provider,
                macro=macro.name,
                terminal=ComputerUseTerminal.VERIFIED,
                final_state=wrong_app,
                steps=[
                    MacroStepResult(
                        name=macro.steps[0].name,
                        before_sequence=initial_state.sequence,
                        after_sequence=wrong_app.sequence,
                        terminal=ComputerUseTerminal.VERIFIED,
                        actions_sent=[action.label for action in actions],
                        before_state=initial_state.semantics.state,
                        after_state=wrong_app.semantics.state,
                    )
                ],
            )

    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _NeverPolicy()
    controller = WrongAppAfterExitController()
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(
                1,
                ProviderState.BLANK_OR_PROTECTED,
                playback="paused",
                png=b"drm-black-frame",
                blank=True,
            ),
            result=_result(ContentGoal.RESUME),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert _action_names(controller) == ["menu"]
    assert policy.calls == 0
    assert outcome.terminal_status == TerminalStatus.HANDOFF
    assert "foreground app was not Netflix" in outcome.warnings[-1]


@pytest.mark.asyncio
async def test_failed_focus_macro_is_recorded_as_failed_stage() -> None:
    initial = _state(
        1,
        ProviderState.SEARCH_RESULTS,
        keyboard_focused=True,
        keyboard_text="Avatar: The Last Airbender",
    )

    class FocusFailureController(_Controller):
        async def run_macro(  # noqa: ANN202
            self,
            _room_key: str,
            macro: VerifiedMacro,
            *,
            initial_state: ComputerUseState | None = None,
        ):
            assert initial_state is not None
            final = _state(
                2,
                ProviderState.UNKNOWN,
                playback="idle",
                png=None,
                blank=True,
            )
            self.current = final
            return _failed_macro_result(macro, final, actions_sent=["press_key:down"])

    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _NeverPolicy()
    controller = FocusFailureController()
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.TITLE_OPEN,
            state=initial,
            result=_result(ContentGoal.TITLE_OPEN),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    focus_stage = next(
        stage for stage in outcome.stages if stage.name == "focus_first_search_result"
    )
    assert focus_stage.status == StageStatus.FAILED
    assert policy.calls == 0
    assert outcome.terminal_status == TerminalStatus.HANDOFF


@pytest.mark.asyncio
async def test_drm_black_after_playback_cta_needs_observed_playing_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("home_media.service.asyncio.sleep", no_sleep)
    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _Policy(_decision("title_detail", "select", focused="Resume"))
    controller = _Controller(
        _state(
            2,
            ProviderState.BLANK_OR_PROTECTED,
            playback="idle",
            png=b"drm-black-frame",
            blank=True,
        )
    )
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(1, ProviderState.TITLE_DETAIL),
            result=_result(ContentGoal.RESUME),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert _action_names(controller) == ["select"]
    assert outcome.terminal_status == TerminalStatus.HANDOFF
    assert outcome.playback_started is False
    assert "playing metadata never appeared" in outcome.warnings[-1]


@pytest.mark.asyncio
async def test_exact_title_paused_overlay_selects_play_once_then_pauses() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _Policy(
        _decision(
            "playback",
            "select",
            focused="Play",
            focused_kind_override="playback_cta",
        )
    )
    controller = _Controller(
        _state(
            2,
            ProviderState.PLAYING,
            playback="playing",
            now_playing_title="Avatar: The Last Airbender",
            png=None,
            blank=True,
        ),
        _state(
            3,
            ProviderState.PLAYING,
            playback="idle",
            now_playing_title="Avatar: The Last Airbender",
            png=b"paused-overlay-controls",
        ),
    )
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(
                1,
                ProviderState.PLAYING,
                playback="paused",
                now_playing_title="Avatar: The Last Airbender",
                png=b"visible-paused-overlay-with-focused-play",
            ),
            result=_result(ContentGoal.RESUME),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert _action_names(controller) == ["select", "pause"]
    assert outcome.terminal_status == TerminalStatus.PLAYBACK_PAUSED_VERIFIED
    assert outcome.playback_started is True


@pytest.mark.asyncio
async def test_wrong_title_paused_overlay_never_selects_play() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    policy = _Policy(
        _decision(
            "playback",
            "select",
            focused="Play",
            focused_kind_override="playback_cta",
            title_match="no",
        )
    )
    controller = _Controller()
    service.vision_policy = policy  # type: ignore[assignment]
    service.computer_use = controller  # type: ignore[assignment]
    try:
        outcome = await service._run_netflix_visual_goal(  # noqa: SLF001
            room_key="living_room",
            title="Avatar: The Last Airbender",
            goal=ContentGoal.RESUME,
            state=_state(
                1,
                ProviderState.PLAYING,
                playback="paused",
                now_playing_title="Sonic X",
                png=b"visible-wrong-title-paused-overlay",
            ),
            result=_result(ContentGoal.RESUME),
            mutation_state={"attempted": False},
        )
    finally:
        await service.aclose()

    assert controller.actions == []
    assert outcome.terminal_status == TerminalStatus.HANDOFF
    assert outcome.playback_started is False
