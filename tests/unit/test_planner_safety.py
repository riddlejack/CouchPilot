"""Planner and safety tests."""

from __future__ import annotations

import pytest

from home_media.config import theater_seed_config
from home_media.errors import SafetyBlockedError, UnsupportedError
from home_media.planner import Planner
from home_media.registry import RoomRegistry
from home_media.service import ApplicationService


@pytest.fixture
def planner() -> Planner:
    return Planner(RoomRegistry(theater_seed_config()))


def test_volume_ceiling_blocks_without_override(planner: Planner) -> None:
    with pytest.raises(SafetyBlockedError, match="ceiling"):
        planner.plan_volume_set("theater", 55)


def test_volume_ceiling_allows_override(planner: Planner) -> None:
    plan = planner.plan_volume_set("theater", 55, override_ceiling=True)
    assert plan.steps[0].params["level"] == 55
    assert plan.steps[0].target_device_id == "sonos-theater-beam"


def test_exact_volume_routes_to_sonos_not_apple_tv(planner: Planner) -> None:
    plan = planner.plan_volume_set("theater", 20)
    assert plan.steps[0].adapter == "sonos"
    assert plan.steps[0].target_device_id != "00000000-0000-4000-8000-000000000001"


@pytest.mark.asyncio
async def test_power_off_requires_confirmation() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    with pytest.raises(SafetyBlockedError, match="confirm_power_off"):
        await svc.set_power("theater", "off")


@pytest.mark.asyncio
async def test_dry_run_does_not_mutate() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    apple = svc.adapters["apple_tv"]
    before = len(apple.mutations)
    result = await svc.open_app("theater", "netflix", dry_run=True)
    assert result.execution_status.value == "dry_run"
    assert len(apple.mutations) == before


@pytest.mark.asyncio
async def test_idempotency_prevents_duplicate_mutation() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    apple = svc.adapters["apple_tv"]
    r1 = await svc.open_app("theater", "netflix", idempotency_key="k1")
    count = len(apple.mutations)
    r2 = await svc.open_app("theater", "netflix", idempotency_key="k1")
    assert r1.action_id == r2.action_id
    assert len(apple.mutations) == count


@pytest.mark.asyncio
async def test_mutations_disabled_kill_switch() -> None:
    cfg = theater_seed_config()
    cfg.mutations_enabled = False
    from home_media.adapters.fake import FakeAppleTVAdapter, FakePhysicalTVAdapter, FakeSonosAdapter
    from home_media.executor import Executor
    from home_media.registry import RoomRegistry

    reg = RoomRegistry(cfg)
    apple = FakeAppleTVAdapter()
    apple.seed(
        "00000000-0000-4000-8000-000000000001", name="Theater", address="1.1.1.1", paired=True
    )
    adapters = {
        "apple_tv": apple,
        "sonos": FakeSonosAdapter(),
        "android_tv": FakePhysicalTVAdapter(),
    }
    svc = ApplicationService(reg, adapters, executor=Executor(adapters, mutations_enabled=False))
    from home_media.errors import MutationsDisabledError

    with pytest.raises(MutationsDisabledError):
        await svc.open_app("theater", "netflix")


def test_watch_scene_partial_steps(planner: Planner) -> None:
    plan = planner.plan_watch_scene("theater", service="netflix", volume=20)
    actions = [s.action for s in plan.steps]
    assert "set_power" in actions
    assert "open_app" in actions
    assert "set_volume" in actions


def test_cec_exact_volume_unsupported_when_preferred() -> None:
    cfg = theater_seed_config()
    for room in cfg.rooms:
        if room.key == "theater":
            room.preferred_volume_target = "apple_tv_cec"
    planner = Planner(RoomRegistry(cfg))
    with pytest.raises(UnsupportedError):
        planner.plan_volume_set("theater", 20)
