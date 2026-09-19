"""Typed error taxonomy for adapters and the application core."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    AMBIGUOUS_ROOM = "ambiguous_room"
    UNKNOWN_ROOM = "unknown_room"
    UNSUPPORTED = "unsupported"
    AUTH_REQUIRED = "auth_required"
    AUTH_FAILED = "auth_failed"
    TIMEOUT = "timeout"
    NETWORK = "network"
    DEVICE_REJECTED = "device_rejected"
    VERIFICATION_FAILED = "verification_failed"
    SAFETY_BLOCKED = "safety_blocked"
    STALE_ENDPOINT = "stale_endpoint"
    PARTIAL = "partial"
    CONFIG = "config"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    MUTATIONS_DISABLED = "mutations_disabled"


class HomeMediaError(Exception):
    """Base application error with a stable machine-readable code."""

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode,
        retryable: bool = False,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, object]:
        return {
            "error": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class AmbiguousRoomError(HomeMediaError):
    def __init__(self, message: str, *, matches: list[str]) -> None:
        super().__init__(
            message,
            code=ErrorCode.AMBIGUOUS_ROOM,
            retryable=False,
            details={"matches": matches},
        )


class UnknownRoomError(HomeMediaError):
    def __init__(self, room: str) -> None:
        super().__init__(
            f"Unknown room: {room}",
            code=ErrorCode.UNKNOWN_ROOM,
            details={"room": room},
        )


class UnsupportedError(HomeMediaError):
    def __init__(self, capability: str, *, reason: str | None = None) -> None:
        msg = f"Unsupported capability: {capability}"
        if reason:
            msg = f"{msg} ({reason})"
        super().__init__(
            msg,
            code=ErrorCode.UNSUPPORTED,
            details={"capability": capability, "reason": reason},
        )


class AuthRequiredError(HomeMediaError):
    def __init__(self, device_id: str, *, protocol: str | None = None) -> None:
        super().__init__(
            f"Authentication required for device {device_id}",
            code=ErrorCode.AUTH_REQUIRED,
            details={"device_id": device_id, "protocol": protocol},
        )


class AuthFailedError(HomeMediaError):
    def __init__(self, message: str = "Authentication failed") -> None:
        super().__init__(message, code=ErrorCode.AUTH_FAILED)


class TimeoutError_(HomeMediaError):  # noqa: N818 — avoid shadowing builtin name in imports
    def __init__(self, message: str = "Operation timed out") -> None:
        super().__init__(message, code=ErrorCode.TIMEOUT, retryable=True)


class NetworkError(HomeMediaError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message, code=ErrorCode.NETWORK, retryable=retryable)


class DeviceRejectedError(HomeMediaError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.DEVICE_REJECTED)


class VerificationFailedError(HomeMediaError):
    def __init__(self, message: str, *, observed: dict[str, object] | None = None) -> None:
        super().__init__(
            message,
            code=ErrorCode.VERIFICATION_FAILED,
            details={"observed": observed or {}},
        )


class SafetyBlockedError(HomeMediaError):
    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(
            message,
            code=ErrorCode.SAFETY_BLOCKED,
            details={"reason": reason},
        )


class StaleEndpointError(HomeMediaError):
    def __init__(self, device_id: str, address: str) -> None:
        super().__init__(
            f"Stale endpoint for {device_id} at {address}",
            code=ErrorCode.STALE_ENDPOINT,
            retryable=True,
            details={"device_id": device_id, "address": address},
        )


class MutationsDisabledError(HomeMediaError):
    def __init__(self, *, intent: str | None = None) -> None:
        message = "Mutations are disabled by kill switch"
        if intent:
            message = f"{message}; refusing {intent}"
        super().__init__(
            message,
            code=ErrorCode.MUTATIONS_DISABLED,
            details={"intent": intent} if intent else None,
        )


class ConfigError(HomeMediaError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.CONFIG)


class IdempotencyConflictError(HomeMediaError):
    def __init__(self, message: str = "Idempotency key reused with a different request") -> None:
        super().__init__(message, code=ErrorCode.IDEMPOTENCY_CONFLICT, retryable=False)


class AmbiguousOutcomeError(HomeMediaError):
    """Mutation may have been sent; do not automatically retry the send."""

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(
            message,
            code=ErrorCode.PARTIAL,
            retryable=False,
            details=details or {"outcome": "unknown_outcome"},
        )
