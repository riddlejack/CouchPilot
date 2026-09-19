"""Process-scoped hub lifecycle and health contracts."""

from __future__ import annotations

import asyncio

import pytest

from home_media.hub import HomeMediaHub, HubStatus
from home_media.service import ApplicationService


async def test_hub_singleton_health_reload_and_shutdown() -> None:
    built: list[ApplicationService] = []

    def factory() -> ApplicationService:
        service = ApplicationService.from_config_path(use_fakes=True)
        built.append(service)
        return service

    hub = HomeMediaHub(factory)
    first = await hub.start()
    assert await hub.start() is first
    assert len(built) == 1

    health = await hub.health()
    assert health.status == HubStatus.HEALTHY
    assert health.generation == 1
    assert health.room_count == 6
    assert health.mutations_enabled is True
    assert set(health.adapters) == {"android_tv", "apple_tv", "sonos"}
    assert "address" not in health.model_dump_json()

    reloaded = await hub.reload()
    assert reloaded.generation == 2
    assert hub.service is built[1]
    assert hub.service is not first

    await hub.aclose()
    stopped = await hub.health()
    assert stopped.status == HubStatus.STOPPED


async def test_hub_context_manager_closes() -> None:
    hub = HomeMediaHub.from_config_path(use_fakes=True)
    async with hub as running:
        assert running.service is hub.service
        assert (await running.health()).status == HubStatus.HEALTHY
    assert (await hub.health()).status == HubStatus.STOPPED


async def test_reload_drains_inflight_request_before_swap() -> None:
    built: list[ApplicationService] = []

    def factory() -> ApplicationService:
        service = ApplicationService.from_config_path(use_fakes=True)
        built.append(service)
        return service

    hub = HomeMediaHub(factory)
    await hub.start()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def held_call(service: ApplicationService) -> int:
        assert service is built[0]
        entered.set()
        await release.wait()
        return 1

    request = asyncio.create_task(hub.call(held_call))
    await entered.wait()
    reload_task = asyncio.create_task(hub.reload())
    await asyncio.sleep(0)
    assert not reload_task.done()
    release.set()
    assert await request == 1
    health = await reload_task
    assert health.generation == 2
    assert hub.service is built[1]
    await hub.aclose()


async def test_hub_rejects_calls_after_shutdown() -> None:
    hub = HomeMediaHub.from_config_path(use_fakes=True)
    await hub.start()
    await hub.aclose()
    with pytest.raises(RuntimeError, match="not accepting"):
        await hub.call(lambda svc: svc.list_rooms())
