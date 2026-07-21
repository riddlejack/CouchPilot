"""Room-bound screenshot capture orchestration."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

from home_media.errors import SafetyBlockedError, UnsupportedError
from home_media.observers.binding import ObserverBindingStore, fingerprint_udid
from home_media.observers.capture import PyMobileDeviceScreenshotCapturer
from home_media.observers.classify import (
    FakeLabelClassifier,
    ScreenClassifier,
    ScreenClassifierResult,
    VisionNetflixClassifier,
)
from home_media.observers.retention import prune_screenshots, save_png_private
from home_media.observers.screenshot import (
    BoundScreenshotProvider,
    ScreenshotProvider,
    ScreenshotResult,
    UnavailableScreenshotProvider,
)
from home_media.providers.base import ProviderState
from home_media.registry import RoomRegistry

LOW_CONFIDENCE_STOP = 0.5
NAV_CONFIDENCE_MIN = 0.70
PROFILE_SELECT_CONFIDENCE_MIN = 0.85


class RoomScreenshotService:
    """Resolve room → Apple TV stable id → confirmed observer UDID → pixels."""

    def __init__(
        self,
        registry: RoomRegistry,
        *,
        bindings: ObserverBindingStore | None = None,
        provider: ScreenshotProvider | None = None,
        capturer: PyMobileDeviceScreenshotCapturer | None = None,
        classifier: ScreenClassifier | None = None,
        require_gate: bool | None = None,
    ) -> None:
        self.registry = registry
        self.bindings = bindings if bindings is not None else ObserverBindingStore.empty()
        self.bindings.validate_unique_udids()
        self._capturer = capturer or PyMobileDeviceScreenshotCapturer()
        self._provider = provider
        self._classifier = classifier
        if require_gate is None:
            require_gate = os.environ.get("HOME_MEDIA_REQUIRE_SCREENSHOT_GATE", "").lower() in {
                "1",
                "true",
                "yes",
            }
        self.require_gate = require_gate

    @property
    def provider(self) -> ScreenshotProvider:
        if self._provider is not None:
            return self._provider
        if not self.bindings.bindings:
            return UnavailableScreenshotProvider()
        mapping = {
            sid: binding.observer_udid for sid, binding in self.bindings.bindings.items()
        }

        async def _capture(udid: str, stable_device_id: str, room_key: str) -> ScreenshotResult:
            return await self._capturer.capture_udid(
                udid, stable_device_id=stable_device_id, room_key=room_key
            )

        return BoundScreenshotProvider(mapping, capturer=_capture)

    @property
    def classifier(self) -> ScreenClassifier:
        if self._classifier is not None:
            return self._classifier
        provider = self._provider
        if provider is not None and hasattr(provider, "label_for_sha"):
            highlighted = getattr(provider, "highlighted_for_sha", None)

            def _label(sha: str | None) -> ProviderState | None:
                return provider.label_for_sha(sha)  # type: ignore[no-any-return]

            def _highlighted(sha: str | None) -> str | None:
                if callable(highlighted):
                    return highlighted(sha)  # type: ignore[no-any-return]
                return None

            return FakeLabelClassifier(_label, highlighted_resolver=_highlighted)
        return VisionNetflixClassifier()

    def apple_tv_id_for_room(self, room: str) -> tuple[str, str]:
        room_cfg = self.registry.resolve_room(room)
        targets = self.registry.room_targets(room_cfg.key)
        apple = targets.get("apple_tv")
        if apple is None:
            raise UnsupportedError(
                "screenshot.capture",
                reason=f"No Apple TV in room {room_cfg.key}",
            )
        return room_cfg.key, apple.id

    def require_binding_for_device(self, room_key: str, stable_device_id: str) -> None:
        """Fail unless the exact room's Apple TV stable id has a confirmed binding."""
        if not stable_device_id:
            raise SafetyBlockedError(
                "Screenshot observer binding required for exact room Apple TV",
                reason="no_observer_binding",
            )
        if stable_device_id not in self.bindings.bindings:
            raise SafetyBlockedError(
                "Screenshot observer binding required for exact room Apple TV",
                reason="no_observer_binding_for_device",
            )
        self.bindings.get_for_room(room_key, expected_stable_id=stable_device_id)

    async def capture_room(
        self,
        room: str,
        *,
        save: bool = True,
        prune: bool = True,
    ) -> ScreenshotResult:
        room_key, stable_id = self.apple_tv_id_for_room(room)
        # Enforce binding room match when bindings exist for this device.
        if stable_id in self.bindings.bindings:
            self.bindings.get_for_room(room_key, expected_stable_id=stable_id)
        result = await self.provider.capture(stable_id, room_key)
        if result.png_bytes and result.metadata.sha256 and save:
            path = save_png_private(
                result.png_bytes,
                room_key=room_key,
                sha256=result.metadata.sha256,
            )
            result.metadata.path = str(path)
        if prune:
            prune_screenshots()
        return result

    async def classify_capture(
        self,
        result: ScreenshotResult,
        *,
        requested_query: str | None = None,
    ) -> ScreenClassifierResult:
        return await self.classifier.classify(result, requested_query=requested_query)

    def confirm_binding(
        self,
        *,
        room: str,
        observer_udid: str,
        notes: str | None = None,
        persist: bool = True,
        require_capture_proof: bool = True,
        capture_proof_nonblank: bool = False,
        allow_unproven: bool = False,
    ) -> dict[str, Any]:
        """Persist binding only with capture proof unless a narrow override is set.

        Normal CLI flow must prove a successful nonblank capture from the exact
        UDID in the same operation/session. ``allow_unproven=True`` is the
        narrowly named migration/debug override (``--allow-unproven-binding``).
        """
        room_key, stable_id = self.apple_tv_id_for_room(room)
        if require_capture_proof and not allow_unproven and not capture_proof_nonblank:
            raise SafetyBlockedError(
                "Refusing to persist observer binding without a successful nonblank "
                "capture from the exact UDID in this session "
                "(or --allow-unproven-binding)",
                reason="binding_requires_capture_proof",
            )
        binding = self.bindings.confirm(
            room_key=room_key,
            stable_device_id=stable_id,
            observer_udid=observer_udid,
            notes=notes,
        )
        path = None
        if persist:
            path = str(self.bindings.save())
        # Invalidate cached provider so mapping rebuilds.
        self._provider = None
        return {
            "room_key": binding.room_key,
            "stable_device_id": binding.stable_device_id,
            "observer_udid_fingerprint": fingerprint_udid(binding.observer_udid),
            "confirmed_at": binding.confirmed_at,
            "bindings_path": path,
            "capture_proof": bool(capture_proof_nonblank),
        }


def classify_from_screenshot(
    result: ScreenshotResult,
    *,
    label: ProviderState | None = None,
    confidence: float = 0.0,
    evidence: list[str] | None = None,
) -> tuple[ProviderState | None, float, list[str]]:
    """Map capture → provider state inputs for ProviderObservation.

    Legacy sync helper: blank gate + optional fake SHA label. Live Vision OCR
    uses :meth:`RoomScreenshotService.classify_capture` / VisionNetflixClassifier.
    """
    ev = list(evidence or [])
    if result.error and result.png_bytes is None:
        return None, 0.0, ev + [f"capture_error:{result.error}"]
    if result.blank_or_protected:
        ev.append(result.metadata.blank_reason or "blank_or_protected")
        return ProviderState.BLANK_OR_PROTECTED, max(confidence, 0.9), ev
    if label is not None:
        ev.append(f"visual_label:{label.value}")
        return label, confidence, ev
    return None, confidence, ev + ["visual_unclassified"]


def assert_visual_gate(
    state: ProviderState | None,
    *,
    confidence: float,
    require: bool,
) -> None:
    if not require:
        return
    if state is None:
        raise SafetyBlockedError(
            "Screenshot gate: unclassified visual state; refusing blind navigation",
            reason="screenshot_gate_unclassified",
        )
    if state == ProviderState.BLANK_OR_PROTECTED:
        raise SafetyBlockedError(
            "Screenshot is blank or DRM-protected; treating as unobservable (not failure invent)",
            reason="blank_or_protected",
        )
    if state == ProviderState.UNKNOWN and confidence < LOW_CONFIDENCE_STOP:
        raise SafetyBlockedError(
            "Screenshot gate: low-confidence/unknown visual state; refusing blind navigation",
            reason="screenshot_gate_low_confidence",
        )
    if confidence < LOW_CONFIDENCE_STOP:
        raise SafetyBlockedError(
            "Screenshot gate: confidence below stop threshold; refusing blind navigation",
            reason="screenshot_gate_low_confidence",
        )


LabelResolver = Callable[
    [ScreenshotResult],
    Awaitable[tuple[ProviderState | None, float, list[str]]],
]


async def resolve_label_from_fake(
    result: ScreenshotResult,
    fake: Any,
) -> tuple[ProviderState | None, float, list[str]]:
    label = fake.label_for_sha(result.metadata.sha256)
    return classify_from_screenshot(result, label=label, confidence=0.95 if label else 0.0)
