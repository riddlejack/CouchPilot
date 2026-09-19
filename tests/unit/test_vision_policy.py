"""Fast Codex vision policy contract tests."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from home_media.errors import SafetyBlockedError
from home_media.vision_policy import (
    CodexVisionPolicy,
    ScreenSurface,
    VisionContext,
    VisionRemoteAction,
    is_playback_cta,
)


def _context() -> VisionContext:
    return VisionContext(
        room_key="living_room",
        provider="netflix",
        requested_title="Avatar: The Last Airbender",
        goal="resume_then_pause",
    )


def _decision(**overrides: object) -> str:
    value: dict[str, object] = {
        "surface": "search",
        "confidence": 0.93,
        "title_match": "exact",
        "focused_target": "Avatar: The Last Airbender",
        "focused_kind": "title_result",
        "visible_titles": ["Avatar: The Last Airbender"],
        "safe_to_select": True,
        "playback_visible": False,
        "next_action": "select",
        "reason_code": "exact_title_focused",
    }
    value.update(overrides)
    return json.dumps(value)


@pytest.mark.parametrize(
    "label",
    [
        "Play Season 1: Episode 1",
        "Resume Season 2 Episode 4",
        "Resume S1: Ep. 1",
        "Continue Watching",
    ],
)
def test_episode_playback_cta_labels_are_allowed(label: str) -> None:
    assert is_playback_cta(label)


@pytest.mark.asyncio
async def test_policy_forces_fast_low_and_private_structured_output(tmp_path: Path) -> None:
    seen: list[str] = []

    async def runner(
        argv: list[str] | tuple[str, ...], _timeout: float, prompt: str
    ) -> str:
        seen.extend(argv)
        assert _context().requested_title in prompt
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text(_decision(), encoding="utf-8")
        return ""

    policy = CodexVisionPolicy(executable="/opt/codex", runner=runner)
    result = await policy.decide([b"png"], _context())

    assert result.surface == ScreenSurface.SEARCH
    assert result.next_action == VisionRemoteAction.SELECT
    assert 'service_tier="fast"' in seen
    assert 'model_reasoning_effort="low"' in seen
    assert "--ephemeral" in seen
    assert "--ignore-user-config" in seen
    assert "--ignore-rules" in seen
    assert "--cd" in seen
    assert "read-only" in seen
    assert "danger-full-access" not in seen
    assert _context().requested_title not in seen


@pytest.mark.asyncio
async def test_policy_rejects_select_without_exact_visible_focus() -> None:
    async def runner(
        argv: list[str] | tuple[str, ...], _timeout: float, _prompt: str
    ) -> str:
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text(
            _decision(title_match="likely", safe_to_select=False, confidence=0.7),
            encoding="utf-8",
        )
        return ""

    with pytest.raises(SafetyBlockedError) as error:
        await CodexVisionPolicy(executable="/opt/codex", runner=runner).decide(
            [b"png"], _context()
        )
    assert error.value.details["reason"] == "vision_select_unverified"


@pytest.mark.asyncio
async def test_policy_allows_visually_verified_profile_select() -> None:
    async def runner(
        argv: list[str] | tuple[str, ...], _timeout: float, _prompt: str
    ) -> str:
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text(
            _decision(
                surface="profile_picker",
                title_match="unknown",
                focused_target="primary",
                focused_kind="profile",
                visible_titles=[],
                reason_code="target_profile_focused",
            ),
            encoding="utf-8",
        )
        return ""

    context = _context().model_copy(update={"desired_profile": "primary"})
    result = await CodexVisionPolicy(executable="/opt/codex", runner=runner).decide(
        [b"png"], context
    )
    assert result.next_action == VisionRemoteAction.SELECT


@pytest.mark.asyncio
async def test_policy_rejects_exact_claim_for_wrong_visible_title() -> None:
    async def runner(
        argv: list[str] | tuple[str, ...], _timeout: float, _prompt: str
    ) -> str:
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text(
            _decision(
                focused_target="Avatar: The Way of Water",
                visible_titles=["Avatar: The Way of Water"],
            ),
            encoding="utf-8",
        )
        return ""

    with pytest.raises(SafetyBlockedError) as error:
        await CodexVisionPolicy(executable="/opt/codex", runner=runner).decide(
            [b"png"], _context()
        )
    assert error.value.details["reason"] == "vision_title_mismatch"


@pytest.mark.asyncio
async def test_policy_rejects_wrong_profile_even_when_model_calls_it_safe() -> None:
    async def runner(
        argv: list[str] | tuple[str, ...], _timeout: float, _prompt: str
    ) -> str:
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text(
            _decision(
                surface="profile_picker",
                title_match="unknown",
                focused_target="Kids",
                focused_kind="profile",
                visible_titles=[],
                reason_code="profile_focused",
            ),
            encoding="utf-8",
        )
        return ""

    context = _context().model_copy(update={"desired_profile": "primary"})
    with pytest.raises(SafetyBlockedError) as error:
        await CodexVisionPolicy(executable="/opt/codex", runner=runner).decide(
            [b"png"], context
        )
    assert error.value.details["reason"] == "vision_select_unverified"


@pytest.mark.asyncio
async def test_policy_rejects_play_pause_outside_resume_playback() -> None:
    async def runner(
        argv: list[str] | tuple[str, ...], _timeout: float, _prompt: str
    ) -> str:
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text(
            _decision(
                surface="title_detail",
                focused_kind="playback_cta",
                next_action="play_pause",
                safe_to_select=False,
            ),
            encoding="utf-8",
        )
        return ""

    context = _context().model_copy(update={"goal": "title_open"})
    with pytest.raises(SafetyBlockedError) as error:
        await CodexVisionPolicy(executable="/opt/codex", runner=runner).decide(
            [b"png"], context
        )
    assert error.value.details["reason"] == "vision_play_pause_unverified"


@pytest.mark.asyncio
async def test_policy_rejects_unrelated_title_detail_control() -> None:
    async def runner(
        argv: list[str] | tuple[str, ...], _timeout: float, _prompt: str
    ) -> str:
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text(
            _decision(
                surface="title_detail",
                focused_target="More Like This",
                focused_kind="other",
            ),
            encoding="utf-8",
        )
        return ""

    with pytest.raises(SafetyBlockedError) as error:
        await CodexVisionPolicy(executable="/opt/codex", runner=runner).decide(
            [b"png"], _context()
        )
    assert error.value.details["reason"] == "vision_select_unverified"


@pytest.mark.asyncio
async def test_policy_allows_exact_paused_overlay_play_select() -> None:
    async def runner(
        argv: list[str] | tuple[str, ...], _timeout: float, _prompt: str
    ) -> str:
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text(
            _decision(
                surface="playback",
                focused_target="Play",
                focused_kind="playback_cta",
                playback_visible=True,
                reason_code="exact_paused_overlay_play_focused",
            ),
            encoding="utf-8",
        )
        return ""

    context = _context().model_copy(
        update={
            "current_app": "com.netflix.Netflix",
            "now_playing_state": "paused",
            "now_playing_title": "Avatar: The Last Airbender",
        }
    )
    result = await CodexVisionPolicy(executable="/opt/codex", runner=runner).decide(
        [b"png"], context
    )

    assert result.surface == ScreenSurface.PLAYBACK
    assert result.next_action == VisionRemoteAction.SELECT


@pytest.mark.asyncio
async def test_policy_rejects_wrong_title_paused_overlay_play_select() -> None:
    async def runner(
        argv: list[str] | tuple[str, ...], _timeout: float, _prompt: str
    ) -> str:
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text(
            _decision(
                surface="playback",
                focused_target="Play",
                focused_kind="playback_cta",
                playback_visible=True,
                visible_titles=["Sonic X"],
                reason_code="wrong_title_paused_overlay",
            ),
            encoding="utf-8",
        )
        return ""

    context = _context().model_copy(
        update={
            "current_app": "com.netflix.Netflix",
            "now_playing_state": "paused",
        }
    )
    with pytest.raises(SafetyBlockedError) as error:
        await CodexVisionPolicy(executable="/opt/codex", runner=runner).decide(
            [b"png"], context
        )

    assert error.value.details["reason"] == "vision_title_mismatch"


@pytest.mark.asyncio
async def test_policy_rejects_low_confidence_directional_action() -> None:
    async def runner(
        argv: list[str] | tuple[str, ...], _timeout: float, _prompt: str
    ) -> str:
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text(
            _decision(
                confidence=0.4,
                title_match="unknown",
                visible_titles=[],
                focused_target=None,
                focused_kind="none",
                safe_to_select=False,
                next_action="right",
            ),
            encoding="utf-8",
        )
        return ""

    with pytest.raises(SafetyBlockedError) as error:
        await CodexVisionPolicy(executable="/opt/codex", runner=runner).decide(
            [b"png"], _context()
        )
    assert error.value.details["reason"] == "vision_action_low_confidence"


@pytest.mark.asyncio
async def test_policy_rejects_missing_frame_before_model_call() -> None:
    called = False

    async def runner(
        _argv: list[str] | tuple[str, ...], _timeout: float, _prompt: str
    ) -> str:
        nonlocal called
        called = True
        return ""

    with pytest.raises(SafetyBlockedError) as error:
        await CodexVisionPolicy(executable="/opt/codex", runner=runner).decide([], _context())
    assert error.value.details["reason"] == "vision_frame_missing"
    assert called is False


@pytest.mark.asyncio
async def test_policy_uses_only_four_most_recent_frames() -> None:
    image_args: list[str] = []

    async def runner(
        argv: list[str] | tuple[str, ...], _timeout: float, _prompt: str
    ) -> str:
        start = argv.index("-i") + 1
        image_args.extend(argv[start:-1])
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text(_decision(next_action="none", safe_to_select=False), encoding="utf-8")
        return ""

    await CodexVisionPolicy(executable="/opt/codex", runner=runner).decide(
        [b"1", b"2", b"3", b"4", b"5"], _context()
    )
    assert len(image_args) == 4


@pytest.mark.asyncio
async def test_policy_downscales_large_frames_before_codex_upload() -> None:
    source = io.BytesIO()
    Image.new("RGB", (3840, 2160), (22, 33, 44)).save(source, format="PNG")
    seen_size: tuple[int, int] | None = None

    async def runner(
        argv: list[str] | tuple[str, ...], _timeout: float, _prompt: str
    ) -> str:
        nonlocal seen_size
        image_path = Path(argv[argv.index("-i") + 1])
        with Image.open(image_path) as uploaded:
            seen_size = uploaded.size
        assert image_path.suffix == ".jpg"
        assert image_path.stat().st_size < 1_000_000
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text(
            _decision(next_action="none", safe_to_select=False),
            encoding="utf-8",
        )
        return ""

    await CodexVisionPolicy(executable="/opt/codex", runner=runner).decide(
        [source.getvalue()], _context()
    )
    assert seen_size == (1280, 720)
