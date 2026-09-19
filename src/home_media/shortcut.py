"""Restricted JSON-over-stdin facade for the first Siri Shortcut.

This module is intentionally not a general CLI.  It accepts one bounded,
versioned JSON object and can dispatch only the semantic ``prepare_content``
operation.  In particular, it has no shell, raw remote, text-entry, or batch
surface.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import stat
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Literal, Protocol, TextIO

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from home_media.config import (
    DEFAULT_CONFIG_DIR,
    ensure_private_dir,
    validate_private_file,
    write_private_text,
)
from home_media.content.prepare import PrepareContentResult, TerminalStatus
from home_media.errors import ConfigError, ErrorCode, HomeMediaError
from home_media.service import ApplicationService

SHORTCUT_SCHEMA_VERSION: Literal[1] = 1
MAX_REQUEST_BYTES = 8 * 1024
MAX_LEDGER_BYTES = 1024 * 1024
MAX_LEDGER_ENTRIES = 1024
DEFAULT_LEDGER_PATH = DEFAULT_CONFIG_DIR / "shortcut_idempotency.json"

BoundedRoom = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
BoundedTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]
BoundedProvider = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
IdempotencyKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class ShortcutRequest(BaseModel):
    """The complete v1 input surface accepted from Shortcuts over SSH."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    action: Literal["prepare_content"]
    room: BoundedRoom
    title: BoundedTitle
    provider: BoundedProvider | None = None
    goal: Literal["search_ready"]
    wake: bool = True
    idempotency_key: IdempotencyKey


class ShortcutResult(BaseModel):
    """Sanitized backend result safe to return to a Shortcut."""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["prepare_content"] = "prepare_content"
    idempotency_key: str
    room_key: str
    title: str
    provider: str | None
    goal: Literal["search_ready"] = "search_ready"
    terminal_status: str
    verification_status: str
    selected_result: bool
    playback_started: bool


class ShortcutError(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str
    message: str
    retryable: bool = False


class ShortcutResponse(BaseModel):
    """One versioned response object written to standard output."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = SHORTCUT_SCHEMA_VERSION
    ok: bool
    status: Literal["verified", "degraded", "unverified", "failed"]
    spoken_response: str
    data: ShortcutResult | None = None
    error: ShortcutError | None = None


class ShortcutIdempotencyStore:
    """Small private ledger that survives one-process-per-SSH invocation.

    A request is durably marked pending before the service is constructed. If
    the process dies at any later point, reuse of that key fails closed as an
    ambiguous outcome. Completed results are cached; clearly pre-send failures
    remove the pending record and may be retried with the same key.
    """

    def __init__(self, path: Path | None = None) -> None:
        configured = os.environ.get("HOME_MEDIA_SHORTCUT_STATE")
        self.path = (
            Path(configured).expanduser()
            if path is None and configured
            else path or DEFAULT_LEDGER_PATH
        )
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")

    @staticmethod
    def fingerprint(request: ShortcutRequest) -> str:
        canonical = json.dumps(
            request.model_dump(mode="json"),
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        ensure_private_dir(self.path.parent)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise ConfigError("Shortcut idempotency lock is unavailable") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ConfigError("Shortcut idempotency lock must be a regular file")
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                raise ConfigError("Shortcut idempotency lock has the wrong owner")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise ConfigError("Shortcut idempotency lock permissions are unsafe")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield self._load()
        finally:
            with suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self) -> dict[str, Any]:
        try:
            self.path.lstat()
        except FileNotFoundError:
            return {"schema_version": 1, "entries": {}}
        validate_private_file(self.path, label="Shortcut idempotency ledger")
        if self.path.stat().st_size > MAX_LEDGER_BYTES:
            raise ConfigError("Shortcut idempotency ledger is too large")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError("Shortcut idempotency ledger is invalid") from exc
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != 1
            or not isinstance(document.get("entries"), dict)
        ):
            raise ConfigError("Shortcut idempotency ledger has an invalid schema")
        for key, entry in document["entries"].items():
            if (
                not isinstance(key, str)
                or not isinstance(entry, dict)
                or entry.get("state") not in {"pending", "ambiguous", "succeeded"}
                or not isinstance(entry.get("fingerprint"), str)
                or not isinstance(entry.get("updated_ns"), int)
            ):
                raise ConfigError("Shortcut idempotency ledger has an invalid entry")
        return document

    def _save(self, document: dict[str, Any]) -> None:
        payload = json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n"
        if len(payload.encode("utf-8")) > MAX_LEDGER_BYTES:
            raise ConfigError("Shortcut idempotency ledger is too large")
        write_private_text(self.path, payload)

    def begin(
        self, request: ShortcutRequest
    ) -> tuple[Literal["execute", "cached", "ambiguous", "conflict"], ShortcutResponse | None]:
        fingerprint = self.fingerprint(request)
        with self._locked() as document:
            entries: dict[str, Any] = document["entries"]
            existing = entries.get(request.idempotency_key)
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    return "conflict", None
                if existing["state"] == "succeeded":
                    try:
                        cached = ShortcutResponse.model_validate(existing.get("response"))
                    except ValidationError as exc:
                        raise ConfigError(
                            "Shortcut idempotency ledger cached result is invalid"
                        ) from exc
                    return "cached", cached
                return "ambiguous", None

            if len(entries) >= MAX_LEDGER_ENTRIES:
                completed = sorted(
                    (
                        (entry["updated_ns"], key)
                        for key, entry in entries.items()
                        if entry["state"] == "succeeded"
                    )
                )
                while len(entries) >= MAX_LEDGER_ENTRIES and completed:
                    _, oldest = completed.pop(0)
                    entries.pop(oldest, None)
            if len(entries) >= MAX_LEDGER_ENTRIES:
                raise ConfigError("Shortcut idempotency ledger is full")

            entries[request.idempotency_key] = {
                "fingerprint": fingerprint,
                "state": "pending",
                "updated_ns": time.time_ns(),
                "response": None,
            }
            self._save(document)
        return "execute", None

    def complete(self, request: ShortcutRequest, response: ShortcutResponse) -> None:
        fingerprint = self.fingerprint(request)
        with self._locked() as document:
            entries: dict[str, Any] = document["entries"]
            existing = entries.get(request.idempotency_key)
            if existing is None or existing.get("fingerprint") != fingerprint:
                raise ConfigError("Shortcut idempotency state changed during execution")

            error_code = response.error.code if response.error is not None else None
            if response.ok or response.data is not None:
                existing.update(
                    {
                        "state": "succeeded",
                        "updated_ns": time.time_ns(),
                        "response": response.model_dump(mode="json"),
                    }
                )
            elif error_code in {
                ErrorCode.PARTIAL.value,
                "backend_contract_violation",
                "internal_error",
            }:
                existing.update(
                    {
                        "state": "ambiguous",
                        "updated_ns": time.time_ns(),
                        "response": None,
                    }
                )
            else:
                # The service rejected the request before a mutation boundary.
                entries.pop(request.idempotency_key, None)
            self._save(document)


class ShortcutService(Protocol):
    async def prepare_content(
        self,
        room_name: str,
        title: str,
        *,
        provider: str | None = None,
        goal: str = "search_ready",
        wake: bool = True,
        idempotency_key: str | None = None,
    ) -> PrepareContentResult: ...

    async def aclose(self) -> None: ...


ServiceFactory = Callable[[], ShortcutService]


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError("duplicate object key")
        result[key] = value
    return result


def _reject_nonstandard_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _parse_request(payload: bytes) -> ShortcutRequest:
    if not payload:
        raise ValueError("empty request")
    if len(payload) > MAX_REQUEST_BYTES:
        raise OverflowError("request too large")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("request must be UTF-8 JSON") from exc
    raw = json.loads(
        text,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonstandard_constant,
    )
    if not isinstance(raw, dict):
        raise TypeError("request must be a JSON object")
    return ShortcutRequest.model_validate(raw, strict=True)


def _default_service_factory() -> ShortcutService:
    config_path = os.environ.get("HOME_MEDIA_CONFIG")
    return ApplicationService.from_config_path(
        Path(config_path) if config_path else None,
        use_fakes=False,
    )


def _humanize_room(room_key: str) -> str:
    return room_key.replace("_", " ").replace("-", " ").title()


def _provider_name(provider: str | None) -> str:
    if not provider:
        return "Content"
    return provider.replace("_", " ").title()


def _invalid_request_response(code: str = "invalid_request") -> ShortcutResponse:
    return ShortcutResponse(
        ok=False,
        status="failed",
        spoken_response="Home Media could not understand that request.",
        error=ShortcutError(
            code=code,
            message="Request did not match the Home Media Shortcut v1 schema.",
        ),
    )


def _backend_error_response(exc: HomeMediaError) -> ShortcutResponse:
    if exc.code in {ErrorCode.AMBIGUOUS_ROOM, ErrorCode.UNKNOWN_ROOM}:
        spoken = "Home Media could not resolve exactly one room."
    elif exc.code == ErrorCode.MUTATIONS_DISABLED:
        spoken = "Home Media control is disabled on the hub."
    elif exc.code in {ErrorCode.AUTH_REQUIRED, ErrorCode.AUTH_FAILED}:
        spoken = "Home Media needs Apple TV authentication before it can continue."
    elif exc.code in {ErrorCode.TIMEOUT, ErrorCode.NETWORK, ErrorCode.PARTIAL}:
        spoken = (
            "Home Media could not verify the search-ready outcome. "
            "It will not retry automatically."
        )
    else:
        spoken = "Home Media could not complete that request safely."
    return ShortcutResponse(
        ok=False,
        status="failed",
        spoken_response=spoken,
        error=ShortcutError(
            code=exc.code.value,
            message="The Home Media backend rejected or could not verify the request.",
            # A failed mutation is never automatically retried by this boundary.
            retryable=False,
        ),
    )


def _internal_error_response() -> ShortcutResponse:
    return ShortcutResponse(
        ok=False,
        status="failed",
        spoken_response="The Home Media hub encountered an internal error.",
        error=ShortcutError(
            code="internal_error",
            message="The Home Media backend did not return a usable result.",
        ),
    )


def _ledger_error_response(code: str, spoken_response: str) -> ShortcutResponse:
    return ShortcutResponse(
        ok=False,
        status="failed",
        spoken_response=spoken_response,
        error=ShortcutError(
            code=code,
            message="The request was not dispatched because idempotency could not be proven.",
            retryable=False,
        ),
    )


def _result_response(
    request: ShortcutRequest,
    result: PrepareContentResult,
) -> ShortcutResponse:
    if (
        result.goal.value != request.goal
        or result.title != request.title
        or result.idempotency_key != request.idempotency_key
        or result.selected_result
        or result.playback_started
    ):
        return ShortcutResponse(
            ok=False,
            status="failed",
            spoken_response=(
                "Home Media stopped because the backend returned an unsafe or mismatched "
                "Search Ready outcome."
            ),
            error=ShortcutError(
                code="backend_contract_violation",
                message="Backend result violated the Shortcut v1 contract.",
            ),
        )

    data = ShortcutResult(
        idempotency_key=request.idempotency_key,
        room_key=result.room_key,
        title=result.title,
        provider=result.provider,
        terminal_status=result.terminal_status.value,
        verification_status=result.verification_status,
        selected_result=False,
        playback_started=False,
    )
    provider = _provider_name(result.provider)
    room = _humanize_room(result.room_key)
    if (
        result.terminal_status == TerminalStatus.QUERY_VERIFIED
        and result.verification_status == "verified"
    ):
        return ShortcutResponse(
            ok=True,
            status="verified",
            spoken_response=(
                f"{provider} search is ready for {result.title} in {room}. Select a result."
            ),
            data=data,
        )

    status: Literal["degraded", "unverified", "failed"]
    if result.verification_status == "degraded":
        status = "degraded"
    elif result.verification_status == "unverified":
        status = "unverified"
    else:
        status = "failed"
    return ShortcutResponse(
        ok=False,
        status=status,
        spoken_response=(
            f"{provider} search could not be verified for {result.title} in {room}. "
            "No result was selected."
        ),
        data=data,
        error=ShortcutError(
            code="search_ready_not_verified",
            message="Backend did not verify the requested Search Ready terminal state.",
        ),
    )


async def handle_request(
    request: ShortcutRequest,
    *,
    service_factory: ServiceFactory = _default_service_factory,
) -> ShortcutResponse:
    """Dispatch one already-validated request and always close its service."""
    service: ShortcutService | None = None
    try:
        service = service_factory()
        result = await service.prepare_content(
            request.room,
            request.title,
            provider=request.provider,
            goal=request.goal,
            wake=request.wake,
            idempotency_key=request.idempotency_key,
        )
        return _result_response(request, result)
    except HomeMediaError as exc:
        return _backend_error_response(exc)
    except Exception:  # noqa: BLE001 -- restricted boundary must return one JSON response
        return _internal_error_response()
    finally:
        if service is not None:
            # A close failure must not invite a retry after a completed mutation.
            with suppress(Exception):
                await service.aclose()


def run(
    *,
    stdin: BinaryIO | None = None,
    stdout: TextIO | None = None,
    service_factory: ServiceFactory = _default_service_factory,
    state_path: Path | None = None,
) -> int:
    """Read one bounded request, emit one compact response, and return an exit code."""
    input_stream = stdin if stdin is not None else sys.stdin.buffer
    output_stream = stdout if stdout is not None else sys.stdout
    payload = input_stream.read(MAX_REQUEST_BYTES + 1)
    try:
        request = _parse_request(payload)
    except OverflowError:
        response = _invalid_request_response("request_too_large")
        exit_code = 2
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
        response = _invalid_request_response()
        exit_code = 2
    else:
        store = ShortcutIdempotencyStore(state_path)
        try:
            decision, cached = store.begin(request)
        except HomeMediaError:
            response = _ledger_error_response(
                "idempotency_state_unavailable",
                "Home Media could not verify request safety, so nothing was sent.",
            )
        else:
            if decision == "cached":
                assert cached is not None
                response = cached
            elif decision == "conflict":
                response = _ledger_error_response(
                    ErrorCode.IDEMPOTENCY_CONFLICT.value,
                    "Home Media rejected a reused request identifier.",
                )
            elif decision == "ambiguous":
                response = _ledger_error_response(
                    ErrorCode.PARTIAL.value,
                    "Home Media will not repeat a request whose prior outcome is uncertain.",
                )
            else:
                response = asyncio.run(
                    handle_request(request, service_factory=service_factory)
                )
                try:
                    store.complete(request, response)
                except HomeMediaError:
                    response = _ledger_error_response(
                        "idempotency_state_unavailable",
                        "Home Media could not safely record the outcome. It will not retry.",
                    )
        exit_code = 0 if response.ok else 1
    output_stream.write(response.model_dump_json())
    output_stream.write("\n")
    output_stream.flush()
    return exit_code


def main() -> None:
    """Console-script entry point; arguments are deliberately unsupported."""
    if len(sys.argv) != 1:
        response = _invalid_request_response("arguments_not_allowed")
        sys.stdout.write(response.model_dump_json() + "\n")
        raise SystemExit(2)
    raise SystemExit(run())


if __name__ == "__main__":
    main()
