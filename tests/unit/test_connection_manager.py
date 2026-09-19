"""Connection manager reuse and shutdown-race contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pyatv
import pytest
from pyatv.interface import AppleTV, BaseConfig
from pyatv.storage.file_storage import FileStorage

from home_media.connection import AppleTVConnectionManager
from home_media.errors import NetworkError


class FakeAppleTVConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> set[asyncio.Task[Any]]:
        self.closed = True
        return set()


@pytest.mark.asyncio
async def test_connection_reuse_and_shutdown_drains_active_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = cast(BaseConfig, SimpleNamespace())
    connection = FakeAppleTVConnection()
    scans = 0
    connects = 0

    async def scan(*args: Any, **kwargs: Any) -> list[BaseConfig]:
        nonlocal scans
        _ = args, kwargs
        scans += 1
        return [config]

    async def connect(*args: Any, **kwargs: Any) -> AppleTV:
        nonlocal connects
        _ = args, kwargs
        connects += 1
        return cast(AppleTV, connection)

    monkeypatch.setattr(pyatv, "scan", scan)
    monkeypatch.setattr(pyatv, "connect", connect)
    manager = AppleTVConnectionManager(
        storage=cast(FileStorage, object()),
        scan_timeout=0.1,
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def operation(atv: AppleTV, live: BaseConfig) -> str:
        assert atv is connection
        assert live is config
        entered.set()
        await release.wait()
        return "ok"

    request = asyncio.create_task(manager.with_connection("stable-id", operation))
    await entered.wait()
    close_task = asyncio.create_task(manager.aclose())
    await asyncio.sleep(0)
    assert not close_task.done()
    assert connection.closed is False
    release.set()
    assert await request == "ok"
    await close_task
    assert connection.closed is True
    assert scans == 1
    assert connects == 1

    with pytest.raises(NetworkError, match="closed"):
        await manager.with_connection("stable-id", operation)


@pytest.mark.asyncio
async def test_connection_manager_reuses_cached_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = cast(BaseConfig, SimpleNamespace())
    connection = FakeAppleTVConnection()
    connects = 0

    async def scan(*args: Any, **kwargs: Any) -> list[BaseConfig]:
        _ = args, kwargs
        return [config]

    async def connect(*args: Any, **kwargs: Any) -> AppleTV:
        nonlocal connects
        _ = args, kwargs
        connects += 1
        return cast(AppleTV, connection)

    monkeypatch.setattr(pyatv, "scan", scan)
    monkeypatch.setattr(pyatv, "connect", connect)
    manager = AppleTVConnectionManager(storage=cast(FileStorage, object()))

    async def operation(atv: AppleTV, live: BaseConfig) -> bool:
        return atv is connection and live is config

    assert await manager.with_connection("stable-id", operation)
    assert await manager.with_connection("stable-id", operation)
    assert connects == 1
    assert manager.health_snapshot()["reuses"] == 1
    await manager.aclose()


@pytest.mark.asyncio
async def test_shutdown_drains_standalone_config_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = cast(BaseConfig, SimpleNamespace())
    entered = asyncio.Event()
    release = asyncio.Event()

    async def scan(*args: Any, **kwargs: Any) -> list[BaseConfig]:
        _ = args, kwargs
        entered.set()
        await release.wait()
        return [config]

    monkeypatch.setattr(pyatv, "scan", scan)
    manager = AppleTVConnectionManager(
        storage=cast(FileStorage, object()),
        scan_timeout=1.0,
    )

    resolution = asyncio.create_task(manager.resolve_config("stable-id"))
    await entered.wait()
    close_task = asyncio.create_task(manager.aclose())
    await asyncio.sleep(0)
    assert not close_task.done()
    release.set()
    assert await resolution is config
    await close_task

    with pytest.raises(NetworkError, match="closed"):
        await manager.resolve_config("stable-id")
