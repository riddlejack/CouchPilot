"""Bounded async client for an already-running tvOS WebDriverAgent helper.

The client does not discover helpers, launch an application while creating a
session, or retry mutations.  Callers are responsible for pinning
``device_identity()`` to their configured Apple TV before using this client.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from home_media.errors import ErrorCode, HomeMediaError

_ELEMENT_ID = "element-6066-11e4-a52e-4f735466cecf"
_JSON_OBJECT = dict[str, Any]
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ROLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
MAX_VISIBLE_ELEMENTS = 64
_ACTIONABLE_ROLES = {
    "Button",
    "Cell",
    "Icon",
    "Key",
    "Link",
    "SearchField",
    "SecureTextField",
    "Slider",
    "Switch",
    "TextField",
    "TextView",
}
_REMOTE_KEYS = frozenset({"up", "down", "left", "right", "menu", "home", "select", "playpause"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WDAIdentity(_StrictModel):
    """Non-device helper/session identity safe for ordinary diagnostics."""

    helper_build_id: str
    session_id: str | None = None
    generation: int = 0


class WDAStatus(_StrictModel):
    ready: bool
    os_name: str
    os_version: str | None = None
    wda_version: str | None = None
    identity: WDAIdentity
    request_started_at: datetime
    received_at: datetime


class WDAElement(_StrictModel):
    """One compact actionable accessibility element; never the full WDA tree."""

    label: str | None = None
    role: str
    value: str | None = None
    identifier: str | None = None
    focused: bool | None = None


class WDAImage(_StrictModel):
    """A direct WDA screenshot with bytes excluded from serialization."""

    request_started_at: datetime
    received_at: datetime
    byte_count: int
    sha256: str
    png_bytes: bytes = Field(exclude=True, repr=False)


class WDAObservation(_StrictModel):
    active_app: str | None = None
    focused_label: str | None = None
    visible: list[WDAElement] = Field(default_factory=list)
    expected_app: str | None = None
    expected_label: str | None = None
    expected_app_verified: bool = False
    expected_label_verified: bool = False
    identity: WDAIdentity | None = None
    request_started_at: datetime | None = None
    received_at: datetime | None = None
    image: WDAImage | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class WDAMutationResult(_StrictModel):
    """WDA acknowledged a mutation; this does not verify its destination."""

    operation: Literal["press", "activate", "select", "set_text"]
    acknowledged: bool = True
    identity: WDAIdentity
    request_started_at: datetime
    received_at: datetime


class WDAError(HomeMediaError):
    """Safe WDA failure that excludes endpoint, device UUID, and response bodies."""

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode = ErrorCode.NETWORK,
        retryable: bool = False,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, code=code, retryable=retryable, details=details)


class WDAMutationUncertain(WDAError):
    """A mutation was handed to HTTP but no conclusive response was received."""

    def __init__(self, operation: str) -> None:
        super().__init__(
            f"WDA {operation} outcome is uncertain; observe before deciding whether to retry",
            code=ErrorCode.PARTIAL,
            retryable=False,
            details={"operation": operation, "outcome": "unknown"},
        )


@dataclass(frozen=True, slots=True)
class _TimedResponse:
    data: _JSON_OBJECT
    started_at: datetime
    received_at: datetime


class WDAClient:
    """Async WDA protocol client with bounded reads and conservative mutations."""

    def __init__(
        self,
        endpoint: str,
        client: httpx.AsyncClient | None = None,
        request_timeout_s: float = 5.0,
    ) -> None:
        self._endpoint = _validate_endpoint(endpoint)
        if request_timeout_s <= 0:
            raise ValueError("request_timeout_s must be positive")
        self._request_timeout_s = request_timeout_s
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None
        self._session_lock = asyncio.Lock()
        self._session_id: str | None = None
        self._configured_session_id: str | None = None
        self._helper_build_id: str | None = None
        self._reported_session_id: str | None = None
        self._generation = 0
        self._closed = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._session_id = None
        self._configured_session_id = None
        if self._owns_client:
            await self._client.aclose()

    async def status(self) -> WDAStatus:
        response = await self._request("GET", "/status", operation="status")
        return self._status_from_response(response)

    async def device_identity(self) -> str:
        """Return identifierForVendor only for an explicit binding check."""

        response = await self._request(
            "GET",
            "/wda/device/info",
            operation="device_identity",
        )
        value = _object_value(response.data, operation="device_identity")
        identifier = value.get("uuid")
        if not isinstance(identifier, str) or not identifier or identifier == "unknown":
            raise WDAError(
                "WDA device identity is unavailable",
                code=ErrorCode.DEVICE_REJECTED,
            )
        return identifier

    async def screenshot(self) -> WDAImage:
        """Capture an image without creating or consulting a semantic session."""

        response = await self._request("GET", "/screenshot", operation="screenshot")
        encoded = response.data.get("value")
        if not isinstance(encoded, str):
            raise WDAError("WDA screenshot response was malformed", code=ErrorCode.DEVICE_REJECTED)
        try:
            png = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise WDAError(
                "WDA screenshot response was not valid base64",
                code=ErrorCode.DEVICE_REJECTED,
            ) from exc
        if not png.startswith(_PNG_SIGNATURE):
            raise WDAError("WDA screenshot was not a PNG", code=ErrorCode.DEVICE_REJECTED)
        return WDAImage(
            request_started_at=response.started_at,
            received_at=response.received_at,
            byte_count=len(png),
            sha256=hashlib.sha256(png).hexdigest(),
            png_bytes=png,
        )

    async def observe(
        self,
        include_image: bool = False,
        expected_app: str | None = None,
        expected_label: str | None = None,
        timeout_s: float = 8.0,
    ) -> WDAObservation:
        """Read a stable-app compact tree, optionally alongside an independent image."""

        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        deadline = time.monotonic() + timeout_s
        image_task = asyncio.create_task(self.screenshot()) if include_image else None
        image: WDAImage | None = None
        warnings = ["sequential_samples_not_atomic"]
        errors: list[str] = []
        active_app: str | None = None
        focused_label: str | None = None
        visible: list[WDAElement] = []
        source_started: datetime | None = None
        source_received: datetime | None = None
        identity: WDAIdentity | None = None
        expected_app_verified = False
        expected_label_verified = False

        try:
            identity = await self._ensure_session(deadline)
            while _remaining(deadline) > 0:
                before = await self._active_app(deadline)
                active_app = before
                if expected_app is not None and before != expected_app:
                    await _bounded_pause(deadline)
                    continue

                expected_app_verified = expected_app is not None and before == expected_app
                await self._set_default_active_application(before, deadline)
                source = await self._request(
                    "GET",
                    f"/session/{identity.session_id}/source?format=json",
                    operation="source",
                    deadline=deadline,
                )
                source_started = source.started_at
                source_received = source.received_at
                tree = source.data.get("value")
                after = await self._active_app(deadline)
                active_app = after
                if before != after or (expected_app is not None and after != expected_app):
                    expected_app_verified = False
                    visible = []
                    await _bounded_pause(deadline)
                    continue
                truncated = False
                visibility_conflict = False
                if isinstance(tree, Mapping) and tree:
                    visible, truncated, visibility_conflict = _compact_visible_elements(tree)
                if truncated:
                    warnings.append("visible_elements_truncated")
                if visibility_conflict:
                    warnings.append("visibility_conflicts_with_focus")
                if not visible:
                    errors.append("source_empty")
                    try:
                        focused_label = await self._focused_label(identity, deadline)
                    except (WDAError, TimeoutError):
                        warnings.append("focus_query_failed")
                    break
                label_matched = expected_label is None or any(
                    element.label == expected_label for element in visible
                )
                expected_label_verified = expected_label is not None and label_matched
                if not label_matched:
                    await _bounded_pause(deadline)
                    continue
                try:
                    focused_label = await self._focused_label(identity, deadline)
                except WDAError:
                    focused_label = next(
                        (element.label for element in visible if element.focused),
                        None,
                    )
                    warnings.append("focus_query_failed")
                break
            else:
                errors.append("observation_timeout")
        except (WDAError, TimeoutError):
            errors.append("semantic_observation_failed")

        if expected_app is not None and not expected_app_verified:
            errors.append("expected_app_not_observed")
        if expected_label is not None and not expected_label_verified:
            errors.append("expected_label_not_observed")
        if source_started is None and not errors:
            errors.append("semantic_observation_failed")

        if image_task is not None:
            try:
                if image_task.done():
                    image = image_task.result()
                else:
                    async with asyncio.timeout(_remaining(deadline)):
                        image = await image_task
                warnings.append("image_is_independent_sample")
            except (WDAError, TimeoutError):
                image_task.cancel()
                await asyncio.gather(image_task, return_exceptions=True)
                warnings.append("image_unavailable")

        return WDAObservation(
            active_app=active_app,
            focused_label=focused_label,
            visible=visible,
            expected_app=expected_app,
            expected_label=expected_label,
            expected_app_verified=expected_app_verified,
            expected_label_verified=expected_label_verified,
            identity=identity,
            request_started_at=source_started,
            received_at=source_received,
            image=image,
            warnings=_deduplicate(warnings),
            errors=_deduplicate(errors),
        )

    async def press(
        self,
        key: str,
        *,
        expected_identity: WDAIdentity | None = None,
    ) -> WDAMutationResult:
        normalized = key.casefold()
        if normalized not in _REMOTE_KEYS:
            raise ValueError(f"Unsupported tvOS remote key: {key}")
        identity = await self._ensure_session()
        _require_identity(expected_identity, identity)
        response = await self._mutation_request(
            f"/session/{identity.session_id}/wda/pressButton",
            {"name": normalized},
            operation="press",
        )
        return _mutation_result("press", identity, response)

    async def activate(self, bundle_id: str) -> WDAMutationResult:
        if not bundle_id.strip():
            raise ValueError("bundle_id must not be empty")
        identity = await self._ensure_session()
        response = await self._mutation_request(
            f"/session/{identity.session_id}/wda/apps/activate",
            {"bundleId": bundle_id},
            operation="activate",
        )
        return _mutation_result("activate", identity, response)

    async def select(
        self,
        label: str,
        role: str = "Cell",
        *,
        expected_identity: WDAIdentity | None = None,
    ) -> WDAMutationResult:
        if not label:
            raise ValueError("label must not be empty")
        normalized_role = role.removeprefix("XCUIElementType")
        if not _ROLE_RE.fullmatch(normalized_role):
            raise ValueError("role must be an XCUIElement role name")
        identity = await self._ensure_session()
        _require_identity(expected_identity, identity)
        predicate = (
            f'type == "XCUIElementType{normalized_role}" AND label == '
            f"{json.dumps(label, ensure_ascii=False)}"
        )
        found = await self._request(
            "POST",
            f"/session/{identity.session_id}/elements",
            operation="select_query",
            json_body={"using": "predicate string", "value": predicate},
        )
        value = found.data.get("value")
        if not isinstance(value, list) or len(value) != 1:
            count = len(value) if isinstance(value, list) else 0
            raise WDAError(
                f"WDA select requires exactly one target; found {count}",
                code=ErrorCode.VERIFICATION_FAILED,
                details={"reason": "target_not_unique", "match_count": count},
            )
        element_id = _element_id(value[0])
        if element_id is None:
            raise WDAError(
                "WDA select target did not contain an element identifier",
                code=ErrorCode.DEVICE_REJECTED,
            )
        focused = await self._request(
            "GET",
            f"/session/{identity.session_id}/element/{element_id}/attribute/focused",
            operation="select_focus",
        )
        focus_state = _wda_bool(focused.data.get("value"))
        if focus_state is None:
            raise WDAError(
                "WDA select target focus state was unavailable",
                code=ErrorCode.DEVICE_REJECTED,
            )
        if not focus_state:
            raise WDAError(
                "WDA select target is not focused; observe and move focus one step",
                code=ErrorCode.VERIFICATION_FAILED,
                details={"reason": "target_not_focused"},
            )
        response = await self._mutation_request(
            f"/session/{identity.session_id}/wda/pressButton",
            {"name": "select"},
            operation="select",
        )
        return _mutation_result("select", identity, response)

    async def set_text(
        self,
        text: str,
        *,
        expected_identity: WDAIdentity | None = None,
    ) -> WDAMutationResult:
        """Type through WDA's tvOS global keyboard route.

        WDA's current tvOS source implements ``/wda/keys`` with ``FBTypeText``
        and documents SearchField and TextView as tested element types.
        """

        identity = await self._ensure_session()
        _require_identity(expected_identity, identity)
        response = await self._mutation_request(
            f"/session/{identity.session_id}/wda/keys",
            {"value": list(text)},
            operation="set_text",
        )
        return _mutation_result("set_text", identity, response)

    async def _active_app(self, deadline: float) -> str:
        response = await self._request(
            "GET",
            "/wda/activeAppInfo",
            operation="active_app",
            deadline=deadline,
        )
        value = _object_value(response.data, operation="active_app")
        bundle_id = value.get("bundleId")
        if not isinstance(bundle_id, str) or not bundle_id:
            raise WDAError("WDA active application was unavailable", code=ErrorCode.DEVICE_REJECTED)
        return bundle_id

    async def _focused_label(self, identity: WDAIdentity, deadline: float) -> str | None:
        active = await self._request(
            "GET",
            f"/session/{identity.session_id}/element/active",
            operation="focused_element",
            deadline=deadline,
        )
        element_id = _element_id(active.data.get("value"))
        if element_id is None:
            raise WDAError("WDA focused element was unavailable", code=ErrorCode.DEVICE_REJECTED)
        label = await self._request(
            "GET",
            f"/session/{identity.session_id}/element/{element_id}/attribute/label",
            operation="focused_label",
            deadline=deadline,
        )
        value = label.data.get("value")
        return value if isinstance(value, str) and value else None

    async def _set_default_active_application(
        self,
        bundle_id: str,
        deadline: float,
    ) -> None:
        session_id = self._session_id
        if session_id is None:
            raise WDAError("WDA session is unavailable", code=ErrorCode.STALE_ENDPOINT)
        await self._request(
            "POST",
            f"/session/{session_id}/appium/settings",
            operation="set_active_application",
            deadline=deadline,
            json_body={"settings": {"defaultActiveApplication": bundle_id}},
        )

    async def _ensure_session(self, deadline: float | None = None) -> WDAIdentity:
        async with self._session_lock:
            status_response = await self._request(
                "GET",
                "/status",
                operation="status",
                deadline=deadline,
            )
            status = self._status_from_response(status_response)
            reported = status.identity.session_id
            if reported is not None:
                self._session_id = reported
            if self._session_id is None:
                created = await self._request(
                    "POST",
                    "/session",
                    operation="create_session",
                    deadline=deadline,
                    json_body={
                        "capabilities": {
                            "alwaysMatch": {
                                "shouldTerminateApp": False,
                            }
                        }
                    },
                )
                self._session_id = _session_id_from(created.data)
                if self._session_id is None:
                    raise WDAError(
                        "WDA did not return a session identifier",
                        code=ErrorCode.DEVICE_REJECTED,
                    )
                self._record_server_identity(self._helper_build_id or "unknown", self._session_id)
            if self._configured_session_id != self._session_id:
                await self._request(
                    "POST",
                    f"/session/{self._session_id}/appium/settings",
                    operation="configure_session",
                    deadline=deadline,
                    json_body={
                        "settings": {
                            "waitForIdleTimeout": 0,
                            "animationCoolOffTimeout": 0,
                        }
                    },
                )
                self._configured_session_id = self._session_id
            return WDAIdentity(
                helper_build_id=self._helper_build_id or "unknown",
                session_id=self._session_id,
                generation=self._generation,
            )

    def _status_from_response(self, response: _TimedResponse) -> WDAStatus:
        value = _object_value(response.data, operation="status")
        os_info = value.get("os")
        build = value.get("build")
        os_data = os_info if isinstance(os_info, Mapping) else {}
        build_data = build if isinstance(build, Mapping) else {}
        os_name = os_data.get("name")
        if os_name != "tvOS":
            raise WDAError(
                "WDA helper is not running on tvOS",
                code=ErrorCode.DEVICE_REJECTED,
            )
        ready = value.get("ready") is True
        if not ready:
            raise WDAError("WDA helper is not ready", code=ErrorCode.DEVICE_REJECTED)
        helper_build_id = _helper_build_id(build_data, os_data)
        reported_session = _session_id_from(response.data)
        self._record_server_identity(helper_build_id, reported_session)
        os_version = os_data.get("version")
        wda_version = build_data.get("version")
        return WDAStatus(
            ready=True,
            os_name="tvOS",
            os_version=os_version if isinstance(os_version, str) else None,
            wda_version=wda_version if isinstance(wda_version, str) else None,
            identity=WDAIdentity(
                helper_build_id=helper_build_id,
                session_id=reported_session,
                generation=self._generation,
            ),
            request_started_at=response.started_at,
            received_at=response.received_at,
        )

    def _record_server_identity(self, helper_build_id: str, session_id: str | None) -> None:
        changed = (
            self._helper_build_id is not None
            and self._helper_build_id != helper_build_id
        ) or (
            self._reported_session_id is not None
            and self._reported_session_id != session_id
        )
        disappeared = self._reported_session_id is not None and session_id is None
        if changed or disappeared:
            self._session_id = None
            self._configured_session_id = None
            self._generation += 1
        self._helper_build_id = helper_build_id
        self._reported_session_id = session_id

    async def _mutation_request(
        self,
        path: str,
        body: _JSON_OBJECT,
        *,
        operation: str,
    ) -> _TimedResponse:
        try:
            return await self._request(
                "POST",
                path,
                operation=operation,
                json_body=body,
                mutation=True,
            )
        except (httpx.RequestError, TimeoutError) as exc:
            raise WDAMutationUncertain(operation) from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        deadline: float | None = None,
        json_body: _JSON_OBJECT | None = None,
        mutation: bool = False,
    ) -> _TimedResponse:
        if self._closed:
            raise WDAError("WDA client is closed", code=ErrorCode.NETWORK)
        timeout_s = self._request_timeout_s
        if deadline is not None:
            timeout_s = min(timeout_s, _remaining(deadline))
        if timeout_s <= 0:
            if mutation:
                raise WDAMutationUncertain(operation)
            raise TimeoutError(f"WDA {operation} deadline expired")
        started_at = _utcnow()
        try:
            async with asyncio.timeout(timeout_s):
                response = await self._client.request(
                    method,
                    self._endpoint + path,
                    json=json_body,
                    timeout=httpx.Timeout(timeout_s),
                    follow_redirects=False,
                )
        except TimeoutError:
            if mutation:
                raise WDAMutationUncertain(operation) from None
            raise
        except httpx.RequestError as exc:
            if mutation:
                raise WDAMutationUncertain(operation) from exc
            raise WDAError(
                f"WDA transport failed during {operation}",
                code=ErrorCode.NETWORK,
                retryable=True,
            ) from exc
        received_at = _utcnow()
        if response.is_redirect:
            raise WDAError(
                f"WDA redirect refused during {operation}",
                code=ErrorCode.STALE_ENDPOINT,
            )
        if not 200 <= response.status_code < 300:
            raise WDAError(
                f"WDA returned HTTP {response.status_code} during {operation}",
                code=ErrorCode.DEVICE_REJECTED,
            )
        try:
            parsed = response.json()
        except ValueError as exc:
            raise WDAError(
                f"WDA returned malformed JSON during {operation}",
                code=ErrorCode.DEVICE_REJECTED,
            ) from exc
        if not isinstance(parsed, dict):
            raise WDAError(
                f"WDA returned a non-object during {operation}",
                code=ErrorCode.DEVICE_REJECTED,
            )
        data = cast(_JSON_OBJECT, parsed)
        value = data.get("value")
        if isinstance(value, Mapping) and value.get("error"):
            raise WDAError(
                f"WDA rejected {operation}",
                code=ErrorCode.DEVICE_REJECTED,
            )
        return _TimedResponse(data=data, started_at=started_at, received_at=received_at)


def compact_visible_elements(tree: Mapping[str, object]) -> list[WDAElement]:
    """Flatten a WDA JSON source into compact, ordered, de-duplicated semantics."""

    elements, _, _ = _compact_visible_elements(tree)
    return elements


def _compact_visible_elements(
    tree: Mapping[str, object],
) -> tuple[list[WDAElement], bool, bool]:
    """Return capped semantics plus truncation and focus/visibility conflict flags."""

    result: list[WDAElement] = []
    seen: set[tuple[str, str | None, str | None, str | None, bool | None]] = set()
    visibility_conflict = False

    def visit(node: Mapping[str, object]) -> None:
        nonlocal visibility_conflict
        role = _text(node.get("type")) or "Unknown"
        if role.startswith("XCUIElementType"):
            role = role.removeprefix("XCUIElementType")
        label = _text(node.get("label"))
        value = _text(node.get("value"))
        identifier = _text(node.get("rawIdentifier")) or _text(node.get("name"))
        focused = _wda_bool(node.get("isFocused"))
        reported_visibility = _wda_bool(node.get("isVisible"))
        visible = reported_visibility is True
        accessible = _wda_bool(node.get("isAccessible")) is True
        meaningful = label is not None or value is not None or identifier is not None
        positively_focused = focused is True
        if meaningful and positively_focused and reported_visibility is False:
            visibility_conflict = True
        if meaningful and (
            (visible and (accessible or role in _ACTIONABLE_ROLES)) or positively_focused
        ):
            key = (role, label, value, identifier, focused)
            if key not in seen:
                seen.add(key)
                result.append(
                    WDAElement(
                        label=label,
                        role=role,
                        value=value,
                        identifier=identifier,
                        focused=focused,
                    )
                )
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, Mapping):
                    visit(cast(Mapping[str, object], child))

    visit(tree)
    if len(result) <= MAX_VISIBLE_ELEMENTS:
        return result, False, visibility_conflict

    selected_indices = list(range(MAX_VISIBLE_ELEMENTS))
    required_indices: list[int] = []
    page_title = next(
        (index for index, element in enumerate(result) if element.role == "StaticText"),
        None,
    )
    if page_title is not None:
        required_indices.append(page_title)
    required_indices.extend(index for index, element in enumerate(result) if element.focused)
    for required in required_indices:
        if required in selected_indices:
            continue
        replace_at = next(
            (
                index
                for index in range(len(selected_indices) - 1, -1, -1)
                if selected_indices[index] not in required_indices
            ),
            None,
        )
        if replace_at is not None:
            selected_indices[replace_at] = required
    selected_indices.sort()
    return [result[index] for index in selected_indices], True, visibility_conflict


def _validate_endpoint(endpoint: str) -> str:
    try:
        parsed = urlsplit(endpoint)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("WDA endpoint is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("WDA endpoint must be an HTTP(S) host with no path")
    return endpoint.rstrip("/")


def _helper_build_id(build: Mapping[str, object], os_info: Mapping[str, object]) -> str:
    parts = (
        _text(build.get("productBundleIdentifier")) or "unknown",
        _text(build.get("time")) or "unknown",
        _text(build.get("version")) or "unknown",
        _text(os_info.get("name")) or "unknown",
        _text(os_info.get("version")) or "unknown",
    )
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


def _session_id_from(data: Mapping[str, object]) -> str | None:
    session_id = data.get("sessionId")
    if isinstance(session_id, str) and session_id:
        return session_id
    value = data.get("value")
    if isinstance(value, Mapping):
        nested = value.get("sessionId")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _object_value(data: Mapping[str, object], *, operation: str) -> Mapping[str, object]:
    value = data.get("value")
    if not isinstance(value, Mapping):
        raise WDAError(
            f"WDA returned malformed data during {operation}",
            code=ErrorCode.DEVICE_REJECTED,
        )
    return cast(Mapping[str, object], value)


def _element_id(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    identifier = value.get(_ELEMENT_ID, value.get("ELEMENT"))
    return identifier if isinstance(identifier, str) and identifier else None


def _mutation_result(
    operation: Literal["press", "activate", "select", "set_text"],
    identity: WDAIdentity,
    response: _TimedResponse,
) -> WDAMutationResult:
    return WDAMutationResult(
        operation=operation,
        identity=identity,
        request_started_at=response.started_at,
        received_at=response.received_at,
    )


def _require_identity(expected: WDAIdentity | None, current: WDAIdentity) -> None:
    if expected is None:
        return
    if expected != current:
        raise WDAError(
            "WDA helper or session changed since the observation; observe again",
            code=ErrorCode.STALE_ENDPOINT,
            retryable=False,
            details={"reason": "observation_identity_changed"},
        )


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def _wda_bool(value: object) -> bool | None:
    if value in (True, 1, "1", "true", "True"):
        return True
    if value in (False, 0, "0", "false", "False"):
        return False
    return None


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


async def _bounded_pause(deadline: float) -> None:
    remaining = _remaining(deadline)
    if remaining > 0:
        await asyncio.sleep(min(0.05, remaining))


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
