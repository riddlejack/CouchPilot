"""Fake screenshot provider with recorded synthetic fixtures per provider state."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from home_media.errors import UnsupportedError
from home_media.observers.blank import synthesize_png
from home_media.observers.capture import finalize_png_result
from home_media.observers.screenshot import ScreenshotResult
from home_media.providers.base import ProviderState

# Distinct non-black colors per state so blank detection stays false (except blank/playing).
_STATE_COLORS: dict[ProviderState, tuple[int, int, int]] = {
    ProviderState.APPLE_HOME: (32, 64, 110),
    ProviderState.PROFILE_PICKER: (40, 120, 200),
    ProviderState.HOME: (200, 40, 40),
    ProviderState.SEARCH_NAV: (40, 200, 80),
    ProviderState.SEARCH_KEYBOARD: (200, 180, 40),
    ProviderState.SEARCH_RESULTS: (160, 80, 200),
    ProviderState.TITLE_DETAIL: (80, 160, 200),
    ProviderState.PLAYING: (2, 2, 2),
    ProviderState.ERROR_OR_MODAL: (220, 100, 20),
    ProviderState.BLANK_OR_PROTECTED: (0, 0, 0),
    ProviderState.UNKNOWN: (90, 90, 90),
}


@dataclass(frozen=True)
class FakeFixture:
    state: ProviderState
    png_bytes: bytes
    label: str


def fixture_for_state(state: ProviderState, *, width: int = 32, height: int = 18) -> FakeFixture:
    color = _STATE_COLORS.get(state, (90, 90, 90))
    png = synthesize_png(width, height, color)
    return FakeFixture(state=state, png_bytes=png, label=state.value)


def all_state_fixtures() -> dict[ProviderState, FakeFixture]:
    return {state: fixture_for_state(state) for state in ProviderState}


class FakeScreenshotProvider:
    """Map stable_device_id → fixture. Never returns another device's pixels."""

    def __init__(self, fixtures: dict[str, FakeFixture] | None = None) -> None:
        self.fixtures = dict(fixtures or {})
        self.capture_calls: list[tuple[str, str]] = []
        self._labels: dict[str, ProviderState] = {}
        self._highlighted: dict[str, str] = {}
        self._highlighted_by_device: dict[str, str] = {}
        self._state_source: object | None = None

    def link_provider_state(self, apple_adapter: object) -> None:
        """When set, capture reads ``apple_adapter.provider_state[device_id]``."""
        self._state_source = apple_adapter

    def set_state(self, stable_device_id: str, state: ProviderState) -> None:
        self.fixtures[stable_device_id] = fixture_for_state(state)

    def set_highlighted_profile(self, stable_device_id: str, name: str | None) -> None:
        if name is None:
            self._highlighted_by_device.pop(stable_device_id, None)
        else:
            self._highlighted_by_device[stable_device_id] = name

    def label_for_sha(self, sha256: str | None) -> ProviderState | None:
        if not sha256:
            return None
        return self._labels.get(sha256)

    def highlighted_for_sha(self, sha256: str | None) -> str | None:
        if not sha256:
            return None
        return self._highlighted.get(sha256)

    async def capture(self, stable_device_id: str, room_key: str) -> ScreenshotResult:
        self.capture_calls.append((stable_device_id, room_key))
        if not stable_device_id:
            raise UnsupportedError(
                "screenshot.capture",
                reason="stable_device_id is required; refusing unbound capture",
            )
        fixture = self.fixtures.get(stable_device_id)
        if self._state_source is not None:
            raw = getattr(self._state_source, "provider_state", {}).get(stable_device_id)
            if raw:
                try:
                    fixture = fixture_for_state(ProviderState(str(raw)))
                    self.fixtures[stable_device_id] = fixture
                except ValueError:
                    pass
        if fixture is None:
            raise UnsupportedError(
                "screenshot.capture",
                reason=(
                    f"No fake fixture for stable_device_id={stable_device_id!r}; "
                    "never falling back to devices[0]"
                ),
            )
        t0 = time.perf_counter()
        result = finalize_png_result(
            fixture.png_bytes,
            stable_device_id=stable_device_id,
            room_key=room_key,
            udid=f"fake-udid-{stable_device_id}",
            latency_ms=max(1, int((time.perf_counter() - t0) * 1000)),
        )
        digest = hashlib.sha256(fixture.png_bytes).hexdigest()
        result.metadata.sha256 = digest
        result.metadata.capture_backend = "fake"
        self._labels[digest] = fixture.state
        highlighted = self._highlighted_by_device.get(stable_device_id)
        if highlighted is None and self._state_source is not None:
            highlighted = getattr(self._state_source, "highlighted_profile", {}).get(
                stable_device_id
            )
        if highlighted and fixture.state == ProviderState.PROFILE_PICKER:
            self._highlighted[digest] = highlighted
        if fixture.state in {ProviderState.BLANK_OR_PROTECTED, ProviderState.PLAYING}:
            result.blank_or_protected = True
            result.metadata.blank_reason = "fake_blank_or_protected_fixture"
        return result
