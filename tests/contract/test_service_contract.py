"""Contract tests for ApplicationService with fakes."""

from __future__ import annotations

import asyncio

import pytest

from home_media.errors import AuthRequiredError, ErrorCode
from home_media.models import ExecutionStatus, VerificationStatus
from home_media.service import ApplicationService


@pytest.fixture
def svc() -> ApplicationService:
    return ApplicationService.from_config_path(use_fakes=True)


@pytest.mark.asyncio
async def test_discover_filters_noise(svc: ApplicationService) -> None:
    result = await svc.discover()
    assert result["apple_tv_count"] >= 1
    assert "endpoints" not in result  # private addresses omitted by default
    names = [e.get("name") for e in result["devices"] if isinstance(e, dict)]
    assert "Personal MacBook Pro" not in names


@pytest.mark.asyncio
async def test_discover_private_inventory_opt_in(svc: ApplicationService) -> None:
    result = await svc.discover(include_private_inventory=True)
    assert "endpoints" in result
    assert any(e.get("address") for e in result["endpoints"] if isinstance(e, dict))


@pytest.mark.asyncio
async def test_theater_status_three_devices(svc: ApplicationService) -> None:
    status = await svc.get_room_status("theater")
    assert status.apple_tv is not None
    assert status.physical_tv is not None
    assert status.audio is not None
    assert status.apple_tv.device_id != status.physical_tv.device_id
    assert status.audio.device_id == "sonos-theater-beam"


@pytest.mark.asyncio
async def test_volume_set_verified(svc: ApplicationService) -> None:
    result = await svc.set_volume("theater", 22)
    assert result.execution_status == ExecutionStatus.SUCCEEDED
    assert result.verification_status == VerificationStatus.VERIFIED
    vol = await svc.get_volume("theater")
    assert vol["level"] == 22


@pytest.mark.asyncio
async def test_wrong_room_does_not_fallback(svc: ApplicationService) -> None:
    from home_media.errors import UnknownRoomError

    with pytest.raises(UnknownRoomError):
        await svc.open_app("not-a-room", "netflix")


@pytest.mark.asyncio
async def test_unpaired_status_is_auth_error() -> None:
    svc = ApplicationService.from_config_path(use_fakes=True)
    # Family Room seeded unpaired — status must not crash the service
    await svc.get_room_status("family_room")
    rooms = await svc.list_rooms()
    assert any(r["key"] == "family_room" for r in rooms)
    apple = svc.adapters["apple_tv"]
    with pytest.raises(AuthRequiredError):
        await apple.get_status("00000000-0000-4000-8000-000000000002")


@pytest.mark.asyncio
async def test_retry_on_transient_then_success(svc: ApplicationService) -> None:
    apple = svc.adapters["apple_tv"]
    apple.fail_next = "status"
    # get_status is used as before-observation; mutation should still succeed
    result = await svc.open_app("theater", "netflix")
    assert result.execution_status == ExecutionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_stale_endpoint_retryable_code() -> None:
    from home_media.errors import StaleEndpointError

    svc = ApplicationService.from_config_path(use_fakes=True)
    apple = svc.adapters["apple_tv"]
    apple.stale_ids.add("00000000-0000-4000-8000-000000000001")
    with pytest.raises(StaleEndpointError) as exc:
        await apple.get_status("00000000-0000-4000-8000-000000000001")
    assert exc.value.code == ErrorCode.STALE_ENDPOINT
    assert exc.value.retryable


@pytest.mark.asyncio
async def test_partial_scene_on_step_failure(svc: ApplicationService) -> None:
    # Break sonos volume mid-scene by removing zone after planning path — inject via adapter
    sonos = svc.adapters["sonos"]
    original = sonos.set_volume

    async def boom(device_id: str, level: int):
        from home_media.errors import NetworkError

        raise NetworkError("speaker offline", retryable=False)

    sonos.set_volume = boom  # type: ignore[method-assign]
    result = await svc.execute_watch_scene("theater", service="netflix", volume=20)
    assert result.execution_status in {ExecutionStatus.PARTIAL, ExecutionStatus.FAILED}
    assert any(
        s.action == "open_app" and s.execution_status == ExecutionStatus.SUCCEEDED
        for s in result.steps
    )
    sonos.set_volume = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_per_room_lock_serializes_mutations(svc: ApplicationService) -> None:
    apple = svc.adapters["apple_tv"]
    started: list[str] = []
    real = apple.open_app

    async def wrapped(device_id: str, app_id: str):
        started.append("start")
        await asyncio.sleep(0.05)
        result = await real(device_id, app_id)
        started.append("end")
        return result

    apple.open_app = wrapped  # type: ignore[method-assign]
    await asyncio.gather(
        svc.open_app("theater", "netflix"),
        svc.open_app("theater", "youtube"),
    )
    # Serialized: start,end,start,end — not start,start,end,end
    assert started == ["start", "end", "start", "end"]


@pytest.mark.asyncio
async def test_pairing_does_not_echo_pin(svc: ApplicationService) -> None:
    session = await svc.start_pairing("family_room", protocol="companion")
    finished = await svc.finish_pairing(session.session_id, "1234")
    dumped = finished.model_dump_json()
    assert "1234" not in dumped
    assert finished.state == "completed"
