"""Application service shared by CLI and MCP."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

from home_media.audit import audit, configure_logging, redact_obj
from home_media.config import DEFAULT_CONFIG_DIR, theater_seed_config, load_home_config
from home_media.content.cache import VerifiedTargetCache, normalize_title
from home_media.content.direct_url import DirectURLResolver
from home_media.content.prepare import (
    PrepareContentRequest,
    PrepareContentResult,
    PrepareStage,
    StageStatus,
    TerminalStatus,
    normalize_query,
)
from home_media.content.router import ContentGoal, ContentRoute, plan_content_routes
from home_media.errors import HomeMediaError, SafetyBlockedError, UnsupportedError
from home_media.executor import Executor
from home_media.models import (
    ActionResult,
    AppInfo,
    Capability,
    ContentOutcome,
    DeviceKind,
    Envelope,
    ExecutionStatus,
    PairingSession,
    RoomStatus,
    SupportLevel,
    VerificationStatus,
)
from home_media.planner import Planner
from home_media.registry import RoomRegistry


class ApplicationService:
    def __init__(
        self,
        registry: RoomRegistry,
        adapters: dict[str, Any],
        *,
        executor: Executor | None = None,
        content_cache: VerifiedTargetCache | None = None,
        screenshot_service: Any | None = None,
        use_fakes: bool = False,
    ) -> None:
        configure_logging()
        self.registry = registry
        self.adapters = adapters
        self.planner = Planner(registry)
        self.executor = executor or Executor(
            adapters,
            mutations_enabled=registry.config.mutations_enabled,
        )
        self.content = DirectURLResolver(registry.config.content_aliases)
        self.content_cache = content_cache or VerifiedTargetCache()
        self.screenshot_service = screenshot_service
        self._use_fakes = use_fakes
        self._pairing_meta: dict[str, PairingSession] = {}
        self._prepare_idempotency: dict[str, PrepareContentResult] = {}
        self._prepare_fingerprints: dict[str, str] = {}
        self._prepare_inflight: dict[str, asyncio.Future[PrepareContentResult]] = {}

    @classmethod
    def from_config_path(
        cls,
        path: Path | None = None,
        *,
        use_fakes: bool = False,
    ) -> ApplicationService:
        """Load config fail-closed unless ``use_fakes`` explicitly opts into demo seed."""
        if use_fakes:
            # A plain fake/test invocation must never depend on the operator's
            # private default config. Callers can still pass an explicit path to
            # exercise a custom room layout with fake adapters.
            config = theater_seed_config() if path is None else load_home_config(path)
        elif path is None:
            config = load_home_config()
        else:
            config = load_home_config(path)
        registry = RoomRegistry(config)
        cache = VerifiedTargetCache(DEFAULT_CONFIG_DIR / "verified_targets.json")
        if use_fakes:
            from home_media.adapters.fake import (
                FakeAppleTVAdapter,
                FakePhysicalTVAdapter,
                FakeSonosAdapter,
            )

            apple = FakeAppleTVAdapter()
            sonos = FakeSonosAdapter()
            tv = FakePhysicalTVAdapter()
            # Seed from the selected config so an explicitly supplied fake
            # layout cannot drift from its adapters. Addresses use TEST-NET-1.
            rooms_by_key = {room.key: room for room in config.rooms}
            for offset, device in enumerate(config.devices, start=10):
                room = rooms_by_key[device.room_key]
                name = device.aliases[0] if device.aliases else room.display_name
                address = f"192.0.2.{offset}"
                if device.kind == DeviceKind.APPLE_TV:
                    apple.seed(
                        device.id,
                        name=name,
                        address=address,
                        paired=device.room_key in {"theater", "living_room"},
                        model=device.model or "Apple TV 4K",
                    )
                elif device.kind == DeviceKind.AUDIO:
                    sonos.seed(
                        device.id,
                        name=room.display_name,
                        address=address,
                        volume=18 if device.room_key == "theater" else 15,
                    )
                elif device.kind == DeviceKind.PHYSICAL_TV:
                    tv.seed(
                        device.id,
                        name=name,
                        address=address,
                        paired=device.room_key == "theater",
                    )
            adapters: dict[str, Any] = {
                "apple_tv": apple,
                "sonos": sonos,
                "android_tv": tv,
            }
            cache = VerifiedTargetCache()  # in-memory only for fakes/tests
        else:
            from home_media.adapters.apple_tv import AppleTVAdapter
            from home_media.adapters.physical_tv import AndroidTVAdapter
            from home_media.adapters.sonos import SonosAdapter

            adapters = {
                "apple_tv": AppleTVAdapter(registry),
                "sonos": SonosAdapter(registry),
                "android_tv": AndroidTVAdapter(registry),
            }
        screenshot_service = None
        try:
            from home_media.observers.binding import ObserverBindingStore
            from home_media.observers.service import RoomScreenshotService

            bindings = (
                ObserverBindingStore.empty() if use_fakes else ObserverBindingStore.load()
            )
            screenshot_service = RoomScreenshotService(registry, bindings=bindings)
        except Exception:  # noqa: BLE001 — screenshots are optional
            screenshot_service = None
        return cls(
            registry,
            adapters,
            content_cache=cache,
            screenshot_service=screenshot_service,
            use_fakes=use_fakes,
        )

    async def aclose(self) -> None:
        for adapter in self.adapters.values():
            close = getattr(adapter, "aclose", None)
            if close is not None:
                await close()

    def envelope(
        self,
        data: Any = None,
        *,
        ok: bool = True,
        error: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> Envelope:
        return Envelope(ok=ok, data=data, error=error, warnings=warnings or [])

    async def discover(self, *, include_private_inventory: bool = False) -> dict[str, Any]:
        collected: list[dict[str, Any]] = []
        for adapter in self.adapters.values():
            try:
                endpoints = await adapter.discover()
            except Exception as exc:  # noqa: BLE001
                collected.append({"adapter": getattr(adapter, "name", "?"), "error": str(exc)})
                continue
            for ep in endpoints:
                # Apply Apple TV filter when model/os look like Apple ecosystem noise.
                if ep.kind in {DeviceKind.APPLE_TV, DeviceKind.MAC, DeviceKind.AUDIO}:
                    maybe = self.registry.filter_apple_tvs([ep])
                    if ep.kind == DeviceKind.APPLE_TV or (
                        ep.model and "apple tv" in ep.model.lower()
                    ):
                        if not maybe:
                            continue
                        ep = maybe[0]
                    elif ep.kind == DeviceKind.MAC:
                        continue
                self.registry.update_endpoint(ep)
                collected.append(ep.model_dump(mode="json"))
        apple = [e for e in collected if e.get("kind") == DeviceKind.APPLE_TV.value]
        audio = [e for e in collected if e.get("kind") == DeviceKind.AUDIO.value]
        tvs = [e for e in collected if e.get("kind") == DeviceKind.PHYSICAL_TV.value]
        notes: list[str] = [
            "Default discover omits LAN addresses; pass include_private_inventory for local debug",
            "IPs are ephemeral; stable IDs and vendor_stable_id are identities",
            "Mac/Sonos AirPlay noise filtered from Apple TV inventory where possible",
        ]
        summary: dict[str, Any] = {
            "apple_tv_count": len(apple),
            "audio_count": len(audio),
            "physical_tv_count": len(tvs),
            "devices": [
                {
                    "device_id": e.get("device_id"),
                    "kind": e.get("kind"),
                    "name": e.get("name"),
                    "model": e.get("model"),
                    "os": e.get("os"),
                    "protocols": e.get("protocols"),
                }
                for e in collected
                if isinstance(e, dict)
                and e.get("kind")
                in {
                    DeviceKind.APPLE_TV.value,
                    DeviceKind.PHYSICAL_TV.value,
                    DeviceKind.AUDIO.value,
                }
            ],
            "notes": notes,
        }
        if include_private_inventory:
            summary["endpoints"] = collected
            summary["notes"] = [
                "Private inventory includes observed addresses — do not publish",
                *notes[1:],
            ]
        return summary

    async def list_rooms(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for room in self.registry.rooms():
            targets = self.registry.room_targets(room.key)
            out.append(
                {
                    "key": room.key,
                    "display_name": room.display_name,
                    "aliases": room.aliases,
                    "apple_tv_id": targets["apple_tv"].id if targets["apple_tv"] else None,
                    "physical_tv_id": targets["physical_tv"].id if targets["physical_tv"] else None,
                    "audio_id": targets["audio"].id if targets["audio"] else None,
                }
            )
        return out

    async def get_room_capabilities(self, room_name: str) -> list[Capability]:
        room = self.registry.resolve_room(room_name)
        caps: list[Capability] = []
        for role, device in self.registry.room_targets(room.key).items():
            if device is None:
                caps.append(
                    Capability(
                        name=f"{role}.present",
                        support=SupportLevel.UNAVAILABLE,
                        adapter="none",
                        evidence="not configured",
                    )
                )
                continue
            adapter = self.adapters.get(device.adapter)
            if adapter is None:
                continue
            try:
                caps.extend(await adapter.get_capabilities(device.id))
            except Exception as exc:  # noqa: BLE001
                caps.append(
                    Capability(
                        name=f"{role}.error",
                        support=SupportLevel.UNKNOWN,
                        adapter=device.adapter,
                        evidence=str(exc),
                    )
                )
        return caps

    async def get_room_status(self, room_name: str) -> RoomStatus:
        room = self.registry.resolve_room(room_name)
        targets = self.registry.room_targets(room.key)
        statuses: dict[str, Any] = {}
        device_errors: dict[str, dict[str, Any]] = {}
        for role, device in targets.items():
            if device is None:
                statuses[role] = None
                continue
            adapter = self.adapters[device.adapter]
            try:
                statuses[role] = await adapter.get_status(device.id)
            except HomeMediaError as exc:
                statuses[role] = None
                device_errors[role] = {
                    "device_id": device.id,
                    "kind": device.kind.value,
                    "available": False,
                    "error": exc.to_dict(),
                }
        caps = await self.get_room_capabilities(room_name)
        return RoomStatus(
            room_key=room.key,
            display_name=room.display_name,
            apple_tv=statuses.get("apple_tv")
            if hasattr(statuses.get("apple_tv"), "device_id")
            else None,
            physical_tv=statuses.get("physical_tv")
            if hasattr(statuses.get("physical_tv"), "device_id")
            else None,
            audio=statuses.get("audio") if hasattr(statuses.get("audio"), "device_id") else None,
            capabilities=caps,
            device_errors=device_errors,
        )

    async def start_pairing(
        self,
        room_name: str,
        *,
        protocol: str = "companion",
        device_role: str = "apple_tv",
    ) -> PairingSession:
        room = self.registry.resolve_room(room_name)
        targets = self.registry.room_targets(room.key)
        device = targets.get(device_role)
        if device is None:
            raise SafetyBlockedError(
                f"No {device_role} configured for {room.key}",
                reason="missing_device",
            )
        adapter = self.adapters[device.adapter]
        if not hasattr(adapter, "pair_start"):
            raise SafetyBlockedError("Adapter does not support pairing", reason="unsupported")
        session = cast(PairingSession, await adapter.pair_start(device.id, protocol))
        session.room_key = room.key
        self._pairing_meta[session.session_id] = session
        audit(
            "pairing_started",
            room_key=room.key,
            intent="pair.start",
            targets=[device.id],
            extra={"protocol": protocol, "session_id": session.session_id},
        )
        return session

    async def finish_pairing(self, session_id: str, pin: str) -> PairingSession:
        # Never log or audit the PIN.
        meta = self._pairing_meta.get(session_id)
        adapter_name = None
        if meta is not None:
            device = self.registry.device(meta.device_id)
            adapter_name = device.adapter
        else:
            # Fall back: ask each pairable adapter
            for name, adapter in self.adapters.items():
                if hasattr(adapter, "pair_finish"):
                    adapter_name = name
                    break
        if adapter_name is None:
            raise SafetyBlockedError("Unknown pairing session", reason="unknown_session")
        adapter = self.adapters[adapter_name]
        session = cast(PairingSession, await adapter.pair_finish(session_id, pin))
        audit(
            "pairing_finished",
            room_key=session.room_key or (meta.room_key if meta else None),
            intent="pair.finish",
            targets=[session.device_id],
            result={"state": session.state},
        )
        return session

    async def list_apps(self, room_name: str) -> list[AppInfo]:
        room = self.registry.resolve_room(room_name)
        apple = self.registry.room_targets(room.key)["apple_tv"]
        if apple is None:
            return []
        return cast(list[AppInfo], await self.adapters[apple.adapter].list_apps(apple.id))

    async def _run_plan(
        self,
        plan_factory: Any,
        *,
        dry_run: bool = False,
        idempotency_key: str | None = None,
        confirm_power_off: bool = False,
    ) -> ActionResult:
        plan = plan_factory()
        if plan.intent == "power.off" and not confirm_power_off and not dry_run:
            raise SafetyBlockedError(
                "Power off requires confirm_power_off=true in the same request",
                reason="power_off_confirmation",
            )
        result = await self.executor.execute(
            plan,
            dry_run=dry_run,
            idempotency_key=idempotency_key,
        )
        return result

    async def set_power(
        self,
        room_name: str,
        state: str,
        *,
        dry_run: bool = False,
        confirm_power_off: bool = False,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        return await self._run_plan(
            lambda: self.planner.plan_power(room_name, state, dry_run=dry_run),
            dry_run=dry_run,
            idempotency_key=idempotency_key,
            confirm_power_off=confirm_power_off,
        )

    async def open_app(
        self,
        room_name: str,
        app: str,
        *,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        return await self._run_plan(
            lambda: self.planner.plan_open_app(room_name, app, dry_run=dry_run),
            dry_run=dry_run,
            idempotency_key=idempotency_key,
        )

    async def open_content(
        self,
        room_name: str,
        *,
        url: str | None = None,
        alias: str | None = None,
        resume: bool = False,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        target = self.content.resolve(url=url, alias=alias)
        provider_warning = self.content.warning_for(target)
        result = await self._run_plan(
            lambda: self.planner.plan_open_content(
                room_name,
                url=target.url,
                alias=alias,
                expected_app=target.expected_app or target.app_bundle_id,
                expected_title=target.title,
                dry_run=dry_run,
            ),
            dry_run=dry_run,
            idempotency_key=idempotency_key,
        )
        if provider_warning:
            result.warnings.append(provider_warning)
        # Honest resume / deep-link semantics — match requested app/title when known.
        if result.execution_status in {ExecutionStatus.SUCCEEDED, ExecutionStatus.DRY_RUN}:
            after = result.steps[-1].observed_after if result.steps else {}
            np = after.get("now_playing") if isinstance(after, dict) else None
            current_app = after.get("current_app") if isinstance(after, dict) else None
            expected_app = target.expected_app or target.app_bundle_id
            observed_title = (np or {}).get("title") if isinstance(np, dict) else None
            playing = bool(
                np and str(np.get("device_state") or "").lower() in {"playing", "paused"}
            )
            app_ok = bool(expected_app and current_app and current_app == expected_app)
            title_ok = bool(
                target.title
                and observed_title
                and (
                    target.title.casefold() in str(observed_title).casefold()
                    or str(observed_title).casefold() in target.title.casefold()
                )
            )
            if app_ok and title_ok and playing:
                result.content_outcome = ContentOutcome.VERIFIED_PLAYBACK
                result.verification_status = VerificationStatus.VERIFIED
            elif expected_app and current_app and current_app != expected_app:
                result.content_outcome = ContentOutcome.FAILED
                result.verification_status = VerificationStatus.FAILED
                result.warnings.append(
                    f"Wrong app after open: expected {expected_app}, observed {current_app}"
                )
            elif app_ok and target.url and target.confidence >= 0.6:
                result.content_outcome = ContentOutcome.OPENED_TARGET
                result.verification_status = VerificationStatus.DEGRADED
            elif current_app:
                result.content_outcome = ContentOutcome.OPENED_APP_ONLY
                result.verification_status = VerificationStatus.DEGRADED
            else:
                result.content_outcome = ContentOutcome.OPENED_TARGET
                result.verification_status = VerificationStatus.UNVERIFIED
            if resume:
                result.warnings.append(
                    "resume means provider-profile progress if any; "
                    "no universal Apple watch-history API"
                )
                if result.content_outcome != ContentOutcome.VERIFIED_PLAYBACK:
                    result.warnings.append(
                        "Exact resume progress was not verified from now-playing metadata"
                    )
                    result.verification_status = VerificationStatus.UNVERIFIED
        result.observed_after["content_target"] = redact_obj(target.model_dump(mode="json"))
        return result

    async def control_playback(
        self,
        room_name: str,
        action: str,
        *,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        return await self._run_plan(
            lambda: self.planner.plan_transport(room_name, action, dry_run=dry_run),
            dry_run=dry_run,
            idempotency_key=idempotency_key,
        )

    async def press_remote_key(
        self,
        room_name: str,
        key: str,
        *,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        room = self.registry.resolve_room(room_name)
        apple = self.registry.room_targets(room.key)["apple_tv"]
        if apple is None:
            raise SafetyBlockedError("No Apple TV", reason="missing_device")
        from home_media.models import ActionPlan, PlanStep

        plan = ActionPlan(
            intent="remote.press",
            room_key=room.key,
            steps=[
                PlanStep(
                    action="press_key",
                    target_device_id=apple.id,
                    adapter=apple.adapter,
                    params={"key": key},
                    dry_run=dry_run,
                )
            ],
        )
        return await self.executor.execute(plan, dry_run=dry_run, idempotency_key=idempotency_key)

    async def enter_text(
        self,
        room_name: str,
        text: str,
        *,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        room = self.registry.resolve_room(room_name)
        apple = self.registry.room_targets(room.key)["apple_tv"]
        if apple is None:
            raise SafetyBlockedError("No Apple TV", reason="missing_device")
        from home_media.models import ActionPlan, PlanStep

        plan = ActionPlan(
            intent="text.enter",
            room_key=room.key,
            steps=[
                PlanStep(
                    action="enter_text",
                    target_device_id=apple.id,
                    adapter=apple.adapter,
                    params={"text": text},
                    dry_run=dry_run,
                )
            ],
        )
        return await self.executor.execute(plan, dry_run=dry_run, idempotency_key=idempotency_key)

    async def get_volume(self, room_name: str) -> dict[str, Any]:
        room = self.registry.resolve_room(room_name)
        audio = self.registry.room_targets(room.key)["audio"]
        if audio is None:
            raise SafetyBlockedError("No audio target", reason="missing_device")
        return cast(dict[str, Any], await self.adapters[audio.adapter].get_volume(audio.id))

    async def set_volume(
        self,
        room_name: str,
        level: int,
        *,
        override_ceiling: bool = False,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        return await self._run_plan(
            lambda: self.planner.plan_volume_set(
                room_name,
                level,
                override_ceiling=override_ceiling,
                dry_run=dry_run,
            ),
            dry_run=dry_run,
            idempotency_key=idempotency_key,
        )

    async def change_volume(
        self,
        room_name: str,
        delta: int,
        *,
        override_ceiling: bool = False,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        room = self.registry.resolve_room(room_name)
        audio = self.registry.room_targets(room.key)["audio"]
        if audio is None:
            raise SafetyBlockedError("No audio target", reason="missing_device")
        # Convert relative → bounded absolute after read so ceiling is enforceable.
        current = await self.adapters[audio.adapter].get_volume(audio.id)
        if current.get("level") is None:
            raise SafetyBlockedError(
                "Cannot enforce volume ceiling without an absolute volume reading",
                reason="volume_ceiling_unguarded",
            )
        target_level = max(0, min(100, int(current["level"]) + int(delta)))
        ceiling = self.registry.config.volume_ceiling
        if (
            target_level > ceiling
            and self.registry.config.volume_ceiling_override_required
            and not override_ceiling
        ):
            raise SafetyBlockedError(
                f"Volume {target_level} exceeds ceiling {ceiling}; pass override_ceiling=true",
                reason="volume_ceiling",
            )
        from home_media.models import ActionPlan, PlanStep

        plan = ActionPlan(
            intent="volume.change",
            room_key=room.key,
            steps=[
                PlanStep(
                    action="set_volume",
                    target_device_id=audio.id,
                    adapter=audio.adapter,
                    params={"level": target_level, "from_delta": delta},
                    dry_run=dry_run,
                )
            ],
            warnings=[f"Relative delta {delta} converted to absolute target {target_level}"],
        )
        return await self.executor.execute(plan, dry_run=dry_run, idempotency_key=idempotency_key)

    async def set_tv_input(
        self,
        room_name: str,
        source: str,
        *,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        return await self._run_plan(
            lambda: self.planner.plan_tv_input(room_name, source, dry_run=dry_run),
            dry_run=dry_run,
            idempotency_key=idempotency_key,
        )

    async def execute_watch_scene(
        self,
        room_name: str,
        *,
        service: str | None = None,
        url: str | None = None,
        volume: int | None = None,
        override_ceiling: bool = False,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        return await self._run_plan(
            lambda: self.planner.plan_watch_scene(
                room_name,
                service=service,
                url=url,
                volume=volume,
                override_ceiling=override_ceiling,
                dry_run=dry_run,
            ),
            dry_run=dry_run,
            idempotency_key=idempotency_key,
        )

    def _netflix_profile_index(self) -> int:
        prefs = self.registry.config.provider_prefs.get("netflix") or {}
        raw = prefs.get("profile_index", 2)
        try:
            return max(1, int(str(raw)))
        except (TypeError, ValueError):
            return 2

    def _netflix_profile_name(self) -> str | None:
        prefs = self.registry.config.provider_prefs.get("netflix") or {}
        raw = prefs.get("profile_name")
        if raw is None:
            return None
        name = str(raw).strip()
        return name or None

    async def prepare_content(
        self,
        room_name: str,
        title: str,
        *,
        provider: str | None = None,
        goal: ContentGoal | str = ContentGoal.SEARCH_READY,
        wake: bool = True,
        dry_run: bool = False,
        url: str | None = None,
        profile_index: int | None = None,
        idempotency_key: str | None = None,
    ) -> PrepareContentResult:
        """Semantic content preparation via the provider-aware route ladder."""
        from home_media.connection import fingerprint_request
        from home_media.errors import IdempotencyConflictError

        goal_enum = ContentGoal(goal) if isinstance(goal, str) else goal
        room = self.registry.resolve_room(room_name)
        apple = self.registry.room_targets(room.key)["apple_tv"]
        if apple is None:
            raise SafetyBlockedError("No Apple TV in room", reason="missing_device")

        resolved_profile = (
            profile_index if profile_index is not None else self._netflix_profile_index()
        )
        fp = fingerprint_request(
            "prepare_content",
            room.key,
            {
                "title": normalize_query(title),
                "provider": (provider or "").lower() or None,
                "goal": goal_enum.value,
                "wake": wake,
                "url": url,
                "profile_index": resolved_profile,
                "dry_run": dry_run,
            },
        )

        lock = self.executor._lock_for(room.key)  # noqa: SLF001 — share room serialization
        waiter: asyncio.Future[PrepareContentResult] | None = None
        async with lock:
            if idempotency_key:
                prior = self._prepare_idempotency.get(idempotency_key)
                prior_fp = self._prepare_fingerprints.get(idempotency_key)
                if prior is not None and prior_fp == fp:
                    return prior
                if prior_fp is not None and prior_fp != fp:
                    raise IdempotencyConflictError(
                        "Idempotency key reused with a different prepare_content request"
                    )
                inflight = self._prepare_inflight.get(idempotency_key)
                if inflight is not None:
                    waiter = inflight
                else:
                    loop = asyncio.get_running_loop()
                    fut: asyncio.Future[PrepareContentResult] = loop.create_future()
                    self._prepare_inflight[idempotency_key] = fut
                    self._prepare_fingerprints[idempotency_key] = fp

        if waiter is not None:
            return await waiter

        try:
            async with lock:
                result = await self._prepare_content_unlocked(
                    room_key=room.key,
                    apple_id=apple.id,
                    title=title,
                    provider=provider,
                    goal_enum=goal_enum,
                    wake=wake,
                    dry_run=dry_run,
                    url=url,
                    profile_index=resolved_profile,
                    idempotency_key=idempotency_key,
                )
        except Exception as exc:
            if idempotency_key:
                async with lock:
                    fut2 = self._prepare_inflight.pop(idempotency_key, None)
                    self._prepare_fingerprints.pop(idempotency_key, None)
                    if fut2 is not None and not fut2.done():
                        fut2.set_exception(exc)
            raise

        if idempotency_key:
            async with lock:
                self._prepare_idempotency[idempotency_key] = result
                self._prepare_fingerprints[idempotency_key] = fp
                fut3 = self._prepare_inflight.pop(idempotency_key, None)
                if fut3 is not None and not fut3.done():
                    fut3.set_result(result)
        return result

    async def _prepare_content_unlocked(
        self,
        *,
        room_key: str,
        apple_id: str,
        title: str,
        provider: str | None,
        goal_enum: ContentGoal,
        wake: bool,
        dry_run: bool,
        url: str | None,
        profile_index: int,
        idempotency_key: str | None,
    ) -> PrepareContentResult:
        provider_norm = (provider or "").lower() or None
        if provider_norm is None and url:
            from home_media.content.urls import validate_content_url

            provider_norm = validate_content_url(url).provider
        if provider_norm is None:
            # Alias / title cache hints
            alias_url = self.registry.config.content_aliases.get(title.lower())
            if alias_url:
                from home_media.content.urls import validate_content_url

                provider_norm = validate_content_url(alias_url).provider

        cached = None
        if provider_norm:
            cached = self.content_cache.lookup_by_title(provider_norm, title)
        has_verified = cached is not None or bool(url)
        # Apple Search participates for many providers; Netflix is Originals-only.
        apple_participates = provider_norm not in {"netflix", "youtube"}
        route_plan = plan_content_routes(
            title,
            provider_norm,
            goal_enum,
            apple_search_participates=apple_participates,
            has_verified_deep_link=has_verified,
        )

        result = PrepareContentResult(
            room_key=room_key,
            title=title,
            normalized_title=normalize_query(title),
            provider=provider_norm,
            goal=goal_enum,
            warnings=list(route_plan.warnings),
            physical_tv_state_known=False,
            idempotency_key=idempotency_key,
            selected_result=False,
            playback_started=False,
        )
        result.warnings.append(
            "Physical TV / CEC power is not independently confirmed by this operation"
        )

        if dry_run:
            result.route_used = route_plan.routes[0] if route_plan.routes else None
            result.terminal_status = TerminalStatus.HANDOFF
            result.stages.append(
                PrepareStage(
                    name="dry_run",
                    status=StageStatus.SUCCEEDED,
                    evidence=[f"routes={[r.value for r in route_plan.routes]}"],
                )
            )
            result.verification_status = "not_applicable"
            return result

        if goal_enum == ContentGoal.RESUME:
            result.terminal_status = TerminalStatus.UNSUPPORTED_GOAL
            result.warnings.append(
                "resume goal is deferred until search_ready is proven; use search_ready"
            )
            result.verification_status = "failed"
            return result

        device = self.registry.device(apple_id)
        apple_adapter = self.adapters[device.adapter]

        for route in route_plan.routes:
            if route == ContentRoute.DIRECT_DEEP_LINK and goal_enum != ContentGoal.SEARCH_READY:
                launch = url or (cached.canonical_url if cached else None)
                if not launch and provider_norm == "netflix":
                    # Prefer alias map for known titles.
                    launch = self.registry.config.content_aliases.get(title.lower())
                if not launch:
                    continue
                if wake and hasattr(apple_adapter, "set_power"):
                    from home_media.models import PowerState

                    await apple_adapter.set_power(apple_id, PowerState.ON)
                status = await apple_adapter.open_url(apple_id, launch)
                result.route_used = route
                result.stages.append(
                    PrepareStage(
                        name="direct_deep_link",
                        status=StageStatus.SUCCEEDED,
                        evidence=[
                            f"url_form={launch.split(':', 1)[0]}",
                            f"current_app={status.current_app}",
                        ],
                    )
                )
                result.observed_states.append("title_detail_unverified")
                result.terminal_status = TerminalStatus.TITLE_DETAIL_UNVERIFIED
                result.verification_status = "unverified"
                result.warnings.append(
                    "Deep link dispatched; title-detail confirmation requires user or screenshot"
                )
                break

            if route == ContentRoute.APPLE_SYSTEM_SEARCH and provider_norm != "netflix":
                # Bounded Apple Search path for participating providers.
                from home_media.content.prepare import queries_match

                if wake and hasattr(apple_adapter, "set_power"):
                    from home_media.models import PowerState

                    await apple_adapter.set_power(apple_id, PowerState.ON)
                await apple_adapter.open_app(apple_id, "com.apple.TVSearch")
                focus = "unknown"
                if hasattr(apple_adapter, "wait_keyboard_focused"):
                    focus = await apple_adapter.wait_keyboard_focused(apple_id)
                if focus != "focused":
                    result.route_used = route
                    result.terminal_status = TerminalStatus.KEYBOARD_NOT_FOCUSED
                    result.stages.append(
                        PrepareStage(
                            name="apple_system_search",
                            status=StageStatus.FAILED,
                            evidence=[f"focus={focus}"],
                        )
                    )
                    continue
                if hasattr(apple_adapter, "keyboard_text_clear"):
                    await apple_adapter.keyboard_text_clear(apple_id)
                await apple_adapter.enter_text(apple_id, title)
                readback = None
                if hasattr(apple_adapter, "keyboard_text_get"):
                    readback = await apple_adapter.keyboard_text_get(apple_id)
                result.route_used = route
                result.selected_result = False
                result.playback_started = False
                if queries_match(title, readback):
                    result.terminal_status = TerminalStatus.QUERY_VERIFIED
                    result.verification_status = "verified"
                else:
                    result.terminal_status = TerminalStatus.QUERY_MISMATCH
                    result.verification_status = "failed"
                result.stages.append(
                    PrepareStage(
                        name="apple_system_search",
                        status=StageStatus.SUCCEEDED
                        if result.terminal_status == TerminalStatus.QUERY_VERIFIED
                        else StageStatus.FAILED,
                        evidence=[f"readback={readback!r}", f"focus={focus}"],
                    )
                )
                break

            if route == ContentRoute.PROVIDER_STATE_MACHINE and provider_norm == "netflix":
                result = await self._run_netflix_search_ready(
                    room_key=room_key,
                    device_id=apple_id,
                    title=title,
                    profile_index=profile_index,
                    wake=wake,
                    goal=goal_enum,
                    idempotency_key=idempotency_key,
                    prior_warnings=list(result.warnings),
                )
                break

            if route in {ContentRoute.SCREENSHOT_RECOVERY, ContentRoute.HUMAN_HANDOFF}:
                result.route_used = route
                result.terminal_status = TerminalStatus.HANDOFF
                result.warnings.append(
                    "Bounded human handoff: screenshots require separate authorization"
                )
                result.stages.append(
                    PrepareStage(
                        name=route.value,
                        status=StageStatus.SKIPPED,
                        evidence=["screenshot_stack_unauthorized_or_handoff"],
                    )
                )
                break

        _ = normalize_title
        _ = PrepareContentRequest
        return result

    async def _run_netflix_search_ready(
        self,
        *,
        room_key: str,
        device_id: str,
        title: str,
        profile_index: int,
        wake: bool,
        goal: ContentGoal,
        idempotency_key: str | None,
        prior_warnings: list[str],
    ) -> PrepareContentResult:
        from home_media.observers.classify import ScreenClassifierResult
        from home_media.providers.base import ProviderObservation, ProviderState
        from home_media.providers.executor import ProviderRecipeRunner
        from home_media.providers.netflix import NetflixAdapter

        netflix = NetflixAdapter()
        profile_name = self._netflix_profile_name()
        transitions = netflix.plan_search_ready(
            title, profile_index=profile_index, profile_name=profile_name
        )
        apple_adapter = self.adapters["apple_tv"]
        state_box: dict[str, Any] = {
            "keyboard_text": None,
            "bundle": netflix.app_bundle_id,
            "action_token": 0,
            "capture_token": -1,
            "screenshot_state": None,
            "screenshot_confidence": 0.0,
            "screenshot_evidence": [],
            "screenshot_anchors": [],
            "highlighted_profile_name": None,
            "classifier_name": None,
            "last_shot_sha": None,
            "last_classify": None,
        }

        # Live path: exact room Apple TV must have a confirmed observer binding.
        if not self._use_fakes:
            svc = self.screenshot_service
            if svc is None:
                return self._netflix_handoff_result(
                    room_key=room_key,
                    title=title,
                    goal=goal,
                    idempotency_key=idempotency_key,
                    prior_warnings=prior_warnings,
                    evidence=[
                        "screenshot_gate_required",
                        "no_screenshot_service",
                        "refusing_blind_netflix_navigation",
                    ],
                    warning=(
                        "Screenshot observer required before live Netflix navigation; "
                        "user screen narration is not part of normal operation"
                    ),
                )
            try:
                svc.require_binding_for_device(room_key, device_id)
            except SafetyBlockedError as exc:
                return self._netflix_handoff_result(
                    room_key=room_key,
                    title=title,
                    goal=goal,
                    idempotency_key=idempotency_key,
                    prior_warnings=prior_warnings,
                    evidence=[
                        "screenshot_gate_required",
                        str(exc.details.get("reason") or "no_observer_binding"),
                        "refusing_blind_netflix_navigation",
                    ],
                    warning=exc.message,
                )

        async def _maybe_capture(*, force: bool = False) -> ScreenClassifierResult | None:
            """Capture at state boundaries only — never poll without an intervening action."""
            svc = self.screenshot_service
            if svc is None:
                return None
            if (
                not force
                and int(state_box["capture_token"]) >= int(state_box["action_token"])
            ):
                cached = state_box.get("last_classify")
                return cached if isinstance(cached, ScreenClassifierResult) else None
            try:
                shot = await svc.capture_room(room_key, save=True, prune=True)
            except HomeMediaError as exc:
                state_box["screenshot_evidence"] = [f"capture_blocked:{exc.code.value}"]
                state_box["screenshot_state"] = None
                state_box["screenshot_confidence"] = 0.0
                state_box["screenshot_anchors"] = []
                state_box["highlighted_profile_name"] = None
                state_box["last_classify"] = None
                return None
            except Exception as exc:  # noqa: BLE001
                state_box["screenshot_evidence"] = [f"capture_error:{type(exc).__name__}"]
                state_box["screenshot_state"] = None
                state_box["screenshot_confidence"] = 0.0
                state_box["screenshot_anchors"] = []
                state_box["highlighted_profile_name"] = None
                state_box["last_classify"] = None
                return None
            state_box["capture_token"] = state_box["action_token"]
            state_box["last_shot_sha"] = shot.metadata.sha256
            classified_raw = await svc.classify_capture(shot, requested_query=title)
            if not isinstance(classified_raw, ScreenClassifierResult):
                state_box["last_classify"] = None
                return None
            classified = classified_raw
            state_box["last_classify"] = classified
            state_box["screenshot_state"] = (
                classified.provider_state.value if classified.provider_state else None
            )
            state_box["screenshot_confidence"] = classified.confidence
            state_box["screenshot_anchors"] = list(classified.anchor_codes)
            state_box["highlighted_profile_name"] = classified.highlighted_profile_name
            state_box["classifier_name"] = classified.classifier_name
            # Safe log fields only — no OCR dump / paths / pixels.
            state_box["screenshot_evidence"] = [
                f"classifier:{classified.classifier_name}",
                f"confidence:{classified.confidence:.2f}",
                *(f"anchor:{code}" for code in classified.anchor_codes[:8]),
            ]
            if classified.screenshot_sha:
                state_box["screenshot_evidence"].append(f"sha256:{classified.screenshot_sha}")
            if classified.error:
                state_box["screenshot_evidence"].append(f"classify_error:{classified.error}")
            return classified

        async def observe() -> ProviderObservation:
            # Live path never trusts manually injected user_confirmed_state.
            confirmed_state: ProviderState | None = None
            if self._use_fakes and hasattr(apple_adapter, "provider_state"):
                confirmed = apple_adapter.provider_state.get(device_id)
                if isinstance(confirmed, ProviderState):
                    confirmed_state = confirmed
                elif isinstance(confirmed, str):
                    try:
                        confirmed_state = ProviderState(confirmed)
                    except ValueError:
                        confirmed_state = None
            status = None
            try:
                status = await apple_adapter.get_status(device_id)
            except Exception:  # noqa: BLE001
                status = None
            focus = False
            text: str | None = None
            if hasattr(apple_adapter, "keyboard_focus_state"):
                focus_label = await apple_adapter.keyboard_focus_state(device_id)
                focus = focus_label == "focused"
            # Only trust keyboard text when Apple TV reports real focus.
            if hasattr(apple_adapter, "keyboard_text_get") and focus:
                try:
                    text = await apple_adapter.keyboard_text_get(device_id)
                except Exception:  # noqa: BLE001
                    text = None
            # Screenshot only after an intervening action (never poll the remote).
            if (
                int(state_box["action_token"]) > 0
                and int(state_box["action_token"]) > int(state_box["capture_token"])
            ):
                await _maybe_capture(force=False)
            evidence = ["prepare_content_observe", *list(state_box["screenshot_evidence"])]
            conf = float(state_box["screenshot_confidence"] or 0.0)
            if self._use_fakes and confirmed_state is not None and conf < 0.9:
                # Fake path without pixels still needs navigable confidence.
                conf = max(conf, 0.95)
            screenshot_state = state_box["screenshot_state"]
            anchors = list(state_box["screenshot_anchors"])
            highlighted = state_box["highlighted_profile_name"]
            if (
                self._use_fakes
                and confirmed_state == ProviderState.PROFILE_PICKER
                and not anchors
            ):
                from home_media.observers.netflix_anchors import (
                    ANCHOR_HIGHLIGHTED_PROFILE,
                    ANCHOR_PROFILE_CHOOSE,
                    ANCHOR_PROFILE_WHOS,
                )

                anchors = [ANCHOR_PROFILE_CHOOSE, ANCHOR_PROFILE_WHOS]
                if highlighted is None and hasattr(apple_adapter, "highlighted_profile"):
                    highlighted = apple_adapter.highlighted_profile.get(device_id)
                if highlighted is not None:
                    anchors.append(ANCHOR_HIGHLIGHTED_PROFILE)
            return ProviderObservation(
                current_app=status.current_app if status else state_box["bundle"],
                keyboard_focus=focus,
                keyboard_text=text,
                now_playing_title=(
                    status.now_playing.title
                    if status is not None and status.now_playing is not None
                    else None
                ),
                screenshot_state=screenshot_state,
                user_confirmed_state=confirmed_state if self._use_fakes else None,
                confidence=conf,
                evidence=evidence,
                screenshot_anchors=anchors,
                highlighted_profile_name=highlighted,
                classifier_name=state_box["classifier_name"],
            )

        async def execute(action: dict[str, Any]) -> None:
            atype = str(action.get("type", "")).lower()
            if atype == "launch_app":
                if wake and hasattr(apple_adapter, "set_power"):
                    from home_media.models import PowerState

                    await apple_adapter.set_power(device_id, PowerState.ON)
                await apple_adapter.open_app(
                    device_id, action.get("app_bundle_id") or state_box["bundle"]
                )
                state_box["action_token"] = int(state_box["action_token"]) + 1
                return
            if atype in {"select", "select_profile"}:
                await apple_adapter.press_key(device_id, "select")
                state_box["action_token"] = int(state_box["action_token"]) + 1
                return
            if atype == "press_key":
                await apple_adapter.press_key(device_id, str(action["key"]))
                # Never invent synthetic keyboard focus from arrow presses.
                state_box["action_token"] = int(state_box["action_token"]) + 1
                return
            if atype == "keyboard_set":
                text_value = str(action.get("text") or "")
                focus_label = "unfocused"
                if hasattr(apple_adapter, "keyboard_focus_state"):
                    focus_label = await apple_adapter.keyboard_focus_state(device_id)
                if focus_label != "focused":
                    raise SafetyBlockedError(
                        "keyboard_set refused without real Apple TV keyboard focus",
                        reason="keyboard_not_focused",
                    )
                # Replace at the semantic level. Clearing first prevents a
                # TextAppend-only fallback from concatenating onto a restored
                # prior query; one post-action screenshot verifies the result.
                if hasattr(apple_adapter, "keyboard_text_clear"):
                    await apple_adapter.keyboard_text_clear(device_id)
                if text_value:
                    await apple_adapter.enter_text(device_id, text_value)
                # Do not cache synthetic verified text — observe() reads focus+get.
                state_box["action_token"] = int(state_box["action_token"]) + 1
                return
            if atype == "keyboard_readback":
                # Readback is observed via keyboard_text_get under real focus only.
                return
            raise UnsupportedError("provider.action", reason=f"Unknown action {atype}")

        # Preflight capture when pixels are actually available (bound live or fake provider).
        svc = self.screenshot_service
        can_capture = svc is not None and (
            getattr(svc, "_provider", None) is not None or bool(svc.bindings.bindings)
        )
        if can_capture:
            pre = await _maybe_capture(force=True)
            if pre is None:
                return self._netflix_handoff_result(
                    room_key=room_key,
                    title=title,
                    goal=goal,
                    idempotency_key=idempotency_key,
                    prior_warnings=prior_warnings,
                    evidence=list(state_box["screenshot_evidence"])
                    or ["capture_failed_preflight"],
                    warning="Screenshot capture failed before Netflix navigation",
                )
            if pre.provider_state is None:
                return self._netflix_handoff_result(
                    room_key=room_key,
                    title=title,
                    goal=goal,
                    idempotency_key=idempotency_key,
                    prior_warnings=prior_warnings,
                    evidence=[
                        *list(state_box["screenshot_evidence"]),
                        "unclassified_frame",
                    ],
                    warning="Unclassified screenshot; refusing Netflix navigation",
                )
            if pre.provider_state in {
                ProviderState.PLAYING,
                ProviderState.TITLE_DETAIL,
                ProviderState.BLANK_OR_PROTECTED,
                ProviderState.ERROR_OR_MODAL,
            }:
                return self._netflix_handoff_result(
                    room_key=room_key,
                    title=title,
                    goal=goal,
                    idempotency_key=idempotency_key,
                    prior_warnings=prior_warnings,
                    evidence=[
                        *list(state_box["screenshot_evidence"]),
                        f"preflight_stop:{pre.provider_state.value}",
                    ],
                    warning=(
                        f"Observed {pre.provider_state.value} before Netflix launch; "
                        "refusing navigation"
                    ),
                )
            # UNKNOWN (e.g. Apple Home) may proceed to launch; in-app nav still gated.
            if (
                pre.provider_state != ProviderState.UNKNOWN
                and pre.confidence < 0.5
            ):
                return self._netflix_handoff_result(
                    room_key=room_key,
                    title=title,
                    goal=goal,
                    idempotency_key=idempotency_key,
                    prior_warnings=prior_warnings,
                    evidence=[
                        *list(state_box["screenshot_evidence"]),
                        "screenshot_gate_low_confidence",
                    ],
                    warning="Low-confidence screenshot; refusing Netflix navigation",
                )

        runner = ProviderRecipeRunner(
            observe=observe,
            execute=execute,
            provider="netflix",
        )
        raw = await runner.run_search_ready(
            room_key=room_key,
            title=title,
            transitions=transitions,
            classify=netflix.classify,
            required_profile_name=profile_name,
        )
        observed = list(raw.get("observed_states") or [])
        if ProviderState.BLANK_OR_PROTECTED.value in observed:
            raw["terminal_status"] = TerminalStatus.HANDOFF.value
            raw["warnings"] = list(raw.get("warnings") or []) + [
                "Blank/protected screenshot treated as unobservable; stopped"
            ]
        result = PrepareContentResult.model_validate(
            {
                **raw,
                "goal": goal.value,
                "idempotency_key": idempotency_key,
                "warnings": prior_warnings + list(raw.get("warnings") or []),
                "physical_tv_state_known": False,
            }
        )
        result.route_used = ContentRoute.PROVIDER_STATE_MACHINE
        return result

    def _netflix_handoff_result(
        self,
        *,
        room_key: str,
        title: str,
        goal: ContentGoal,
        idempotency_key: str | None,
        prior_warnings: list[str],
        evidence: list[str],
        warning: str,
    ) -> PrepareContentResult:
        return PrepareContentResult(
            room_key=room_key,
            title=title,
            normalized_title=normalize_query(title),
            provider="netflix",
            goal=goal,
            route_used=ContentRoute.SCREENSHOT_RECOVERY,
            stages=[
                PrepareStage(
                    name="screenshot_gate",
                    status=StageStatus.FAILED,
                    evidence=evidence,
                )
            ],
            observed_states=["unknown"],
            selected_result=False,
            playback_started=False,
            verification_status="failed",
            terminal_status=TerminalStatus.HANDOFF,
            warnings=prior_warnings + [warning],
            physical_tv_state_known=False,
            idempotency_key=idempotency_key,
        )
