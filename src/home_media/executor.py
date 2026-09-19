"""Plan executor with per-room locks, safe retries, and verification."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from home_media.audit import audit
from home_media.errors import (
    AmbiguousOutcomeError,
    HomeMediaError,
    IdempotencyConflictError,
    MutationsDisabledError,
    NetworkError,
    StaleEndpointError,
    TimeoutError_,
    UnsupportedError,
)
from home_media.models import (
    ActionPlan,
    ActionResult,
    ExecutionStatus,
    StepResult,
    VerificationStatus,
    utcnow,
)

AdapterMap = dict[str, Any]
MutatingHandler = Callable[[Any, str, dict[str, Any]], Awaitable[dict[str, Any]]]


def plan_fingerprint(plan: ActionPlan) -> str:
    payload = {
        "intent": plan.intent,
        "room_key": plan.room_key,
        "steps": [
            {
                "action": s.action,
                "target_device_id": s.target_device_id,
                "adapter": s.adapter,
                "params": s.params,
            }
            for s in plan.steps
        ],
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class _IdempotencyEntry:
    fingerprint: str
    result: ActionResult | None = None
    in_flight: asyncio.Future[ActionResult] | None = None
    touched_at: float = field(default_factory=time.monotonic)


class Executor:
    def __init__(
        self,
        adapters: AdapterMap,
        *,
        mutations_enabled: bool = True,
        max_retries: int = 2,
        retry_backoff_s: float = 0.25,
        idempotency_ttl_s: float = 15 * 60,
        max_idempotency_entries: int = 512,
    ) -> None:
        self.adapters = adapters
        self.mutations_enabled = mutations_enabled
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        self.idempotency_ttl_s = idempotency_ttl_s
        self.max_idempotency_entries = max_idempotency_entries
        self._room_locks: dict[str, asyncio.Lock] = {}
        self._idempotency: dict[str, _IdempotencyEntry] = {}
        self._idempotency_lock = asyncio.Lock()

    def _lock_for(self, room_key: str) -> asyncio.Lock:
        if room_key not in self._room_locks:
            self._room_locks[room_key] = asyncio.Lock()
        return self._room_locks[room_key]

    def room_lock(self, room_key: str) -> asyncio.Lock:
        """Supported per-room serialization seam for composite operations."""
        return self._lock_for(room_key)

    def _prune_idempotency(self) -> None:
        now = time.monotonic()
        expired = [
            key
            for key, entry in self._idempotency.items()
            if entry.in_flight is None and now - entry.touched_at > self.idempotency_ttl_s
        ]
        for key in expired:
            self._idempotency.pop(key, None)
        completed = sorted(
            (
                (entry.touched_at, key)
                for key, entry in self._idempotency.items()
                if entry.in_flight is None
            )
        )
        overflow = max(0, len(self._idempotency) - self.max_idempotency_entries)
        for _, key in completed[:overflow]:
            self._idempotency.pop(key, None)

    async def execute(
        self,
        plan: ActionPlan,
        *,
        dry_run: bool = False,
        idempotency_key: str | None = None,
        rediscover: Callable[[str], Awaitable[None]] | None = None,
    ) -> ActionResult:
        fingerprint = plan_fingerprint(plan)
        async with self._idempotency_lock:
            self._prune_idempotency()
            if idempotency_key:
                existing = self._idempotency.get(idempotency_key)
                if existing is not None:
                    existing.touched_at = time.monotonic()
                    if existing.fingerprint != fingerprint:
                        raise IdempotencyConflictError(
                            "Idempotency key reused with a different request fingerprint"
                        )
                    if existing.result is not None:
                        audit(
                            "idempotency_hit",
                            room_key=plan.room_key,
                            intent=plan.intent,
                            extra={"idempotency_key": idempotency_key},
                        )
                        return existing.result
                    waiter = (
                        existing.in_flight if existing.in_flight is not None else None
                    )
                else:
                    waiter = None
                    loop = asyncio.get_running_loop()
                    fut: asyncio.Future[ActionResult] = loop.create_future()
                    self._idempotency[idempotency_key] = _IdempotencyEntry(
                        fingerprint=fingerprint,
                        in_flight=fut,
                    )
            else:
                waiter = None

        if waiter is not None:
            return await waiter

        try:
            result = await self._execute_unlocked(
                plan,
                dry_run=dry_run,
                idempotency_key=idempotency_key,
                rediscover=rediscover,
            )
        except Exception as exc:
            if idempotency_key:
                async with self._idempotency_lock:
                    entry = self._idempotency.get(idempotency_key)
                    if entry and entry.in_flight and not entry.in_flight.done():
                        entry.in_flight.set_exception(exc)
                        entry.in_flight.exception()
                        self._idempotency.pop(idempotency_key, None)
            raise

        if idempotency_key:
            async with self._idempotency_lock:
                entry = self._idempotency.get(idempotency_key)
                if entry is None:
                    self._idempotency[idempotency_key] = _IdempotencyEntry(
                        fingerprint=fingerprint,
                        result=result,
                    )
                else:
                    entry.result = result
                    entry.touched_at = time.monotonic()
                    if entry.in_flight is not None and not entry.in_flight.done():
                        entry.in_flight.set_result(result)
                    entry.in_flight = None
                self._prune_idempotency()
        return result

    async def _execute_unlocked(
        self,
        plan: ActionPlan,
        *,
        dry_run: bool,
        idempotency_key: str | None,
        rediscover: Callable[[str], Awaitable[None]] | None,
    ) -> ActionResult:
        result = ActionResult(
            requested_intent=plan.intent,
            room_key=plan.room_key,
            targets=[s.target_device_id for s in plan.steps],
            plan=plan,
            warnings=list(plan.warnings),
            dry_run=dry_run,
            idempotency_key=idempotency_key,
        )

        if dry_run or all(step.dry_run for step in plan.steps):
            for step in plan.steps:
                result.steps.append(
                    StepResult(
                        step_id=step.step_id,
                        action=step.action,
                        target_device_id=step.target_device_id,
                        execution_status=ExecutionStatus.DRY_RUN,
                        verification_status=VerificationStatus.NOT_APPLICABLE,
                        observed_after={"planned_params": step.params},
                    )
                )
            result.finish(
                execution_status=ExecutionStatus.DRY_RUN,
                verification_status=VerificationStatus.NOT_APPLICABLE,
            )
            return result

        if not self.mutations_enabled:
            raise MutationsDisabledError()

        # Room lock is held by caller around registration; hold again for mutation body.
        async with self._lock_for(plan.room_key):
            for step in plan.steps:
                step_result = await self._run_step(step, rediscover=rediscover)
                result.steps.append(step_result)
                if step_result.execution_status in {
                    ExecutionStatus.FAILED,
                    ExecutionStatus.PARTIAL,
                }:
                    result.error = step_result.error
                    result.retryable = bool((step_result.error or {}).get("retryable"))
                    result.finish(
                        execution_status=ExecutionStatus.PARTIAL
                        if any(
                            s.execution_status == ExecutionStatus.SUCCEEDED for s in result.steps
                        )
                        else ExecutionStatus.FAILED,
                        verification_status=VerificationStatus.FAILED,
                    )
                    audit(
                        "action_partial_or_failed",
                        room_key=plan.room_key,
                        intent=plan.intent,
                        targets=result.targets,
                        result=result.model_dump(mode="json"),
                    )
                    return result

            verifications = [s.verification_status for s in result.steps]
            if all(
                v == VerificationStatus.VERIFIED
                for v in verifications
                if v != VerificationStatus.NOT_APPLICABLE
            ):
                vstatus = VerificationStatus.VERIFIED
            elif any(v == VerificationStatus.FAILED for v in verifications):
                vstatus = VerificationStatus.FAILED
            elif any(v == VerificationStatus.DEGRADED for v in verifications):
                vstatus = VerificationStatus.DEGRADED
            else:
                vstatus = VerificationStatus.UNVERIFIED

            result.finish(execution_status=ExecutionStatus.SUCCEEDED, verification_status=vstatus)
            audit(
                "action_succeeded",
                room_key=plan.room_key,
                intent=plan.intent,
                targets=result.targets,
                result={"action_id": result.action_id, "verification": vstatus.value},
            )
            return result

    async def _run_step(
        self,
        step: Any,
        *,
        rediscover: Callable[[str], Awaitable[None]] | None = None,
    ) -> StepResult:
        adapter = self.adapters.get(step.adapter)
        if adapter is None:
            return StepResult(
                step_id=step.step_id,
                action=step.action,
                target_device_id=step.target_device_id,
                execution_status=ExecutionStatus.FAILED,
                error={"error": "unsupported", "message": f"No adapter '{step.adapter}'"},
            )

        started = time.perf_counter()
        attempt = 0
        last_error: HomeMediaError | None = None
        mutation_dispatched = False

        while attempt <= self.max_retries:
            attempt += 1
            try:
                if not mutation_dispatched:
                    observed_before: dict[str, Any] = await self._safe_status(
                        adapter, step.target_device_id
                    )
                else:
                    observed_before = {}

                if mutation_dispatched:
                    # Post-send: observe only; never re-dispatch.
                    observed_after = await self._safe_status(adapter, step.target_device_id)
                    verification = self._verify(
                        step.action, step.params, observed_before, observed_after
                    )
                    return StepResult(
                        step_id=step.step_id,
                        action=step.action,
                        target_device_id=step.target_device_id,
                        execution_status=ExecutionStatus.SUCCEEDED,
                        verification_status=verification,
                        observed_before=observed_before,
                        observed_after=observed_after,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        error={
                            "error": "unknown_outcome",
                            "message": "Observed after ambiguous post-send failure",
                        }
                        if verification == VerificationStatus.UNVERIFIED
                        else None,
                    )

                # Once dispatch begins, the command may have been sent — never re-send.
                mutation_dispatched = True
                observed_after = await self._dispatch(
                    adapter, step.action, step.target_device_id, step.params
                )
                verification = self._verify(
                    step.action, step.params, observed_before, observed_after
                )
                return StepResult(
                    step_id=step.step_id,
                    action=step.action,
                    target_device_id=step.target_device_id,
                    execution_status=ExecutionStatus.SUCCEEDED,
                    verification_status=verification,
                    observed_before=observed_before,
                    observed_after=observed_after,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            except StaleEndpointError as exc:
                if mutation_dispatched:
                    last_error = AmbiguousOutcomeError(
                        f"Ambiguous outcome after send: {exc.message}",
                        details=exc.to_dict(),
                    )
                    break
                last_error = exc
                if rediscover is not None:
                    await rediscover(step.target_device_id)
                await asyncio.sleep(self.retry_backoff_s * attempt)
            except AmbiguousOutcomeError as exc:
                last_error = exc
                break
            except (NetworkError, TimeoutError_) as exc:
                if mutation_dispatched:
                    # Command may have been sent; observe once then stop.
                    last_error = AmbiguousOutcomeError(
                        f"Ambiguous outcome after send: {exc.message}",
                        details=exc.to_dict(),
                    )
                    try:
                        observed_after = await self._safe_status(adapter, step.target_device_id)
                        verification = self._verify(
                            step.action, step.params, {}, observed_after
                        )
                        return StepResult(
                            step_id=step.step_id,
                            action=step.action,
                            target_device_id=step.target_device_id,
                            execution_status=ExecutionStatus.PARTIAL,
                            verification_status=verification,
                            observed_after=observed_after,
                            error=last_error.to_dict(),
                            latency_ms=int((time.perf_counter() - started) * 1000),
                        )
                    except Exception:  # noqa: BLE001
                        break
                last_error = exc
                if not exc.retryable or attempt > self.max_retries:
                    break
                await asyncio.sleep(self.retry_backoff_s * attempt)
            except HomeMediaError as exc:
                return StepResult(
                    step_id=step.step_id,
                    action=step.action,
                    target_device_id=step.target_device_id,
                    execution_status=ExecutionStatus.FAILED,
                    verification_status=VerificationStatus.FAILED,
                    error=exc.to_dict(),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            except Exception as exc:  # noqa: BLE001
                if mutation_dispatched:
                    last_error = AmbiguousOutcomeError(
                        f"Ambiguous outcome after send: {exc}",
                    )
                    break
                return StepResult(
                    step_id=step.step_id,
                    action=step.action,
                    target_device_id=step.target_device_id,
                    execution_status=ExecutionStatus.FAILED,
                    error={"error": "device_rejected", "message": str(exc)},
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )

        assert last_error is not None
        return StepResult(
            step_id=step.step_id,
            action=step.action,
            target_device_id=step.target_device_id,
            execution_status=ExecutionStatus.FAILED,
            error=last_error.to_dict(),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def _safe_status(self, adapter: Any, device_id: str) -> dict[str, Any]:
        if not hasattr(adapter, "get_status"):
            return {}
        try:
            status = await adapter.get_status(device_id)
            if hasattr(status, "model_dump"):
                dumped: dict[str, Any] = status.model_dump(mode="json")
                return dumped
            return dict(status)
        except Exception:  # noqa: BLE001 — before-status is best-effort
            return {}

    async def _dispatch(
        self,
        adapter: Any,
        action: str,
        device_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if action == "set_power":
            from home_media.models import PowerState

            status = await adapter.set_power(device_id, PowerState(params["state"]))
            return status.model_dump(mode="json") if hasattr(status, "model_dump") else dict(status)
        if action == "open_app":
            status = await adapter.open_app(device_id, params["app_id"])
            return status.model_dump(mode="json") if hasattr(status, "model_dump") else dict(status)
        if action == "open_url":
            status = await adapter.open_url(device_id, params["url"])
            return status.model_dump(mode="json") if hasattr(status, "model_dump") else dict(status)
        if action == "control_transport":
            status = await adapter.control_transport(device_id, params["action"])
            return status.model_dump(mode="json") if hasattr(status, "model_dump") else dict(status)
        if action == "set_volume":
            result = await adapter.set_volume(device_id, int(params["level"]))
            return dict(result)
        if action == "change_volume":
            result = await adapter.change_volume(device_id, int(params["delta"]))
            return dict(result)
        if action == "set_input":
            status = await adapter.set_input(device_id, params["source"])
            return status.model_dump(mode="json") if hasattr(status, "model_dump") else dict(status)
        if action == "press_key":
            await adapter.press_key(device_id, params["key"])
            return {"key": params["key"]}
        if action == "enter_text":
            await adapter.enter_text(device_id, params["text"])
            return {"text_len": len(params["text"])}
        raise UnsupportedError(action, reason="Unknown executor action")

    def _verify(
        self,
        action: str,
        params: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> VerificationStatus:
        _ = before
        if action == "set_volume":
            level = after.get("level")
            if level is None:
                return VerificationStatus.UNVERIFIED
            if abs(int(level) - int(params["level"])) <= 1:
                return VerificationStatus.VERIFIED
            return VerificationStatus.FAILED
        if action == "set_power":
            power = after.get("power")
            if power == params.get("state"):
                return VerificationStatus.VERIFIED
            if power is None:
                return VerificationStatus.UNVERIFIED
            return VerificationStatus.DEGRADED
        if action == "open_app":
            requested = params.get("app_id")
            current = after.get("current_app")
            if requested and current and current == requested:
                return VerificationStatus.VERIFIED
            if current:
                return VerificationStatus.FAILED
            return VerificationStatus.UNVERIFIED
        if action == "open_url":
            expected_app = params.get("expected_app")
            current = after.get("current_app")
            np = after.get("now_playing") or {}
            title = params.get("expected_title")
            if expected_app and current and current != expected_app:
                return VerificationStatus.FAILED
            if title and isinstance(np, dict) and np.get("title"):
                observed = str(np["title"]).casefold()
                if title.casefold() in observed or observed in title.casefold():
                    return VerificationStatus.VERIFIED
                return VerificationStatus.FAILED
            if current and expected_app and current == expected_app:
                return VerificationStatus.DEGRADED
            return VerificationStatus.UNVERIFIED
        if action == "set_input":
            # Never treat echoed request as verified; require independent read.
            observed_raw = after.get("input_source")
            independently_read = bool(after.get("input_independently_read"))
            if (
                independently_read
                and isinstance(observed_raw, str)
                and observed_raw == params.get("source")
            ):
                return VerificationStatus.VERIFIED
            return VerificationStatus.UNVERIFIED
        return VerificationStatus.UNVERIFIED


# keep utcnow available for future timing fields
_ = utcnow
