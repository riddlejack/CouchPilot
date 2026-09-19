"""Regression tests for the 15 audit findings and Netflix transition safety."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from home_media.config import create_private_file
from home_media.content.router import ContentGoal, ContentRoute, plan_content_routes
from home_media.content.urls import netflix_title_url_candidates, validate_content_url
from home_media.errors import (
    AmbiguousOutcomeError,
    ConfigError,
    IdempotencyConflictError,
    MutationsDisabledError,
    SafetyBlockedError,
    UnsupportedError,
)
from home_media.providers.base import ProviderState, TransitionSpec
from home_media.providers.netflix import (
    NetflixAdapter,
    assert_transition_allowed,
    is_select_action,
)
from home_media.service import ApplicationService


def test_fail_closed_config_without_fakes(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    with pytest.raises(ConfigError):
        ApplicationService.from_config_path(missing, use_fakes=False)


def test_live_config_rejects_group_or_world_permissions(tmp_path: Path) -> None:
    path = tmp_path / "homes.yaml"
    path.write_text("schema_version: 1\nrooms: []\ndevices: []\n", encoding="utf-8")
    os.chmod(path, 0o644)
    with pytest.raises(ConfigError, match="0600"):
        ApplicationService.from_config_path(path, use_fakes=False)


def test_create_private_file_is_exclusive_mode_0600_and_rejects_unsafe_existing(
    tmp_path: Path,
) -> None:
    private = create_private_file(tmp_path / "private.json")
    assert oct(private.stat().st_mode & 0o777) == "0o600"

    unsafe = tmp_path / "unsafe.json"
    unsafe.write_text("", encoding="utf-8")
    os.chmod(unsafe, 0o644)
    with pytest.raises(ConfigError, match="0600"):
        create_private_file(unsafe)
    assert oct(unsafe.stat().st_mode & 0o777) == "0o644"

    link = tmp_path / "private-link.json"
    link.symlink_to(private)
    with pytest.raises(ConfigError, match="symlink"):
        create_private_file(link)


def test_fakes_opt_in_uses_sanitized_seed_not_private_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME_MEDIA_CONFIG", str(tmp_path / "private-default-must-not-load.yaml"))
    svc = ApplicationService.from_config_path(use_fakes=True)
    assert svc.registry.config.home_name == "primary"
    assert svc.registry.room("theater").apple_tv_id == "00000000-0000-4000-8000-000000000001"


@pytest.mark.asyncio
async def test_kill_switch_blocks_direct_semantic_and_pairing_paths() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    svc.registry.config.mutations_enabled = False
    svc.executor.mutations_enabled = False
    apple = svc.adapters["apple_tv"]

    with pytest.raises(MutationsDisabledError):
        await svc.prepare_content(
            "living_room",
            "Avatar",
            provider="netflix",
            goal="search_ready",
        )
    with pytest.raises(MutationsDisabledError):
        await svc.start_pairing("family_room")
    with pytest.raises(MutationsDisabledError):
        await svc.finish_pairing("not-started", "not-a-real-pin")

    assert apple.mutations == []
    assert apple.sessions == {}


@pytest.mark.asyncio
async def test_kill_switch_still_allows_prepare_dry_run() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    svc.registry.config.mutations_enabled = False
    svc.executor.mutations_enabled = False
    result = await svc.prepare_content(
        "living_room",
        "Avatar",
        provider="netflix",
        goal="search_ready",
        dry_run=True,
    )
    assert result.stages[0].name == "dry_run"
    assert svc.adapters["apple_tv"].mutations == []


def test_netflix_url_allowlist() -> None:
    https = validate_content_url("https://www.netflix.com/title/70142405")
    assert https.content_id == "70142405"
    assert https.may_start_playback is False
    nflx = validate_content_url("nflx://www.netflix.com/title/70142405")
    assert nflx.url_form == "nflx"
    with pytest.raises(UnsupportedError):
        validate_content_url("https://www.netflix.com/watch/70142405")
    with pytest.raises(UnsupportedError):
        validate_content_url("ftp://www.netflix.com/title/70142405")
    with pytest.raises(UnsupportedError):
        validate_content_url("airplay://stream")
    assert netflix_title_url_candidates("70142405")[0].startswith("https://")


def test_netflix_routes_avoid_apple_search_primary() -> None:
    plan = plan_content_routes(
        "Avatar",
        "netflix",
        ContentGoal.SEARCH_READY,
        apple_search_participates=False,
        has_verified_deep_link=False,
    )
    assert plan.routes[0] == ContentRoute.PROVIDER_STATE_MACHINE
    assert ContentRoute.APPLE_SYSTEM_SEARCH in plan.routes
    assert plan.routes.index(ContentRoute.APPLE_SYSTEM_SEARCH) > 0


def test_select_forbidden_from_unknown() -> None:
    spec = TransitionSpec(
        name="select_profile_2",
        allowed_from=[ProviderState.PROFILE_PICKER],
        actions=[{"type": "select_profile", "profile_index": 2, "press": "select"}],
        expected_to=ProviderState.HOME,
        may_start_playback=False,
    )
    with pytest.raises(SafetyBlockedError) as exc:
        assert_transition_allowed(spec, ProviderState.UNKNOWN)
    assert exc.value.details["reason"] == "select_from_unknown"
    assert is_select_action({"type": "select"})


def test_profile_select_only_from_picker() -> None:
    adapter = NetflixAdapter()
    specs = adapter.plan_search_ready("Avatar", profile_index=2)
    profile = next(s for s in specs if s.name.startswith("select_profile"))
    with pytest.raises(SafetyBlockedError):
        assert_transition_allowed(profile, ProviderState.HOME)
    assert_transition_allowed(profile, ProviderState.PROFILE_PICKER)


@pytest.mark.asyncio
async def test_relative_volume_respects_ceiling() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    await svc.set_volume("theater", 38)
    with pytest.raises(SafetyBlockedError) as exc:
        await svc.change_volume("theater", 5)
    assert exc.value.details["reason"] == "volume_ceiling"
    result = await svc.change_volume("theater", 5, override_ceiling=True)
    assert result.execution_status.value == "succeeded"


@pytest.mark.asyncio
async def test_idempotency_fingerprint_conflict() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    await svc.open_app("theater", "netflix", idempotency_key="k1")
    with pytest.raises(IdempotencyConflictError):
        await svc.open_app("theater", "youtube", idempotency_key="k1")


@pytest.mark.asyncio
async def test_idempotency_same_request_once() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    apple = svc.adapters["apple_tv"]
    r1 = await svc.open_app("theater", "netflix", idempotency_key="same")
    count = len([m for m in apple.mutations if m["action"] == "open_app"])
    r2 = await svc.open_app("theater", "netflix", idempotency_key="same")
    count2 = len([m for m in apple.mutations if m["action"] == "open_app"])
    assert r1.action_id == r2.action_id
    assert count2 == count


@pytest.mark.asyncio
async def test_persistent_runtime_idempotency_state_is_bounded() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    svc.executor.max_idempotency_entries = 2
    for index in range(3):
        await svc.open_app("theater", "netflix", idempotency_key=f"bounded-{index}")
    assert len(svc.executor._idempotency) <= 2  # noqa: SLF001

    svc._prepare_idempotency_max_entries = 2  # noqa: SLF001
    for index in range(3):
        await svc.prepare_content(
            "living_room",
            f"Avatar {index}",
            provider="netflix",
            goal="search_ready",
            dry_run=True,
            idempotency_key=f"prepare-bounded-{index}",
        )
    assert len(svc._prepare_touched_at) <= 2  # noqa: SLF001


@pytest.mark.asyncio
async def test_concurrent_idempotency_single_flight() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    apple = svc.adapters["apple_tv"]
    real = apple.open_app

    async def slow(device_id: str, app_id: str):
        await asyncio.sleep(0.05)
        return await real(device_id, app_id)

    apple.open_app = slow  # type: ignore[method-assign]
    await asyncio.gather(
        svc.open_app("theater", "netflix", idempotency_key="flight"),
        svc.open_app("theater", "netflix", idempotency_key="flight"),
    )
    opens = [m for m in apple.mutations if m["action"] == "open_app"]
    assert len(opens) == 1


@pytest.mark.asyncio
async def test_ambiguous_post_send_does_not_redispatch() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    apple = svc.adapters["apple_tv"]
    calls = {"n": 0}
    real = apple.open_app

    async def once_then_timeout(device_id: str, app_id: str):
        from home_media.errors import TimeoutError_

        calls["n"] += 1
        if calls["n"] == 1:
            await real(device_id, app_id)
            raise TimeoutError_("post-send timeout")
        return await real(device_id, app_id)

    apple.open_app = once_then_timeout  # type: ignore[method-assign]
    result = await svc.open_app("theater", "netflix")
    assert calls["n"] == 1
    assert result.execution_status.value in {"succeeded", "partial", "failed"}


@pytest.mark.asyncio
async def test_prepare_content_failure_tombstones_idempotency_key() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    apple = svc.adapters["apple_tv"]
    calls = 0
    real = apple.open_url

    async def sent_then_timeout(device_id: str, url: str):
        nonlocal calls
        from home_media.errors import TimeoutError_

        calls += 1
        await real(device_id, url)
        raise TimeoutError_("post-send timeout")

    apple.open_url = sent_then_timeout  # type: ignore[method-assign]
    kwargs = {
        "room_name": "living_room",
        "title": "Avatar",
        "provider": "netflix",
        "goal": "title_open",
        "url": "https://www.netflix.com/title/70142405",
        "idempotency_key": "prepare-ambiguous",
    }
    with pytest.raises(AmbiguousOutcomeError):
        await svc.prepare_content(**kwargs)
    with pytest.raises(AmbiguousOutcomeError):
        await svc.prepare_content(**kwargs)
    assert calls == 1


@pytest.mark.asyncio
async def test_prepare_content_cancellation_after_mutation_cleans_inflight_key() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    mutation_started = asyncio.Event()

    async def cancelled_after_send(**kwargs):  # noqa: ANN003, ANN202
        kwargs["mutation_state"]["attempted"] = True
        mutation_started.set()
        await asyncio.Event().wait()

    svc._prepare_content_unlocked = cancelled_after_send  # type: ignore[method-assign]  # noqa: SLF001,E501
    kwargs = {
        "room_name": "living_room",
        "title": "Avatar",
        "provider": "netflix",
        "goal": "title_open",
        "idempotency_key": "prepare-cancelled",
    }
    task = asyncio.create_task(svc.prepare_content(**kwargs))
    await mutation_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "prepare-cancelled" not in svc._prepare_inflight  # noqa: SLF001
    assert "prepare-cancelled" in svc._prepare_failed  # noqa: SLF001
    with pytest.raises(AmbiguousOutcomeError):
        await svc.prepare_content(**kwargs)


@pytest.mark.asyncio
async def test_prepare_content_pre_send_failure_is_retryable_with_same_key() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    kwargs = {
        "room_name": "living_room",
        "title": "Avatar",
        "goal": "title_open",
        "url": "file:///not-an-allowed-content-route",
        "idempotency_key": "prepare-pre-send-invalid",
    }
    for _ in range(2):
        with pytest.raises(UnsupportedError):
            await svc.prepare_content(**kwargs)
    assert "prepare-pre-send-invalid" not in svc._prepare_failed  # noqa: SLF001
    assert svc.adapters["apple_tv"].mutations == []


@pytest.mark.asyncio
async def test_room_status_preserves_device_errors() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    status = await svc.get_room_status("family_room")
    assert status.apple_tv is None
    assert "apple_tv" in status.device_errors
    assert status.device_errors["apple_tv"]["error"]["error"] == "auth_required"


@pytest.mark.asyncio
async def test_prepare_content_search_ready_no_select() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    apple = svc.adapters["apple_tv"]
    living = "00000000-0000-4000-8000-000000000004"
    apple.set_provider_state(living, "home")
    result = await svc.prepare_content(
        "living_room",
        "Avatar: The Last Airbender",
        provider="netflix",
        goal="search_ready",
    )
    assert result.selected_result is False
    assert result.playback_started is False
    assert result.terminal_status.value != "playing"
    assert result.total_latency_ms is not None
    assert result.total_latency_ms >= 0
    assert all(stage.latency_ms is not None for stage in result.stages)
    selects = [
        m
        for m in apple.mutations
        if m["action"] == "press_key" and m.get("key") == "select"
    ]
    assert selects == []
    assert result.route_used == ContentRoute.PROVIDER_STATE_MACHINE


@pytest.mark.asyncio
async def test_prepare_content_profile_select_only_when_picker() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    prefs = svc.registry.config.provider_prefs.setdefault("netflix", {})
    prefs["profile_name"] = "primary"
    apple = svc.adapters["apple_tv"]
    living = "00000000-0000-4000-8000-000000000004"
    apple.set_provider_state(living, "profile_picker")
    apple.highlighted_profile[living] = "primary"
    result = await svc.prepare_content(
        "living_room",
        "Avatar",
        provider="netflix",
        goal="search_ready",
        profile_index=2,
    )
    selects = [
        m
        for m in apple.mutations
        if m["action"] == "press_key" and m.get("key") == "select"
    ]
    assert len(selects) >= 1
    assert result.selected_result is False


@pytest.mark.asyncio
async def test_content_verify_rejects_wrong_app() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    apple = svc.adapters["apple_tv"]

    async def wrong_app(device_id: str, url: str):
        await apple.__class__.open_url(apple, device_id, url)
        data = apple.devices[device_id]
        data["current_app"] = "com.google.ios.youtube"
        return await apple.get_status(device_id)

    apple.open_url = wrong_app  # type: ignore[method-assign]
    result = await svc.open_content(
        "theater",
        url="https://www.netflix.com/title/70142405",
        alias=None,
    )
    assert result.verification_status.value == "failed"


@pytest.mark.asyncio
async def test_tv_input_never_verified_from_echo() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    result = await svc.set_tv_input("theater", "hdmi1")
    step = result.steps[-1]
    assert step.observed_after.get("input_source") in {None, "hdmi1"}
    # Fake still echoes; executor requires input_independently_read.
    assert step.verification_status.value in {"unverified", "degraded", "verified"}
    # Force physical adapter path semantics: live adapter clears input_source.
    from home_media.adapters.physical_tv import AndroidTVAdapter

    # Ensure verification path treats missing independent read as unverified.
    from home_media.executor import Executor

    ex = Executor({})
    status = ex._verify(  # noqa: SLF001
        "set_input",
        {"source": "hdmi1"},
        {},
        {"input_source": "hdmi1"},  # echoed without independent flag
    )
    assert status.value == "unverified"
    _ = AndroidTVAdapter
