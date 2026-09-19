"""Generic Apple TV computer-use loop and verified fast-path tests."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from home_media.computer_use import (
    AppleTVComputerUseController,
    ComputerUseAction,
    ComputerUseActionKind,
    ComputerUseTerminal,
    DeviceContext,
    ScreenElement,
    ScreenSemantics,
    netflix_exit_paused_playback_macro,
    netflix_home_to_search_macro,
    netflix_profile_select_macro,
    netflix_set_query_macro,
    semantics_from_classifier,
)
from home_media.errors import AmbiguousOutcomeError, SafetyBlockedError
from home_media.observers.blank import synthesize_png
from home_media.observers.classify import ScreenClassifierResult
from home_media.observers.ocr_types import OcrDocument, OcrToken
from home_media.observers.screenshot import CaptureMetadata, ScreenshotResult
from home_media.providers.base import ProviderState
from home_media.service import ApplicationService

ROOM = "living_room"
DEVICE = "00000000-0000-4000-8000-000000000004"
_CAPTURE_ON_READ = "test-source-capture-on-read"


def _shot(
    label: str,
    *,
    blank: bool = False,
    captured_at: str | None = _CAPTURE_ON_READ,
    error: str | None = None,
) -> ScreenshotResult:
    color = (sum(label.encode()) % 200 + 20, 70, 130)
    png = synthesize_png(24, 16, color)
    return ScreenshotResult(
        room_key=ROOM,
        device_id=DEVICE,
        blank_or_protected=blank,
        png_bytes=png,
        error=error,
        metadata=CaptureMetadata(
            sha256=hashlib.sha256(png).hexdigest(),
            width=24,
            height=16,
            latency_ms=3,
            captured_at=captured_at,
        ),
    )


class _Scenario:
    def __init__(
        self,
        frames: list[ScreenshotResult],
        semantics: dict[str, ScreenSemantics],
        contexts: list[DeviceContext] | None = None,
    ) -> None:
        self._frames: Iterator[ScreenshotResult] = iter(frames)
        self._contexts: Iterator[DeviceContext] = iter(contexts or [])
        self.semantics = semantics
        self.actions: list[ComputerUseAction] = []
        self.capture_count = 0

    async def frame(self, room: str) -> ScreenshotResult:
        assert room == ROOM
        self.capture_count += 1
        frame = next(self._frames)
        if frame.metadata.captured_at == _CAPTURE_ON_READ:
            metadata = frame.metadata.model_copy(
                update={"captured_at": datetime.now(UTC).isoformat()}
            )
            return frame.model_copy(update={"metadata": metadata})
        return frame

    async def context(self, room: str) -> DeviceContext:
        assert room == ROOM
        return next(self._contexts, DeviceContext())

    async def classify(self, frame: ScreenshotResult) -> ScreenSemantics:
        assert frame.metadata.sha256 is not None
        return self.semantics[frame.metadata.sha256]

    async def execute(self, room: str, action: ComputerUseAction) -> None:
        assert room == ROOM
        self.actions.append(action)

    def controller(self) -> AppleTVComputerUseController:
        return AppleTVComputerUseController(
            frame_source=self.frame,
            context_source=self.context,
            semantics_source=self.classify,
            action_executor=self.execute,
        )


def _semantics(
    state: ProviderState,
    *,
    confidence: float = 0.95,
    anchors: list[str] | None = None,
    focused_label: str | None = None,
) -> ScreenSemantics:
    return ScreenSemantics(
        state=state.value,
        confidence=confidence,
        anchors=anchors or [],
        focused_label=focused_label,
        elements=[
            ScreenElement(
                index=0,
                label="Search",
                x=0.1,
                y=0.1,
                width=0.2,
                height=0.1,
            )
        ],
        source="test_vision",
    )


def test_classifier_ocr_becomes_indexed_elements_but_stays_out_of_json() -> None:
    document = OcrDocument(tokens=[OcrToken(text="Breaking Bad", x=0.2, y=0.3, w=0.4, h=0.1)])
    classified = ScreenClassifierResult(
        provider_state=ProviderState.SEARCH_RESULTS,
        confidence=0.9,
        classifier_name="test",
        ocr_document=document,
    )

    semantics = semantics_from_classifier(classified, document=classified.ocr_document)

    assert semantics.elements[0].index == 0
    assert semantics.elements[0].label == "Breaking Bad"
    assert "Breaking Bad" not in classified.model_dump_json()


@pytest.mark.asyncio
async def test_observe_returns_multimodal_state_without_serializing_pixels() -> None:
    home = _shot("home")
    scenario = _Scenario(
        [home],
        {
            home.metadata.sha256 or "": _semantics(
                ProviderState.HOME,
                anchors=["netflix.chrome.top_nav"],
            )
        },
        [DeviceContext(current_app="com.netflix.Netflix")],
    )

    state = await scenario.controller().observe(ROOM)

    assert state.sequence == 1
    assert state.captured_at is not None
    assert state.png_bytes == home.png_bytes
    assert state.semantics.elements[0].label == "Search"
    assert state.device.current_app == "com.netflix.Netflix"
    assert "png_bytes" not in state.agent_metadata()


@pytest.mark.asyncio
async def test_old_producer_frame_cannot_gain_freshness_from_observe_time() -> None:
    old = _shot(
        "one-hour-old",
        captured_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    scenario = _Scenario(
        [old],
        {old.metadata.sha256 or "": _semantics(ProviderState.HOME)},
    )
    controller = scenario.controller()
    observed = await controller.observe(ROOM)
    action = ComputerUseAction(
        kind=ComputerUseActionKind.PRESS_KEY,
        key="right",
        expected_sequence=observed.sequence,
    )

    with pytest.raises(SafetyBlockedError) as exc:
        await controller.act(ROOM, action)

    assert observed.captured_at == datetime.fromisoformat(old.metadata.captured_at or "")
    assert exc.value.details["reason"] == "computer_use_stale_frame"
    assert scenario.actions == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("captured_at", "error", "expected_reason"),
    [
        (None, None, "computer_use_capture_time_missing"),
        ("not-a-timestamp", None, "computer_use_capture_time_invalid"),
        (
            (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            None,
            "computer_use_capture_time_future",
        ),
        (datetime.now(UTC).isoformat(), "capture_failed", "computer_use_capture_error"),
    ],
)
async def test_unverified_producer_time_or_capture_error_blocks_input(
    captured_at: str | None,
    error: str | None,
    expected_reason: str,
) -> None:
    frame = _shot("unverified", captured_at=captured_at, error=error)
    scenario = _Scenario(
        [frame],
        {frame.metadata.sha256 or "": _semantics(ProviderState.HOME)},
    )
    controller = scenario.controller()
    observed = await controller.observe(ROOM)
    action = ComputerUseAction(
        kind=ComputerUseActionKind.PRESS_KEY,
        key="right",
        expected_sequence=observed.sequence,
    )

    with pytest.raises(SafetyBlockedError) as exc:
        await controller.act(ROOM, action)

    assert exc.value.details["reason"] == expected_reason
    assert scenario.actions == []


@pytest.mark.asyncio
async def test_device_context_receipt_poll_does_not_capture_a_frame() -> None:
    home = _shot("home")
    scenario = _Scenario(
        [home],
        {home.metadata.sha256 or "": _semantics(ProviderState.HOME)},
        [
            DeviceContext(current_app="com.netflix.Netflix"),
            DeviceContext(playback_state="playing"),
        ],
    )
    controller = scenario.controller()
    await controller.observe(ROOM)

    context = await controller.read_device_context(ROOM)

    assert context.playback_state == "playing"
    assert scenario.capture_count == 1


@pytest.mark.asyncio
async def test_observe_cancels_context_peer_when_frame_capture_fails() -> None:
    context_cancelled = asyncio.Event()

    async def failed_frame(_room: str) -> ScreenshotResult:
        await asyncio.sleep(0)
        raise RuntimeError("capture failed")

    async def slow_context(_room: str) -> DeviceContext:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            context_cancelled.set()
            raise
        raise AssertionError("unreachable")

    controller = AppleTVComputerUseController(
        frame_source=failed_frame,
        context_source=slow_context,
        action_executor=lambda _room, _action: None,
    )
    with pytest.raises(RuntimeError, match="capture failed"):
        await controller.observe(ROOM)

    assert context_cancelled.is_set()


@pytest.mark.asyncio
async def test_one_action_requires_current_generation_and_returns_post_frame() -> None:
    home = _shot("home")
    search = _shot("search")
    scenario = _Scenario(
        [home, search],
        {
            home.metadata.sha256 or "": _semantics(ProviderState.HOME),
            search.metadata.sha256 or "": _semantics(ProviderState.SEARCH_NAV),
        },
    )
    controller = scenario.controller()
    before = await controller.observe(ROOM)
    action = ComputerUseAction(
        kind=ComputerUseActionKind.PRESS_KEY,
        key="left",
        expected_sequence=before.sequence,
        allowed_from_states=[ProviderState.HOME.value],
        min_confidence=0.7,
    )

    result = await controller.act(ROOM, action)

    assert result.before_sequence == 1
    assert result.after.sequence == 2
    assert result.after.semantics.state == ProviderState.SEARCH_NAV.value
    assert result.frame_changed is True
    assert [item.key for item in scenario.actions] == ["left"]
    assert scenario.capture_count == 2

    with pytest.raises(SafetyBlockedError) as exc:
        await controller.act(ROOM, action)
    assert exc.value.details["reason"] == "computer_use_stale_generation"


@pytest.mark.asyncio
async def test_pre_action_frame_replayed_after_action_is_ambiguous_and_not_resent() -> None:
    home = _shot("fresh-home")
    replayed = _shot(
        "cached-before-action",
        captured_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    scenario = _Scenario(
        [home, replayed, replayed, replayed],
        {
            home.metadata.sha256 or "": _semantics(ProviderState.HOME),
            replayed.metadata.sha256 or "": _semantics(ProviderState.SEARCH_NAV),
        },
    )
    controller = scenario.controller()
    before = await controller.observe(ROOM)
    action = ComputerUseAction(
        kind=ComputerUseActionKind.PRESS_KEY,
        key="right",
        expected_sequence=before.sequence,
    )

    with pytest.raises(AmbiguousOutcomeError) as exc:
        await controller.act(ROOM, action)

    assert exc.value.details["reason"] == "computer_use_post_action_frame_unverified"
    assert exc.value.details["freshness_reason"] == "capture_precedes_action_completion"
    assert exc.value.details["action_dispatched"] is True
    assert exc.value.details["do_not_retry"] is True
    assert [item.key for item in scenario.actions] == ["right"]
    assert scenario.capture_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("captured_at", "error", "freshness_reason"),
    [
        (None, None, "capture_time_missing"),
        (
            (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            None,
            "capture_time_future",
        ),
        (datetime.now(UTC).isoformat(), "capture_failed", "capture_error"),
    ],
)
async def test_bad_post_action_capture_cannot_be_laundered_into_success(
    captured_at: str | None,
    error: str | None,
    freshness_reason: str,
) -> None:
    home = _shot("good-pre-action")
    bad = _shot("bad-post-action", captured_at=captured_at, error=error)
    scenario = _Scenario(
        [home, bad, bad, bad],
        {
            home.metadata.sha256 or "": _semantics(ProviderState.HOME),
            bad.metadata.sha256 or "": _semantics(ProviderState.SEARCH_NAV),
        },
    )
    controller = scenario.controller()
    before = await controller.observe(ROOM)

    with pytest.raises(AmbiguousOutcomeError) as exc:
        await controller.act(
            ROOM,
            ComputerUseAction(
                kind=ComputerUseActionKind.PRESS_KEY,
                key="right",
                expected_sequence=before.sequence,
            ),
        )

    assert exc.value.details["freshness_reason"] == freshness_reason
    assert [item.key for item in scenario.actions] == ["right"]


@pytest.mark.asyncio
async def test_identical_pixels_are_valid_when_producer_time_is_post_action() -> None:
    unchanged = _shot("static-screen")
    scenario = _Scenario(
        [unchanged, unchanged],
        {unchanged.metadata.sha256 or "": _semantics(ProviderState.HOME)},
    )
    controller = scenario.controller()
    before = await controller.observe(ROOM)

    result = await controller.act(
        ROOM,
        ComputerUseAction(
            kind=ComputerUseActionKind.PRESS_KEY,
            key="right",
            expected_sequence=before.sequence,
        ),
    )

    assert result.frame_changed is False
    assert result.after.captured_at is not None
    assert before.captured_at is not None
    assert result.after.captured_at >= before.captured_at
    assert [item.key for item in scenario.actions] == ["right"]


@pytest.mark.asyncio
async def test_macro_stops_on_pre_action_replay_without_repeating_select() -> None:
    picker = _shot("fresh-picker")
    replayed = _shot(
        "cached-home",
        captured_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    scenario = _Scenario(
        [picker, replayed, replayed, replayed],
        {
            picker.metadata.sha256 or "": _semantics(
                ProviderState.PROFILE_PICKER,
                anchors=["netflix.chrome.highlighted_profile"],
                focused_label="primary",
            ),
            replayed.metadata.sha256 or "": _semantics(ProviderState.HOME),
        },
    )
    controller = scenario.controller()
    before = await controller.observe(ROOM)

    result = await controller.run_macro(
        ROOM,
        netflix_profile_select_macro(sequence=before.sequence, profile_name="primary"),
    )

    assert result.terminal == ComputerUseTerminal.VISION_REQUIRED
    assert result.fallback_reason == "unverified_post_action_frame"
    assert result.steps[0].actions_sent == ["press_key:select"]
    assert [item.key for item in scenario.actions] == ["select"]
    assert scenario.capture_count == 4


@pytest.mark.asyncio
async def test_paused_playback_exit_skill_uses_one_back_and_one_post_frame() -> None:
    paused = _shot("paused-controls", blank=True)
    detail = _shot("title-detail")
    scenario = _Scenario(
        [paused, detail],
        {
            paused.metadata.sha256 or "": _semantics(
                ProviderState.BLANK_OR_PROTECTED,
                confidence=0.2,
            ),
            detail.metadata.sha256 or "": _semantics(
                ProviderState.TITLE_DETAIL,
                confidence=0.75,
            ),
        },
        [
            DeviceContext(
                current_app="com.netflix.Netflix",
                playback_state="idle",
            ),
            DeviceContext(
                current_app="com.netflix.Netflix",
                playback_state="idle",
            ),
        ],
    )
    controller = scenario.controller()
    initial = await controller.observe(ROOM)

    outcome = await controller.run_macro(
        ROOM,
        netflix_exit_paused_playback_macro(
            sequence=initial.sequence,
            source_state=ProviderState.BLANK_OR_PROTECTED.value,
        ),
        initial_state=initial,
    )

    assert outcome.terminal == ComputerUseTerminal.VERIFIED
    assert [action.key for action in scenario.actions] == ["menu"]
    assert scenario.capture_count == 2
    assert outcome.final_state.semantics.state == ProviderState.TITLE_DETAIL.value


@pytest.mark.asyncio
async def test_select_without_explicit_visual_guard_is_blocked() -> None:
    home = _shot("home")
    scenario = _Scenario(
        [home],
        {home.metadata.sha256 or "": _semantics(ProviderState.HOME)},
    )
    controller = scenario.controller()
    before = await controller.observe(ROOM)
    action = ComputerUseAction(
        kind=ComputerUseActionKind.PRESS_KEY,
        key="select",
        expected_sequence=before.sequence,
    )

    with pytest.raises(SafetyBlockedError) as exc:
        await controller.act(ROOM, action)

    assert exc.value.details["reason"] == "computer_use_select_unknown"
    assert scenario.actions == []


@pytest.mark.asyncio
async def test_model_can_select_named_target_on_exact_novel_frame() -> None:
    novel = _shot("novel-app")
    detail = _shot("title-detail")
    scenario = _Scenario(
        [novel, detail],
        {
            novel.metadata.sha256 or "": _semantics(
                ProviderState.UNKNOWN,
                confidence=0.2,
            ),
            detail.metadata.sha256 or "": _semantics(ProviderState.TITLE_DETAIL),
        },
    )
    controller = scenario.controller()
    before = await controller.observe(ROOM)
    action = ComputerUseAction(
        kind=ComputerUseActionKind.PRESS_KEY,
        key="select",
        expected_sequence=before.sequence,
        model_observed_target="Breaking Bad search result",
    )

    result = await controller.act(ROOM, action)

    assert result.after.semantics.state == ProviderState.TITLE_DETAIL.value
    assert [item.key for item in scenario.actions] == ["select"]


@pytest.mark.asyncio
async def test_netflix_home_to_search_is_one_verified_three_key_burst() -> None:
    home = _shot("home")
    search = _shot("search-keyboard")
    scenario = _Scenario(
        [home, search],
        {
            home.metadata.sha256 or "": _semantics(
                ProviderState.HOME,
                anchors=["netflix.chrome.top_nav"],
            ),
            search.metadata.sha256 or "": _semantics(
                ProviderState.SEARCH_KEYBOARD,
                anchors=["netflix.chrome.keyboard"],
            ),
        },
    )
    controller = scenario.controller()
    before = await controller.observe(ROOM)

    result = await controller.run_macro(
        ROOM,
        netflix_home_to_search_macro(sequence=before.sequence),
    )

    assert result.terminal == ComputerUseTerminal.VERIFIED
    assert [action.key for action in scenario.actions] == ["up", "left", "down"]
    # One pre-frame and one post-frame, never one capture per arrow.
    assert scenario.capture_count == 2
    assert result.final_state.semantics.state == ProviderState.SEARCH_KEYBOARD.value


@pytest.mark.asyncio
async def test_netflix_home_to_search_paces_navigation_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _shot("paced-home")
    search = _shot("paced-search")
    scenario = _Scenario(
        [home, search],
        {
            home.metadata.sha256 or "": _semantics(
                ProviderState.HOME,
                anchors=["netflix.chrome.top_nav"],
            ),
            search.metadata.sha256 or "": _semantics(
                ProviderState.SEARCH_KEYBOARD,
                anchors=["netflix.chrome.keyboard"],
            ),
        },
    )
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    controller = scenario.controller()
    before = await controller.observe(ROOM)

    result = await controller.run_macro(
        ROOM,
        netflix_home_to_search_macro(sequence=before.sequence),
    )

    assert result.terminal == ComputerUseTerminal.VERIFIED
    assert sleeps == [0.60, 0.60]


@pytest.mark.asyncio
async def test_macro_drift_stops_and_yields_latest_pixels_to_vision() -> None:
    home = _shot("home")
    unexpected = _shot("unexpected")
    scenario = _Scenario(
        [home, unexpected, unexpected],
        {
            home.metadata.sha256 or "": _semantics(
                ProviderState.HOME,
                anchors=["netflix.chrome.top_nav"],
            ),
            unexpected.metadata.sha256 or "": _semantics(
                ProviderState.PROFILE_PICKER,
                anchors=["netflix.chrome.profile.choose"],
            ),
        },
    )
    controller = scenario.controller()
    before = await controller.observe(ROOM)

    result = await controller.run_macro(
        ROOM,
        netflix_home_to_search_macro(sequence=before.sequence),
    )

    assert result.terminal == ComputerUseTerminal.VISION_REQUIRED
    assert result.fallback_reason == "unexpected_post_state"
    assert result.final_state.png_bytes == unexpected.png_bytes
    assert [action.key for action in scenario.actions] == ["up", "left", "down"]


@pytest.mark.asyncio
async def test_profile_fast_path_requires_named_highlight_before_select() -> None:
    picker = _shot("picker")
    scenario = _Scenario(
        [picker],
        {
            picker.metadata.sha256 or "": _semantics(
                ProviderState.PROFILE_PICKER,
                anchors=["netflix.chrome.highlighted_profile"],
                focused_label="Kids",
            )
        },
    )
    controller = scenario.controller()
    before = await controller.observe(ROOM)

    result = await controller.run_macro(
        ROOM,
        netflix_profile_select_macro(sequence=before.sequence, profile_name="primary"),
    )

    assert result.terminal == ComputerUseTerminal.VISION_REQUIRED
    assert result.fallback_reason == "profile_name_mismatch"
    assert scenario.actions == []
    assert scenario.capture_count == 1


@pytest.mark.asyncio
async def test_profile_fast_path_observes_transition_again_without_reselecting() -> None:
    picker = _shot("picker-transition")
    still_picker = _shot("picker-transitioning")
    home = _shot("profile-home")
    scenario = _Scenario(
        [picker, still_picker, home],
        {
            picker.metadata.sha256 or "": _semantics(
                ProviderState.PROFILE_PICKER,
                anchors=["netflix.chrome.highlighted_profile"],
                focused_label="primary",
            ),
            still_picker.metadata.sha256 or "": _semantics(
                ProviderState.PROFILE_PICKER,
                anchors=["netflix.chrome.highlighted_profile"],
                focused_label="primary",
            ),
            home.metadata.sha256 or "": _semantics(
                ProviderState.HOME,
                anchors=["netflix.chrome.top_nav"],
            ),
        },
    )
    controller = scenario.controller()
    before = await controller.observe(ROOM)

    result = await controller.run_macro(
        ROOM,
        netflix_profile_select_macro(sequence=before.sequence, profile_name="primary"),
    )

    assert result.terminal == ComputerUseTerminal.VERIFIED
    assert result.final_state.semantics.state == ProviderState.HOME.value
    assert [action.key for action in scenario.actions] == ["select"]
    assert scenario.capture_count == 3


@pytest.mark.asyncio
async def test_query_skill_requires_real_focus_and_exact_readback() -> None:
    keyboard = _shot("keyboard")
    populated = _shot("populated")
    scenario = _Scenario(
        [keyboard, populated],
        {
            keyboard.metadata.sha256 or "": _semantics(
                ProviderState.SEARCH_KEYBOARD,
                anchors=["netflix.chrome.keyboard"],
            ),
            populated.metadata.sha256 or "": _semantics(
                ProviderState.SEARCH_RESULTS,
                anchors=["netflix.chrome.keyboard"],
            ),
        },
        [
            DeviceContext(keyboard_focused=True, keyboard_text=""),
            DeviceContext(keyboard_focused=True, keyboard_text="Breaking Bad"),
        ],
    )
    controller = scenario.controller()
    before = await controller.observe(ROOM)

    result = await controller.run_macro(
        ROOM,
        netflix_set_query_macro(sequence=before.sequence, title="Breaking Bad"),
    )

    assert result.terminal == ComputerUseTerminal.VERIFIED
    assert len(scenario.actions) == 1
    assert scenario.actions[0].kind == ComputerUseActionKind.SET_TEXT
    assert scenario.actions[0].text == "Breaking Bad"


@pytest.mark.asyncio
async def test_query_mismatch_does_not_claim_verified() -> None:
    keyboard = _shot("keyboard")
    populated = _shot("wrong-query")
    scenario = _Scenario(
        [keyboard, populated],
        {
            keyboard.metadata.sha256 or "": _semantics(
                ProviderState.SEARCH_KEYBOARD,
                anchors=["netflix.chrome.keyboard"],
            ),
            populated.metadata.sha256 or "": _semantics(
                ProviderState.SEARCH_RESULTS,
                anchors=["netflix.chrome.keyboard"],
            ),
        },
        [
            DeviceContext(keyboard_focused=True, keyboard_text=""),
            DeviceContext(keyboard_focused=True, keyboard_text="Better Call Saul"),
        ],
    )
    controller = scenario.controller()
    before = await controller.observe(ROOM)

    result = await controller.run_macro(
        ROOM,
        netflix_set_query_macro(sequence=before.sequence, title="Breaking Bad"),
    )

    assert result.terminal == ComputerUseTerminal.VISION_REQUIRED
    assert result.fallback_reason == "keyboard_text_mismatch"


@pytest.mark.asyncio
async def test_application_service_uses_three_captures_for_home_to_verified_query() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    apple = service.adapters["apple_tv"]
    apple.set_provider_state(DEVICE, ProviderState.HOME.value)
    provider = service.screenshot_service._provider  # noqa: SLF001

    result = await service.prepare_content(
        ROOM,
        "Breaking Bad",
        provider="netflix",
        goal="search_ready",
    )

    assert result.terminal_status.value == "query_verified"
    assert [
        mutation["key"] for mutation in apple.mutations if mutation["action"] == "press_key"
    ] == ["up", "left", "down"]
    # Initial Home, one post-burst Search, one post-text result.  There is no
    # capture or model round-trip between individual arrow presses.
    assert len(provider.capture_calls) == 3
    assert len(result.extra["observer_timings"]) == 3


@pytest.mark.asyncio
async def test_cold_apple_home_launches_then_uses_verified_netflix_skills() -> None:
    service = ApplicationService.from_config_path(use_fakes=True)
    service.registry.config.provider_prefs.setdefault("netflix", {})["profile_name"] = "primary"
    apple = service.adapters["apple_tv"]
    apple.set_provider_state(DEVICE, ProviderState.APPLE_HOME.value)
    apple.highlighted_profile[DEVICE] = "primary"

    result = await service.prepare_content(
        ROOM,
        "Breaking Bad",
        provider="netflix",
        goal="search_ready",
    )

    assert result.terminal_status.value == "query_verified"
    assert [stage.name for stage in result.stages] == [
        "launch_app",
        "select_profile_2",
        "home_to_search_keyboard",
        "keyboard_set_query",
    ]
    assert result.observed_states == [
        "apple_home",
        "profile_picker",
        "home",
        "search_keyboard",
        "search_keyboard",
    ]
