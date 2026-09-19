"""Application service shared by CLI and MCP."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from home_media.audit import audit, configure_logging, redact_obj
from home_media.computer_use import (
    AppleTVComputerUseController,
    ComputerUseAction,
    ComputerUseActionKind,
    ComputerUseActionResult,
    ComputerUseState,
    ComputerUseTerminal,
    DeviceContext,
    VerifiedMacro,
    VerifiedMacroResult,
    netflix_exit_paused_playback_macro,
    netflix_focus_first_result_macro,
    netflix_home_to_search_macro,
    netflix_profile_select_macro,
    netflix_set_query_macro,
    semantics_from_classifier,
    state_age_seconds,
)
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
from home_media.errors import (
    HomeMediaError,
    MutationsDisabledError,
    SafetyBlockedError,
    UnsupportedError,
)
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
    PowerState,
    RoomStatus,
    SupportLevel,
    VerificationStatus,
)
from home_media.observers.screenshot import (
    prepare_screenshot_for_analysis,
    ui_stability_fingerprint,
)
from home_media.planner import Planner
from home_media.registry import RoomRegistry
from home_media.vision_policy import (
    CodexVisionPolicy,
    FocusedTargetKind,
    ScreenSurface,
    TitleMatch,
    VisionContext,
    VisionRemoteAction,
    is_playback_cta,
    labels_match,
)


class ApplicationService:
    def __init__(
        self,
        registry: RoomRegistry,
        adapters: dict[str, Any],
        *,
        executor: Executor | None = None,
        content_cache: VerifiedTargetCache | None = None,
        screenshot_service: Any | None = None,
        vision_policy: CodexVisionPolicy | None = None,
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
        self.vision_policy = vision_policy or CodexVisionPolicy()
        self._use_fakes = use_fakes
        self.computer_use = self._build_computer_use_controller()
        self._pairing_meta: dict[str, PairingSession] = {}
        self._prepare_idempotency: dict[str, PrepareContentResult] = {}
        self._prepare_fingerprints: dict[str, str] = {}
        self._prepare_inflight: dict[str, asyncio.Future[PrepareContentResult]] = {}
        self._prepare_failed: set[str] = set()
        self._prepare_touched_at: dict[str, float] = {}
        self._prepare_idempotency_lock = asyncio.Lock()
        self._prepare_idempotency_ttl_s = 15 * 60.0
        self._prepare_idempotency_max_entries = 256

    def _build_computer_use_controller(self) -> AppleTVComputerUseController:
        """Build the one process-scoped Apple TV observe/action seam.

        The screenshot service owns the persistent DVT worker and the Apple TV
        adapter owns its persistent pyatv connection.  This controller only
        coordinates those long-lived components; it never spawns a per-action
        connection or capture process itself.
        """

        def _target(room_name: str) -> tuple[str, Any]:
            room = self.registry.resolve_room(room_name)
            apple = self.registry.room_targets(room.key).get("apple_tv")
            if apple is None:
                raise UnsupportedError(
                    "computer_use",
                    reason=f"No Apple TV configured for {room.key}",
                )
            return apple.id, self.adapters[apple.adapter]

        async def _frame(room_name: str) -> Any:
            if self.screenshot_service is None:
                raise UnsupportedError(
                    "computer_use.observe",
                    reason="Screenshot observer is unavailable",
                )
            return await self.screenshot_service.capture_room(
                room_name,
                save=True,
                prune=True,
            )

        async def _context(room_name: str) -> DeviceContext:
            device_id, adapter = _target(room_name)
            context = DeviceContext()
            try:
                status = await adapter.get_status(device_id)
                context.current_app = status.current_app
                context.power = status.power
                if status.now_playing is not None:
                    context.now_playing_title = status.now_playing.title
                    context.now_playing_series = status.now_playing.album
                    context.playback_state = status.now_playing.device_state
            except Exception:  # noqa: BLE001 - screenshot remains authoritative
                pass
            if hasattr(adapter, "keyboard_focus_state"):
                try:
                    focus = await adapter.keyboard_focus_state(device_id)
                    context.keyboard_focused = focus == "focused"
                except Exception:  # noqa: BLE001 - optional signal
                    context.keyboard_focused = None
            if context.keyboard_focused and hasattr(adapter, "keyboard_text_get"):
                try:
                    context.keyboard_text = await adapter.keyboard_text_get(device_id)
                except Exception:  # noqa: BLE001 - optional signal
                    context.keyboard_text = None
            return context

        async def _semantics(frame: Any) -> Any:
            if self.screenshot_service is None:
                raise UnsupportedError(
                    "computer_use.observe",
                    reason="Screenshot classifier is unavailable",
                )
            classified = await self.screenshot_service.classify_capture(
                prepare_screenshot_for_analysis(frame)
            )
            return semantics_from_classifier(
                classified,
                document=classified.ocr_document,
            )

        async def _execute(room_name: str, action: ComputerUseAction) -> None:
            self.require_mutations_enabled(f"computer_use.{action.kind.value}")
            device_id, adapter = _target(room_name)
            if action.kind == ComputerUseActionKind.WAKE:
                await adapter.set_power(device_id, PowerState.ON)
                return
            if action.kind == ComputerUseActionKind.LAUNCH_APP:
                if action.wake_before:
                    await adapter.set_power(device_id, PowerState.ON)
                assert action.app_bundle_id is not None
                await adapter.open_app(device_id, action.app_bundle_id)
                return
            if action.kind == ComputerUseActionKind.PRESS_KEY:
                assert action.key is not None
                await adapter.press_key(device_id, action.key)
                return
            if action.kind == ComputerUseActionKind.SET_TEXT:
                focus = "unfocused"
                if hasattr(adapter, "keyboard_focus_state"):
                    focus = await adapter.keyboard_focus_state(device_id)
                if focus != "focused":
                    raise SafetyBlockedError(
                        "Text replacement requires real Apple TV keyboard focus",
                        reason="computer_use_keyboard_not_focused",
                    )
                if hasattr(adapter, "keyboard_text_clear"):
                    await adapter.keyboard_text_clear(device_id)
                if action.text:
                    await adapter.enter_text(device_id, action.text)
                return
            if action.kind == ComputerUseActionKind.TRANSPORT:
                assert action.transport_action is not None
                await adapter.control_transport(device_id, action.transport_action)
                return
            raise UnsupportedError(
                "computer_use.action",
                reason=f"Unsupported action {action.kind.value}",
            )

        return AppleTVComputerUseController(
            frame_source=_frame,
            context_source=_context,
            semantics_source=_semantics,
            action_executor=_execute,
        )

    async def observe_apple_tv(self, room_name: str) -> ComputerUseState:
        """Return a fresh exact-room screen plus structured agent context."""
        return await self.computer_use.observe(room_name)

    async def act_apple_tv(
        self,
        room_name: str,
        action: ComputerUseAction,
    ) -> ComputerUseActionResult:
        """Execute one generation-bound remote action and observe its result."""
        self.require_mutations_enabled("computer_use.act")
        return await self.computer_use.act(room_name, action)

    async def run_apple_tv_macro(
        self,
        room_name: str,
        macro: VerifiedMacro,
    ) -> VerifiedMacroResult:
        """Run a bounded visual-state-to-state fast path."""
        self.require_mutations_enabled(f"computer_use.macro.{macro.name}")
        return await self.computer_use.run_macro(room_name, macro)

    def _prune_prepare_idempotency(self) -> None:
        now = time.monotonic()
        completed_keys = [
            key for key in self._prepare_touched_at if key not in self._prepare_inflight
        ]
        for key in completed_keys:
            if now - self._prepare_touched_at[key] > self._prepare_idempotency_ttl_s:
                self._drop_prepare_idempotency(key)
        completed = sorted(
            (self._prepare_touched_at[key], key)
            for key in self._prepare_touched_at
            if key not in self._prepare_inflight
        )
        overflow = max(0, len(self._prepare_touched_at) - self._prepare_idempotency_max_entries)
        for _, key in completed[:overflow]:
            self._drop_prepare_idempotency(key)

    def _drop_prepare_idempotency(self, key: str) -> None:
        self._prepare_idempotency.pop(key, None)
        self._prepare_fingerprints.pop(key, None)
        self._prepare_failed.discard(key)
        self._prepare_touched_at.pop(key, None)

    def require_mutations_enabled(self, intent: str) -> None:
        """Enforce the process kill switch for every service-owned mutation path."""
        if not self.registry.config.mutations_enabled or not self.executor.mutations_enabled:
            raise MutationsDisabledError(intent=intent)

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
        from home_media.observers.binding import ObserverBindingStore
        from home_media.observers.service import RoomScreenshotService

        bindings = ObserverBindingStore.empty() if use_fakes else ObserverBindingStore.load()
        screenshot_service = RoomScreenshotService(registry, bindings=bindings)
        if use_fakes:
            from home_media.observers.fake import FakeScreenshotProvider, fixture_for_state
            from home_media.providers.base import ProviderState

            apple = adapters["apple_tv"]
            fake_frames = FakeScreenshotProvider(
                {
                    device.id: fixture_for_state(ProviderState.UNKNOWN)
                    for device in config.devices
                    if device.kind == DeviceKind.APPLE_TV
                }
            )
            fake_frames.link_provider_state(apple)
            screenshot_service._provider = fake_frames  # noqa: SLF001 - test/demo backend
        return cls(
            registry,
            adapters,
            content_cache=cache,
            screenshot_service=screenshot_service,
            use_fakes=use_fakes,
        )

    async def aclose(self) -> None:
        if self.screenshot_service is not None:
            close = getattr(self.screenshot_service, "aclose", None)
            if close is not None:
                await close()
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
        self.require_mutations_enabled("pair.start")
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
        self.require_mutations_enabled("pair.finish")
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
        from home_media.errors import AmbiguousOutcomeError, IdempotencyConflictError

        request_started = time.perf_counter()
        if not dry_run:
            self.require_mutations_enabled("prepare_content")
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

        room_lock = self.executor.room_lock(room.key)
        waiter: asyncio.Future[PrepareContentResult] | None = None
        async with self._prepare_idempotency_lock:
            self._prune_prepare_idempotency()
            if idempotency_key:
                self._prepare_touched_at[idempotency_key] = time.monotonic()
                prior = self._prepare_idempotency.get(idempotency_key)
                prior_fp = self._prepare_fingerprints.get(idempotency_key)
                if prior is not None and prior_fp == fp:
                    return prior
                if prior_fp is not None and prior_fp != fp:
                    raise IdempotencyConflictError(
                        "Idempotency key reused with a different prepare_content request"
                    )
                if idempotency_key in self._prepare_failed:
                    raise AmbiguousOutcomeError(
                        "Previous prepare_content attempt with this idempotency key failed "
                        "after execution began; refusing to dispatch it again",
                        details={"idempotency_key": idempotency_key},
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

        mutation_state = {"attempted": False}
        try:
            async with room_lock:
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
                    mutation_state=mutation_state,
                )
        except asyncio.CancelledError:
            if idempotency_key:
                async with self._prepare_idempotency_lock:
                    fut_cancelled = self._prepare_inflight.pop(idempotency_key, None)
                    if mutation_state["attempted"]:
                        self._prepare_failed.add(idempotency_key)
                        self._prepare_touched_at[idempotency_key] = time.monotonic()
                    else:
                        self._prepare_fingerprints.pop(idempotency_key, None)
                        self._prepare_touched_at.pop(idempotency_key, None)
                    if fut_cancelled is not None and not fut_cancelled.done():
                        if mutation_state["attempted"]:
                            cancelled_error = AmbiguousOutcomeError(
                                "prepare_content was cancelled after a device mutation; "
                                "refusing to dispatch this idempotency key again",
                                details={"idempotency_key": idempotency_key},
                            )
                            fut_cancelled.set_exception(cancelled_error)
                            fut_cancelled.exception()
                        else:
                            fut_cancelled.cancel()
            raise
        except Exception as exc:
            is_ambiguous = mutation_state["attempted"] or isinstance(exc, AmbiguousOutcomeError)
            reported = (
                exc
                if isinstance(exc, AmbiguousOutcomeError)
                else AmbiguousOutcomeError(
                    "prepare_content failed after a device mutation was attempted; "
                    "outcome may be ambiguous",
                    details={"cause": type(exc).__name__},
                )
                if is_ambiguous
                else exc
            )
            if idempotency_key:
                async with self._prepare_idempotency_lock:
                    fut2 = self._prepare_inflight.pop(idempotency_key, None)
                    if is_ambiguous:
                        self._prepare_failed.add(idempotency_key)
                        self._prepare_touched_at[idempotency_key] = time.monotonic()
                    else:
                        self._prepare_fingerprints.pop(idempotency_key, None)
                        self._prepare_touched_at.pop(idempotency_key, None)
                    if fut2 is not None and not fut2.done():
                        fut2.set_exception(reported)
                        # Mark the exception retrieved even when this request had no waiter.
                        # Concurrent waiters still receive it when awaiting the future.
                        fut2.exception()
            if reported is exc:
                raise
            raise reported from exc

        result.total_latency_ms = max(0, int((time.perf_counter() - request_started) * 1000))
        if idempotency_key:
            async with self._prepare_idempotency_lock:
                self._prepare_idempotency[idempotency_key] = result
                self._prepare_fingerprints[idempotency_key] = fp
                self._prepare_failed.discard(idempotency_key)
                self._prepare_touched_at[idempotency_key] = time.monotonic()
                fut3 = self._prepare_inflight.pop(idempotency_key, None)
                if fut3 is not None and not fut3.done():
                    fut3.set_result(result)
                self._prune_prepare_idempotency()
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
        mutation_state: dict[str, bool],
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

        device = self.registry.device(apple_id)
        apple_adapter = self.adapters[device.adapter]

        for route in route_plan.routes:
            route_started = time.perf_counter()
            if route == ContentRoute.DIRECT_DEEP_LINK and goal_enum != ContentGoal.SEARCH_READY:
                launch = url or (cached.canonical_url if cached else None)
                if not launch and provider_norm == "netflix":
                    # Prefer alias map for known titles.
                    launch = self.registry.config.content_aliases.get(title.lower())
                if not launch:
                    continue
                if wake and hasattr(apple_adapter, "set_power"):
                    from home_media.models import PowerState

                    mutation_state["attempted"] = True
                    await apple_adapter.set_power(apple_id, PowerState.ON)
                mutation_state["attempted"] = True
                status = await apple_adapter.open_url(apple_id, launch)
                result.route_used = route
                result.stages.append(
                    PrepareStage(
                        name="direct_deep_link",
                        status=StageStatus.SUCCEEDED,
                        latency_ms=max(0, int((time.perf_counter() - route_started) * 1000)),
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
                    "Deep link dispatched; exact-title confirmation still requires observation"
                )
                if provider_norm == "netflix":
                    # A Netflix URL often lands on a profile/home/detail screen.
                    # Continue into the same screenshot-verified controller
                    # instead of treating URL dispatch as terminal success.
                    continue
                break

            if route == ContentRoute.APPLE_SYSTEM_SEARCH and provider_norm != "netflix":
                # Bounded Apple Search path for participating providers.
                from home_media.content.prepare import queries_match

                if wake and hasattr(apple_adapter, "set_power"):
                    from home_media.models import PowerState

                    mutation_state["attempted"] = True
                    await apple_adapter.set_power(apple_id, PowerState.ON)
                mutation_state["attempted"] = True
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
                            latency_ms=max(0, int((time.perf_counter() - route_started) * 1000)),
                            evidence=[f"focus={focus}"],
                        )
                    )
                    continue
                if hasattr(apple_adapter, "keyboard_text_clear"):
                    mutation_state["attempted"] = True
                    await apple_adapter.keyboard_text_clear(apple_id)
                mutation_state["attempted"] = True
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
                        latency_ms=max(0, int((time.perf_counter() - route_started) * 1000)),
                        evidence=[f"readback={readback!r}", f"focus={focus}"],
                    )
                )
                break

            if route == ContentRoute.PROVIDER_STATE_MACHINE and provider_norm == "netflix":
                prior_stages = list(result.stages)
                prior_observed = list(result.observed_states)
                result = await self._run_netflix_search_ready(
                    room_key=room_key,
                    device_id=apple_id,
                    title=title,
                    profile_index=profile_index,
                    wake=wake,
                    goal=goal_enum,
                    idempotency_key=idempotency_key,
                    prior_warnings=list(result.warnings),
                    mutation_state=mutation_state,
                )
                result.stages = [*prior_stages, *result.stages]
                result.observed_states = [*prior_observed, *result.observed_states]
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
        mutation_state: dict[str, bool],
    ) -> PrepareContentResult:
        from home_media.providers.base import ProviderState
        from home_media.providers.netflix import NetflixAdapter

        netflix = NetflixAdapter()
        profile_name = self._netflix_profile_name()
        result = PrepareContentResult(
            room_key=room_key,
            title=title,
            normalized_title=normalize_query(title),
            provider="netflix",
            goal=goal,
            route_used=ContentRoute.PROVIDER_STATE_MACHINE,
            selected_result=False,
            playback_started=False,
            verification_status="unverified",
            terminal_status=TerminalStatus.HANDOFF,
            warnings=list(prior_warnings),
            physical_tv_state_known=False,
            idempotency_key=idempotency_key,
        )
        observer_timings: list[dict[str, object]] = []

        def _state_evidence(state: ComputerUseState) -> list[str]:
            evidence = [
                f"sequence={state.sequence}",
                f"state={state.semantics.state or 'unclassified'}",
                f"confidence={state.semantics.confidence:.2f}",
                f"source={state.semantics.source}",
            ]
            if state.frame_sha256:
                evidence.append(f"sha256={state.frame_sha256}")
            evidence.extend(f"anchor={anchor}" for anchor in state.semantics.anchors[:8])
            return evidence

        def _remember(state: ComputerUseState) -> None:
            if state.semantics.state:
                result.observed_states.append(state.semantics.state)
            observer_timings.append(
                {
                    "capture_ms": state.capture_latency_ms,
                    "observation_ms": state.observation_latency_ms,
                    "sequence": state.sequence,
                }
            )

        def _macro_stage(
            name: str,
            macro_result: VerifiedMacroResult,
            *,
            latency_ms: int,
        ) -> PrepareStage:
            evidence = _state_evidence(macro_result.final_state)
            for step in macro_result.steps:
                evidence.extend(
                    [
                        f"macro_step={step.name}",
                        f"macro_terminal={step.terminal.value}",
                        *(f"action={action}" for action in step.actions_sent),
                    ]
                )
                if step.reason:
                    evidence.append(f"reason={step.reason}")
            return PrepareStage(
                name=name,
                status=(
                    StageStatus.SUCCEEDED
                    if macro_result.terminal == ComputerUseTerminal.VERIFIED
                    else StageStatus.FAILED
                ),
                latency_ms=latency_ms,
                evidence=evidence,
            )

        def _handoff(
            *,
            name: str,
            warning: str,
            state: ComputerUseState | None = None,
            evidence: list[str] | None = None,
            terminal: TerminalStatus = TerminalStatus.HANDOFF,
        ) -> PrepareContentResult:
            if not result.stages or result.stages[-1].name != name:
                result.stages.append(
                    PrepareStage(
                        name=name,
                        status=StageStatus.FAILED,
                        evidence=(evidence or [])
                        + (_state_evidence(state) if state is not None else []),
                    )
                )
            result.route_used = ContentRoute.SCREENSHOT_RECOVERY
            result.terminal_status = terminal
            result.verification_status = "failed"
            result.warnings.append(warning)
            result.extra["observer_timings"] = observer_timings
            return result

        async def _visual(state: ComputerUseState) -> PrepareContentResult:
            return await self._run_netflix_visual_goal(
                room_key=room_key,
                title=title,
                goal=goal,
                state=state,
                result=result,
                mutation_state=mutation_state,
                on_observe=_remember,
            )

        # Live path: exact room Apple TV must have a confirmed observer binding.
        if not self._use_fakes:
            svc = self.screenshot_service
            if svc is None:
                return _handoff(
                    name="screenshot_gate",
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
                return _handoff(
                    name="screenshot_gate",
                    evidence=[
                        "screenshot_gate_required",
                        str(exc.details.get("reason") or "no_observer_binding"),
                        "refusing_blind_netflix_navigation",
                    ],
                    warning=exc.message,
                )

        try:
            state = await self.computer_use.observe(room_key)
        except Exception as exc:  # noqa: BLE001 - return a bounded recovery state
            return _handoff(
                name="screenshot_gate",
                evidence=["capture_failed_preflight", type(exc).__name__],
                warning="Screenshot capture failed before Netflix navigation",
            )
        _remember(state)
        if state.semantics.state is None:
            if state.png_bytes:
                return await _visual(state)
            return _handoff(
                name="screenshot_gate",
                state=state,
                evidence=["unclassified_frame"],
                warning="Unclassified screenshot; model vision is required",
            )

        current_state = ProviderState(state.semantics.state)
        if current_state == ProviderState.UNKNOWN and goal != ContentGoal.SEARCH_READY:
            return await _visual(state)
        if current_state in {
            ProviderState.PLAYING,
            ProviderState.TITLE_DETAIL,
            ProviderState.BLANK_OR_PROTECTED,
            ProviderState.ERROR_OR_MODAL,
        }:
            return await _visual(state)

        # Apple Home and generic/unknown screens are safe launch points.  Launch
        # and wake are one action with one post-frame, so cold-start latency does
        # not include a redundant capture between power-on and app launch.
        home_is_causal = False
        if current_state in {ProviderState.UNKNOWN, ProviderState.APPLE_HOME}:
            started = time.perf_counter()
            launch = ComputerUseAction(
                kind=ComputerUseActionKind.LAUNCH_APP,
                app_bundle_id=netflix.app_bundle_id,
                wake_before=wake,
                expected_sequence=state.sequence,
                allowed_from_states=[current_state.value],
            )
            mutation_state["attempted"] = True
            try:
                launched = await self.computer_use.act(room_key, launch)
            except Exception as exc:  # noqa: BLE001
                return _handoff(
                    name="launch_app",
                    state=state,
                    evidence=[type(exc).__name__],
                    warning="Netflix launch failed before a verified post-frame",
                )
            state = launched.after
            _remember(state)
            result.stages.append(
                PrepareStage(
                    name="launch_app",
                    status=StageStatus.SUCCEEDED,
                    latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
                    evidence=[
                        *_state_evidence(state),
                        "action=launch_app",
                        f"frame_changed={launched.frame_changed}",
                    ],
                )
            )
            if state.semantics.state is None:
                return await _visual(state)
            current_state = ProviderState(state.semantics.state)
            home_is_causal = current_state == ProviderState.HOME

        if current_state == ProviderState.PROFILE_PICKER:
            if not profile_name:
                return _handoff(
                    name=f"select_profile_{profile_index}",
                    state=state,
                    evidence=["profile_name_unconfigured"],
                    warning="Netflix profile selection needs a configured profile name",
                )
            started = time.perf_counter()
            mutation_state["attempted"] = True
            before_sequence = state.sequence
            profile_result = await self.computer_use.run_macro(
                room_key,
                netflix_profile_select_macro(
                    sequence=state.sequence,
                    profile_name=profile_name,
                ),
            )
            state = profile_result.final_state
            if state.sequence != before_sequence:
                _remember(state)
            result.stages.append(
                _macro_stage(
                    f"select_profile_{profile_index}",
                    profile_result,
                    latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
                )
            )
            if profile_result.terminal != ComputerUseTerminal.VERIFIED:
                profile_select_sent = any(
                    action == "press_key:select"
                    for step in profile_result.steps
                    for action in step.actions_sent
                )
                if profile_select_sent:
                    return _handoff(
                        name=f"select_profile_{profile_index}",
                        state=state,
                        evidence=["profile_select_sent", "outcome_not_verified"],
                        warning=(
                            "Netflix did not reveal the profile Select outcome; "
                            "it was not repeated"
                        ),
                    )
                if profile_result.fallback_reason == "profile_name_mismatch":
                    return _handoff(
                        name=f"select_profile_{profile_index}",
                        state=state,
                        evidence=["profile_name_mismatch"],
                        terminal=TerminalStatus.FAILED,
                        warning="The highlighted Netflix profile was not the configured profile",
                    )
                return await _visual(state)
            current_state = ProviderState(state.semantics.state or ProviderState.UNKNOWN)
            home_is_causal = current_state == ProviderState.HOME

        # OCR can mistake an open Netflix overlay or populated search grid for
        # Home because the top navigation remains visible. Only run the fast
        # Home recipe when this request causally reached Home by launch/profile
        # selection. An arbitrary warm Home classification is visually gated.
        if self._use_fakes and current_state == ProviderState.HOME:
            home_is_causal = True
        if current_state == ProviderState.HOME and not home_is_causal:
            return await _visual(state)

        if current_state == ProviderState.HOME:
            started = time.perf_counter()
            mutation_state["attempted"] = True
            before_sequence = state.sequence
            before_frame_sha256 = state.frame_sha256
            search_result = await self.computer_use.run_macro(
                room_key,
                netflix_home_to_search_macro(sequence=state.sequence),
            )
            state = search_result.final_state
            if state.sequence != before_sequence:
                _remember(state)
            if (
                search_result.terminal != ComputerUseTerminal.VERIFIED
                and search_result.fallback_reason == "unexpected_post_state"
                and state.semantics.state == ProviderState.HOME.value
                and state.frame_sha256 == before_frame_sha256
            ):
                # Two postcondition frames were byte-identical to the original
                # Home frame, so none of the reversible navigation burst took
                # effect. Replay it once with the newly observed generation;
                # Select is never part of this retry.
                result.stages.append(
                    _macro_stage(
                        "home_to_search_keyboard_attempt_1",
                        search_result,
                        latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
                    )
                )
                await asyncio.sleep(0.5)
                before_sequence = state.sequence
                search_result = await self.computer_use.run_macro(
                    room_key,
                    netflix_home_to_search_macro(sequence=state.sequence),
                )
                state = search_result.final_state
                if state.sequence != before_sequence:
                    _remember(state)
            result.stages.append(
                _macro_stage(
                    "home_to_search_keyboard",
                    search_result,
                    latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
                )
            )
            if search_result.terminal != ComputerUseTerminal.VERIFIED:
                return await _visual(state)
            current_state = ProviderState(state.semantics.state or ProviderState.UNKNOWN)

        if current_state not in {
            ProviderState.SEARCH_KEYBOARD,
            ProviderState.SEARCH_RESULTS,
        }:
            return await _visual(state)

        # Once focus has moved from the keyboard into populated results, tvOS
        # intentionally stops exposing keyboard text. For title/resume goals,
        # preserve that exact frame and let the strict visual policy prove the
        # requested title; trying to rewrite text would require a blind Up and
        # would discard a safely focused result.
        if (
            goal != ContentGoal.SEARCH_READY
            and current_state == ProviderState.SEARCH_RESULTS
            and not state.device.keyboard_focused
        ):
            return await _visual(state)

        if state.device.keyboard_focused and normalize_query(
            state.device.keyboard_text or ""
        ) == normalize_query(title):
            result.stages.append(
                PrepareStage(
                    name="existing_query_verified",
                    status=StageStatus.SUCCEEDED,
                    evidence=[*_state_evidence(state), "exact_query_readback"],
                )
            )
        else:
            started = time.perf_counter()
            mutation_state["attempted"] = True
            before_sequence = state.sequence
            query_result = await self.computer_use.run_macro(
                room_key,
                netflix_set_query_macro(sequence=state.sequence, title=title),
            )
            state = query_result.final_state
            if state.sequence != before_sequence:
                _remember(state)
            result.stages.append(
                _macro_stage(
                    "keyboard_set_query",
                    query_result,
                    latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
                )
            )
            if query_result.terminal != ComputerUseTerminal.VERIFIED:
                return await _visual(state)

        result.terminal_status = TerminalStatus.QUERY_VERIFIED
        result.verification_status = "verified"
        result.route_used = ContentRoute.PROVIDER_STATE_MACHINE
        result.extra["observer_timings"] = observer_timings
        if goal != ContentGoal.SEARCH_READY:
            return await _visual(state)
        return result

    async def _run_netflix_visual_goal(
        self,
        *,
        room_key: str,
        title: str,
        goal: ContentGoal,
        state: ComputerUseState,
        result: PrepareContentResult,
        mutation_state: dict[str, bool],
        on_observe: Callable[[ComputerUseState], None] | None = None,
    ) -> PrepareContentResult:
        """Use exact-frame model decisions to open a title or resume then pause.

        Every recommendation is tied to ``state.sequence`` and immediately
        followed by a fresh frame. Select is only representable when the vision
        policy named the exact visible target on that generation. Playback is
        verified through pyatv metadata because DRM video frames may be black.
        """
        from home_media.providers.base import ProviderState

        max_steps = 6
        configured = os.environ.get("HOME_MEDIA_VISION_MAX_STEPS")
        if configured:
            with suppress(ValueError):
                max_steps = max(1, min(8, int(configured)))
        max_decision_age_s = 12.0
        configured_age = os.environ.get("HOME_MEDIA_VISION_MAX_DECISION_AGE_S")
        if configured_age:
            with suppress(ValueError):
                max_decision_age_s = max(8.0, min(20.0, float(configured_age)))
        frames: list[bytes] = [state.png_bytes] if state.png_bytes else []
        title_context_verified = False
        playback_trigger_sent = False
        settle_polls_remaining = 2
        select_sent: set[str] = set()
        select_candidate: tuple[str, str, str] | None = None
        terminal_candidate: tuple[str, str] | None = None
        playback_exit_attempts = 0

        def _record_observation(observed: ComputerUseState) -> None:
            if on_observe is not None:
                on_observe(observed)

        async def _refresh_device_context(
            current: ComputerUseState,
        ) -> tuple[ComputerUseState, bool]:
            reader = getattr(self.computer_use, "read_device_context", None)
            if not callable(reader):
                return current, False
            try:
                context = await reader(room_key)
            except Exception:  # noqa: BLE001 - retain the last receipt signals
                return current, False
            return current.model_copy(update={"device": context}), True

        def _stability_fingerprint(current: ComputerUseState) -> str | None:
            return ui_stability_fingerprint(current.png_bytes) or current.frame_sha256

        playback_state_at_start = (state.device.playback_state or "").casefold()
        if (
            state.blank_or_protected
            and state.png_bytes is not None
            and playback_state_at_start in {"idle", "paused"}
            and state.device.current_app == "com.netflix.Netflix"
            and not result.selected_result
            and not select_sent
        ):
            started = time.perf_counter()
            mutation_state["attempted"] = True
            exit_result = await self.computer_use.run_macro(
                room_key,
                netflix_exit_paused_playback_macro(
                    sequence=state.sequence,
                    source_state=(
                        state.semantics.state
                        or ProviderState.BLANK_OR_PROTECTED.value
                    ),
                ),
                initial_state=state,
            )
            state = exit_result.final_state
            _record_observation(state)
            playback_exit_attempts = 2
            if state.png_bytes and not state.blank_or_protected:
                frames.append(state.png_bytes)
                frames = frames[-4:]
            result.stages.append(
                PrepareStage(
                    name="exit_paused_playback_skill",
                    status=(
                        StageStatus.SUCCEEDED
                        if not state.blank_or_protected
                        else StageStatus.FAILED
                    ),
                    latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
                    evidence=[
                        "skill=netflix_exit_paused_playback",
                        "actions=menu",
                        f"terminal={exit_result.terminal.value}",
                        f"sequence={state.sequence}",
                    ],
                )
            )

        def _complete_query(*, visual: bool = False) -> PrepareContentResult:
            result.selected_result = False
            result.playback_started = False
            result.terminal_status = TerminalStatus.QUERY_VERIFIED
            result.verification_status = "verified"
            result.idempotency_notes = (
                "Requested title is visibly present on Netflix Search"
                if visual
                else "Exact query verified through the Apple TV keyboard"
            )
            return result

        def _complete_title_open() -> PrepareContentResult:
            result.selected_result = True
            result.playback_started = False
            result.terminal_status = TerminalStatus.TITLE_OPEN_VERIFIED
            result.verification_status = "verified"
            result.idempotency_notes = "Exact title detail visually verified"
            return result

        def _complete_paused() -> PrepareContentResult:
            result.selected_result = True
            result.playback_started = True
            result.terminal_status = TerminalStatus.PLAYBACK_PAUSED_VERIFIED
            result.verification_status = "verified"
            result.idempotency_notes = "Playback started and paused through observed metadata"
            return result

        def _handoff(reason: str) -> PrepareContentResult:
            result.terminal_status = TerminalStatus.HANDOFF
            result.verification_status = "unverified"
            result.warnings.append(reason)
            result.extra["vision_required"] = True
            return result

        # Real tvOS keyboard focus is stronger evidence than the OCR surface
        # label. It lets the loop recover from overlays/search screens that the
        # rule classifier called Home, and text replacement is still guarded by
        # the live keyboard API before mutation.
        if state.device.keyboard_focused:
            exact_query = normalize_query(state.device.keyboard_text or "") == normalize_query(
                title
            )
            if not exact_query:
                started = time.perf_counter()
                mutation_state["attempted"] = True
                try:
                    query_action = await self.computer_use.act(
                        room_key,
                        ComputerUseAction(
                            kind=ComputerUseActionKind.SET_TEXT,
                            text=title,
                            expected_sequence=state.sequence,
                            allowed_from_states=(
                                [state.semantics.state] if state.semantics.state else []
                            ),
                            require_keyboard_focus=True,
                        ),
                        classify_after=False,
                    )
                except HomeMediaError as exc:
                    return _handoff(
                        "The visible Netflix keyboard rejected exact query entry: "
                        f"{exc.code.value}"
                    )
                state = query_action.after
                _record_observation(state)
                frames = [state.png_bytes] if state.png_bytes else []
                exact_query = bool(
                    state.device.keyboard_focused
                    and normalize_query(state.device.keyboard_text or "")
                    == normalize_query(title)
                )
                result.stages.append(
                    PrepareStage(
                        name="visual_loop_set_query",
                        status=(
                            StageStatus.SUCCEEDED if exact_query else StageStatus.FAILED
                        ),
                        latency_ms=max(
                            0, int((time.perf_counter() - started) * 1000)
                        ),
                        evidence=[
                            "action=set_text",
                            f"sequence={state.sequence}",
                            f"exact_readback={exact_query}",
                        ],
                    )
                )
                if not exact_query:
                    return _handoff("Netflix query entry did not produce exact readback")
            if goal == ContentGoal.SEARCH_READY:
                return _complete_query()

        # Netflix keeps keyboard focus after a query is populated. Moving to
        # the first result is a stable, non-Select action and the subsequent
        # screenshot/model gate still has to prove the exact requested title
        # before Select can exist. This removes one full model round-trip from
        # the common title-open path without weakening the selection boundary.
        if (
            goal != ContentGoal.SEARCH_READY
            and state.device.keyboard_focused
            and normalize_query(state.device.keyboard_text or "") == normalize_query(title)
        ):
            started = time.perf_counter()
            mutation_state["attempted"] = True
            before_focus_hash = state.frame_sha256
            source_state = state.semantics.state or ProviderState.UNKNOWN.value
            focus_result = await self.computer_use.run_macro(
                room_key,
                netflix_focus_first_result_macro(
                    sequence=state.sequence,
                    source_state=source_state,
                ),
                initial_state=state,
            )
            state = focus_result.final_state
            _record_observation(state)
            frames = [state.png_bytes] if state.png_bytes else []
            result.stages.append(
                PrepareStage(
                    name="focus_first_search_result",
                    status=(
                        StageStatus.SUCCEEDED
                        if focus_result.terminal == ComputerUseTerminal.VERIFIED
                        else StageStatus.FAILED
                    ),
                    latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
                    evidence=[
                        "skill=netflix_focus_first_result",
                        "actions=down,down,down",
                        "selection_not_sent",
                        f"sequence={state.sequence}",
                        f"frame_changed={state.frame_sha256 != before_focus_hash}",
                        f"terminal={focus_result.terminal.value}",
                    ],
                )
            )

        # The bound applies to model decisions, not deterministic receipt work.
        # Exiting a paused DRM surface and verifying the outcome of a once-only
        # Select must not consume the final decision slot. A playback CTA
        # accepted on the last allowed decision still gets its metadata-driven
        # pause/verification pass at the top of this loop.
        decision_index = 0
        while True:
            playback_state = (state.device.playback_state or "").casefold()
            now_playing_matches = bool(
                any(
                    value and labels_match(title, value)
                    for value in (
                        state.device.now_playing_title,
                        state.device.now_playing_series,
                    )
                )
            )
            now_playing_conflicts = bool(
                (
                    state.device.now_playing_title
                    or state.device.now_playing_series
                )
                and not now_playing_matches
            )
            if (
                goal == ContentGoal.RESUME
                and playback_state == "paused"
                and result.playback_started
                and (playback_trigger_sent or now_playing_matches)
            ):
                return _complete_paused()
            if goal == ContentGoal.RESUME and playback_state == "playing":
                if now_playing_conflicts:
                    return _handoff(
                        "Playback metadata contradicted the requested title; it was not paused"
                    )
                if not (playback_trigger_sent or now_playing_matches):
                    return _handoff(
                        "Existing playback did not match the requested title; it was not paused"
                    )
                mutation_state["attempted"] = True
                result.playback_started = True
                for pause_attempt in range(1, 3):
                    started = time.perf_counter()
                    before_pause_state = (
                        state.device.playback_state or ""
                    ).casefold()
                    pause = ComputerUseAction(
                        kind=ComputerUseActionKind.TRANSPORT,
                        transport_action="pause",
                        expected_sequence=state.sequence,
                    )
                    action_error: str | None = None
                    context_receipt_fresh = True
                    try:
                        paused = await self.computer_use.act(
                            room_key,
                            pause,
                            classify_after=False,
                        )
                    except HomeMediaError as exc:
                        # Explicit Pause is idempotent. An ambiguous adapter
                        # receipt may be checked and, if still playing, safely
                        # sent once more; Select never receives this treatment.
                        action_error = exc.code.value
                        if exc.code.value != "partial":
                            return _handoff(
                                "Netflix playback could not be paused: "
                                f"{exc.code.value}"
                            )
                        await asyncio.sleep(0.25)
                        state, context_receipt_fresh = (
                            await _refresh_device_context(state)
                        )
                    else:
                        state = paused.after
                        _record_observation(state)
                        if state.png_bytes:
                            frames.append(state.png_bytes)
                            frames = frames[-4:]

                    stable_stopped_reads = 0
                    after_pause = (state.device.playback_state or "").casefold()
                    for settle_poll in range(8):
                        if (
                            context_receipt_fresh
                            and after_pause in {"idle", "paused"}
                        ):
                            stable_stopped_reads += 1
                            if stable_stopped_reads >= 2:
                                break
                        elif after_pause not in {"idle", "paused"}:
                            stable_stopped_reads = 0
                        # Once the post-action capture still says playing, a
                        # short metadata-only check is enough to decide whether
                        # the one safe explicit-Pause retry is needed.
                        if after_pause == "playing" and settle_poll >= 2:
                            break
                        await asyncio.sleep(0.2)
                        state, context_receipt_fresh = (
                            await _refresh_device_context(state)
                        )
                        after_pause = (
                            state.device.playback_state or ""
                        ).casefold()
                    result.stages.append(
                        PrepareStage(
                            name=(
                                "pause_after_resume"
                                if pause_attempt == 1
                                else "pause_after_resume_retry"
                            ),
                            status=(
                                StageStatus.SUCCEEDED
                                if stable_stopped_reads >= 2
                                else StageStatus.FAILED
                            ),
                            latency_ms=max(
                                0, int((time.perf_counter() - started) * 1000)
                            ),
                            evidence=[
                                f"attempt={pause_attempt}",
                                f"before_playback_state={before_pause_state}",
                                f"after_playback_state={after_pause}",
                                f"stable_stopped_reads={stable_stopped_reads}",
                                f"action_error={action_error}",
                                f"sequence={state.sequence}",
                            ],
                        )
                    )
                    if stable_stopped_reads >= 2:
                        return _complete_paused()
                return _handoff("Playback began but the paused state could not be verified")

            if state.device.current_app not in {None, "com.netflix.Netflix"}:
                if playback_exit_attempts:
                    # pyatv can briefly report the prior foreground app after
                    # Menu exits DRM playback. Tolerate only a bounded metadata
                    # lag; never carry that exception into a model Select.
                    for _app_settle in range(3):
                        await asyncio.sleep(0.25)
                        state, _context_fresh = await _refresh_device_context(state)
                        if state.device.current_app in {None, "com.netflix.Netflix"}:
                            break
                if state.device.current_app not in {None, "com.netflix.Netflix"}:
                    return _handoff("The observed foreground app was not Netflix")
            if (
                state.blank_or_protected
                and state.png_bytes is not None
                and playback_state in {"idle", "paused"}
                and playback_exit_attempts < 2
            ):
                # Netflix hides paused DRM video from DVT. Menu is the bounded
                # semantic Back action from that known paused surface; it does
                # not start playback or select content. Observe the resulting
                # title/search page before doing anything else.
                playback_exit_attempts += 1
                mutation_state["attempted"] = True
                exited = await self.computer_use.act(
                    room_key,
                    ComputerUseAction(
                        kind=ComputerUseActionKind.PRESS_KEY,
                        key="menu",
                        expected_sequence=state.sequence,
                    ),
                )
                state = exited.after
                _record_observation(state)
                for _exit_settle in range(2):
                    if not state.blank_or_protected and state.png_bytes:
                        break
                    await asyncio.sleep(0.5)
                    state = await self.computer_use.observe(room_key, classify=False)
                    _record_observation(state)
                if state.png_bytes and not state.blank_or_protected:
                    frames.append(state.png_bytes)
                    frames = frames[-4:]
                result.stages.append(
                    PrepareStage(
                        name="exit_paused_playback",
                        status=StageStatus.SUCCEEDED,
                        evidence=[
                            "action=menu",
                            f"attempt={playback_exit_attempts}",
                            f"sequence={state.sequence}",
                            f"frame_changed={exited.frame_changed}",
                        ],
                    )
                )
                continue
            if not state.png_bytes or state.blank_or_protected:
                return _handoff("No current Apple TV frame was available for title selection")
            if not frames or frames[-1] != state.png_bytes:
                frames.append(state.png_bytes)
                frames = frames[-4:]
            context = VisionContext(
                room_key=room_key,
                provider="netflix",
                requested_title=title,
                goal=(
                    "resume_then_pause"
                    if goal == ContentGoal.RESUME
                    else goal.value
                ),
                desired_profile=self._netflix_profile_name(),
                current_app=state.device.current_app,
                keyboard_focused=state.device.keyboard_focused,
                keyboard_text=state.device.keyboard_text,
                now_playing_state=state.device.playback_state,
                now_playing_title=state.device.now_playing_title,
                now_playing_series=state.device.now_playing_series,
            )
            if decision_index >= max_steps:
                return _handoff("Visual control reached its bounded decision limit")
            decision_index += 1
            step_index = decision_index
            started = time.perf_counter()
            try:
                decision = await self.vision_policy.decide(frames, context)
            except HomeMediaError as exc:
                result.stages.append(
                    PrepareStage(
                        name=f"vision_decision_{step_index}",
                        status=StageStatus.FAILED,
                        latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
                        evidence=[exc.code.value, str(exc.details.get("reason") or "")],
                    )
                )
                return _handoff("Fast visual control could not prove a safe next action")

            result.stages.append(
                PrepareStage(
                    name=f"vision_decision_{step_index}",
                    status=StageStatus.SUCCEEDED,
                    latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
                    evidence=[
                        f"surface={decision.surface.value}",
                        f"confidence={decision.confidence:.2f}",
                        f"title_match={decision.title_match.value}",
                        f"next_action={decision.next_action.value}",
                        f"reason={decision.reason_code}",
                    ],
                )
            )

            # A model decision is advisory only for the exact frame it saw.
            # A slow Fast response cannot turn an old screenshot into success or
            # input; recapture and ask again instead.
            if state_age_seconds(state) > max_decision_age_s:
                try:
                    state = await self.computer_use.observe(room_key, classify=False)
                except Exception:  # noqa: BLE001 - bounded handoff below
                    return _handoff("The visual decision expired and refresh failed")
                _record_observation(state)
                if state.png_bytes and not state.blank_or_protected:
                    frames.append(state.png_bytes)
                    frames = frames[-4:]
                result.stages.append(
                    PrepareStage(
                        name=f"vision_refresh_{step_index}",
                        status=StageStatus.SUCCEEDED,
                        evidence=[f"sequence={state.sequence}", "reason=stale_model_frame"],
                    )
                )
                continue

            decision_stability_verified = False
            exact_visible_title = any(
                labels_match(title, visible) for visible in decision.visible_titles
            )
            exact_title_detail = (
                decision.surface == ScreenSurface.TITLE_DETAIL
                and decision.title_match == TitleMatch.EXACT
                and exact_visible_title
                and decision.confidence >= 0.85
            )
            exact_paused_overlay = bool(
                goal == ContentGoal.RESUME
                and decision.surface == ScreenSurface.PLAYBACK
                and decision.playback_visible
                and decision.title_match == TitleMatch.EXACT
                and exact_visible_title
                and decision.confidence >= 0.85
                and state.device.current_app == "com.netflix.Netflix"
                and playback_state in {"idle", "paused"}
            )
            exact_title_context = exact_title_detail or exact_paused_overlay
            visually_verified_search = bool(
                goal == ContentGoal.SEARCH_READY
                and decision.surface == ScreenSurface.SEARCH
                and decision.title_match == TitleMatch.EXACT
                and decision.confidence >= 0.85
            )
            if exact_title_context or visually_verified_search:
                terminal_kind = (
                    "paused_overlay"
                    if exact_paused_overlay
                    else "title_detail"
                    if exact_title_detail
                    else "search_ready"
                )
                terminal_hash = state.frame_sha256 or ""
                terminal_consensus = bool(
                    terminal_hash
                    and terminal_candidate is not None
                    and terminal_candidate[0] == terminal_kind
                    and terminal_candidate[1] != terminal_hash
                )
                # A terminal claim must describe the screen *after* model
                # latency, not merely the frame supplied to the model. A
                # byte-identical refresh is sufficient; animated artwork can
                # instead use two exact model classifications on distinct
                # frames, with no remote input between them.
                if terminal_consensus:
                    result.stages.append(
                        PrepareStage(
                            name=f"terminal_consensus_{step_index}",
                            status=StageStatus.SUCCEEDED,
                            evidence=[
                                f"sequence={state.sequence}",
                                "visual_consensus=two_distinct_frames",
                            ],
                        )
                    )
                else:
                    try:
                        stable_terminal = await self.computer_use.observe(
                            room_key, classify=False
                        )
                    except Exception:  # noqa: BLE001 - bounded handoff below
                        return _handoff("Terminal visual proof could not be refreshed")
                    _record_observation(stable_terminal)
                    same_terminal = bool(
                        _stability_fingerprint(state)
                        and _stability_fingerprint(stable_terminal)
                        == _stability_fingerprint(state)
                        and stable_terminal.device.current_app == state.device.current_app
                    )
                    result.stages.append(
                        PrepareStage(
                            name=f"terminal_stability_{step_index}",
                            status=StageStatus.SUCCEEDED,
                            evidence=[
                                f"sequence={stable_terminal.sequence}",
                                f"frame_stable={same_terminal}",
                            ],
                        )
                    )
                    if not same_terminal:
                        terminal_candidate = (terminal_kind, terminal_hash)
                        state = stable_terminal
                        if state.png_bytes and not state.blank_or_protected:
                            frames.append(state.png_bytes)
                            frames = frames[-4:]
                        continue
                    state = stable_terminal
                    decision_stability_verified = True
                if visually_verified_search:
                    return _complete_query(visual=True)
            else:
                terminal_candidate = None
            if exact_title_context:
                title_context_verified = True
                result.selected_result = True
                if exact_title_detail and goal == ContentGoal.TITLE_OPEN:
                    return _complete_title_open()

            # A visually proven provider Home surface unlocks the calibrated
            # no-Select navigation skill. This is the recovery path for OCR
            # ambiguity without paying three separate model round trips.
            if (
                decision.surface == ScreenSurface.PROVIDER_HOME
                and decision.confidence >= 0.85
                and state.semantics.state == ProviderState.HOME.value
            ):
                started = time.perf_counter()
                home_result = await self.computer_use.run_macro(
                    room_key,
                    netflix_home_to_search_macro(sequence=state.sequence),
                    initial_state=state,
                )
                state = home_result.final_state
                _record_observation(state)
                if state.png_bytes:
                    frames.append(state.png_bytes)
                    frames = frames[-4:]
                result.stages.append(
                    PrepareStage(
                        name=f"visual_home_skill_{step_index}",
                        status=(
                            StageStatus.SUCCEEDED
                            if home_result.terminal == ComputerUseTerminal.VERIFIED
                            else StageStatus.FAILED
                        ),
                        latency_ms=max(
                            0, int((time.perf_counter() - started) * 1000)
                        ),
                        evidence=[
                            "skill=netflix_home_to_search",
                            f"terminal={home_result.terminal.value}",
                            f"sequence={state.sequence}",
                        ],
                    )
                )
                continue

            if decision.next_action == VisionRemoteAction.NONE:
                if decision.surface == ScreenSurface.SEARCH and settle_polls_remaining:
                    settle_polls_remaining -= 1
                    await asyncio.sleep(0.25)
                    try:
                        state = await self.computer_use.observe(room_key, classify=False)
                    except Exception:  # noqa: BLE001 - bounded handoff below
                        return _handoff("Netflix search results did not become observable")
                    _record_observation(state)
                    if state.png_bytes and not state.blank_or_protected:
                        frames.append(state.png_bytes)
                        frames = frames[-4:]
                    continue
                return _handoff("Fast visual control found no safely verified next action")

            key_by_action = {
                VisionRemoteAction.UP: "up",
                VisionRemoteAction.DOWN: "down",
                VisionRemoteAction.LEFT: "left",
                VisionRemoteAction.RIGHT: "right",
                VisionRemoteAction.SELECT: "select",
                VisionRemoteAction.MENU: "menu",
                VisionRemoteAction.PLAY_PAUSE: "play_pause",
            }
            key = key_by_action.get(decision.next_action)
            if key is None:
                return _handoff("Fast visual control returned an unsupported remote action")
            if decision.next_action != VisionRemoteAction.SELECT:
                select_candidate = None

            model_target = None
            selected_search_result = False
            selected_playback_cta = False
            selected_profile = False
            select_phase: str | None = None
            if decision.next_action == VisionRemoteAction.SELECT:
                model_target = decision.focused_target
                selected_profile = bool(
                    decision.surface == ScreenSurface.PROFILE_PICKER
                    and decision.focused_kind == FocusedTargetKind.PROFILE
                    and decision.focused_target
                    and self._netflix_profile_name()
                    and labels_match(
                        self._netflix_profile_name() or "",
                        decision.focused_target,
                    )
                )
                selected_search_result = bool(
                    decision.surface == ScreenSurface.SEARCH
                    and decision.focused_kind == FocusedTargetKind.TITLE_RESULT
                    and decision.focused_target
                    and labels_match(title, decision.focused_target)
                )
                selected_playback_cta = bool(
                    decision.surface
                    in {ScreenSurface.TITLE_DETAIL, ScreenSurface.PLAYBACK}
                    and title_context_verified
                    and decision.focused_kind == FocusedTargetKind.PLAYBACK_CTA
                    and decision.focused_target
                    and is_playback_cta(decision.focused_target)
                    and (
                        decision.surface == ScreenSurface.TITLE_DETAIL
                        or (
                            goal == ContentGoal.RESUME
                            and decision.playback_visible
                            and decision.title_match == TitleMatch.EXACT
                            and any(
                                labels_match(title, visible)
                                for visible in decision.visible_titles
                            )
                            and state.device.current_app == "com.netflix.Netflix"
                            and playback_state in {"idle", "paused"}
                        )
                    )
                )
                if selected_profile:
                    select_phase = "profile"
                elif selected_search_result:
                    select_phase = "title_result"
                elif selected_playback_cta:
                    select_phase = "playback_cta"
                if select_phase is None:
                    return _handoff(
                        "The focused control was not a verified profile, title, or playback action"
                    )
                if select_phase in select_sent:
                    return _handoff(
                        f"A {select_phase} Select was already sent; its outcome is ambiguous"
                    )

                normalized_target = normalize_query(decision.focused_target or "")
                current_hash = state.frame_sha256 or ""
                candidate_matches = bool(
                    current_hash
                    and select_candidate is not None
                    and select_candidate[0] == select_phase
                    and select_candidate[1] == normalized_target
                    and select_candidate[2] != current_hash
                )

                # Netflix focus animations can show the destination border
                # before the prior navigation event has fully settled. Select
                # needs either one byte-identical refresh or two consecutive
                # exact-target model decisions on distinct frames. The latter
                # ignores animated artwork while still proving focus twice.
                if candidate_matches:
                    result.stages.append(
                        PrepareStage(
                            name=f"select_consensus_{step_index}",
                            status=StageStatus.SUCCEEDED,
                            evidence=[
                                f"sequence={state.sequence}",
                                "focus_consensus=two_distinct_frames",
                            ],
                        )
                    )
                elif decision_stability_verified:
                    result.stages.append(
                        PrepareStage(
                            name=f"select_stability_{step_index}",
                            status=StageStatus.SUCCEEDED,
                            evidence=[
                                f"sequence={state.sequence}",
                                "frame_stable=True",
                                "reused_terminal_refresh=True",
                            ],
                        )
                    )
                else:
                    await asyncio.sleep(0.25)
                    stable = await self.computer_use.observe(room_key, classify=False)
                    _record_observation(stable)
                    same_select_frame = bool(
                        _stability_fingerprint(state)
                        and _stability_fingerprint(stable)
                        == _stability_fingerprint(state)
                        and stable.device.current_app == state.device.current_app
                    )
                    result.stages.append(
                        PrepareStage(
                            name=f"select_stability_{step_index}",
                            status=StageStatus.SUCCEEDED,
                            evidence=[
                                f"sequence={stable.sequence}",
                                f"frame_stable={same_select_frame}",
                            ],
                        )
                    )
                    if not same_select_frame:
                        select_candidate = (
                            select_phase,
                            normalized_target,
                            current_hash,
                        )
                        state = stable
                        if state.png_bytes and not state.blank_or_protected:
                            frames.append(state.png_bytes)
                            frames = frames[-4:]
                        continue
                    state = stable
            observed_state = state.semantics.state
            action = ComputerUseAction(
                kind=ComputerUseActionKind.PRESS_KEY,
                key=key,
                expected_sequence=state.sequence,
                model_observed_target=model_target,
                allowed_from_states=[observed_state] if observed_state else [],
                min_confidence=0.70 if key == "select" else 0.0,
            )
            mutation_state["attempted"] = True
            if select_phase is not None:
                # Record before dispatch. A timeout or lost post-frame is an
                # ambiguous non-idempotent mutation and must never be retried.
                select_sent.add(select_phase)
            before_action_hash = state.frame_sha256
            try:
                action_result = await self.computer_use.act(
                    room_key,
                    action,
                    max_state_age_s=max_decision_age_s,
                    classify_after=False,
                )
            except HomeMediaError as exc:
                return _handoff(
                    "Exact-frame visual action was rejected before a verified post-frame: "
                    f"{exc.code.value}"
                )
            state = action_result.after
            _record_observation(state)
            if (
                decision.next_action
                in {
                    VisionRemoteAction.UP,
                    VisionRemoteAction.DOWN,
                    VisionRemoteAction.LEFT,
                    VisionRemoteAction.RIGHT,
                }
                and action_result.frame_changed is False
            ):
                # One read-only settlement frame distinguishes a dropped arrow
                # from a slow animation. Only if both frames are identical do
                # we retry the same reversible navigation input once.
                settled = await self.computer_use.observe(room_key, classify=False)
                _record_observation(settled)
                if settled.frame_sha256 == state.frame_sha256:
                    retry_action = action.model_copy(
                        update={"expected_sequence": settled.sequence}
                    )
                    retry_result = await self.computer_use.act(
                        room_key,
                        retry_action,
                        max_state_age_s=max_decision_age_s,
                        classify_after=False,
                    )
                    state = retry_result.after
                    _record_observation(state)
                    result.stages.append(
                        PrepareStage(
                            name=f"navigation_retry_{step_index}",
                            status=StageStatus.SUCCEEDED,
                            evidence=[
                                f"action={key}",
                                "reason=two_identical_post_frames",
                                f"frame_changed={retry_result.frame_changed}",
                            ],
                        )
                    )
                else:
                    state = settled

            if select_phase is not None:
                # Select is a receipt-bearing, once-per-phase mutation. Poll
                # for its outcome; never send it again when Netflix is merely
                # slow to animate or the post-frame is unchanged.
                outcome_changed = bool(
                    before_action_hash
                    and state.frame_sha256
                    and state.frame_sha256 != before_action_hash
                )
                for _settle_attempt in range(4):
                    if outcome_changed:
                        break
                    await asyncio.sleep(0.25)
                    settled = await self.computer_use.observe(room_key, classify=False)
                    _record_observation(settled)
                    state = settled
                    outcome_changed = bool(
                        before_action_hash
                        and state.frame_sha256
                        and state.frame_sha256 != before_action_hash
                    )
                if not outcome_changed:
                    return _handoff(
                        f"Netflix did not reveal the outcome of the {select_phase} Select; "
                        "it was not repeated"
                    )
            if selected_playback_cta and title_context_verified:
                # A dynamic title-page background can change immediately and
                # masquerade as the Select outcome. Hold this non-idempotent
                # receipt open until either metadata reports playing or DVT
                # shows a DRM playback surface. Do not ask the model to click
                # the CTA again while Netflix is starting the episode.
                playback_trigger_sent = True
                for _playback_attempt in range(20):
                    if (state.device.playback_state or "").casefold() == "playing":
                        break
                    await asyncio.sleep(0.25)
                    state, _context_fresh = await _refresh_device_context(state)
                else:
                    # Refresh pixels once so a delayed DRM transition is
                    # recorded, but never treat black pixels alone as proof
                    # that playback started. A verified Pause requires pyatv to
                    # have reported playing first.
                    state = await self.computer_use.observe(
                        room_key,
                        classify=False,
                    )
                    _record_observation(state)
                    if (state.device.playback_state or "").casefold() != "playing":
                        return _handoff(
                            "Netflix accepted the playback CTA but playing metadata "
                            "never appeared; "
                            "the CTA was not repeated"
                        )
            post_state = state.semantics.state
            verified_playback_transition = bool(
                action_result.frame_changed is not False
                and (state.device.playback_state or "").casefold() == "playing"
                and post_state
                in {
                    ProviderState.PLAYING.value,
                    ProviderState.BLANK_OR_PROTECTED.value,
                }
                and not (
                    state.device.now_playing_series
                    and not labels_match(title, state.device.now_playing_series)
                )
            )
            post_metadata_matches = any(
                value and labels_match(title, value)
                for value in (
                    state.device.now_playing_title,
                    state.device.now_playing_series,
                )
            )
            if selected_search_result:
                result.selected_result = True
                if verified_playback_transition and post_metadata_matches:
                    playback_trigger_sent = True
            if (
                selected_playback_cta
                and title_context_verified
            ):
                # This Select was model-verified on an exact title detail and
                # named a primary Play/Resume CTA. It is the causal playback
                # trigger even when Netflix withholds episode metadata or maps
                # paused playback to idle. Conflicting metadata still fails at
                # the top of the next loop before any pause is sent.
                playback_trigger_sent = True
            if state.png_bytes:
                frames.append(state.png_bytes)
                frames = frames[-4:]
