"""Device observation helpers (screenshots, etc.).

Live screenshot capture uses an optional GPL pymobiledevice3 tool via
subprocess only. Core home-media never imports pymobiledevice3.
"""

from home_media.observers.binding import ObserverBinding, ObserverBindingStore, fingerprint_udid
from home_media.observers.fake import FakeFixture, FakeScreenshotProvider, fixture_for_state
from home_media.observers.screenshot import (
    BoundScreenshotProvider,
    CaptureMetadata,
    ScreenshotProvider,
    ScreenshotResult,
    UnavailableScreenshotProvider,
)
from home_media.observers.service import RoomScreenshotService

__all__ = [
    "BoundScreenshotProvider",
    "CaptureMetadata",
    "FakeFixture",
    "FakeScreenshotProvider",
    "ObserverBinding",
    "ObserverBindingStore",
    "RoomScreenshotService",
    "ScreenshotProvider",
    "ScreenshotResult",
    "UnavailableScreenshotProvider",
    "fingerprint_udid",
    "fixture_for_state",
]
