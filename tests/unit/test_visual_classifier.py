"""Focused tests for Vision OCR seam, Netflix anchors, and visual gates."""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

import home_media.cli as cli_module
from home_media.cli import app as cli_app
from home_media.errors import SafetyBlockedError
from home_media.observers.binding import ObserverBindingStore
from home_media.observers.blank import synthesize_png
from home_media.observers.classify import (
    FakeLabelClassifier,
    VisionNetflixClassifier,
    profile_picker_chrome_present,
)
from home_media.observers.fake import FakeScreenshotProvider, fixture_for_state
from home_media.observers.netflix_anchors import classify_netflix_anchors
from home_media.observers.ocr import helper_source_path, parse_ocr_payload
from home_media.observers.ocr_types import OcrDocument, OcrToken
from home_media.observers.screenshot import CaptureMetadata, ScreenshotResult
from home_media.observers.service import RoomScreenshotService, assert_visual_gate
from home_media.providers.base import ProviderObservation, ProviderState, TransitionSpec
from home_media.providers.executor import ProviderRecipeRunner
from home_media.providers.netflix import NetflixAdapter
from home_media.service import ApplicationService
from home_media.vision_policy import (
    FocusedTargetKind,
    ScreenSurface,
    TitleMatch,
    VisionDecision,
    VisionRemoteAction,
)

LIVING = "00000000-0000-4000-8000-000000000004"
THEATER = "00000000-0000-4000-8000-000000000001"


def _ensure_netflix_profile_name(svc: ApplicationService, name: str = "primary") -> None:
    prefs = svc.registry.config.provider_prefs.setdefault("netflix", {})
    prefs["profile_name"] = name



def _tok(text: str, x: float, y: float, w: float = 0.1, h: float = 0.05) -> OcrToken:
    return OcrToken(text=text, x=x, y=y, w=w, h=h)


def test_netflix_anchor_profile_picker_and_highlighted() -> None:
    doc = OcrDocument(
        tokens=[
            _tok("Who's watching?", 0.3, 0.1, 0.4, 0.08),
            _tok("primary*", 0.2, 0.45, 0.15, 0.08),
            _tok("kids", 0.5, 0.45, 0.15, 0.08),
        ]
    )
    result = classify_netflix_anchors(doc)
    assert result.state == ProviderState.PROFILE_PICKER
    assert result.confidence >= 0.85
    assert profile_picker_chrome_present(result.anchors)
    assert result.highlighted_profile_name is not None
    assert result.highlighted_profile_name.lower().startswith("primary")


def test_netflix_anchor_picker_treats_kids_as_explicit_highlight() -> None:
    result = classify_netflix_anchors(
        OcrDocument(
            tokens=[
                _tok("Choose a profile", 0.08, 0.10, 0.18, 0.06),
                _tok("Kids", 0.18, 0.42, 0.10, 0.06),
            ]
        )
    )
    assert result.state == ProviderState.PROFILE_PICKER
    assert result.highlighted_profile_name == "Kids"
    assert "netflix.chrome.highlighted_profile" in result.anchors


def test_netflix_anchor_picker_multiple_visible_names_is_not_selectable() -> None:
    result = classify_netflix_anchors(
        OcrDocument(
            tokens=[
                _tok("Choose a profile", 0.08, 0.10, 0.18, 0.06),
                _tok("primary", 0.18, 0.38, 0.10, 0.06),
                _tok("Dad", 0.18, 0.55, 0.10, 0.06),
            ]
        )
    )
    assert result.state == ProviderState.PROFILE_PICKER
    assert result.highlighted_profile_name is None
    assert "netflix.chrome.highlighted_profile" not in result.anchors


def test_netflix_anchor_home_excludes_search_keyboard() -> None:
    home = OcrDocument(
        tokens=[
            _tok("Home", 0.05, 0.05),
            _tok("Shows", 0.2, 0.05),
            _tok("Movies", 0.35, 0.05),
            _tok("My Netflix", 0.55, 0.05),
        ]
    )
    assert classify_netflix_anchors(home).state == ProviderState.HOME

    keyboard = OcrDocument(
        tokens=[
            _tok("Search", 0.1, 0.08),
            _tok("Home", 0.05, 0.05),
            _tok("Shows", 0.2, 0.05),
            _tok("Movies", 0.35, 0.05),
            *[_tok(ch, 0.1 + i * 0.03, 0.6) for i, ch in enumerate("abcdefghijkl")],
            _tok("TV Shows", 0.7, 0.4),
        ]
    )
    kb = classify_netflix_anchors(keyboard)
    assert kb.state == ProviderState.SEARCH_KEYBOARD
    assert kb.state != ProviderState.HOME


def test_apple_home_requires_three_known_app_grid_labels() -> None:
    app_grid = OcrDocument(
        tokens=[
            _tok("Netflix", 0.08, 0.22),
            _tok("YouTube", 0.30, 0.22),
            _tok("Hulu", 0.52, 0.22),
            _tok("Settings", 0.74, 0.22),
        ]
    )
    classified = classify_netflix_anchors(app_grid)
    assert classified.state == ProviderState.APPLE_HOME
    assert classified.confidence >= 0.9
    assert "apple.chrome.app_grid" in classified.anchors

    only_two = OcrDocument(
        tokens=[
            _tok("Netflix", 0.08, 0.22),
            _tok("YouTube", 0.30, 0.22),
        ]
    )
    assert classify_netflix_anchors(only_two).state == ProviderState.UNKNOWN


def test_netflix_home_wins_over_any_incidental_app_labels() -> None:
    home = OcrDocument(
        tokens=[
            _tok("Home", 0.05, 0.05),
            _tok("Shows", 0.2, 0.05),
            _tok("Movies", 0.35, 0.05),
            _tok("My Netflix", 0.55, 0.05),
            _tok("YouTube", 0.10, 0.50),
            _tok("Hulu", 0.30, 0.50),
            _tok("Settings", 0.50, 0.50),
        ]
    )
    assert classify_netflix_anchors(home).state == ProviderState.HOME


def test_netflix_anchor_search_results_requires_exact_query() -> None:
    title = "Avatar: The Last Airbender"
    tokens = [
        _tok("Search", 0.1, 0.08),
        _tok(title, 0.3, 0.15, 0.4, 0.06),
        _tok("Top results", 0.1, 0.35),
        _tok("Titles", 0.1, 0.45),
    ]
    hit = classify_netflix_anchors(OcrDocument(tokens=tokens), requested_query=title)
    assert hit.state == ProviderState.SEARCH_RESULTS
    miss = classify_netflix_anchors(
        OcrDocument(tokens=tokens), requested_query="Stranger Things"
    )
    assert miss.state != ProviderState.SEARCH_RESULTS or "query_visible" not in miss.anchors


def test_netflix_anchor_live_results_without_search_heading() -> None:
    """Netflix replaces the Search heading with the populated query live."""
    title = "Avatar: The Last Airbender"
    doc = OcrDocument(
        tokens=[
            _tok(title, 0.03, 0.08, 0.42, 0.07),
            _tok("Home", 0.03, 0.02),
            _tok("Shows", 0.17, 0.02),
            _tok("Movies", 0.30, 0.02),
            _tok("My Netflix", 0.45, 0.02),
            *[_tok(ch, 0.03 + i * 0.035, 0.55) for i, ch in enumerate("abcdefghijkl")],
        ]
    )
    result = classify_netflix_anchors(doc, requested_query=title)
    assert result.state == ProviderState.SEARCH_RESULTS
    assert result.confidence >= 0.85
    assert "netflix.chrome.query_visible" in result.anchors
    assert "netflix.chrome.keyboard" in result.anchors


def test_netflix_anchor_restored_different_query_is_reusable_results() -> None:
    doc = OcrDocument(
        tokens=[
            _tok("Q Avatar: The Last Airbender", 0.055, 0.11, 0.41, 0.06),
            _tok("Home", 0.38, 0.04),
            _tok("Shows", 0.45, 0.04),
            _tok("Movies", 0.52, 0.04),
            *[_tok(ch, 0.03 + i * 0.035, 0.55) for i, ch in enumerate("abcdefghijkl")],
        ]
    )
    result = classify_netflix_anchors(doc, requested_query="Archer")
    assert result.state == ProviderState.SEARCH_RESULTS
    assert "netflix.chrome.query_populated" in result.anchors
    assert "netflix.chrome.query_visible" not in result.anchors


def test_netflix_anchor_grouped_vision_alphabet_is_keyboard() -> None:
    doc = OcrDocument(
        tokens=[
            _tok("Q Archer", 0.055, 0.11, 0.24, 0.06),
            _tok("Home", 0.38, 0.04),
            _tok("Shows", 0.45, 0.04),
            _tok("Movies", 0.52, 0.04),
            _tok("a", 0.19, 0.21, 0.02, 0.04),
            _tok("bodefghijklmnoparstuvwxyz", 0.22, 0.21, 0.69, 0.04),
        ]
    )
    result = classify_netflix_anchors(doc, requested_query="Archer")
    assert result.state == ProviderState.SEARCH_RESULTS
    assert "netflix.chrome.keyboard" in result.anchors
    assert "netflix.chrome.query_populated" in result.anchors


def test_netflix_anchor_title_detail_error_unknown_conservative() -> None:
    detail = classify_netflix_anchors(
        OcrDocument(
            tokens=[
                _tok("Play", 0.1, 0.7),
                _tok("Episodes", 0.3, 0.7),
                _tok("Trailers & More", 0.5, 0.7),
            ]
        )
    )
    assert detail.state == ProviderState.TITLE_DETAIL

    err = classify_netflix_anchors(
        OcrDocument(tokens=[_tok("Something went wrong", 0.2, 0.4), _tok("Try again", 0.2, 0.5)])
    )
    assert err.state == ProviderState.ERROR_OR_MODAL

    unknown = classify_netflix_anchors(OcrDocument(tokens=[_tok("Poster Art", 0.4, 0.5)]))
    assert unknown.state == ProviderState.UNKNOWN
    assert unknown.confidence < 0.5


@pytest.mark.asyncio
async def test_blank_gate_runs_before_ocr() -> None:
    calls = {"n": 0}

    async def ocr(_png: bytes):
        calls["n"] += 1
        return OcrDocument(tokens=[_tok("Home", 0.1, 0.1)])

    blank = ScreenshotResult(
        device_id=LIVING,
        room_key="living_room",
        blank_or_protected=True,
        png_bytes=synthesize_png(8, 8, (0, 0, 0)),
        metadata=CaptureMetadata(sha256="blanksha", blank_reason="near_black_frame"),
    )
    result = await VisionNetflixClassifier(ocr=ocr).classify(blank)
    assert result.provider_state == ProviderState.BLANK_OR_PROTECTED
    assert calls["n"] == 0


def test_assert_visual_gate_low_confidence() -> None:
    with pytest.raises(SafetyBlockedError) as exc:
        assert_visual_gate(ProviderState.UNKNOWN, confidence=0.2, require=True)
    assert exc.value.details["reason"] == "screenshot_gate_low_confidence"


@pytest.mark.asyncio
async def test_wrong_room_binding_refuses_before_launch() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    store = ObserverBindingStore.empty()
    # Bind Theater only — Living Room request must refuse.
    store.confirm(room_key="theater", stable_device_id=THEATER, observer_udid="udid-theater-only")
    svc.screenshot_service = RoomScreenshotService(
        svc.registry,
        bindings=store,
        provider=FakeScreenshotProvider({THEATER: fixture_for_state(ProviderState.HOME)}),
    )
    svc._use_fakes = False  # noqa: SLF001 — exercise live binding gate
    apple = svc.adapters["apple_tv"]
    before = len(apple.mutations)
    result = await svc.prepare_content(
        "living_room",
        "Avatar",
        provider="netflix",
        goal="search_ready",
    )
    assert result.terminal_status.value == "handoff"
    assert any(s.name == "screenshot_gate" for s in result.stages)
    assert len(apple.mutations) == before  # no launch / remote


@pytest.mark.asyncio
async def test_profile_name_mismatch_prevents_select() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    _ensure_netflix_profile_name(svc, "primary")
    apple = svc.adapters["apple_tv"]
    apple.set_provider_state(LIVING, "profile_picker")
    apple.highlighted_profile[LIVING] = "somebody_else"
    result = await svc.prepare_content(
        "living_room",
        "Avatar",
        provider="netflix",
        goal="search_ready",
    )
    selects = [
        m for m in apple.mutations if m["action"] == "press_key" and m.get("key") == "select"
    ]
    assert selects == []
    assert any("profile_name_mismatch" in str(s.evidence) for s in result.stages) or any(
        "profile_name_mismatch" in w for w in result.warnings
    )


@pytest.mark.asyncio
async def test_kids_highlight_prevents_jack_profile_select() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    _ensure_netflix_profile_name(svc, "primary")
    apple = svc.adapters["apple_tv"]
    apple.set_provider_state(LIVING, "profile_picker")
    apple.highlighted_profile[LIVING] = "Kids"
    result = await svc.prepare_content(
        "living_room",
        "Avatar",
        provider="netflix",
        goal="search_ready",
    )
    selects = [
        m for m in apple.mutations if m["action"] == "press_key" and m.get("key") == "select"
    ]
    assert selects == []
    assert result.terminal_status.value == "failed"


@pytest.mark.asyncio
async def test_exact_highlighted_profile_allows_one_select() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    _ensure_netflix_profile_name(svc, "primary")
    apple = svc.adapters["apple_tv"]
    apple.set_provider_state(LIVING, "profile_picker")
    apple.highlighted_profile[LIVING] = "primary"
    result = await svc.prepare_content(
        "living_room",
        "Avatar",
        provider="netflix",
        goal="search_ready",
    )
    selects = [
        m for m in apple.mutations if m["action"] == "press_key" and m.get("key") == "select"
    ]
    assert len(selects) == 1
    assert result.selected_result is False
    assert result.playback_started is False


@pytest.mark.asyncio
async def test_expected_state_mismatch_stops_recipe() -> None:
    async def observe() -> ProviderObservation:
        return ProviderObservation(
            screenshot_state=ProviderState.HOME.value,
            confidence=0.95,
            screenshot_anchors=["netflix.chrome.top_nav"],
        )

    async def execute(action: dict) -> None:
        _ = action

    runner = ProviderRecipeRunner(observe=observe, execute=execute)
    # Force a transition that expects search_keyboard but observation stays home.
    specs = [
        TransitionSpec(
            name="home_to_search_keyboard",
            allowed_from=[ProviderState.HOME],
            actions=[{"type": "press_key", "key": "up"}],
            expected_to=ProviderState.SEARCH_KEYBOARD,
            accepted_post_states=[ProviderState.SEARCH_KEYBOARD],
            timeout_s=2.0,
            requires_observation=True,
        ),
        TransitionSpec(
            name="should_not_run",
            allowed_from=[ProviderState.SEARCH_KEYBOARD],
            actions=[{"type": "keyboard_set", "text": "x"}],
            expected_to=ProviderState.SEARCH_KEYBOARD,
            timeout_s=2.0,
        ),
    ]
    raw = await runner.run_search_ready(
        room_key="living_room",
        title="Avatar",
        transitions=specs,
        classify=NetflixAdapter().classify,
    )
    assert raw["stages"][0]["status"] == "failed"
    assert "expected_state_mismatch" in raw["stages"][0]["evidence"]
    assert all(s["name"] != "should_not_run" or s["status"] != "succeeded" for s in raw["stages"])
    assert len([s for s in raw["stages"] if s["name"] == "should_not_run"]) == 0


@pytest.mark.asyncio
async def test_transition_timeout_behavior() -> None:
    async def observe() -> ProviderObservation:
        return ProviderObservation(
            screenshot_state=ProviderState.HOME.value,
            confidence=0.95,
            screenshot_anchors=["netflix.chrome.top_nav"],
        )

    async def slow(action: dict) -> None:
        _ = action
        await asyncio.sleep(0.2)

    runner = ProviderRecipeRunner(observe=observe, execute=slow)
    specs = [
        TransitionSpec(
            name="home_to_search_keyboard",
            allowed_from=[ProviderState.HOME],
            actions=[{"type": "press_key", "key": "up"}],
            expected_to=ProviderState.SEARCH_KEYBOARD,
            timeout_s=0.05,
            requires_observation=True,
        )
    ]
    raw = await runner.run_search_ready(
        room_key="living_room",
        title="Avatar",
        transitions=specs,
        classify=NetflixAdapter().classify,
    )
    assert raw["stages"][0]["status"] == "failed"
    assert any("timeout" in e for e in raw["stages"][0]["evidence"])


@pytest.mark.asyncio
async def test_transition_timeout_includes_observation() -> None:
    calls = {"execute": 0}

    async def slow_observe() -> ProviderObservation:
        await asyncio.sleep(0.2)
        return ProviderObservation(
            screenshot_state=ProviderState.HOME.value,
            confidence=0.95,
            screenshot_anchors=["netflix.chrome.top_nav"],
        )

    async def execute(action: dict) -> None:
        _ = action
        calls["execute"] += 1

    runner = ProviderRecipeRunner(observe=slow_observe, execute=execute)
    specs = [
        TransitionSpec(
            name="bounded_observation",
            allowed_from=[ProviderState.HOME],
            actions=[{"type": "press_key", "key": "up"}],
            expected_to=ProviderState.SEARCH_KEYBOARD,
            timeout_s=0.05,
            requires_observation=True,
        )
    ]
    raw = await runner.run_search_ready(
        room_key="living_room",
        title="Avatar",
        transitions=specs,
        classify=NetflixAdapter().classify,
    )
    assert raw["stages"][0]["status"] == "failed"
    assert "phase=pre_observation" in raw["stages"][0]["evidence"]
    assert calls["execute"] == 0


@pytest.mark.asyncio
async def test_no_synthetic_focus_readback_success() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    apple = svc.adapters["apple_tv"]
    apple.set_provider_state(LIVING, "home")
    # Force keyboard_focus to stay false even after navigation attempts.
    data = apple.devices[LIVING]
    original_press = apple.press_key

    async def press_no_focus(device_id: str, key: str) -> None:
        await original_press(device_id, key)
        apple.devices[device_id]["keyboard_focus"] = False

    apple.press_key = press_no_focus  # type: ignore[method-assign]
    result = await svc.prepare_content(
        "living_room",
        "Avatar: The Last Airbender",
        provider="netflix",
        goal="search_ready",
    )
    assert result.terminal_status.value != "query_verified"
    assert data.get("keyboard_focus") is False


@pytest.mark.asyncio
async def test_launch_to_playing_stops_without_select() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    apple = svc.adapters["apple_tv"]
    apple.set_provider_state(LIVING, "unknown")

    original_open = apple.open_app

    async def open_to_playing(device_id: str, app_id: str):
        status = await original_open(device_id, app_id)
        apple.provider_state[device_id] = "playing"
        apple.devices[device_id]["now_playing"] = apple.devices[device_id].get("now_playing")
        from home_media.models import NowPlaying

        apple.devices[device_id]["now_playing"] = NowPlaying(
            title="Resumed Show", app_id=app_id, device_state="playing"
        )
        return status

    apple.open_app = open_to_playing  # type: ignore[method-assign]
    fake = FakeScreenshotProvider({LIVING: fixture_for_state(ProviderState.UNKNOWN)})
    fake.link_provider_state(apple)
    assert svc.screenshot_service is not None
    svc.screenshot_service._provider = fake  # noqa: SLF001
    result = await svc.prepare_content(
        "living_room",
        "Avatar",
        provider="netflix",
        goal="search_ready",
    )
    selects = [
        m for m in apple.mutations if m["action"] == "press_key" and m.get("key") == "select"
    ]
    assert selects == []
    assert result.playback_started is False
    assert result.terminal_status.value in {"handoff", "failed"}


@pytest.mark.asyncio
async def test_visible_unknown_screen_uses_vision_for_title_goal_without_relaunch() -> None:
    class NoActionVision:
        async def decide(self, _frames, _context):  # noqa: ANN001, ANN202
            return VisionDecision(
                surface=ScreenSurface.OTHER,
                confidence=0.4,
                title_match=TitleMatch.UNKNOWN,
                focused_target=None,
                focused_kind=FocusedTargetKind.NONE,
                visible_titles=[],
                safe_to_select=False,
                playback_visible=False,
                next_action=VisionRemoteAction.NONE,
                reason_code="no_safe_action",
            )

    svc = ApplicationService.from_config_path(use_fakes=True)
    svc.vision_policy = NoActionVision()  # type: ignore[assignment]
    apple = svc.adapters["apple_tv"]
    apple.set_provider_state(LIVING, ProviderState.UNKNOWN.value)
    fake = FakeScreenshotProvider({LIVING: fixture_for_state(ProviderState.UNKNOWN)})
    fake.link_provider_state(apple)
    assert svc.screenshot_service is not None
    svc.screenshot_service._provider = fake  # noqa: SLF001

    result = await svc.prepare_content(
        "living_room",
        "Avatar: The Last Airbender",
        provider="netflix",
        goal="title_open",
    )

    assert result.terminal_status.value == "handoff"
    assert not any(mutation["action"] == "open_app" for mutation in apple.mutations)


def test_unknown_visual_state_yields_to_now_playing_metadata() -> None:
    state = NetflixAdapter().classify(
        ProviderObservation(
            screenshot_state=ProviderState.UNKNOWN.value,
            confidence=0.25,
            now_playing_title="Resumed Show",
        )
    )
    assert state == ProviderState.PLAYING


@pytest.mark.asyncio
async def test_integration_unknown_to_query_verified_with_fake_vision() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    _ensure_netflix_profile_name(svc, "primary")
    apple = svc.adapters["apple_tv"]
    apple.set_provider_state(LIVING, "unknown")
    apple.highlighted_profile[LIVING] = "primary"
    fake = FakeScreenshotProvider({LIVING: fixture_for_state(ProviderState.UNKNOWN)})
    fake.link_provider_state(apple)
    fake.set_highlighted_profile(LIVING, "primary")
    assert svc.screenshot_service is not None
    svc.screenshot_service._provider = fake  # noqa: SLF001
    result = await svc.prepare_content(
        "living_room",
        "Avatar: The Last Airbender",
        provider="netflix",
        goal="search_ready",
    )
    assert result.terminal_status.value == "query_verified"
    assert result.selected_result is False
    assert result.playback_started is False
    selects = [
        m for m in apple.mutations if m["action"] == "press_key" and m.get("key") == "select"
    ]
    assert len(selects) == 1
    assert any(m["action"] == "enter_text" for m in apple.mutations)


@pytest.mark.asyncio
async def test_existing_exact_query_stops_without_actions() -> None:
    actions: list[dict] = []

    async def observe() -> ProviderObservation:
        return ProviderObservation(
            screenshot_state=ProviderState.SEARCH_RESULTS.value,
            confidence=0.9,
            screenshot_anchors=[
                "netflix.chrome.keyboard",
                "netflix.chrome.query_visible",
            ],
            keyboard_focus=True,
            keyboard_text="Avatar: The Last Airbender",
        )

    async def execute(action: dict) -> None:
        actions.append(action)

    adapter = NetflixAdapter()
    raw = await ProviderRecipeRunner(observe=observe, execute=execute).run_search_ready(
        room_key="living_room",
        title="Avatar: The Last Airbender",
        transitions=adapter.plan_search_ready(
            "Avatar: The Last Airbender",
            profile_index=2,
            profile_name="primary",
        ),
        classify=adapter.classify,
        required_profile_name="primary",
    )
    assert raw["terminal_status"] == "query_verified"
    assert raw["stages"][0]["name"] == "existing_query_verified"
    assert actions == []


@pytest.mark.asyncio
async def test_later_query_mismatch_clears_prior_match() -> None:
    observations = iter(
        [
            ProviderObservation(
                screenshot_state=ProviderState.HOME.value,
                confidence=0.9,
                screenshot_anchors=["netflix.chrome.top_nav"],
            ),
            ProviderObservation(
                screenshot_state=ProviderState.SEARCH_KEYBOARD.value,
                confidence=0.9,
                screenshot_anchors=["netflix.chrome.keyboard"],
                keyboard_focus=True,
                keyboard_text="Avatar",
            ),
            ProviderObservation(
                screenshot_state=ProviderState.SEARCH_KEYBOARD.value,
                confidence=0.9,
                screenshot_anchors=["netflix.chrome.keyboard"],
                keyboard_focus=True,
                keyboard_text="Wrong title",
            ),
            ProviderObservation(
                screenshot_state=ProviderState.SEARCH_KEYBOARD.value,
                confidence=0.9,
                screenshot_anchors=["netflix.chrome.keyboard"],
                keyboard_focus=True,
                keyboard_text="Wrong title",
            ),
        ]
    )

    async def observe() -> ProviderObservation:
        return next(observations)

    async def execute(action: dict) -> None:
        _ = action

    transitions = [
        TransitionSpec(
            name="first",
            allowed_from=[ProviderState.HOME],
            actions=[{"type": "press_key", "key": "down"}],
            expected_to=ProviderState.SEARCH_KEYBOARD,
            timeout_s=2.0,
        ),
        TransitionSpec(
            name="later",
            allowed_from=[ProviderState.SEARCH_KEYBOARD],
            actions=[{"type": "keyboard_readback"}],
            expected_to=ProviderState.SEARCH_KEYBOARD,
            timeout_s=2.0,
        ),
    ]
    raw = await ProviderRecipeRunner(observe=observe, execute=execute).run_search_ready(
        room_key="living_room",
        title="Avatar",
        transitions=transitions,
        classify=NetflixAdapter().classify,
    )
    assert raw["terminal_status"] != "query_verified"
    assert raw["verification_status"] != "verified"


def test_cli_raw_remote_gate_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.delenv("HOME_MEDIA_ENABLE_RAW_REMOTE", raising=False)
    blocked = runner.invoke(
        cli_app,
        ["--json", "--fakes", "remote", "press", "--room", "living_room", "--key", "select"],
    )
    assert blocked.exit_code == 2
    assert "raw_remote_disabled" in blocked.stdout

    blocked_text = runner.invoke(
        cli_app,
        ["--json", "--fakes", "text", "enter", "--room", "living_room", "--text", "hi"],
    )
    assert blocked_text.exit_code == 2
    assert "raw_remote_disabled" in blocked_text.stdout

    monkeypatch.setenv("HOME_MEDIA_ENABLE_RAW_REMOTE", "1")
    # Dry-run still exercises the gate-open path without requiring paired live devices.
    ok = runner.invoke(
        cli_app,
        [
            "--json",
            "--fakes",
            "remote",
            "press",
            "--room",
            "living_room",
            "--key",
            "up",
            "--dry-run",
        ],
    )
    assert ok.exit_code == 0


def test_cli_run_closes_registered_service() -> None:
    class StubService:
        def __init__(self) -> None:
            self.closed = 0

        async def aclose(self) -> None:
            self.closed += 1

    stub = StubService()
    cli_module._active_services.append(stub)  # type: ignore[arg-type]  # noqa: SLF001

    async def value() -> int:
        return 7

    assert cli_module._run(value()) == 7  # noqa: SLF001
    assert stub.closed == 1
    assert cli_module._active_services == []  # noqa: SLF001


def test_helper_source_packaging_lookup() -> None:
    path = helper_source_path()
    assert path is not None
    assert path.name == "vision_ocr.swift"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "import Vision" in text
    assert "VNRecognizeTextRequest" in text


def test_parse_ocr_payload_roundtrip() -> None:
    doc = parse_ocr_payload(
        '{"tokens":[{"text":"Home","x":0.1,"y":0.2,"w":0.05,"h":0.04}],"width":100,"height":50}'
    )
    assert len(doc.tokens) == 1
    assert doc.tokens[0].text == "Home"
    assert doc.width == 100


def test_wheel_includes_vision_ocr_source(tmp_path: Path) -> None:
    import subprocess

    dist = tmp_path / "dist"
    dist.mkdir()
    completed = subprocess.run(  # noqa: S603
        ["uv", "build", "--wheel", "--out-dir", str(dist)],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    wheels = list(dist.glob("*.whl"))
    assert wheels
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    assert any(n.endswith("observers/native/vision_ocr.swift") for n in names), names


@pytest.mark.asyncio
async def test_fake_label_classifier_sha_path() -> None:
    fake = FakeScreenshotProvider({LIVING: fixture_for_state(ProviderState.HOME)})
    shot = await fake.capture(LIVING, "living_room")
    clf = FakeLabelClassifier(fake.label_for_sha)
    result = await clf.classify(shot)
    assert result.provider_state == ProviderState.HOME
    assert result.classifier_name == "fake_sha_label"
    public = result.public_log_fields()
    assert "png" not in public
    assert public.get("highlighted_profile_present") is False
