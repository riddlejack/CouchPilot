"""SoCo-backed Sonos adapter (in-process UPnP; not sonoscli)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import soco
from soco import SoCo
from soco.exceptions import SoCoException

from home_media.errors import NetworkError, TimeoutError_, UnsupportedError
from home_media.models import (
    Capability,
    CapabilitySemantics,
    DeviceKind,
    DeviceRef,
    DeviceStatus,
    DiscoveredEndpoint,
    NowPlaying,
    PowerState,
    SupportLevel,
)
from home_media.registry import RoomRegistry

logger = logging.getLogger(__name__)

_DISCOVER_TIMEOUT_S = 8.0
_OP_TIMEOUT_S = 8.0

_BONDED_COMPONENT_NAMES = frozenset(
    {
        "sub",
        "surround",
        "left surround",
        "right surround",
        "surround left",
        "surround right",
    }
)


def _norm(value: str) -> str:
    return " ".join(value.strip().lower().split())


class SonosAdapter:
    """Discover and control Sonos zones with coordinator / bonded-component awareness."""

    name = "sonos"

    def __init__(self, registry: RoomRegistry) -> None:
        self._registry = registry
        self._by_device_id: dict[str, SoCo] = {}
        self._by_uid: dict[str, SoCo] = {}

    async def discover(self) -> list[DiscoveredEndpoint]:
        try:
            speakers = await asyncio.wait_for(
                asyncio.to_thread(self._discover_sync),
                timeout=_DISCOVER_TIMEOUT_S + 2.0,
            )
        except TimeoutError as exc:
            raise TimeoutError_("Sonos discovery timed out") from exc
        except (OSError, SoCoException) as exc:
            raise NetworkError(f"Sonos discovery failed: {exc}", retryable=True) from exc

        audio_refs = [
            d
            for d in self._registry.config.devices
            if d.adapter == self.name and d.kind == DeviceKind.AUDIO
        ]

        self._by_device_id.clear()
        self._by_uid.clear()
        endpoints: list[DiscoveredEndpoint] = []

        for speaker in speakers:
            try:
                meta = await asyncio.wait_for(
                    asyncio.to_thread(self._speaker_meta, speaker),
                    timeout=_OP_TIMEOUT_S,
                )
            except TimeoutError:
                logger.debug(
                    "Timed out reading Sonos speaker at %s",
                    getattr(speaker, "ip_address", "?"),
                )
                continue
            except Exception as exc:  # noqa: BLE001 — one bad speaker must not abort discovery
                logger.debug("Skipping Sonos speaker: %s", exc)
                continue

            uid = str(meta["uid"])
            self._by_uid[uid] = speaker

            is_sub = bool(meta["is_sub"])
            is_bridge = bool(meta["is_bridge"])
            player_name = str(meta["player_name"])
            address = str(meta["address"])
            model = meta.get("model")
            model_str = str(model) if model else None

            # Prefer vendor_stable_id (RINCON UID); never bind Subs/bridges as room targets.
            matched_id: str | None = None
            if not is_sub and not is_bridge:
                matched_id = self._match_audio_device_id(uid, player_name, audio_refs)

            device_id = matched_id or uid
            endpoint = DiscoveredEndpoint(
                device_id=device_id,
                kind=DeviceKind.AUDIO,
                name=player_name,
                address=address,
                protocols=["sonos"],
                model=model_str,
                raw={
                    "uid": uid,
                    "is_coordinator": bool(meta["is_coordinator"]),
                    "is_bridge": is_bridge,
                    "is_sub": is_sub,
                    "is_visible": bool(meta["is_visible"]),
                    "player_name": player_name,
                    "preferred_room_target": matched_id is not None,
                },
            )
            endpoints.append(endpoint)

            if matched_id is not None:
                self._by_device_id[matched_id] = speaker
                self._registry.update_endpoint(endpoint)

        return endpoints

    async def get_capabilities(self, device_id: str) -> list[Capability]:
        await self._resolve(device_id)
        return [
            Capability(
                name="volume.set_absolute",
                support=SupportLevel.SUPPORTED,
                adapter=self.name,
                semantics=CapabilitySemantics.ABSOLUTE,
                evidence="soco absolute volume 0-100",
            ),
            Capability(
                name="transport",
                support=SupportLevel.SUPPORTED,
                adapter=self.name,
                evidence="soco play/pause/stop/next/previous",
            ),
        ]

    async def get_status(self, device_id: str) -> DeviceStatus:
        speaker = await self._resolve(device_id)
        try:
            data = await self._call(self._status_sync, speaker)
        except TimeoutError as exc:
            raise TimeoutError_(f"Sonos status timed out for {device_id}") from exc
        except (OSError, SoCoException) as exc:
            raise NetworkError(
                f"Sonos status failed for {device_id}: {exc}",
                retryable=True,
            ) from exc

        now_playing: NowPlaying | None = None
        if data.get("title") or data.get("device_state"):
            now_playing = NowPlaying(
                title=data.get("title"),
                artist=data.get("artist"),
                album=data.get("album"),
                device_state=data.get("device_state"),
                raw={"transport": data.get("transport_raw") or {}},
            )

        return DeviceStatus(
            device_id=device_id,
            kind=DeviceKind.AUDIO,
            name=data.get("name"),
            address=data.get("address"),
            power=PowerState.ON,
            volume=data.get("volume"),
            muted=data.get("muted"),
            now_playing=now_playing,
            available=True,
            evidence=["soco_status"],
            warnings=list(data.get("warnings") or []),
        )

    async def get_volume(self, device_id: str) -> dict[str, Any]:
        speaker = await self._resolve(device_id)
        try:
            level, muted, is_coordinator = await self._call(self._volume_sync, speaker)
        except TimeoutError as exc:
            raise TimeoutError_(f"Sonos get_volume timed out for {device_id}") from exc
        except (OSError, SoCoException) as exc:
            raise NetworkError(
                f"Sonos get_volume failed for {device_id}: {exc}",
                retryable=True,
            ) from exc
        return {
            "level": level,
            "muted": muted,
            "semantics": "absolute",
            "coordinator": is_coordinator,
        }

    async def set_volume(self, device_id: str, level: int) -> dict[str, Any]:
        if not 0 <= level <= 100:
            raise UnsupportedError("volume.set_absolute", reason="level must be 0-100")
        speaker = await self._resolve(device_id)
        try:
            await self._call(self._set_volume_sync, speaker, level)
        except TimeoutError as exc:
            raise TimeoutError_(f"Sonos set_volume timed out for {device_id}") from exc
        except (OSError, SoCoException) as exc:
            raise NetworkError(
                f"Sonos set_volume failed for {device_id}: {exc}",
                retryable=True,
            ) from exc
        return await self.get_volume(device_id)

    async def change_volume(self, device_id: str, delta: int) -> dict[str, Any]:
        current = await self.get_volume(device_id)
        level = int(current["level"])
        return await self.set_volume(device_id, max(0, min(100, level + delta)))

    async def control_transport(self, device_id: str, action: str) -> DeviceStatus:
        normalized = action.strip().lower()
        if normalized not in {"play", "pause", "stop", "next", "previous"}:
            raise UnsupportedError("transport", reason=f"unknown action '{action}'")
        speaker = await self._resolve(device_id)
        try:
            await self._call(self._transport_sync, speaker, normalized)
        except TimeoutError as exc:
            raise TimeoutError_(f"Sonos transport timed out for {device_id}") from exc
        except (OSError, SoCoException) as exc:
            raise NetworkError(
                f"Sonos transport failed for {device_id}: {exc}",
                retryable=True,
            ) from exc
        return await self.get_status(device_id)

    def _discover_sync(self) -> list[SoCo]:
        found = soco.discover(
            timeout=int(_DISCOVER_TIMEOUT_S),
            include_invisible=True,
        )
        if not found:
            return []
        return list(found)

    def _speaker_meta(self, speaker: SoCo) -> dict[str, Any]:
        player_name = speaker.player_name or ""
        uid = speaker.uid or ""
        address = speaker.ip_address
        is_bridge = bool(speaker.is_bridge)
        is_coordinator = bool(speaker.is_coordinator)
        is_visible = bool(speaker.is_visible)
        is_sub = bool(speaker.is_subwoofer) or _norm(player_name) in _BONDED_COMPONENT_NAMES

        model: str | None = None
        try:
            info = speaker.get_speaker_info(refresh=False) or {}
            model_name = str(info.get("model_name") or "")
            if model_name:
                model = model_name
                if "sub" in model_name.lower():
                    is_sub = True
        except (OSError, SoCoException):
            pass

        return {
            "uid": uid,
            "player_name": player_name,
            "address": address,
            "model": model,
            "is_bridge": is_bridge,
            "is_coordinator": is_coordinator,
            "is_visible": is_visible,
            "is_sub": is_sub,
        }

    def _match_audio_device_id(
        self,
        uid: str,
        player_name: str,
        audio_refs: list[DeviceRef],
    ) -> str | None:
        """Bind by vendor_stable_id first, then unique exact player_name/alias."""
        for ref in audio_refs:
            if ref.vendor_stable_id and ref.vendor_stable_id == uid:
                return ref.id

        needle = _norm(player_name)
        if not needle or needle in _BONDED_COMPONENT_NAMES:
            return None

        matches: list[str] = []
        for ref in audio_refs:
            room = self._registry.room(ref.room_key)
            candidates = {_norm(room.display_name), *(_norm(a) for a in ref.aliases)}
            if needle in candidates:
                matches.append(ref.id)

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                "Ambiguous Sonos player_name '%s' matched device ids %s; skipping name bind",
                player_name,
                matches,
            )
        return None

    async def _resolve(self, device_id: str) -> SoCo:
        speaker = self._by_device_id.get(device_id)
        if speaker is None:
            await self.discover()
            speaker = self._by_device_id.get(device_id)

        if speaker is None:
            # Resolve via configured vendor_stable_id even if name/IP drifted.
            try:
                ref = self._registry.device(device_id)
            except Exception:  # noqa: BLE001
                ref = None
            if ref and ref.vendor_stable_id:
                speaker = self._by_uid.get(ref.vendor_stable_id)
                if speaker is None:
                    await self.discover()
                    speaker = self._by_uid.get(ref.vendor_stable_id)

        if speaker is None:
            endpoint = self._registry.endpoint_for(device_id)
            if endpoint and endpoint.address:
                try:
                    candidate = await self._call(SoCo, endpoint.address)
                except Exception as exc:  # noqa: BLE001
                    raise NetworkError(
                        f"Unable to reach Sonos device {device_id} at {endpoint.address}",
                        retryable=True,
                    ) from exc
                # If config has a UID, refuse to control a speaker whose UID differs.
                try:
                    ref = self._registry.device(device_id)
                except Exception:  # noqa: BLE001
                    ref = None
                if ref and ref.vendor_stable_id and candidate.uid != ref.vendor_stable_id:
                    raise NetworkError(
                        f"Sonos at {endpoint.address} has uid {candidate.uid}, "
                        f"expected {ref.vendor_stable_id}; refusing IP-only bind",
                        retryable=True,
                    )
                speaker = candidate
                self._by_device_id[device_id] = speaker

        if speaker is None:
            raise NetworkError(
                f"Unknown Sonos device {device_id}; rediscover or set vendor_stable_id",
                retryable=False,
            )

        return await self._call(self._ensure_room_target, speaker, device_id)

    def _ensure_room_target(self, speaker: SoCo, device_id: str) -> SoCo:
        """Refuse accidental Sub targets; prefer zone coordinator when needed."""
        name = _norm(speaker.player_name or "")
        is_sub = bool(speaker.is_subwoofer) or name in _BONDED_COMPONENT_NAMES
        if is_sub:
            group = speaker.group
            coordinator = group.coordinator if group is not None else None
            if coordinator is None or coordinator is speaker:
                raise NetworkError(
                    f"Sonos device {device_id} resolved to a Sub/bonded component; "
                    "refusing to control it as a room target",
                    retryable=False,
                )
            coord_name = _norm(coordinator.player_name or "")
            if bool(coordinator.is_subwoofer) or coord_name in _BONDED_COMPONENT_NAMES:
                raise NetworkError(
                    f"Sonos device {device_id} coordinator is also a Sub; refusing",
                    retryable=False,
                )
            speaker = coordinator

        self._by_device_id[device_id] = speaker
        return speaker

    def _volume_target(self, speaker: SoCo) -> SoCo:
        """Use the zone player itself; for HT this is the Beam/Arc, never the Sub."""
        if speaker.is_subwoofer or _norm(speaker.player_name or "") in _BONDED_COMPONENT_NAMES:
            group = speaker.group
            if group and group.coordinator:
                return group.coordinator
        return speaker

    def _transport_target(self, speaker: SoCo) -> SoCo:
        group = speaker.group
        if group and group.coordinator:
            return group.coordinator
        return speaker

    def _volume_sync(self, speaker: SoCo) -> tuple[int, bool, bool]:
        target = self._volume_target(speaker)
        return int(target.volume), bool(target.mute), bool(target.is_coordinator)

    def _set_volume_sync(self, speaker: SoCo, level: int) -> None:
        target = self._volume_target(speaker)
        target.volume = level

    def _transport_sync(self, speaker: SoCo, action: str) -> None:
        target = self._transport_target(speaker)
        if action == "play":
            target.play()
        elif action == "pause":
            target.pause()
        elif action == "stop":
            target.stop()
        elif action == "next":
            target.next()
        elif action == "previous":
            target.previous()

    def _status_sync(self, speaker: SoCo) -> dict[str, Any]:
        target = self._volume_target(speaker)
        transport_target = self._transport_target(speaker)
        volume = int(target.volume)
        muted = bool(target.mute)
        transport_raw: dict[str, Any] = {}
        device_state: str | None = None
        title = artist = album = None
        try:
            transport_raw = dict(transport_target.get_current_transport_info() or {})
            device_state = transport_raw.get("current_transport_state")
        except (OSError, SoCoException):
            pass
        try:
            track = transport_target.get_current_track_info() or {}
            title = track.get("title") or None
            artist = track.get("artist") or None
            album = track.get("album") or None
        except (OSError, SoCoException):
            pass
        return {
            "name": speaker.player_name,
            "address": speaker.ip_address,
            "volume": volume,
            "muted": muted,
            "device_state": device_state,
            "title": title,
            "artist": artist,
            "album": album,
            "transport_raw": transport_raw,
            "warnings": [],
        }

    async def _call(self, func: Any, /, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args, **kwargs),
            timeout=_OP_TIMEOUT_S,
        )
