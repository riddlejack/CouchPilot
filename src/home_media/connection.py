"""Apple TV connection/discovery manager: TTL cache, single-flight, reuse."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar

import pyatv
from pyatv.interface import AppleTV, BaseConfig
from pyatv.storage.file_storage import FileStorage

from home_media.errors import AuthRequiredError, NetworkError, StaleEndpointError, TimeoutError_

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

_DEFAULT_DISCOVERY_TTL_S = 45.0
_CONNECT_TIMEOUT_S = 15.0


@dataclass
class ConnectionManagerStats:
    scans: int = 0
    connects: int = 0
    reuses: int = 0
    invalidations: int = 0


@dataclass
class _CachedConfig:
    config: BaseConfig
    cached_at: float


@dataclass
class _ManagedConnection:
    atv: AppleTV
    config: BaseConfig
    connected_at: float


@dataclass
class AppleTVConnectionManager:
    """Process-scoped connection reuse keyed by stable Apple TV device id."""

    storage: FileStorage
    scan_timeout: float = 5.0
    discovery_ttl_s: float = _DEFAULT_DISCOVERY_TTL_S
    stats: ConnectionManagerStats = field(default_factory=ConnectionManagerStats)
    _configs: dict[str, _CachedConfig] = field(default_factory=dict)
    _connections: dict[str, _ManagedConnection] = field(default_factory=dict)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _closed: bool = False
    _active_operations: int = 0
    _activity_condition: asyncio.Condition = field(default_factory=asyncio.Condition)

    @asynccontextmanager
    async def _activity(self) -> AsyncIterator[None]:
        async with self._activity_condition:
            if self._closed:
                raise NetworkError("Connection manager is closed", retryable=False)
            self._active_operations += 1
        try:
            yield
        finally:
            async with self._activity_condition:
                self._active_operations -= 1
                if self._active_operations == 0:
                    self._activity_condition.notify_all()

    def _lock_for(self, device_id: str) -> asyncio.Lock:
        if device_id not in self._locks:
            self._locks[device_id] = asyncio.Lock()
        return self._locks[device_id]

    def _scan_timeout_int(self) -> int:
        return max(1, int(round(self.scan_timeout)))

    async def resolve_config(
        self,
        device_id: str,
        *,
        force: bool = False,
    ) -> BaseConfig:
        async with self._activity():
            return await self._resolve_config(device_id, force=force)

    async def _resolve_config(
        self,
        device_id: str,
        *,
        force: bool = False,
    ) -> BaseConfig:
        async with self._lock_for(device_id):
            now = time.monotonic()
            cached = self._configs.get(device_id)
            if (
                not force
                and cached is not None
                and (now - cached.cached_at) < self.discovery_ttl_s
            ):
                return cached.config

            loop = asyncio.get_running_loop()
            self.stats.scans += 1
            try:
                found = await asyncio.wait_for(
                    pyatv.scan(
                        loop,
                        timeout=self._scan_timeout_int(),
                        identifier=device_id,
                        storage=self.storage,
                    ),
                    timeout=self.scan_timeout + 2.0,
                )
            except TimeoutError as exc:
                raise TimeoutError_(f"Rediscovery timed out for {device_id}") from exc
            except Exception as exc:  # noqa: BLE001
                raise NetworkError(
                    f"Rediscovery failed for {device_id}: {exc}",
                    retryable=True,
                ) from exc

            if not found:
                raise StaleEndpointError(device_id, "unknown")

            conf = found[0]
            self._configs[device_id] = _CachedConfig(config=conf, cached_at=now)
            return conf

    async def get_connection(self, device_id: str, conf: BaseConfig) -> AppleTV:
        async with self._activity():
            return await self._get_connection(device_id, conf)

    async def _get_connection(self, device_id: str, conf: BaseConfig) -> AppleTV:
        async with self._lock_for(device_id):
            existing = self._connections.get(device_id)
            if existing is not None:
                self.stats.reuses += 1
                return existing.atv

            loop = asyncio.get_running_loop()
            self.stats.connects += 1
            try:
                atv = await asyncio.wait_for(
                    pyatv.connect(conf, loop, storage=self.storage),
                    timeout=_CONNECT_TIMEOUT_S,
                )
            except TimeoutError as exc:
                raise TimeoutError_(f"Connect timed out for {device_id}") from exc
            except Exception as exc:  # noqa: BLE001
                raise NetworkError(
                    f"Connect failed for {device_id}: {exc}",
                    retryable=True,
                ) from exc

            self._connections[device_id] = _ManagedConnection(
                atv=atv,
                config=conf,
                connected_at=time.monotonic(),
            )
            return atv

    async def invalidate(self, device_id: str) -> None:
        async with self._lock_for(device_id):
            self.stats.invalidations += 1
            managed = self._connections.pop(device_id, None)
            self._configs.pop(device_id, None)
            if managed is not None:
                await self._close_atv(managed.atv)

    async def with_connection(
        self,
        device_id: str,
        op: Callable[[AppleTV, BaseConfig], Awaitable[T]],
        *,
        require_credentials: Callable[[BaseConfig], bool] | None = None,
    ) -> T:
        async with self._activity():
            conf = await self._resolve_config(device_id)
            if require_credentials is not None and not require_credentials(conf):
                raise AuthRequiredError(device_id, protocol="companion")
            atv = await self._get_connection(device_id, conf)
            try:
                return await op(atv, conf)
            except (AuthRequiredError, StaleEndpointError):
                await self.invalidate(device_id)
                raise
            except NetworkError:
                await self.invalidate(device_id)
                raise

    async def aclose(self) -> None:
        async with self._activity_condition:
            if self._closed and self._active_operations == 0:
                return
            self._closed = True
            while self._active_operations:
                await self._activity_condition.wait()
        for device_id in list(self._connections):
            await self.invalidate(device_id)

    def health_snapshot(self) -> dict[str, int | bool | str]:
        """Safe aggregate counters for hub health; device keys stay private."""
        return {
            "status": "stopped" if self._closed else "healthy",
            "connection_manager_started": True,
            "cached_connections": len(self._connections),
            "cached_configs": len(self._configs),
            "active_operations": self._active_operations,
            "scans": self.stats.scans,
            "connects": self.stats.connects,
            "reuses": self.stats.reuses,
            "invalidations": self.stats.invalidations,
        }

    @staticmethod
    async def _close_atv(atv: AppleTV) -> None:
        try:
            tasks = atv.close()
            if tasks:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=5.0,
                )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Apple TV close failed", exc_info=True)


def fingerprint_request(intent: str, room_key: str, params: dict[str, Any]) -> str:
    """Canonical request fingerprint for idempotency binding."""
    import hashlib
    import json

    payload = {"intent": intent, "room_key": room_key, "params": params}
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
