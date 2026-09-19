"""Typed asynchronous live screen-classifier seam.

Classifier results carry provider state, confidence, safe anchor codes,
classifier name, latency, screenshot SHA, and optional highlighted profile.
Full OCR text, profile lists, image paths, and pixels stay out of action logs.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from home_media.errors import UnsupportedError
from home_media.observers.netflix_anchors import (
    ANCHOR_PROFILE_CHOOSE,
    ANCHOR_PROFILE_WHOS,
    classify_netflix_anchors,
)
from home_media.observers.ocr_types import OcrDocument
from home_media.observers.screenshot import ScreenshotResult
from home_media.providers.base import ProviderState

NAV_CONFIDENCE_MIN = 0.70
PROFILE_SELECT_CONFIDENCE_MIN = 0.85
CLASSIFIER_NAME_VISION = "macos_vision_netflix_v1"
CLASSIFIER_NAME_FAKE_LABEL = "fake_sha_label"
CLASSIFIER_NAME_BLANK = "blank_gate"


class ScreenClassifierResult(BaseModel):
    """Safe-to-log visual classification outcome (no OCR dump / paths / pixels)."""

    provider_state: ProviderState | None = None
    confidence: float = 0.0
    anchor_codes: list[str] = Field(default_factory=list)
    classifier_name: str = "none"
    latency_ms: int = 0
    screenshot_sha: str | None = None
    highlighted_profile_name: str | None = None
    error: str | None = None
    # Available to the local computer-use bridge as indexed AX-like elements,
    # but deliberately excluded from logs/JSON because it can contain private
    # on-screen text.
    ocr_document: OcrDocument | None = Field(default=None, exclude=True, repr=False)

    def public_log_fields(self) -> dict[str, object]:
        """Fields safe for ordinary action / stage evidence."""
        return {
            "provider_state": self.provider_state.value if self.provider_state else None,
            "confidence": round(self.confidence, 3),
            "anchor_codes": list(self.anchor_codes),
            "classifier_name": self.classifier_name,
            "latency_ms": self.latency_ms,
            "screenshot_sha": self.screenshot_sha,
            "highlighted_profile_present": self.highlighted_profile_name is not None,
            # Never include the raw profile string in generic dumps — callers that
            # need equality checks read highlighted_profile_name explicitly.
            "error": self.error,
        }


@runtime_checkable
class ScreenClassifier(Protocol):
    async def classify(
        self,
        result: ScreenshotResult,
        *,
        requested_query: str | None = None,
    ) -> ScreenClassifierResult: ...


OcrFn = Callable[[bytes], Awaitable[OcrDocument]]


class VisionNetflixClassifier:
    """Blank gate → Vision OCR → portable Netflix anchor rules."""

    def __init__(self, *, ocr: OcrFn | None = None) -> None:
        self._ocr = ocr

    async def classify(
        self,
        result: ScreenshotResult,
        *,
        requested_query: str | None = None,
    ) -> ScreenClassifierResult:
        t0 = time.perf_counter()
        sha = result.metadata.sha256
        if result.error and result.png_bytes is None:
            return ScreenClassifierResult(
                provider_state=None,
                confidence=0.0,
                anchor_codes=["capture_error"],
                classifier_name=CLASSIFIER_NAME_BLANK,
                latency_ms=_ms(t0),
                screenshot_sha=sha,
                error=result.error,
            )
        # Blank / DRM gate runs BEFORE OCR.
        if result.blank_or_protected:
            return ScreenClassifierResult(
                provider_state=ProviderState.BLANK_OR_PROTECTED,
                confidence=0.95,
                anchor_codes=[result.metadata.blank_reason or "blank_or_protected"],
                classifier_name=CLASSIFIER_NAME_BLANK,
                latency_ms=_ms(t0),
                screenshot_sha=sha,
            )
        if not result.png_bytes:
            return ScreenClassifierResult(
                provider_state=ProviderState.BLANK_OR_PROTECTED,
                confidence=0.9,
                anchor_codes=["empty_image"],
                classifier_name=CLASSIFIER_NAME_BLANK,
                latency_ms=_ms(t0),
                screenshot_sha=sha,
            )

        try:
            if self._ocr is not None:
                document = await self._ocr(result.png_bytes)
            else:
                from home_media.observers.ocr import run_vision_ocr

                document = await run_vision_ocr(result.png_bytes)
        except UnsupportedError as exc:
            return ScreenClassifierResult(
                provider_state=ProviderState.UNKNOWN,
                confidence=0.0,
                anchor_codes=["ocr_unavailable"],
                classifier_name=CLASSIFIER_NAME_VISION,
                latency_ms=_ms(t0),
                screenshot_sha=sha,
                error=str(exc.message),
            )
        except Exception as exc:  # noqa: BLE001
            return ScreenClassifierResult(
                provider_state=ProviderState.UNKNOWN,
                confidence=0.0,
                anchor_codes=["ocr_error"],
                classifier_name=CLASSIFIER_NAME_VISION,
                latency_ms=_ms(t0),
                screenshot_sha=sha,
                error=type(exc).__name__,
            )

        classified = classify_netflix_anchors(document, requested_query=requested_query)
        return ScreenClassifierResult(
            provider_state=classified.state,
            confidence=classified.confidence,
            anchor_codes=list(classified.anchors),
            classifier_name=CLASSIFIER_NAME_VISION,
            latency_ms=_ms(t0),
            screenshot_sha=sha,
            highlighted_profile_name=classified.highlighted_profile_name,
            ocr_document=document,
        )


class FakeLabelClassifier:
    """Deterministic SHA → ProviderState labels for FakeScreenshotProvider."""

    def __init__(
        self,
        label_resolver: Callable[[str | None], ProviderState | None],
        *,
        highlighted_resolver: Callable[[str | None], str | None] | None = None,
        confidence: float = 0.95,
    ) -> None:
        self._label_resolver = label_resolver
        self._highlighted_resolver = highlighted_resolver
        self._confidence = confidence

    async def classify(
        self,
        result: ScreenshotResult,
        *,
        requested_query: str | None = None,
    ) -> ScreenClassifierResult:
        _ = requested_query
        t0 = time.perf_counter()
        sha = result.metadata.sha256
        if result.blank_or_protected:
            return ScreenClassifierResult(
                provider_state=ProviderState.BLANK_OR_PROTECTED,
                confidence=0.95,
                anchor_codes=[result.metadata.blank_reason or "blank_or_protected"],
                classifier_name=CLASSIFIER_NAME_BLANK,
                latency_ms=_ms(t0),
                screenshot_sha=sha,
            )
        label = self._label_resolver(sha)
        if label is None:
            return ScreenClassifierResult(
                provider_state=None,
                confidence=0.0,
                anchor_codes=["visual_unclassified"],
                classifier_name=CLASSIFIER_NAME_FAKE_LABEL,
                latency_ms=_ms(t0),
                screenshot_sha=sha,
            )
        anchors = _fake_anchors_for(label)
        highlighted = None
        if self._highlighted_resolver is not None:
            highlighted = self._highlighted_resolver(sha)
        if highlighted and label == ProviderState.PROFILE_PICKER:
            anchors = [*anchors, "netflix.chrome.highlighted_profile"]
        return ScreenClassifierResult(
            provider_state=label,
            confidence=self._confidence,
            anchor_codes=anchors,
            classifier_name=CLASSIFIER_NAME_FAKE_LABEL,
            latency_ms=_ms(t0),
            screenshot_sha=sha,
            highlighted_profile_name=highlighted,
        )


def profile_picker_chrome_present(anchors: list[str]) -> bool:
    return ANCHOR_PROFILE_CHOOSE in anchors or ANCHOR_PROFILE_WHOS in anchors


def _fake_anchors_for(state: ProviderState) -> list[str]:
    if state == ProviderState.APPLE_HOME:
        return ["apple.chrome.app_grid"]
    if state == ProviderState.PROFILE_PICKER:
        return [ANCHOR_PROFILE_CHOOSE, ANCHOR_PROFILE_WHOS]
    if state == ProviderState.HOME:
        return ["netflix.chrome.top_nav"]
    if state == ProviderState.SEARCH_KEYBOARD:
        return ["netflix.chrome.search", "netflix.chrome.keyboard"]
    if state == ProviderState.SEARCH_RESULTS:
        return ["netflix.chrome.search", "netflix.chrome.results", "netflix.chrome.query_visible"]
    if state == ProviderState.SEARCH_NAV:
        return ["netflix.chrome.search"]
    if state == ProviderState.TITLE_DETAIL:
        return ["netflix.chrome.title_detail"]
    if state == ProviderState.ERROR_OR_MODAL:
        return ["netflix.chrome.error_or_modal"]
    if state == ProviderState.BLANK_OR_PROTECTED:
        return ["blank_or_protected"]
    return ["fake_label"]


def _ms(t0: float) -> int:
    return max(0, int((time.perf_counter() - t0) * 1000))
