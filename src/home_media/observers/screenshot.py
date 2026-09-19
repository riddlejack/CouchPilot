"""Screenshot observation contracts and fail-closed providers.

Live capture uses an optional subprocess-boundary pymobiledevice3 stack
(GPL-3.0 optional extra). Core home-media never imports pymobiledevice3.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from home_media.errors import ConfigError, UnsupportedError

ANALYSIS_FRAME_MAX_WIDTH = 1280
STABILITY_FRAME_WIDTH = 256


class CaptureMetadata(BaseModel):
    """Typed capture metadata safe for CLI/MCP text (no image bytes, no raw UDID)."""

    latency_ms: int | None = None
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    mean_luminance: float | None = None
    blank_reason: str | None = None
    capture_backend: str | None = None
    worker_reused: bool | None = None
    worker_startup_ms: int | None = None
    argv_has_udid: bool | None = None
    argv_has_userspace: bool | None = None
    captured_at: str | None = None
    path: str | None = None


class ScreenshotResult(BaseModel):
    device_id: str
    room_key: str
    blank_or_protected: bool = True
    png_bytes: bytes | None = None
    error: str | None = None
    tunnel_identity: str | None = None
    observer_udid_fingerprint: str | None = None
    metadata: CaptureMetadata = Field(default_factory=CaptureMetadata)

    def public_metadata(self) -> dict[str, object]:
        """JSON-safe metadata without image bytes or raw observer identifiers."""
        meta = self.metadata.model_dump(mode="json")
        return {
            "device_id": self.device_id,
            "room_key": self.room_key,
            "blank_or_protected": self.blank_or_protected,
            "error": self.error,
            "observer_udid_fingerprint": self.observer_udid_fingerprint,
            # tunnel_identity is treated as sensitive; only expose fingerprint.
            "has_png": self.png_bytes is not None,
            "png_sha256": meta.get("sha256"),
            "width": meta.get("width"),
            "height": meta.get("height"),
            "latency_ms": meta.get("latency_ms"),
            "mean_luminance": meta.get("mean_luminance"),
            "blank_reason": meta.get("blank_reason"),
            "capture_backend": meta.get("capture_backend"),
            "worker_reused": meta.get("worker_reused"),
            "worker_startup_ms": meta.get("worker_startup_ms"),
            "captured_at": meta.get("captured_at"),
        }


def prepare_screenshot_for_analysis(
    result: ScreenshotResult,
    *,
    max_width: int = ANALYSIS_FRAME_MAX_WIDTH,
) -> ScreenshotResult:
    """Return a smaller in-memory copy for OCR while preserving raw capture evidence."""
    if max_width < 320:
        raise ValueError("analysis frame width must be at least 320 pixels")
    if not result.png_bytes:
        return result

    from PIL import Image

    with Image.open(io.BytesIO(result.png_bytes)) as image:
        image.load()
        if image.width <= max_width:
            return result
        height = max(1, round(image.height * max_width / image.width))
        resized = image.resize((max_width, height), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        resized.save(output, format="PNG", compress_level=1)
    png = output.getvalue()
    metadata = result.metadata.model_copy(
        update={
            "sha256": hashlib.sha256(png).hexdigest(),
            "width": max_width,
            "height": height,
        }
    )
    return result.model_copy(update={"png_bytes": png, "metadata": metadata})


def ui_stability_fingerprint(png_bytes: bytes | None) -> str | None:
    """Hash stable tvOS UI while ignoring Xcode's screenshot notification.

    Every DVT capture can briefly add a ``Screenshot Taken`` toast in the
    upper-right corner of the *next* frame. Raw byte hashes therefore make an
    unchanged focused control look animated and force another model call. The
    toast region never contains a Netflix focus target in the supported flows,
    so mask only that corner and hash a small RGB raster. Changes elsewhere,
    including focus borders and CTA labels, still change the fingerprint.
    """
    if not png_bytes:
        return None

    from PIL import Image, ImageDraw

    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            image.load()
            rgb = image.convert("RGB")
            height = max(1, round(rgb.height * STABILITY_FRAME_WIDTH / rgb.width))
            resized = rgb.resize(
                (STABILITY_FRAME_WIDTH, height),
                Image.Resampling.BILINEAR,
            )
            draw = ImageDraw.Draw(resized)
            draw.rectangle(
                (
                    round(resized.width * 0.60),
                    0,
                    resized.width,
                    round(resized.height * 0.18),
                ),
                fill=(0, 0, 0),
            )
            return hashlib.sha256(resized.tobytes()).hexdigest()
    except (OSError, ValueError):
        # A malformed test/adapter payload must never break the control loop;
        # callers retain the capture's raw SHA-256 fallback.
        return None


@runtime_checkable
class ScreenshotProvider(Protocol):
    async def capture(self, stable_device_id: str, room_key: str) -> ScreenshotResult: ...


CaptureFn = Callable[[str, str, str], Awaitable[ScreenshotResult]]
"""(udid, stable_device_id, room_key) -> ScreenshotResult"""


class UnavailableScreenshotProvider:
    """Always fails closed — Xcode/pymobiledevice3 not authorized in this process."""

    async def capture(self, stable_device_id: str, room_key: str) -> ScreenshotResult:
        message = (
            "Screenshot capture unavailable: Xcode/pymobiledevice3 is not authorized "
            "in this process. Live stack must be separately authorized."
        )
        return ScreenshotResult(
            device_id=stable_device_id,
            room_key=room_key,
            blank_or_protected=True,
            png_bytes=None,
            error=message,
            tunnel_identity=None,
        )


class BoundScreenshotProvider:
    """Bind stable Apple TV ids to observer UDIDs; optionally capture pixels.

    Requires an exact ``stable_device_id`` match in ``mapping``
    (``stable_id -> observer_udid``). Fails on zero matches, ambiguous
    multi-match configurations, or duplicate tunnel targets. Never falls back
    to ``devices[0]``.

    Without ``capturer``, remains a fail-closed binding stub (no pixels).
    """

    def __init__(
        self,
        mapping: dict[str, str],
        *,
        capturer: CaptureFn | None = None,
    ) -> None:
        if not mapping:
            raise ConfigError(
                "BoundScreenshotProvider requires a non-empty stable_id -> tunnel_id mapping"
            )
        tunnel_counts: dict[str, int] = {}
        for tunnel_id in mapping.values():
            tunnel_counts[tunnel_id] = tunnel_counts.get(tunnel_id, 0) + 1
        dupes = [t for t, n in tunnel_counts.items() if n > 1]
        if dupes:
            raise ConfigError(
                "BoundScreenshotProvider mapping has duplicate tunnel identities"
            )
        self.mapping = dict(mapping)
        self._capturer = capturer
        self._inner_error = (
            "BoundScreenshotProvider is a binding stub only; "
            "live Xcode/pymobiledevice3 capture is separately authorized and not invoked here"
        )

    def resolve_udid(self, stable_device_id: str) -> str:
        if not stable_device_id:
            raise UnsupportedError(
                "screenshot.capture",
                reason="stable_device_id is required; refusing unbound capture",
            )
        matches = [sid for sid in self.mapping if sid == stable_device_id]
        if len(matches) == 0:
            raise UnsupportedError(
                "screenshot.capture",
                reason=(
                    f"No tunnel binding for stable_device_id={stable_device_id!r}; "
                    "never falling back to devices[0]"
                ),
            )
        if len(matches) > 1:
            raise ConfigError(f"Multiple bindings for stable_device_id={stable_device_id!r}")
        return self.mapping[stable_device_id]

    async def capture(self, stable_device_id: str, room_key: str) -> ScreenshotResult:
        from home_media.observers.binding import fingerprint_udid

        udid = self.resolve_udid(stable_device_id)
        if self._capturer is None:
            return ScreenshotResult(
                device_id=stable_device_id,
                room_key=room_key,
                blank_or_protected=True,
                png_bytes=None,
                error=self._inner_error,
                tunnel_identity=None,
                observer_udid_fingerprint=fingerprint_udid(udid),
            )
        return await self._capturer(udid, stable_device_id, room_key)
