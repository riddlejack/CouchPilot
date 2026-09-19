"""Generic observe -> act -> observe control loop for focus-based Apple TV UIs.

The controller deliberately separates two speeds of operation:

* :meth:`AppleTVComputerUseController.act` executes one remote action and always
  returns the resulting frame.  This is the safe primitive for model-driven
  exploration.
* :meth:`AppleTVComputerUseController.run_macro` executes a small, previously
  verified burst between recognized visual states.  It checks the pre-frame,
  captures the post-frame, and stops with ``vision_required`` on any mismatch.

No provider recipe is allowed to turn a stale observation into remote input.
PNG bytes remain available to an MCP/image response, while JSON serialization
contains only indexed OCR/accessibility-like elements and redacted metadata.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from home_media.content.prepare import normalize_query, queries_match
from home_media.errors import AmbiguousOutcomeError, SafetyBlockedError
from home_media.models import PowerState, utcnow
from home_media.observers.classify import ScreenClassifierResult
from home_media.observers.ocr_types import OcrDocument
from home_media.observers.screenshot import ScreenshotResult
from home_media.providers.base import ProviderState

MAX_HISTORY = 12
DEFAULT_MAX_STATE_AGE_S = 8.0
SELECT_MIN_CONFIDENCE = 0.70
POST_ACTION_FRESHNESS_ATTEMPTS = 3
POST_ACTION_FRESHNESS_RETRY_S = 0.05


class ComputerUseActionKind(StrEnum):
    PRESS_KEY = "press_key"
    LAUNCH_APP = "launch_app"
    SET_TEXT = "set_text"
    WAKE = "wake"
    TRANSPORT = "transport"


class ComputerUseTerminal(StrEnum):
    VERIFIED = "verified"
    VISION_REQUIRED = "vision_required"
    BLOCKED = "blocked"


class ScreenElement(BaseModel):
    """Accessibility-like element synthesized from OCR or a future tvOS AX tree."""

    index: int
    label: str
    role: str = "text"
    x: float
    y: float
    width: float
    height: float
    focused: bool | None = None
    enabled: bool | None = None


class ScreenSemantics(BaseModel):
    """Provider-neutral screen meaning carried alongside the raw image."""

    state: str | None = None
    confidence: float = 0.0
    anchors: list[str] = Field(default_factory=list)
    focused_label: str | None = None
    elements: list[ScreenElement] = Field(default_factory=list)
    source: str = "pixels"
    error: str | None = None


class DeviceContext(BaseModel):
    """Nonvisual Apple TV signals that make blank/DRM screens observable."""

    current_app: str | None = None
    power: PowerState = PowerState.UNKNOWN
    keyboard_focused: bool | None = None
    keyboard_text: str | None = None
    now_playing_title: str | None = None
    now_playing_series: str | None = None
    playback_state: str | None = None


class ComputerUseState(BaseModel):
    """One exact-room visual state suitable for an image-capable agent."""

    schema_version: int = 1
    room_key: str
    device_id: str
    sequence: int
    observed_at: datetime = Field(default_factory=utcnow)
    # Producer capture time is evidence about the pixels. observed_at is only
    # when this controller finished building the state and must never refresh
    # or substitute for it.
    captured_at: datetime | None = None
    capture_time_error: str | None = None
    capture_error: str | None = None
    frame_sha256: str | None = None
    width: int | None = None
    height: int | None = None
    blank_or_protected: bool = False
    capture_latency_ms: int | None = None
    observation_latency_ms: int | None = None
    semantics: ScreenSemantics = Field(default_factory=ScreenSemantics)
    device: DeviceContext = Field(default_factory=DeviceContext)
    recent_actions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # The MCP facade consumes these bytes as an Image content block.  Keeping
    # them out of model_dump avoids accidentally embedding a household screen in
    # logs or JSON transports.
    png_bytes: bytes | None = Field(default=None, exclude=True, repr=False)

    def agent_metadata(self) -> dict[str, Any]:
        """Return JSON-safe metadata plus indexed elements, never image bytes."""
        return self.model_dump(mode="json")


class ComputerUseAction(BaseModel):
    """One typed remote mutation tied to the exact observed generation."""

    kind: ComputerUseActionKind
    expected_sequence: int
    key: str | None = None
    app_bundle_id: str | None = None
    text: str | None = None
    transport_action: str | None = None
    wake_before: bool = False
    required_current_app: str | None = None
    require_keyboard_focus: bool = False
    # An image-capable agent may name the visible target it inspected on the
    # exact expected generation.  This unlocks Select on novel/unclassified
    # apps without weakening deterministic provider macros or allowing a blind
    # Select with no frame.
    model_observed_target: str | None = None
    allowed_from_states: list[str] = Field(default_factory=list)
    required_anchors: list[str] = Field(default_factory=list)
    min_confidence: float = 0.0

    @model_validator(mode="after")
    def _validate_payload(self) -> ComputerUseAction:
        if self.kind == ComputerUseActionKind.PRESS_KEY and not self.key:
            raise ValueError("press_key requires key")
        if self.kind == ComputerUseActionKind.LAUNCH_APP and not self.app_bundle_id:
            raise ValueError("launch_app requires app_bundle_id")
        if self.kind == ComputerUseActionKind.SET_TEXT and self.text is None:
            raise ValueError("set_text requires text (empty string is allowed)")
        if self.kind == ComputerUseActionKind.TRANSPORT and not self.transport_action:
            raise ValueError("transport requires transport_action")
        if self.model_observed_target is not None:
            target = self.model_observed_target.strip()
            if not target or len(target) > 200:
                raise ValueError("model_observed_target must be 1-200 characters")
            self.model_observed_target = target
        return self

    @property
    def label(self) -> str:
        detail = self.key or self.app_bundle_id or self.transport_action
        return f"{self.kind.value}:{detail}" if detail else self.kind.value


class ComputerUseActionResult(BaseModel):
    room_key: str
    action: ComputerUseAction
    before_sequence: int
    before_frame_sha256: str | None = None
    after: ComputerUseState
    frame_changed: bool | None = None
    latency_ms: int


class VerifiedMacroStep(BaseModel):
    """A bounded burst with explicit visual/device pre- and postconditions."""

    name: str
    allowed_from_states: list[str]
    actions: list[ComputerUseAction]
    accepted_post_states: list[str]
    pre_required_anchors: list[str] = Field(default_factory=list)
    post_required_anchors: list[str] = Field(default_factory=list)
    min_pre_confidence: float = 0.70
    min_post_confidence: float = 0.70
    expected_focused_label: str | None = None
    expected_keyboard_text: str | None = None
    required_current_app: str | None = None
    require_keyboard_focus: bool = False
    allow_blank_or_protected_pre: bool = False
    # Remote actions and tvOS animations can settle after the first fresh DVT
    # frame. Retries are observation-only: actions are never re-sent.
    postcondition_retries: int = Field(default=0, ge=0, le=2)
    inter_action_delay_s: float = Field(default=0.0, ge=0.0, le=2.0)

    @model_validator(mode="after")
    def _bounded_burst(self) -> VerifiedMacroStep:
        if not self.actions:
            raise ValueError("verified macro step requires at least one action")
        if len(self.actions) > 4:
            raise ValueError("verified macro burst is limited to four actions")
        return self


class VerifiedMacro(BaseModel):
    provider: str
    name: str
    version: int = 1
    steps: list[VerifiedMacroStep]
    max_state_age_s: float = DEFAULT_MAX_STATE_AGE_S


class MacroStepResult(BaseModel):
    name: str
    before_sequence: int
    after_sequence: int | None = None
    terminal: ComputerUseTerminal
    reason: str | None = None
    actions_sent: list[str] = Field(default_factory=list)
    before_state: str | None = None
    after_state: str | None = None


class VerifiedMacroResult(BaseModel):
    room_key: str
    provider: str
    macro: str
    terminal: ComputerUseTerminal
    final_state: ComputerUseState
    steps: list[MacroStepResult] = Field(default_factory=list)
    fallback_reason: str | None = None


FrameSource = Callable[[str], ScreenshotResult | Awaitable[ScreenshotResult]]
ContextSource = Callable[[str], DeviceContext | Awaitable[DeviceContext]]
SemanticsSource = Callable[[ScreenshotResult], ScreenSemantics | Awaitable[ScreenSemantics]]
ActionExecutor = Callable[[str, ComputerUseAction], None | Awaitable[None]]


class AppleTVComputerUseController:
    """Process-scoped exact-room controller with stale-frame protection."""

    def __init__(
        self,
        *,
        frame_source: FrameSource,
        action_executor: ActionExecutor,
        context_source: ContextSource | None = None,
        semantics_source: SemanticsSource | None = None,
        max_history: int = MAX_HISTORY,
    ) -> None:
        self._frame_source = frame_source
        self._action_executor = action_executor
        self._context_source = context_source
        self._semantics_source = semantics_source
        self._states: dict[str, ComputerUseState] = {}
        self._sequences: defaultdict[str, int] = defaultdict(int)
        self._history: defaultdict[str, deque[str]] = defaultdict(lambda: deque(maxlen=max_history))
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def observe(
        self,
        room_key: str,
        *,
        classify: bool = True,
    ) -> ComputerUseState:
        async with self._locks[room_key]:
            return await self._observe_unlocked(room_key, classify=classify)

    async def read_device_context(self, room_key: str) -> DeviceContext:
        """Read transport/app metadata without paying for a new screenshot.

        Receipt polling after a Play/Resume or Pause action only needs the
        persistent pyatv connection. Keeping that read on the controller's room
        lock preserves ordering with remote actions while avoiding repeated DVT
        captures during Netflix's playback transition.
        """
        async with self._locks[room_key]:
            if self._context_source is None:
                return DeviceContext()
            return await _await_value(self._context_source(room_key))

    async def act(
        self,
        room_key: str,
        action: ComputerUseAction,
        *,
        max_state_age_s: float = DEFAULT_MAX_STATE_AGE_S,
        classify_after: bool = True,
    ) -> ComputerUseActionResult:
        """Execute one action and return a producer-timed post-action frame.

        Failure to obtain such a frame after dispatch is an ambiguous outcome:
        the action may have reached the device, so callers must observe and
        reconcile instead of automatically sending it again.
        """
        started = time.perf_counter()
        async with self._locks[room_key]:
            before = self._require_current_state(
                room_key, action.expected_sequence, max_state_age_s=max_state_age_s
            )
            self._assert_action_allowed(action, before)
            await self._execute(room_key, action)
            action_completed_at = utcnow()
            self._history[room_key].append(action.label)
            after, freshness_reason = await self._observe_after_barrier_unlocked(
                room_key,
                barrier=action_completed_at,
                classify=classify_after,
            )
            if freshness_reason is not None:
                raise AmbiguousOutcomeError(
                    "Action was dispatched but its post-action frame could not be verified",
                    details={
                        "outcome": "action_dispatched_post_frame_unverified",
                        "reason": "computer_use_post_action_frame_unverified",
                        "freshness_reason": freshness_reason,
                        "action": action.label,
                        "before_sequence": before.sequence,
                        "after_sequence": after.sequence,
                        "action_dispatched": True,
                        "do_not_retry": True,
                    },
                )
        return ComputerUseActionResult(
            room_key=room_key,
            action=action,
            before_sequence=before.sequence,
            before_frame_sha256=before.frame_sha256,
            after=after,
            frame_changed=_frame_changed(before, after),
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
        )

    async def run_macro(
        self,
        room_key: str,
        macro: VerifiedMacro,
        *,
        initial_state: ComputerUseState | None = None,
    ) -> VerifiedMacroResult:
        """Run verified bursts and yield to vision immediately on drift.

        The entire macro is serialized per room.  Every step has a recognized
        pre-state and a freshly captured post-state.  A failed postcondition
        prevents all later actions from being dispatched.
        """
        async with self._locks[room_key]:
            current = initial_state or self._states.get(room_key)
            if current is None:
                current = await self._observe_unlocked(room_key)
            elif self._states.get(room_key) is None:
                # Only accept an externally supplied state if it is the first
                # state and belongs to this room; make it the current generation.
                if current.room_key != room_key:
                    raise SafetyBlockedError(
                        "Macro initial state belongs to a different room",
                        reason="computer_use_room_mismatch",
                    )
                self._states[room_key] = current
            elif initial_state is not None:
                cached = self._states[room_key]
                if (
                    cached.sequence != initial_state.sequence
                    or cached.frame_sha256 != initial_state.frame_sha256
                ):
                    raise SafetyBlockedError(
                        "Macro initial state is not the controller's current frame",
                        reason="computer_use_stale_generation",
                    )

            step_results: list[MacroStepResult] = []
            for step in macro.steps:
                reason = self._macro_precondition_reason(
                    current, step, max_state_age_s=macro.max_state_age_s
                )
                if reason is not None:
                    step_results.append(
                        MacroStepResult(
                            name=step.name,
                            before_sequence=current.sequence,
                            terminal=ComputerUseTerminal.VISION_REQUIRED,
                            reason=reason,
                            before_state=current.semantics.state,
                        )
                    )
                    return VerifiedMacroResult(
                        room_key=room_key,
                        provider=macro.provider,
                        macro=macro.name,
                        terminal=ComputerUseTerminal.VISION_REQUIRED,
                        final_state=current,
                        steps=step_results,
                        fallback_reason=reason,
                    )

                sent: list[str] = []
                action_completed_at: datetime | None = None
                for action_index, action_template in enumerate(step.actions):
                    action = action_template.model_copy(
                        update={"expected_sequence": current.sequence}
                    )
                    self._assert_action_allowed(action, current)
                    await self._execute(room_key, action)
                    action_completed_at = utcnow()
                    sent.append(action.label)
                    self._history[room_key].append(action.label)
                    if (
                        step.inter_action_delay_s > 0
                        and action_index < len(step.actions) - 1
                    ):
                        await asyncio.sleep(step.inter_action_delay_s)

                assert action_completed_at is not None
                after, freshness_reason = await self._observe_after_barrier_unlocked(
                    room_key,
                    barrier=action_completed_at,
                )
                reason = (
                    "unverified_post_action_frame"
                    if freshness_reason is not None
                    else self._macro_postcondition_reason(after, step)
                )
                retries_remaining = step.postcondition_retries
                while (
                    reason is not None
                    and freshness_reason is None
                    and retries_remaining > 0
                ):
                    after, freshness_reason = await self._observe_after_barrier_unlocked(
                        room_key,
                        barrier=action_completed_at,
                    )
                    reason = (
                        "unverified_post_action_frame"
                        if freshness_reason is not None
                        else self._macro_postcondition_reason(after, step)
                    )
                    retries_remaining -= 1
                terminal = (
                    ComputerUseTerminal.VERIFIED
                    if reason is None
                    else ComputerUseTerminal.VISION_REQUIRED
                )
                step_results.append(
                    MacroStepResult(
                        name=step.name,
                        before_sequence=current.sequence,
                        after_sequence=after.sequence,
                        terminal=terminal,
                        reason=reason,
                        actions_sent=sent,
                        before_state=current.semantics.state,
                        after_state=after.semantics.state,
                    )
                )
                current = after
                if reason is not None:
                    return VerifiedMacroResult(
                        room_key=room_key,
                        provider=macro.provider,
                        macro=macro.name,
                        terminal=ComputerUseTerminal.VISION_REQUIRED,
                        final_state=current,
                        steps=step_results,
                        fallback_reason=reason,
                    )

            return VerifiedMacroResult(
                room_key=room_key,
                provider=macro.provider,
                macro=macro.name,
                terminal=ComputerUseTerminal.VERIFIED,
                final_state=current,
                steps=step_results,
            )

    async def _observe_unlocked(
        self,
        room_key: str,
        *,
        classify: bool = True,
    ) -> ComputerUseState:
        started = time.perf_counter()
        frame_task = asyncio.create_task(_await_value(self._frame_source(room_key)))
        context_task: asyncio.Task[DeviceContext] | None = None
        if self._context_source is not None:
            context_task = asyncio.create_task(_await_value(self._context_source(room_key)))

        try:
            frame = await frame_task
        except BaseException:
            if context_task is not None and not context_task.done():
                context_task.cancel()
                await asyncio.gather(context_task, return_exceptions=True)
            raise
        warnings: list[str] = []
        context = DeviceContext()
        if context_task is not None:
            try:
                context = await context_task
            except Exception as exc:  # noqa: BLE001 - image remains useful
                warnings.append(f"device_context_error:{type(exc).__name__}")

        semantics = ScreenSemantics()
        if classify and self._semantics_source is not None:
            try:
                semantics = await _await_value(self._semantics_source(frame))
            except Exception as exc:  # noqa: BLE001 - return pixels for model fallback
                semantics = ScreenSemantics(
                    state=ProviderState.UNKNOWN.value,
                    error=type(exc).__name__,
                )
                warnings.append(f"semantics_error:{type(exc).__name__}")
        elif not classify:
            semantics = ScreenSemantics(
                state=ProviderState.UNKNOWN.value,
                confidence=0.0,
                anchors=["pixels_only"],
                source="pixels_only",
            )
        if frame.error:
            warnings.append(f"capture_error:{frame.error}")

        observed_at = utcnow()
        captured_at, capture_time_error = _parse_capture_time(frame.metadata.captured_at)
        if capture_time_error is not None:
            warnings.append(f"capture_time_{capture_time_error}")
        elif captured_at is not None and captured_at > observed_at:
            warnings.append("capture_time_future")

        self._sequences[room_key] += 1
        state = ComputerUseState(
            room_key=frame.room_key,
            device_id=frame.device_id,
            sequence=self._sequences[room_key],
            observed_at=observed_at,
            captured_at=captured_at,
            capture_time_error=capture_time_error,
            capture_error=frame.error,
            frame_sha256=frame.metadata.sha256,
            width=frame.metadata.width,
            height=frame.metadata.height,
            blank_or_protected=frame.blank_or_protected,
            capture_latency_ms=frame.metadata.latency_ms,
            observation_latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            semantics=semantics,
            device=context,
            recent_actions=list(self._history[room_key]),
            warnings=warnings,
            png_bytes=frame.png_bytes,
        )
        if state.room_key != room_key:
            raise SafetyBlockedError(
                "Frame source returned a different room",
                reason="computer_use_room_mismatch",
            )
        self._states[room_key] = state
        return state

    async def _observe_after_barrier_unlocked(
        self,
        room_key: str,
        *,
        barrier: datetime,
        classify: bool = True,
    ) -> tuple[ComputerUseState, str | None]:
        """Capture without re-sending until producer time clears an action barrier.

        The barrier is recorded immediately after the action executor returns.
        It is intentionally not the time the capture request starts: a warm
        stream may return a buffered frame from just before that request.
        Identical pixels are accepted when their producer timestamp is fresh.
        """
        latest: ComputerUseState | None = None
        reason: str | None = None
        for attempt in range(POST_ACTION_FRESHNESS_ATTEMPTS):
            latest = await self._observe_unlocked(room_key, classify=classify)
            reason = _post_action_freshness_reason(latest, barrier=barrier)
            if reason is None:
                return latest, None
            if attempt < POST_ACTION_FRESHNESS_ATTEMPTS - 1:
                await asyncio.sleep(POST_ACTION_FRESHNESS_RETRY_S)
        assert latest is not None
        return latest, reason

    def _require_current_state(
        self,
        room_key: str,
        expected_sequence: int,
        *,
        max_state_age_s: float,
    ) -> ComputerUseState:
        state = self._states.get(room_key)
        if state is None:
            raise SafetyBlockedError(
                "Observe the room before sending input",
                reason="computer_use_observation_required",
            )
        if state.sequence != expected_sequence:
            raise SafetyBlockedError(
                "Observed state is stale; observe again before sending input",
                reason="computer_use_stale_generation",
            )
        freshness_reason = _capture_freshness_reason(state)
        if freshness_reason is not None:
            raise SafetyBlockedError(
                "Observed pixels do not have a valid producer capture time",
                reason=f"computer_use_{freshness_reason}",
            )
        if _state_age_s(state) > max_state_age_s:
            raise SafetyBlockedError(
                "Observed state is too old; observe again before sending input",
                reason="computer_use_stale_frame",
            )
        return state

    @staticmethod
    def _assert_action_allowed(
        action: ComputerUseAction,
        state: ComputerUseState,
    ) -> None:
        if action.expected_sequence != state.sequence:
            raise SafetyBlockedError(
                "Action generation does not match current state",
                reason="computer_use_stale_generation",
            )
        model_verified_select = bool(
            action.kind == ComputerUseActionKind.PRESS_KEY
            and action.key == "select"
            and action.model_observed_target
            and state.png_bytes
            and not state.blank_or_protected
        )
        observed_state = state.semantics.state
        if action.allowed_from_states and observed_state not in action.allowed_from_states:
            raise SafetyBlockedError(
                "Action is not allowed from the observed screen",
                reason="computer_use_precondition_failed",
            )
        if (
            state.semantics.confidence < action.min_confidence
            and not model_verified_select
        ):
            raise SafetyBlockedError(
                "Screen confidence is below the action threshold",
                reason="computer_use_low_confidence",
            )
        missing = set(action.required_anchors) - set(state.semantics.anchors)
        if missing:
            raise SafetyBlockedError(
                "Required screen anchors are missing",
                reason="computer_use_anchor_missing",
            )
        if (
            action.required_current_app is not None
            and state.device.current_app != action.required_current_app
        ):
            raise SafetyBlockedError(
                "Action requires a different current app",
                reason="computer_use_current_app_mismatch",
            )
        if action.require_keyboard_focus and not state.device.keyboard_focused:
            raise SafetyBlockedError(
                "Action requires real Apple TV keyboard focus",
                reason="computer_use_keyboard_not_focused",
            )
        if action.kind == ComputerUseActionKind.PRESS_KEY and action.key == "select":
            if not action.allowed_from_states and not model_verified_select:
                raise SafetyBlockedError(
                    "Select requires a recognized source state or an exact-frame model target",
                    reason="computer_use_select_unknown",
                )
            if (
                not model_verified_select
                and action.min_confidence < SELECT_MIN_CONFIDENCE
            ):
                raise SafetyBlockedError(
                    "Select requires a confidence threshold of at least 0.70",
                    reason="computer_use_select_low_threshold",
                )

    async def _execute(self, room_key: str, action: ComputerUseAction) -> None:
        await _await_none(self._action_executor(room_key, action))

    @staticmethod
    def _macro_precondition_reason(
        state: ComputerUseState,
        step: VerifiedMacroStep,
        *,
        max_state_age_s: float,
    ) -> str | None:
        freshness_reason = _capture_freshness_reason(state)
        if freshness_reason is not None:
            return freshness_reason
        if _state_age_s(state) > max_state_age_s:
            return "stale_pre_frame"
        if state.blank_or_protected and not step.allow_blank_or_protected_pre:
            return "blank_or_protected_pre_frame"
        if state.semantics.state not in step.allowed_from_states:
            return "unexpected_pre_state"
        if state.semantics.confidence < step.min_pre_confidence:
            return "low_pre_confidence"
        if set(step.pre_required_anchors) - set(state.semantics.anchors):
            return "missing_pre_anchor"
        if (
            step.required_current_app is not None
            and state.device.current_app != step.required_current_app
        ):
            return "current_app_mismatch"
        if step.require_keyboard_focus and not state.device.keyboard_focused:
            return "keyboard_not_focused"
        wanted = normalize_query(step.expected_focused_label or "")
        observed = normalize_query(state.semantics.focused_label or "")
        if wanted and wanted != observed:
            return "profile_name_mismatch"
        return None

    @staticmethod
    def _macro_postcondition_reason(
        state: ComputerUseState,
        step: VerifiedMacroStep,
    ) -> str | None:
        if state.blank_or_protected:
            return "blank_or_protected_post_frame"
        if state.semantics.state not in step.accepted_post_states:
            return "unexpected_post_state"
        if state.semantics.confidence < step.min_post_confidence:
            return "low_post_confidence"
        if set(step.post_required_anchors) - set(state.semantics.anchors):
            return "missing_post_anchor"
        if step.expected_keyboard_text is not None:
            if not state.device.keyboard_focused:
                return "keyboard_not_focused"
            if not queries_match(step.expected_keyboard_text, state.device.keyboard_text):
                return "keyboard_text_mismatch"
        return None


def semantics_from_classifier(
    classified: ScreenClassifierResult,
    *,
    document: OcrDocument | None = None,
) -> ScreenSemantics:
    """Adapt existing classifiers/OCR into the provider-neutral state contract."""
    elements: list[ScreenElement] = []
    if document is not None:
        elements = [
            ScreenElement(
                index=index,
                label=token.text,
                x=token.x,
                y=token.y,
                width=token.w,
                height=token.h,
            )
            for index, token in enumerate(document.tokens)
            if token.text.strip()
        ]
    return ScreenSemantics(
        state=classified.provider_state.value if classified.provider_state else None,
        confidence=classified.confidence,
        anchors=list(classified.anchor_codes),
        focused_label=classified.highlighted_profile_name,
        elements=elements,
        source=classified.classifier_name,
        error=classified.error,
    )


def netflix_profile_select_macro(
    *,
    sequence: int,
    profile_name: str,
) -> VerifiedMacro:
    """Fast Select only when pixels prove the requested profile is highlighted."""
    picker = ProviderState.PROFILE_PICKER.value
    action = ComputerUseAction(
        kind=ComputerUseActionKind.PRESS_KEY,
        key="select",
        expected_sequence=sequence,
        allowed_from_states=[picker],
        required_anchors=["netflix.chrome.highlighted_profile"],
        min_confidence=0.85,
    )
    return VerifiedMacro(
        provider="netflix",
        name="select_highlighted_profile",
        steps=[
            VerifiedMacroStep(
                name="select_profile",
                allowed_from_states=[picker],
                actions=[action],
                accepted_post_states=[
                    ProviderState.HOME.value,
                    ProviderState.SEARCH_NAV.value,
                    ProviderState.SEARCH_KEYBOARD.value,
                ],
                pre_required_anchors=["netflix.chrome.highlighted_profile"],
                min_pre_confidence=0.85,
                expected_focused_label=profile_name,
                postcondition_retries=1,
            )
        ],
    )


def netflix_home_to_search_macro(*, sequence: int) -> VerifiedMacro:
    """Known Netflix Home -> Search burst, verified before and after."""
    home = ProviderState.HOME.value
    actions = [
        ComputerUseAction(
            kind=ComputerUseActionKind.PRESS_KEY,
            key=key,
            expected_sequence=sequence,
            allowed_from_states=[home],
            required_anchors=["netflix.chrome.top_nav"],
            min_confidence=0.70,
        )
        for key in ("up", "left", "down")
    ]
    return VerifiedMacro(
        provider="netflix",
        name="home_to_search",
        steps=[
            VerifiedMacroStep(
                name="home_to_search_keyboard",
                allowed_from_states=[home],
                actions=actions,
                accepted_post_states=[
                    ProviderState.SEARCH_KEYBOARD.value,
                    ProviderState.SEARCH_RESULTS.value,
                ],
                pre_required_anchors=["netflix.chrome.top_nav"],
                post_required_anchors=["netflix.chrome.keyboard"],
                postcondition_retries=1,
                inter_action_delay_s=0.60,
            )
        ],
    )


def netflix_focus_first_result_macro(
    *,
    sequence: int,
    source_state: str,
) -> VerifiedMacro:
    """Move from Netflix's focused keyboard into the first title result.

    Live tvOS needs three paced Down events: keyboard row -> suggestion lane ->
    result lane -> first result. The burst contains no Select and its final
    screenshot is still visually verified before any title can be opened.
    """
    actions = [
        ComputerUseAction(
            kind=ComputerUseActionKind.PRESS_KEY,
            key="down",
            expected_sequence=sequence,
            allowed_from_states=[source_state],
            require_keyboard_focus=True,
        )
        for _ in range(3)
    ]
    accepted = [state.value for state in ProviderState]
    return VerifiedMacro(
        provider="netflix",
        name="focus_first_result",
        steps=[
            VerifiedMacroStep(
                name="keyboard_to_first_result",
                allowed_from_states=[source_state],
                actions=actions,
                accepted_post_states=accepted,
                min_pre_confidence=0.0,
                min_post_confidence=0.0,
                require_keyboard_focus=True,
                inter_action_delay_s=0.75,
            )
        ],
    )


def netflix_exit_paused_playback_macro(
    *,
    sequence: int,
    source_state: str,
) -> VerifiedMacro:
    """Exit Netflix's paused DRM surface with one Back action.

    A single Menu returns the proven paused playback surface to the title page.
    A second blind Back can exit Netflix entirely, so slow transitions are
    handled only with read-only postcondition captures. The skill contains no
    Select and cannot start content.
    """
    actions = [
        ComputerUseAction(
            kind=ComputerUseActionKind.PRESS_KEY,
            key="menu",
            expected_sequence=sequence,
            allowed_from_states=[source_state],
            required_current_app="com.netflix.Netflix",
        )
        for _ in range(1)
    ]
    nonblank_states = [
        state.value
        for state in ProviderState
        if state != ProviderState.BLANK_OR_PROTECTED
    ]
    return VerifiedMacro(
        provider="netflix",
        name="exit_paused_playback",
        steps=[
            VerifiedMacroStep(
                name="paused_playback_to_visible_netflix",
                allowed_from_states=[source_state],
                actions=actions,
                accepted_post_states=nonblank_states,
                min_pre_confidence=0.0,
                min_post_confidence=0.0,
                required_current_app="com.netflix.Netflix",
                allow_blank_or_protected_pre=True,
                postcondition_retries=2,
                inter_action_delay_s=0.75,
            )
        ],
    )


def netflix_set_query_macro(*, sequence: int, title: str) -> VerifiedMacro:
    """Replace the Netflix query and verify real keyboard focus/readback."""
    allowed = [
        ProviderState.SEARCH_KEYBOARD.value,
        ProviderState.SEARCH_RESULTS.value,
    ]
    action = ComputerUseAction(
        kind=ComputerUseActionKind.SET_TEXT,
        text=title,
        expected_sequence=sequence,
        allowed_from_states=allowed,
        required_anchors=["netflix.chrome.keyboard"],
        min_confidence=0.70,
        require_keyboard_focus=True,
    )
    return VerifiedMacro(
        provider="netflix",
        name="set_query",
        steps=[
            VerifiedMacroStep(
                name="set_and_verify_query",
                allowed_from_states=allowed,
                actions=[action],
                accepted_post_states=allowed,
                pre_required_anchors=["netflix.chrome.keyboard"],
                post_required_anchors=["netflix.chrome.keyboard"],
                expected_keyboard_text=title,
                require_keyboard_focus=True,
            )
        ],
    )


async def _await_value[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


async def _await_none(value: None | Awaitable[None]) -> None:
    if inspect.isawaitable(value):
        await value


def _parse_capture_time(raw: str | None) -> tuple[datetime | None, str | None]:
    if raw is None:
        return None, "missing"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None, "invalid"
    if parsed.tzinfo is None:
        return None, "invalid"
    return parsed.astimezone(UTC), None


def _capture_freshness_reason(state: ComputerUseState) -> str | None:
    if state.capture_error is not None:
        return "capture_error"
    if state.capture_time_error is not None:
        return f"capture_time_{state.capture_time_error}"
    if state.captured_at is None:
        return "capture_time_missing"
    if state.captured_at.tzinfo is None:
        return "capture_time_invalid"
    if state.captured_at.astimezone(UTC) > state.observed_at.astimezone(UTC):
        return "capture_time_future"
    return None


def _post_action_freshness_reason(
    state: ComputerUseState,
    *,
    barrier: datetime,
) -> str | None:
    reason = _capture_freshness_reason(state)
    if reason is not None:
        return reason
    assert state.captured_at is not None
    if state.captured_at.astimezone(UTC) < barrier.astimezone(UTC):
        return "capture_precedes_action_completion"
    return None


def _state_age_s(state: ComputerUseState) -> float:
    if _capture_freshness_reason(state) is not None or state.captured_at is None:
        return float("inf")
    return max(0.0, (utcnow() - state.captured_at).total_seconds())


def state_age_seconds(state: ComputerUseState) -> float:
    """Return the age of an observation for read-only verification gates."""
    return _state_age_s(state)


def _frame_changed(before: ComputerUseState, after: ComputerUseState) -> bool | None:
    if before.frame_sha256 is None or after.frame_sha256 is None:
        return None
    return before.frame_sha256 != after.frame_sha256
