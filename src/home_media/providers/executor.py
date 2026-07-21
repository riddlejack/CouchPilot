"""FAKE-friendly provider recipe runner for search_ready transitions."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any

from home_media.content.prepare import (
    PrepareStage,
    StageStatus,
    TerminalStatus,
    normalize_query,
    queries_match,
)
from home_media.errors import SafetyBlockedError
from home_media.observers.classify import (
    NAV_CONFIDENCE_MIN,
    PROFILE_SELECT_CONFIDENCE_MIN,
    profile_picker_chrome_present,
)
from home_media.observers.netflix_anchors import ANCHOR_HIGHLIGHTED_PROFILE
from home_media.providers.base import ProviderObservation, ProviderState, TransitionSpec
from home_media.providers.netflix import assert_transition_allowed, is_select_action

ObservationSource = Callable[[], Awaitable[ProviderObservation] | ProviderObservation]
ActionExecutor = Callable[[dict[str, Any]], Awaitable[None] | None]

# Post-launch stop/handoff outcomes for search_ready (never press Select).
_LAUNCH_STOP_STATES = {
    ProviderState.PLAYING,
    ProviderState.TITLE_DETAIL,
    ProviderState.BLANK_OR_PROTECTED,
    ProviderState.ERROR_OR_MODAL,
    ProviderState.UNKNOWN,
}


class ProviderRecipeRunner:
    """Run a ``search_ready`` transition list against injectable observation/actions.

    Safety:
    - FORBIDS select when state is UNKNOWN
    - FORBIDS profile select unless state is PROFILE_PICKER with visual gates
    - Enforces accepted post-states and bounded timeouts
    - Never claims playing/resumed for search_ready
    """

    def __init__(
        self,
        *,
        observe: ObservationSource,
        execute: ActionExecutor,
        provider: str = "netflix",
    ) -> None:
        self.observe = observe
        self.execute = execute
        self.provider = provider

    async def _resolve_obs(self) -> ProviderObservation:
        result = self.observe()
        if inspect.isawaitable(result):
            return await result
        return result

    async def _run_action(self, action: dict[str, Any]) -> None:
        result = self.execute(action)
        if inspect.isawaitable(result):
            await result

    async def run_search_ready(
        self,
        *,
        room_key: str,
        title: str,
        transitions: list[TransitionSpec],
        classify: Callable[[ProviderObservation], ProviderState] | None = None,
        required_profile_name: str | None = None,
    ) -> dict[str, Any]:
        """Execute transitions; return a PrepareContentResult-like dict."""
        stages: list[PrepareStage] = []
        observed_states: list[str] = []
        warnings: list[str] = []
        selected_result = False
        playback_started = False
        terminal = TerminalStatus.FAILED
        verification_status = "unverified"
        keyboard_seen = False
        query_ok = False
        stopped_early = False

        def _classify(obs: ProviderObservation) -> ProviderState:
            if classify is not None:
                return classify(obs)
            if obs.user_confirmed_state is not None:
                return obs.user_confirmed_state
            return ProviderState.UNKNOWN

        try:
            for spec in transitions:
                if stopped_early:
                    break
                if spec.may_start_playback:
                    raise SafetyBlockedError(
                        (
                            f"Transition '{spec.name}' may_start_playback "
                            "is forbidden for search_ready"
                        ),
                        reason="search_ready_playback_forbidden",
                    )

                t0 = time.perf_counter()
                deadline = asyncio.get_running_loop().time() + spec.timeout_s
                try:
                    obs = await asyncio.wait_for(
                        self._resolve_obs(),
                        timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
                    )
                except TimeoutError:
                    stages.append(
                        PrepareStage(
                            name=spec.name,
                            status=StageStatus.FAILED,
                            latency_ms=int((time.perf_counter() - t0) * 1000),
                            evidence=[
                                f"timeout_s={spec.timeout_s}",
                                "transition_timeout",
                                "phase=pre_observation",
                            ],
                        )
                    )
                    warnings.append(
                        f"Transition '{spec.name}' timed out during pre-observation"
                    )
                    terminal = TerminalStatus.FAILED
                    verification_status = "failed"
                    stopped_early = True
                    break
                state = _classify(obs)
                observed_states.append(state.value)

                # A warm Netflix launch can restore the exact populated query.
                # Verify it through the real keyboard channel and stop without
                # clearing, retyping, or sending navigation.
                if (
                    state in {ProviderState.SEARCH_KEYBOARD, ProviderState.SEARCH_RESULTS}
                    and obs.keyboard_focus
                    and obs.keyboard_text is not None
                    and queries_match(title, obs.keyboard_text)
                ):
                    query_ok = True
                    keyboard_seen = True
                    stages.append(
                        PrepareStage(
                            name="existing_query_verified",
                            status=StageStatus.SUCCEEDED,
                            latency_ms=int((time.perf_counter() - t0) * 1000),
                            evidence=[
                                f"state={state.value}",
                                "real_keyboard_focus",
                                "exact_query_readback",
                                "no_action_required",
                            ],
                        )
                    )
                    break
                if state in {ProviderState.SEARCH_KEYBOARD, ProviderState.SEARCH_RESULTS}:
                    # Verification is about the latest observable query, not
                    # any earlier match in the same recipe.
                    query_ok = False

                has_select = any(is_select_action(a) for a in spec.actions)
                profile_action = spec.name.startswith("select_profile") or any(
                    str(a.get("type", "")).lower() == "select_profile" for a in spec.actions
                )

                # Hard safety: never Select from UNKNOWN (including profile select).
                if has_select and state == ProviderState.UNKNOWN:
                    raise SafetyBlockedError(
                        "Select forbidden while state is UNKNOWN",
                        reason="select_from_unknown",
                    )

                # Conditional steps (e.g. profile picker) are skipped when not applicable.
                if state not in spec.allowed_from:
                    if profile_action and state != ProviderState.PROFILE_PICKER:
                        skip_reason = "profile_select_requires_profile_picker"
                    else:
                        skip_reason = "state_not_in_allowed_from"
                    stages.append(
                        PrepareStage(
                            name=spec.name,
                            status=StageStatus.SKIPPED,
                            latency_ms=int((time.perf_counter() - t0) * 1000),
                            evidence=[f"skipped_from={state.value}", skip_reason],
                        )
                    )
                    continue

                assert_transition_allowed(spec, state)
                self._assert_visual_action_gates(
                    spec,
                    obs,
                    state,
                    profile_action=profile_action,
                    required_profile_name=required_profile_name,
                )

                evidence: list[str] = [
                    f"from={state.value}",
                    f"expected_to={spec.expected_to.value}",
                    f"confidence={obs.confidence:.2f}",
                ]
                if obs.screenshot_anchors:
                    evidence.append(f"anchors={','.join(obs.screenshot_anchors)}")

                actions = list(spec.actions)
                evidence_local = evidence

                async def _execute_all(
                    actions: list[dict[str, Any]] = actions,
                    evidence_local: list[str] = evidence_local,
                ) -> None:
                    for action in actions:
                        await self._run_action(action)
                        evidence_local.append(f"action:{action.get('type')}")

                try:
                    await asyncio.wait_for(
                        _execute_all(),
                        timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
                    )
                except TimeoutError as exc:
                    stages.append(
                        PrepareStage(
                            name=spec.name,
                            status=StageStatus.FAILED,
                            latency_ms=int((time.perf_counter() - t0) * 1000),
                            evidence=[
                                *evidence,
                                f"timeout_s={spec.timeout_s}",
                                "transition_timeout",
                                "phase=actions",
                            ],
                        )
                    )
                    warnings.append(
                        f"Transition '{spec.name}' timed out after {spec.timeout_s:.1f}s"
                    )
                    terminal = TerminalStatus.FAILED
                    verification_status = "failed"
                    stopped_early = True
                    _ = exc
                    break

                if spec.requires_observation:
                    try:
                        after = await asyncio.wait_for(
                            self._resolve_obs(),
                            timeout=max(
                                0.001,
                                deadline - asyncio.get_running_loop().time(),
                            ),
                        )
                    except TimeoutError:
                        stages.append(
                            PrepareStage(
                                name=spec.name,
                                status=StageStatus.FAILED,
                                latency_ms=int((time.perf_counter() - t0) * 1000),
                                evidence=[
                                    *evidence,
                                    f"timeout_s={spec.timeout_s}",
                                    "transition_timeout",
                                    "phase=post_observation",
                                ],
                            )
                        )
                        warnings.append(
                            f"Transition '{spec.name}' timed out during post-observation"
                        )
                        terminal = TerminalStatus.FAILED
                        verification_status = "failed"
                        stopped_early = True
                        break
                    after_state = _classify(after)
                    observed_states.append(after_state.value)
                    evidence.append(f"after={after_state.value}")
                    accepted = spec.resolved_accepted_post_states()
                    if after_state not in accepted:
                        # Launch resume / unexpected screen → handoff, never Select next.
                        handoff = (
                            spec.name == "launch_app" and after_state in _LAUNCH_STOP_STATES
                        )
                        stages.append(
                            PrepareStage(
                                name=spec.name,
                                status=StageStatus.FAILED,
                                latency_ms=int((time.perf_counter() - t0) * 1000),
                                evidence=[
                                    *evidence,
                                    "expected_state_mismatch",
                                    f"accepted={','.join(s.value for s in accepted)}",
                                    "launch_handoff" if handoff else "postcondition_failed",
                                ],
                            )
                        )
                        warnings.append(
                            f"Transition '{spec.name}' post-state {after_state.value} "
                            f"not in accepted post-states"
                        )
                        terminal = (
                            TerminalStatus.HANDOFF if handoff else TerminalStatus.FAILED
                        )
                        verification_status = "failed"
                        stopped_early = True
                        break

                    if after.keyboard_focus:
                        keyboard_seen = True
                    query_ok = False
                    # Query verification requires real focus + matching readback.
                    if (
                        after.keyboard_focus
                        and after.keyboard_text is not None
                        and queries_match(title, after.keyboard_text)
                    ):
                        query_ok = True
                    elif after.keyboard_text is not None and not after.keyboard_focus:
                        evidence.append("readback_ignored_without_keyboard_focus")
                    elif after.keyboard_text is not None:
                        evidence.append(
                            f"readback_mismatch expected={normalize_query(title)!r} "
                            f"observed={normalize_query(after.keyboard_text)!r}"
                        )

                latency_ms = int((time.perf_counter() - t0) * 1000)
                # Bound wall-clock including observation.
                if latency_ms > int(spec.timeout_s * 1000) + 250:
                    stages.append(
                        PrepareStage(
                            name=spec.name,
                            status=StageStatus.FAILED,
                            latency_ms=latency_ms,
                            evidence=[*evidence, "transition_wall_timeout"],
                        )
                    )
                    warnings.append(
                        f"Transition '{spec.name}' exceeded timeout_s={spec.timeout_s}"
                    )
                    terminal = TerminalStatus.FAILED
                    verification_status = "failed"
                    stopped_early = True
                    break

                stages.append(
                    PrepareStage(
                        name=spec.name,
                        status=StageStatus.SUCCEEDED,
                        latency_ms=latency_ms,
                        evidence=evidence,
                    )
                )

            if stopped_early:
                pass
            elif query_ok:
                terminal = TerminalStatus.QUERY_VERIFIED
                verification_status = "verified"
            elif keyboard_seen:
                terminal = TerminalStatus.SEARCH_OPEN_UNVERIFIED
                verification_status = "unverified"
            elif any(s == ProviderState.SEARCH_KEYBOARD.value for s in observed_states):
                terminal = TerminalStatus.KEYBOARD_NOT_FOCUSED
            else:
                terminal = TerminalStatus.SEARCH_OPEN_UNVERIFIED

        except SafetyBlockedError as exc:
            stages.append(
                PrepareStage(
                    name="safety_blocked",
                    status=StageStatus.FAILED,
                    evidence=[exc.message, str(exc.details.get("reason"))],
                )
            )
            warnings.append(exc.message)
            terminal = TerminalStatus.FAILED
            verification_status = "failed"

        return {
            "room_key": room_key,
            "title": title,
            "normalized_title": normalize_query(title),
            "provider": self.provider,
            "goal": "search_ready",
            "route_used": "provider_state_machine",
            "stages": [s.model_dump(mode="json") for s in stages],
            "observed_states": observed_states,
            "selected_result": selected_result,
            "playback_started": playback_started,
            "verification_status": verification_status,
            "terminal_status": terminal.value,
            "warnings": warnings,
            "physical_tv_state_known": False,
            "idempotency_notes": "search_ready never selects results or starts playback",
        }

    def _assert_visual_action_gates(
        self,
        spec: TransitionSpec,
        obs: ProviderObservation,
        state: ProviderState,
        *,
        profile_action: bool,
        required_profile_name: str | None,
    ) -> None:
        has_select = any(is_select_action(a) for a in spec.actions)
        # Non-Select provider navigation requires confidence >= 0.70.
        # Launch from Apple Home / UNKNOWN may be pre-classified weakly; open_app
        # itself is gated by binding + preflight capture in ApplicationService.
        if (
            not has_select
            and spec.name != "launch_app"
            and obs.confidence < NAV_CONFIDENCE_MIN
        ):
            raise SafetyBlockedError(
                (
                    f"Navigation '{spec.name}' blocked: confidence "
                    f"{obs.confidence:.2f} < {NAV_CONFIDENCE_MIN:.2f}"
                ),
                reason="low_confidence_navigation",
            )

        if not profile_action:
            return

        if state != ProviderState.PROFILE_PICKER:
            raise SafetyBlockedError(
                "Profile select requires profile_picker",
                reason="profile_select_outside_picker",
            )
        if obs.confidence < PROFILE_SELECT_CONFIDENCE_MIN:
            raise SafetyBlockedError(
                (
                    "Profile select blocked: confidence "
                    f"{obs.confidence:.2f} < {PROFILE_SELECT_CONFIDENCE_MIN:.2f}"
                ),
                reason="profile_select_low_confidence",
            )
        if not profile_picker_chrome_present(list(obs.screenshot_anchors)):
            raise SafetyBlockedError(
                "Profile select blocked: missing profile-picker chrome anchor",
                reason="profile_select_missing_chrome",
            )
        if ANCHOR_HIGHLIGHTED_PROFILE not in obs.screenshot_anchors:
            raise SafetyBlockedError(
                "Profile select blocked: no visually inferred highlighted-profile anchor",
                reason="profile_select_missing_highlight",
            )
        wanted = (required_profile_name or "").strip().lower()
        action_name = None
        for action in spec.actions:
            if str(action.get("type", "")).lower() == "select_profile":
                raw = action.get("profile_name")
                if raw:
                    action_name = str(raw).strip().lower()
                break
        if action_name:
            wanted = action_name
        observed_name = (obs.highlighted_profile_name or "").strip().lower()
        if not wanted:
            raise SafetyBlockedError(
                "Profile select blocked: provider_prefs.netflix.profile_name is required",
                reason="profile_name_unconfigured",
            )
        if not observed_name or observed_name != wanted:
            raise SafetyBlockedError(
                "Profile select blocked: highlighted profile does not match configured name",
                reason="profile_name_mismatch",
            )
