"""Process-scoped runtime owner for a single :class:`ApplicationService`.

The hub deliberately has no transport or listener.  MCP, a future local service,
and restricted clients can host it inside their own supervised process without
creating a second device-control core.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from home_media.models import JSON_SCHEMA_VERSION
from home_media.service import ApplicationService

ServiceFactory = Callable[[], ApplicationService]
T = TypeVar("T")


class HubStatus(StrEnum):
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STOPPED = "stopped"


class HubHealth(BaseModel):
    """Safe process health; never includes addresses or credential paths."""

    schema_version: int = JSON_SCHEMA_VERSION
    status: HubStatus
    generation: int = 0
    started_at: datetime | None = None
    uptime_ms: int = 0
    room_count: int = 0
    mutations_enabled: bool = False
    adapters: dict[str, dict[str, Any]] = Field(default_factory=dict)
    screenshot_observer: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class HomeMediaHub:
    """Own exactly one live service and replace it only through explicit reload."""

    def __init__(self, factory: ServiceFactory) -> None:
        self._factory = factory
        self._service: ApplicationService | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._drained = asyncio.Condition(self._state_lock)
        self._accepting_requests = False
        self._active_requests = 0
        self._status = HubStatus.STOPPED
        self._generation = 0
        self._started_at: datetime | None = None
        self._started_monotonic: float | None = None

    @classmethod
    def from_config_path(
        cls,
        path: Path | None = None,
        *,
        use_fakes: bool = False,
    ) -> HomeMediaHub:
        return cls(
            lambda: ApplicationService.from_config_path(path, use_fakes=use_fakes)
        )

    @property
    def service(self) -> ApplicationService:
        service = self._service
        if service is None:
            raise RuntimeError("HomeMediaHub is not started")
        return service

    async def start(self) -> ApplicationService:
        async with self._lifecycle_lock:
            async with self._state_lock:
                if self._service is not None:
                    return self._service
                self._status = HubStatus.STARTING
            try:
                service = self._factory()
            except Exception:
                async with self._state_lock:
                    self._status = HubStatus.STOPPED
                raise
            async with self._state_lock:
                self._service = service
                self._generation = 1
                self._started_at = datetime.now(UTC)
                self._started_monotonic = time.monotonic()
                self._status = HubStatus.HEALTHY
                self._accepting_requests = True
            return service

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[ApplicationService]:
        """Lease the current generation and drain it before reload/shutdown."""
        async with self._drained:
            if not self._accepting_requests or self._service is None:
                raise RuntimeError("HomeMediaHub is not accepting requests")
            service = self._service
            self._active_requests += 1
        try:
            yield service
        finally:
            async with self._drained:
                self._active_requests -= 1
                if self._active_requests == 0:
                    self._drained.notify_all()

    async def call(self, operation: Callable[[ApplicationService], Awaitable[T]]) -> T:
        async with self.lease() as service:
            return await operation(service)

    async def _stop_accepting_and_drain(self) -> ApplicationService | None:
        async with self._drained:
            self._accepting_requests = False
            while self._active_requests:
                await self._drained.wait()
            return self._service

    async def reload(self) -> HubHealth:
        """Build a fully validated replacement before swapping the live service.

        Reload is intentionally an administrative in-process operation.  It is
        not exposed as an MCP/Siri mutation and never edits the configuration.
        """

        async with self._lifecycle_lock:
            replacement = self._factory()
            previous = await self._stop_accepting_and_drain()
            if previous is None:
                await replacement.aclose()
                raise RuntimeError("HomeMediaHub is not started")
            async with self._state_lock:
                self._service = replacement
                self._generation += 1
                self._status = HubStatus.HEALTHY
                self._accepting_requests = True
            await previous.aclose()
        return await self.health()

    async def health(self) -> HubHealth:
        service = self._service
        if service is None:
            return HubHealth(status=HubStatus.STOPPED, generation=self._generation)

        warnings: list[str] = []
        adapters: dict[str, dict[str, Any]] = {}
        for name, adapter in sorted(service.adapters.items()):
            snapshot_fn = getattr(adapter, "health_snapshot", None)
            if callable(snapshot_fn):
                try:
                    snapshot = snapshot_fn()
                except Exception as exc:  # noqa: BLE001 - health stays observable
                    snapshot = {"status": "degraded", "error": type(exc).__name__}
                    warnings.append(f"adapter_health_failed:{name}")
            else:
                snapshot = {"status": "loaded"}
            adapters[name] = dict(snapshot)

        observer: dict[str, Any] = {"status": "unavailable", "binding_count": 0}
        screenshot_service = service.screenshot_service
        if screenshot_service is not None:
            health_fn = getattr(screenshot_service, "health_snapshot", None)
            if callable(health_fn):
                try:
                    observer = dict(health_fn())
                except Exception as exc:  # noqa: BLE001 - health stays observable
                    observer = {"status": "degraded", "error": type(exc).__name__}
                    warnings.append("screenshot_health_failed")
            else:
                observer = {
                    "status": "configured",
                    "binding_count": len(screenshot_service.bindings.bindings),
                }

        degraded = bool(warnings) or any(
            item.get("status") == "degraded" for item in [*adapters.values(), observer]
        )
        status = HubStatus.DEGRADED if degraded else self._status
        uptime_ms = 0
        if self._started_monotonic is not None:
            uptime_ms = max(0, int((time.monotonic() - self._started_monotonic) * 1000))
        return HubHealth(
            status=status,
            generation=self._generation,
            started_at=self._started_at,
            uptime_ms=uptime_ms,
            room_count=len(service.registry.rooms()),
            mutations_enabled=service.registry.config.mutations_enabled,
            adapters=adapters,
            screenshot_observer=observer,
            warnings=warnings,
        )

    async def aclose(self) -> None:
        async with self._lifecycle_lock:
            service = await self._stop_accepting_and_drain()
            async with self._state_lock:
                self._service = None
                self._status = HubStatus.STOPPED
            if service is not None:
                await service.aclose()

    async def __aenter__(self) -> HomeMediaHub:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
