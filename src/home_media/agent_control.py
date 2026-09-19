"""Small, model-independent Apple TV agent service.

One owner per physical device, compact fresh observations, and no automatic
mutation retries. The older room/scene API remains available independently.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import yaml
from pydantic import BaseModel, Field, model_validator

from home_media.adapters.apple_tv import AppleTVAdapter
from home_media.agent_urls import validate_app_store_detail_url
from home_media.config import ensure_private_dir, validate_private_file, write_private_text
from home_media.errors import ConfigError, ErrorCode, SafetyBlockedError, UnsupportedError
from home_media.models import (
    DeviceKind,
    DeviceRef,
    DiscoveredEndpoint,
    HomeConfig,
    PowerState,
    RoomConfig,
)
from home_media.registry import RoomRegistry
from home_media.wda import WDAClient, WDAError, WDAObservation

DEFAULT_AGENT_CONFIG = Path.home() / ".config" / "home-media" / "agent.yaml"
HELPER_STARTUP_TIMEOUT_S = 35.0
HELPER_POLL_INTERVAL_S = 0.3
ENDPOINT_REFRESH_TIMEOUT_S = 8.0
HELPER_PROFILE_RENEWAL_WINDOW = timedelta(hours=48)

HelperProfileStatus = Literal[
    "not_configured",
    "external_unknown",
    "valid",
    "renewal_due",
    "expired",
    "unknown",
]

_PRIVATE_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class HelperConfig(BaseModel):
    udid: str = Field(min_length=8, repr=False)
    backend: Literal["xcode", "native"] = "xcode"
    bundle_id: str | None = None
    xctestrun_path: Path | None = None
    developer_dir: Path | None = None

    @model_validator(mode="after")
    def validate_backend(self) -> HelperConfig:
        if self.backend == "native" and not self.bundle_id:
            raise ValueError("Native helper requires its installed bundle ID")
        if self.backend == "xcode" and not (self.xctestrun_path and self.developer_dir):
            raise ValueError("Xcode helper requires xctestrun_path and developer_dir")
        return self


class AgentDevice(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str
    stable_id: str | None = Field(default=None, repr=False)
    wda_endpoint: str | None = Field(default=None, repr=False)
    wda_identity: str | None = Field(default=None, repr=False)
    helper: HelperConfig | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_binding(self) -> AgentDevice:
        if bool(self.wda_endpoint) != bool(self.wda_identity):
            raise ValueError("WDA endpoint requires a pinned device identity; use configure")
        if not self.stable_id and not self.wda_endpoint:
            raise ValueError("Configure a paired control device or a WDA endpoint")
        return self


class AgentConfig(BaseModel):
    schema_version: Literal[1] = 1
    devices: list[AgentDevice] = Field(default_factory=list)
    mutations_enabled: bool = True
    max_observation_age_s: float = Field(default=30, gt=0, le=120)
    country: str = "US"
    preferred_profile: str | None = None
    subscriptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_devices(self) -> AgentConfig:
        for field in ("key", "stable_id", "wda_identity", "wda_endpoint"):
            values = [getattr(d, field) for d in self.devices if getattr(d, field)]
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate device binding: {field}")
        helper_udids = [device.helper.udid.casefold() for device in self.devices if device.helper]
        if len(helper_udids) != len(set(helper_udids)):
            raise ValueError("Duplicate device binding: helper.udid")
        return self


def agent_config_path() -> Path:
    return Path(os.environ.get("APPLE_TV_AGENT_CONFIG", str(DEFAULT_AGENT_CONFIG))).expanduser()


def load_agent_config(path: Path | None = None) -> AgentConfig:
    path = path or agent_config_path()
    if not path.exists():
        return AgentConfig()
    validate_private_file(path, label="Apple TV agent configuration")
    try:
        return AgentConfig.model_validate(yaml.safe_load(path.read_text()))
    except Exception:
        raise ConfigError("Invalid agent configuration; run apple-tv-agent configure") from None


def save_agent_config(config: AgentConfig, path: Path | None = None) -> None:
    write_private_text(
        path or agent_config_path(),
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False),
    )


class DeviceLease:
    """OS-released process lease; a crashed owner does not leave a stale lock."""

    def __init__(self, identity: str, directory: Path) -> None:
        digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
        self.path = ensure_private_dir(directory) / f"{digest}.lock"
        self.fd: int | None = None

    def acquire(self) -> None:
        if self.fd is not None:
            return
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                locking = getattr(msvcrt, "locking", None)
                mode = getattr(msvcrt, "LK_NBLCK", None)
                if locking is None or mode is None:
                    raise OSError("Windows file locking unavailable")
                locking(fd, mode, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise SafetyBlockedError(
                "Another agent bridge owns this device; use that bridge or close it first",
                reason="device_in_use",
            ) from None
        self.fd = fd

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class AgentObservation(BaseModel):
    observation_id: str
    device: str
    observed: WDAObservation
    obtained_monotonic: float = Field(exclude=True, repr=False)


def _valid_sample_interval(
    request_started_at: datetime | None,
    received_at: datetime | None,
    *,
    observation_started_at: datetime,
    observation_returned_at: datetime,
) -> bool:
    """Accept only a complete, aware interval produced during this observe call."""

    if request_started_at is None or received_at is None:
        return False
    if (
        request_started_at.tzinfo is None
        or request_started_at.utcoffset() is None
        or received_at.tzinfo is None
        or received_at.utcoffset() is None
    ):
        return False
    started = request_started_at.astimezone(UTC)
    received = received_at.astimezone(UTC)
    return observation_started_at <= started <= received <= observation_returned_at


def _private_lan_ipv4(value: str | None) -> ipaddress.IPv4Address | None:
    if value is None:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if not isinstance(address, ipaddress.IPv4Address) or address.is_loopback:
        return None
    if address.is_link_local or any(address in network for network in _PRIVATE_IPV4_NETWORKS):
        return address
    return None


def _endpoint_matches_stable_id(endpoint: DiscoveredEndpoint, stable_id: str) -> bool:
    identifiers = {endpoint.device_id.casefold()}
    raw_identifiers = endpoint.raw.get("all_identifiers")
    if isinstance(raw_identifiers, (list, tuple, set, frozenset)):
        identifiers.update(
            identifier.casefold() for identifier in raw_identifiers if isinstance(identifier, str)
        )
    return stable_id.casefold() in identifiers


def _is_wda_transport_failure(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or (
        isinstance(exc, WDAError) and exc.code is ErrorCode.NETWORK
    )


class AppleTVAgent:
    def __init__(
        self,
        config: AgentConfig,
        *,
        client_factory: Callable[[str], WDAClient] = WDAClient,
        lease_dir: Path | None = None,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.config = config
        self._client_factory = client_factory
        self._now = now
        self._clients: dict[str, WDAClient] = {}
        self._active_endpoints: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._leases: dict[str, DeviceLease] = {}
        self._runtime: dict[str, Any] = {}
        self._observations: dict[str, AgentObservation] = {}
        self._lease_dir = lease_dir or DEFAULT_AGENT_CONFIG.parent / "locks"
        rooms = [
            RoomConfig(key=d.key, display_name=d.name, apple_tv_id=d.stable_id)
            for d in config.devices
            if d.stable_id
        ]
        devices = [
            DeviceRef(id=d.stable_id, kind=DeviceKind.APPLE_TV, room_key=d.key, adapter="apple_tv")
            for d in config.devices
            if d.stable_id
        ]
        self.adapter = AppleTVAdapter(RoomRegistry(HomeConfig(rooms=rooms, devices=devices)))

    def device(self, key: str) -> AgentDevice:
        for device in self.config.devices:
            if device.key == key:
                return device
        raise ConfigError("Unknown device; use devices to choose a configured device key")

    def _lock(self, key: str) -> asyncio.Lock:
        self.device(key)
        return self._locks.setdefault(key, asyncio.Lock())

    def _lease(self, device: AgentDevice) -> None:
        if device.key not in self._leases:
            identity = (device.stable_id or device.wda_identity or device.key).casefold()
            lease = DeviceLease(identity, self._lease_dir)
            lease.acquire()
            self._leases[device.key] = lease

    def helper_profile_health(self, device: AgentDevice) -> dict[str, object]:
        """Inspect configured cached signing evidence without network or device access."""

        if device.wda_endpoint is None:
            return {
                "status": "not_configured",
                "basis": "not_configured",
                "apple_account_session": "not_checked",
                "repair_hint": "Run apple-tv-agent configure if screen control is needed.",
            }
        if device.helper is None:
            return {
                "status": "external_unknown",
                "basis": "external_helper_unchecked",
                "apple_account_session": "not_checked",
                "repair_hint": (
                    "Check the profile on the external helper host; local cached evidence "
                    "is unavailable."
                ),
            }

        from home_media.wda_runtime import WDARuntime, WDARuntimeConfig

        try:
            report = WDARuntime(
                WDARuntimeConfig(
                    udid=device.helper.udid,
                    backend=device.helper.backend,
                    bundle_id=device.helper.bundle_id,
                    xctestrun_path=device.helper.xctestrun_path,
                    developer_dir=device.helper.developer_dir,
                )
            ).doctor()
        except Exception:  # noqa: BLE001 - fixed health result, no raw local error
            return {
                "status": "unknown",
                "basis": "configured_cached_profile",
                "apple_account_session": "not_checked",
                "repair_hint": (
                    "Run apple-tv-agent doctor locally; if signing evidence is missing or "
                    "invalid, follow the apple-tv-agent skill renewal flow."
                ),
            }

        expires_at = report.profile_expires_at
        checked_at = self._now()
        status: HelperProfileStatus
        repair_hint: str | None
        if (
            expires_at is None
            or expires_at.tzinfo is None
            or expires_at.utcoffset() is None
            or checked_at.tzinfo is None
            or checked_at.utcoffset() is None
        ):
            status = "unknown"
            repair_hint = (
                "Run apple-tv-agent doctor locally; if signing evidence is missing or "
                "invalid, follow the apple-tv-agent skill renewal flow."
            )
        elif expires_at.astimezone(UTC) <= checked_at.astimezone(UTC):
            status = "expired"
            repair_hint = (
                "Run apple-tv-agent doctor locally, then follow the apple-tv-agent skill "
                "renewal flow to rebuild and re-sign the helper."
            )
        elif (
            report.profile_valid
            and report.ready
            and expires_at.astimezone(UTC) - checked_at.astimezone(UTC)
            <= HELPER_PROFILE_RENEWAL_WINDOW
        ):
            status = "renewal_due"
            repair_hint = (
                "Run apple-tv-agent doctor locally, then follow the apple-tv-agent skill "
                "renewal flow before expiry."
            )
        elif report.profile_valid and report.ready:
            status = "valid"
            repair_hint = None
        else:
            status = "unknown"
            repair_hint = (
                "Run apple-tv-agent doctor locally; if signing evidence is missing or "
                "invalid, follow the apple-tv-agent skill renewal flow."
            )

        result: dict[str, object] = {
            "status": status,
            "basis": "configured_cached_profile",
            "apple_account_session": "not_checked",
        }
        if expires_at is not None:
            result["expires_at"] = expires_at.isoformat()
        if repair_hint is not None:
            result["repair_hint"] = repair_hint
        return result

    def devices(self) -> list[dict[str, Any]]:
        return [
            {
                "device": d.key,
                "name": d.name,
                "direct_control_configured": bool(d.stable_id),
                "screen_control_configured": bool(d.wda_endpoint),
                "managed_helper": d.helper is not None,
                "helper_profile": self.helper_profile_health(d),
            }
            for d in self.config.devices
        ]

    async def _client(self, device: AgentDevice) -> WDAClient:
        self._lease(device)
        if not device.wda_endpoint:
            raise UnsupportedError("screen_control", reason="Configure a screen helper first")
        if device.key not in self._clients:
            self._clients[device.key] = self._client_factory(device.wda_endpoint)
            self._active_endpoints[device.key] = device.wda_endpoint
        client = self._clients[device.key]
        try:
            status = await client.status()
            if not status.ready:
                raise ConfigError("Screen helper reported that it is not ready")
            await self._require_device_identity(client, device)
            return client
        except SafetyBlockedError:
            raise
        except Exception as exc:
            if device.helper is None:
                if _is_wda_transport_failure(exc):
                    refreshed = await self._refresh_wda_endpoint(device, client)
                    if refreshed is not None:
                        return refreshed
                raise
            runtime, started_by_this_call = await self._start_helper(device)
            refresh_attempted = False
            try:
                async with asyncio.timeout(HELPER_STARTUP_TIMEOUT_S):
                    while True:
                        try:
                            status = await client.status()
                            if not status.ready:
                                raise ConfigError("Screen helper reported that it is not ready")
                            await self._require_device_identity(client, device)
                            ready = runtime.mark_ready()
                            if ready.state != "ready":
                                raise ConfigError("Managed helper exited before readiness")
                            return client
                        except SafetyBlockedError:
                            raise
                        except Exception as readiness_exc:
                            if not refresh_attempted and _is_wda_transport_failure(readiness_exc):
                                refresh_attempted = True
                                refreshed = await self._refresh_wda_endpoint(device, client)
                                if refreshed is not None:
                                    ready = runtime.mark_ready()
                                    if ready.state != "ready":
                                        raise ConfigError(
                                            "Managed helper exited before readiness"
                                        ) from None
                                    return refreshed
                            await asyncio.sleep(HELPER_POLL_INTERVAL_S)
            except BaseException as exc:
                cleanup_ok = True
                if started_by_this_call:
                    cleanup_ok = await self._stop_started_helper(device.key, runtime)
                if not cleanup_ok and not isinstance(exc, asyncio.CancelledError):
                    raise ConfigError(
                        "Helper startup failed and its owned process did not stop; run doctor"
                    ) from None
                if isinstance(exc, TimeoutError):
                    raise ConfigError(
                        "Helper did not become ready; run apple-tv-agent doctor"
                    ) from None
                raise

    async def _refresh_wda_endpoint(
        self,
        device: AgentDevice,
        current: WDAClient,
    ) -> WDAClient | None:
        if device.stable_id is None or device.wda_endpoint is None:
            return None
        try:
            configured = urlsplit(self._active_endpoints.get(device.key, device.wda_endpoint))
            configured_address = _private_lan_ipv4(configured.hostname)
            configured_port = configured.port
        except ValueError:
            return None
        if configured_address is None or configured.scheme not in {"http", "https"}:
            return None

        try:
            async with asyncio.timeout(ENDPOINT_REFRESH_TIMEOUT_S):
                endpoints = await self.adapter.discover()
        except Exception:
            return None
        matches = [
            endpoint
            for endpoint in endpoints
            if _endpoint_matches_stable_id(endpoint, device.stable_id)
        ]
        if len(matches) != 1:
            return None
        candidate_address = _private_lan_ipv4(matches[0].address)
        if candidate_address is None or candidate_address == configured_address:
            return None

        port = f":{configured_port}" if configured_port is not None else ""
        candidate_endpoint = f"{configured.scheme}://{candidate_address}{port}"
        candidate = self._client_factory(candidate_endpoint)
        accepted = False
        try:
            status = await candidate.status()
            if not status.ready:
                return None
            await self._require_device_identity(candidate, device)
            accepted = True
        except SafetyBlockedError:
            raise
        except Exception:
            return None
        finally:
            if not accepted:
                await asyncio.gather(candidate.aclose(), return_exceptions=True)

        self._clients[device.key] = candidate
        self._active_endpoints[device.key] = candidate_endpoint
        self._observations.pop(device.key, None)
        if candidate is not current:
            await asyncio.gather(current.aclose(), return_exceptions=True)
        return candidate

    async def _require_device_identity(self, client: WDAClient, device: AgentDevice) -> None:
        identity = await client.device_identity()
        if identity != device.wda_identity:
            self._observations.pop(device.key, None)
            raise SafetyBlockedError(
                "The screen endpoint identifies a different device; recheck its binding",
                reason="device_identity_mismatch",
            )

    async def _start_helper(self, device: AgentDevice) -> tuple[Any, bool]:
        from home_media.wda_runtime import WDARuntime, WDARuntimeConfig

        if device.helper is None:
            raise ConfigError("No managed helper configured")
        runtime = self._runtime.get(device.key)
        if runtime is None:
            runtime = WDARuntime(
                WDARuntimeConfig(
                    udid=device.helper.udid,
                    backend=device.helper.backend,
                    bundle_id=device.helper.bundle_id,
                    xctestrun_path=device.helper.xctestrun_path,
                    developer_dir=device.helper.developer_dir,
                )
            )
            self._runtime[device.key] = runtime
        before = await asyncio.to_thread(runtime.status)
        start_task = asyncio.create_task(asyncio.to_thread(runtime.start))
        try:
            status = await asyncio.shield(start_task)
        except BaseException:
            # Cancellation must not detach a to_thread start that can still
            # create a child after this coroutine exits. Wait for that exact
            # start attempt, then stop it before propagating the original exit.
            with suppress(BaseException):
                await asyncio.shield(start_task)
            if before.pid is None:
                await self._stop_started_helper(device.key, runtime)
            raise
        if status.state not in {"starting", "ready"}:
            stopped = await asyncio.to_thread(runtime.stop)
            if stopped.state == "stopped" and self._runtime.get(device.key) is runtime:
                self._runtime.pop(device.key, None)
            elif stopped.state != "stopped":
                raise ConfigError(
                    "Helper could not start and its owned process did not stop; run doctor"
                )
            raise ConfigError("Helper could not start; run apple-tv-agent doctor")
        started_by_this_call = before.pid is None and status.pid is not None
        return runtime, started_by_this_call

    async def _stop_started_helper(self, key: str, runtime: Any) -> bool:
        try:
            stopped = await asyncio.shield(asyncio.to_thread(runtime.stop))
        except BaseException:  # preserve ownership for a later explicit cleanup attempt
            return False
        if stopped.state != "stopped":
            return False
        if self._runtime.get(key) is runtime:
            self._runtime.pop(key, None)
        return True

    async def observe(
        self,
        key: str,
        *,
        include_image: bool = False,
        expected_app: str | None = None,
        expected_label: str | None = None,
    ) -> AgentObservation:
        async with self._lock(key):
            client = await self._client(self.device(key))
            return await self._observe(key, client, include_image, expected_app, expected_label)

    async def _observe(
        self,
        key: str,
        client: WDAClient,
        image: bool,
        expected_app: str | None,
        expected_label: str | None,
    ) -> AgentObservation:
        self._observations.pop(key, None)
        started = datetime.now(UTC)
        observed = await client.observe(
            include_image=image,
            expected_app=expected_app,
            expected_label=expected_label,
        )
        returned = datetime.now(UTC)
        semantic_interval_valid = _valid_sample_interval(
            observed.request_started_at,
            observed.received_at,
            observation_started_at=started,
            observation_returned_at=returned,
        )
        if not semantic_interval_valid:
            if "semantic_timestamps_invalid" not in observed.errors:
                observed.errors.append("semantic_timestamps_invalid")
            observed.request_started_at = None
            observed.received_at = None
            observed.active_app = None
            observed.focused_label = None
            observed.visible = []
        if observed.errors or not semantic_interval_valid:
            observed.expected_app_verified = False
            observed.expected_label_verified = False
        if observed.image is not None:
            image_interval_valid = _valid_sample_interval(
                observed.image.request_started_at,
                observed.image.received_at,
                observation_started_at=started,
                observation_returned_at=returned,
            )
            if not image_interval_valid:
                if "image_timestamps_invalid" not in observed.warnings:
                    observed.warnings.append("image_timestamps_invalid")
                observed.image = None
        result = AgentObservation(
            observation_id=uuid4().hex[:16],
            device=key,
            observed=observed,
            obtained_monotonic=time.monotonic(),
        )
        if (observed.visible and not observed.errors) or observed.image is not None:
            self._observations[key] = result
        return result

    async def act(
        self,
        key: str,
        *,
        action: str,
        value: str,
        observation_id: str | None = None,
        role: str = "Cell",
        expected_app: str | None = None,
        expected_label: str | None = None,
        include_image: bool = False,
    ) -> dict[str, Any]:
        if not self.config.mutations_enabled:
            raise SafetyBlockedError("Mutations are disabled", reason="mutations_disabled")
        async with self._lock(key):
            device = self.device(key)
            client = await self._client(device)
            expected_identity = None
            if action in {"press", "select", "type"}:
                before = self._observations.get(key)
                if before is None or before.observation_id != observation_id:
                    raise SafetyBlockedError("Observe before acting", reason="stale_observation")
                if action != "press" and (
                    before.observed.errors
                    or not before.observed.visible
                    or before.observed.request_started_at is None
                    or before.observed.received_at is None
                ):
                    raise SafetyBlockedError(
                        "This action requires usable semantic state; observe again",
                        reason="semantic_observation_required",
                    )
                age = time.monotonic() - before.obtained_monotonic
                semantic_usable = (
                    not before.observed.errors
                    and bool(before.observed.visible)
                    and before.observed.request_started_at is not None
                    and before.observed.received_at is not None
                )
                sample_times: list[datetime] = []
                if semantic_usable:
                    semantic_started = before.observed.request_started_at
                    if semantic_started is not None:
                        sample_times.append(semantic_started)
                    if (
                        before.observed.image is not None
                        and before.observed.image.request_started_at is not None
                    ):
                        sample_times.append(before.observed.image.request_started_at)
                elif (
                    before.observed.image is not None
                    and before.observed.image.request_started_at is not None
                ):
                    # An independently fresh image can authorize only press navigation.
                    sample_times.append(before.observed.image.request_started_at)
                if not sample_times:
                    raise SafetyBlockedError(
                        "Observation timing is unavailable; observe again",
                        reason="stale_observation",
                    )
                sample_age = (datetime.now(UTC) - min(sample_times)).total_seconds()
                if max(age, sample_age) > self.config.max_observation_age_s or sample_age < 0:
                    raise SafetyBlockedError(
                        "Observation expired; observe again", reason="stale_observation"
                    )
                current = await client.status()
                if before.observed.identity != current.identity:
                    self._observations.pop(key, None)
                    raise SafetyBlockedError(
                        "Helper session changed; observe again", reason="helper_restarted"
                    )
                expected_identity = before.observed.identity
            # Consume authorization before dispatch, including ambiguous failures.
            self._observations.pop(key, None)
            if action == "press":
                await client.press(value, expected_identity=expected_identity)
            elif action == "select":
                await client.select(value, role=role, expected_identity=expected_identity)
            elif action == "type":
                await client.set_text(value, expected_identity=expected_identity)
            elif action == "launch":
                await client.activate(value)
                expected_app = value
            else:
                raise UnsupportedError("agent_action", reason="Use press, select, type, or launch")
            try:
                after = await self._observe(
                    key, client, include_image, expected_app, expected_label
                )
            except Exception:
                return {
                    "device": key,
                    "action_sent": True,
                    "outcome": "unverified",
                    "recovery": "Observe again; do not repeat the action blindly.",
                }
            verified = (
                not after.observed.errors
                and (bool(expected_app) or bool(expected_label))
                and (not expected_app or after.observed.expected_app_verified)
                and (not expected_label or after.observed.expected_label_verified)
            )
            return {
                "device": key,
                "action_sent": True,
                "outcome": "verified" if verified else "unverified",
                "after": after,
            }

    async def direct(self, key: str, operation: str, value: str = "") -> Any:
        """Existing paired pyatv capabilities remain usable without developer access."""
        async with self._lock(key):
            device = self.device(key)
            self._lease(device)
            if not device.stable_id:
                raise UnsupportedError("direct_control", reason="Pair Companion control first")
            if operation == "status":
                return await self.adapter.get_status(device.stable_id)
            if operation == "apps":
                return await self.adapter.list_apps(device.stable_id)
            if not self.config.mutations_enabled:
                raise SafetyBlockedError("Mutations are disabled", reason="mutations_disabled")
            self._observations.pop(key, None)
            if operation == "wake":
                return await self.adapter.set_power(device.stable_id, PowerState.ON)
            if operation == "sleep":
                return await self.adapter.set_power(device.stable_id, PowerState.OFF)
            if operation == "transport":
                return await self.adapter.control_transport(device.stable_id, value)
            if operation == "press":
                await self.adapter.press_key(device.stable_id, value)
                return {"sent": True, "verified": False}
            if operation == "open_url":
                try:
                    app_store_url = validate_app_store_detail_url(value)
                except UnsupportedError:
                    return await self.adapter.open_url(device.stable_id, value)
                return await self.adapter.open_app(device.stable_id, app_store_url)
            if operation == "launch":
                return await self.adapter.open_app(device.stable_id, value)
            if operation == "type":
                await self.adapter.enter_text(device.stable_id, value)
                return {"sent": True, "verified": False}
            raise UnsupportedError("direct_operation")

    async def aclose(self) -> None:
        runtimes = list(self._runtime.items())
        ordinary_results = await asyncio.gather(
            self.adapter.aclose(),
            *(client.aclose() for client in self._clients.values()),
            return_exceptions=True,
        )
        runtime_results = await asyncio.gather(
            *(asyncio.to_thread(runtime.stop) for _, runtime in runtimes),
            return_exceptions=True,
        )
        failed_runtime_keys = {
            key
            for (key, _runtime), result in zip(runtimes, runtime_results, strict=True)
            if isinstance(result, BaseException) or getattr(result, "state", None) != "stopped"
        }
        for key, _runtime in runtimes:
            if key not in failed_runtime_keys:
                self._runtime.pop(key, None)
        for key, lease in list(self._leases.items()):
            if key not in failed_runtime_keys:
                lease.close()
                self._leases.pop(key, None)
        self._clients.clear()
        self._active_endpoints.clear()
        self._observations.clear()
        if failed_runtime_keys:
            raise ConfigError(
                "A managed helper did not stop; device ownership is retained for cleanup"
            )
        if any(isinstance(result, BaseException) for result in ordinary_results):
            raise ConfigError("One or more agent resources did not close cleanly")
