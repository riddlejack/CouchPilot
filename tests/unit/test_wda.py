"""Protocol and safety contracts for the bounded async tvOS WDA client."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from home_media.wda import (
    WDAClient,
    WDAError,
    WDAIdentity,
    WDAMutationUncertain,
    compact_visible_elements,
)

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]
PNG = b"\x89PNG\r\n\x1a\nunit-test"


def _status(session_id: str | None = None) -> httpx.Response:
    body: dict[str, Any] = {
        "value": {
            "ready": True,
            "os": {"name": "tvOS", "version": "26.6"},
            "build": {
                "time": "Sep 19 2026 12:42:01",
                "version": "16.12.9",
                "productBundleIdentifier": "test.wda",
            },
        }
    }
    if session_id is not None:
        body["sessionId"] = session_id
    return httpx.Response(200, json=body)


def _ok(value: object = None, *, session_id: str | None = None) -> httpx.Response:
    body: dict[str, object] = {"value": value}
    if session_id is not None:
        body["sessionId"] = session_id
    return httpx.Response(200, json=body)


def _tree(label: str, *, focused: bool = True) -> dict[str, object]:
    return {
        "type": "Application",
        "label": "Settings",
        "isVisible": "1",
        "children": [
            {
                "type": "Cell",
                "label": label,
                "value": "On",
                "rawIdentifier": "row-id",
                "isVisible": "1",
                "isAccessible": "1",
                "isFocused": "1" if focused else "0",
                "children": [
                    {
                        "type": "StaticText",
                        "label": label,
                        "value": label,
                        "isVisible": "1",
                        "isAccessible": "0",
                    },
                    {
                        "type": "StaticText",
                        "label": label,
                        "value": label,
                        "isVisible": "1",
                        "isAccessible": "0",
                    },
                ],
            }
        ],
    }


async def _with_client(
    handler: Handler,
    operation: Callable[[WDAClient], Awaitable[Any]],
    *,
    request_timeout_s: float = 0.2,
) -> Any:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = WDAClient(
            "http://wda.test:8100",
            client=http,
            request_timeout_s=request_timeout_s,
        )
        try:
            return await operation(client)
        finally:
            await client.aclose()


@pytest.mark.parametrize(
    "endpoint",
    [
        "wda.test:8100",
        "ftp://wda.test:8100",
        "http://user:pass@wda.test:8100",
        "http://wda.test:8100/wd/hub",
        "http://wda.test:8100?device=one",
        "http://wda.test:8100#fragment",
    ],
)
def test_endpoint_rejects_discovery_like_or_credentialed_urls(endpoint: str) -> None:
    with pytest.raises(ValueError):
        WDAClient(endpoint)


def test_endpoint_allows_http_host_with_or_without_explicit_port() -> None:
    with_port = WDAClient("http://wda.test:8100")
    default_port = WDAClient("https://wda.test/")
    assert with_port is not None
    assert default_port is not None


@pytest.mark.asyncio
async def test_status_and_explicit_device_identity_keep_uuid_out_of_public_status() -> None:
    raw_uuid = "PRIVATE-DEVICE-UUID"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return _status("session-1")
        if request.url.path == "/wda/device/info":
            return _ok({"uuid": raw_uuid, "name": "Office"})
        raise AssertionError(request.url)

    async def operation(client: WDAClient) -> tuple[dict[str, Any], str]:
        status = await client.status()
        return status.model_dump(mode="json"), await client.device_identity()

    public, identity = await _with_client(handler, operation)
    assert identity == raw_uuid
    assert raw_uuid not in json.dumps(public)
    assert public["identity"]["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_observe_waits_for_expected_app_before_requesting_tree() -> None:
    paths: list[str] = []
    active_apps = iter(["com.apple.TVHomeScreen", "com.apple.TVSettings", "com.apple.TVSettings"])

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paths.append(path)
        if path == "/status":
            return _status()
        if path == "/session":
            body = json.loads(request.content)
            assert body["capabilities"]["alwaysMatch"] == {"shouldTerminateApp": False}
            return _ok({"sessionId": "session-1"}, session_id="session-1")
        if path == "/session/session-1/appium/settings":
            return _ok({})
        if path == "/wda/activeAppInfo":
            return _ok({"bundleId": next(active_apps)})
        if path == "/session/session-1/source":
            return _ok(_tree("Video and Audio"))
        if path == "/session/session-1/element/active":
            return _ok({"element-6066-11e4-a52e-4f735466cecf": "focused-1"})
        if path.endswith("/attribute/label"):
            return _ok("Video and Audio")
        raise AssertionError(request.url)

    async def operation(client: WDAClient):  # noqa: ANN202
        return await client.observe(
            expected_app="com.apple.TVSettings",
            expected_label="Video and Audio",
            timeout_s=0.5,
        )

    observation = await _with_client(handler, operation)
    first_source = paths.index("/session/session-1/source")
    assert paths[:first_source].count("/wda/activeAppInfo") == 2
    assert observation.expected_app_verified is True
    assert observation.expected_label_verified is True
    assert observation.focused_label == "Video and Audio"
    assert observation.request_started_at is not None
    assert observation.errors == []


@pytest.mark.asyncio
async def test_observe_discards_tree_when_active_app_changes_during_read() -> None:
    active_apps = iter(["app.old", "app.new", "app.new", "app.new"])
    source_labels = iter(["Old screen", "New screen"])

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/status":
            return _status("session-1")
        if path == "/session/session-1/appium/settings":
            return _ok({})
        if path == "/wda/activeAppInfo":
            return _ok({"bundleId": next(active_apps)})
        if path == "/session/session-1/source":
            return _ok(_tree(next(source_labels)))
        if path == "/session/session-1/element/active":
            return _ok({"element-6066-11e4-a52e-4f735466cecf": "focused-1"})
        if path.endswith("/attribute/label"):
            return _ok("New screen")
        raise AssertionError(request.url)

    observation = await _with_client(
        handler,
        lambda client: client.observe(expected_label="New screen", timeout_s=0.5),
    )
    labels = [element.label for element in observation.visible]
    assert "New screen" in labels
    assert "Old screen" not in labels
    assert observation.active_app == "app.new"
    assert observation.expected_label_verified is True


@pytest.mark.asyncio
async def test_empty_tree_is_reported_as_missing_semantics() -> None:
    active_apps = iter(["app.one", "app.one"])

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/status":
            return _status("session-1")
        if path == "/session/session-1/appium/settings":
            return _ok({})
        if path == "/wda/activeAppInfo":
            return _ok({"bundleId": next(active_apps)})
        if path == "/session/session-1/source":
            return _ok({})
        if path == "/session/session-1/element/active":
            return _ok({"element-6066-11e4-a52e-4f735466cecf": "focused-1"})
        if path.endswith("/attribute/label"):
            return _ok("Destination")
        raise AssertionError(request.url)

    observation = await _with_client(
        handler,
        lambda client: client.observe(expected_label="Destination", timeout_s=0.2),
    )
    assert observation.visible == []
    assert observation.focused_label == "Destination"
    assert observation.expected_label_verified is False
    assert "source_empty" in observation.errors
    assert "expected_label_not_observed" in observation.errors


@pytest.mark.asyncio
async def test_focus_failure_uses_tree_focus_without_claiming_query_success() -> None:
    active_apps = iter(["app.one", "app.one"])

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/status":
            return _status("session-1")
        if path == "/session/session-1/appium/settings":
            return _ok({})
        if path == "/wda/activeAppInfo":
            return _ok({"bundleId": next(active_apps)})
        if path == "/session/session-1/source":
            return _ok(_tree("Focused from tree"))
        if path == "/session/session-1/element/active":
            return httpx.Response(500, json={"value": {"error": "unknown error"}})
        raise AssertionError(request.url)

    observation = await _with_client(handler, lambda client: client.observe(timeout_s=0.2))
    assert observation.focused_label == "Focused from tree"
    assert observation.errors == []
    assert "focus_query_failed" in observation.warnings


def test_compact_tree_deduplicates_and_excludes_invisible_nodes() -> None:
    tree = _tree("Bluetooth")
    children = tree["children"]
    assert isinstance(children, list)
    children.append(
        {
            "type": "Button",
            "label": "Hidden",
            "isVisible": "0",
            "isAccessible": "1",
        }
    )
    elements = compact_visible_elements(tree)
    assert [(element.role, element.label, element.value) for element in elements] == [
        ("Cell", "Bluetooth", "On"),
    ]
    assert elements[0].identifier == "row-id"
    assert elements[0].focused is True


@pytest.mark.asyncio
async def test_hidden_focused_node_is_retained_with_conflict_warning() -> None:
    active_apps = iter(["com.apple.TVAppStore", "com.apple.TVAppStore"])
    tree: dict[str, object] = {
        "type": "Application",
        "label": "App Store",
        "isVisible": "1",
        "children": [
            {
                "type": "Button",
                "label": "Games",
                "isVisible": "0",
                "isAccessible": "1",
                "isFocused": "1",
            },
            {
                "type": "Button",
                "label": "Arcade",
                "isVisible": "0",
                "isAccessible": "1",
                "isFocused": "0",
            },
        ],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/status":
            return _status("session-1")
        if path == "/session/session-1/appium/settings":
            return _ok({})
        if path == "/wda/activeAppInfo":
            return _ok({"bundleId": next(active_apps)})
        if path == "/session/session-1/source":
            return _ok(tree)
        if path == "/session/session-1/element/active":
            return _ok({"element-6066-11e4-a52e-4f735466cecf": "focused-1"})
        if path.endswith("/attribute/label"):
            return _ok("Games")
        raise AssertionError(request.url)

    observation = await _with_client(handler, lambda client: client.observe(timeout_s=0.2))
    assert [(element.label, element.focused) for element in observation.visible] == [
        ("Games", True)
    ]
    assert observation.focused_label == "Games"
    assert "visibility_conflicts_with_focus" in observation.warnings
    assert observation.errors == []

    elements = compact_visible_elements(tree)
    assert [element.label for element in elements] == ["Games"]


def test_compact_tree_caps_rows_but_retains_page_title_and_late_focus() -> None:
    children: list[dict[str, object]] = [
        {
            "type": "StaticText",
            "label": "Search",
            "isVisible": "1",
            "isAccessible": "1",
        }
    ]
    children.extend(
        {
            "type": "Cell",
            "label": f"Result {index}",
            "value": f"Episode {index}",
            "isVisible": "1",
            "isAccessible": "1",
            "isFocused": "1" if index == 79 else "0",
        }
        for index in range(80)
    )
    elements = compact_visible_elements(
        {"type": "Application", "isVisible": "1", "children": children}
    )
    assert len(elements) == 64
    assert elements[0].label == "Search"
    assert any(element.label == "Result 79" and element.focused for element in elements)


@pytest.mark.asyncio
async def test_select_rejects_duplicate_targets_without_mutating() -> None:
    mutations: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/status":
            return _status("session-1")
        if path == "/session/session-1/appium/settings":
            return _ok({})
        if path == "/session/session-1/elements":
            return _ok(
                [
                    {"element-6066-11e4-a52e-4f735466cecf": "one"},
                    {"element-6066-11e4-a52e-4f735466cecf": "two"},
                ]
            )
        if path.endswith("/click") or path.endswith("/wda/pressButton"):
            mutations.append(path)
            return _ok({})
        raise AssertionError(request.url)

    async def operation(client: WDAClient) -> None:
        with pytest.raises(WDAError, match="exactly one") as caught:
            await client.select("Bluetooth")
        assert caught.value.code.value == "verification_failed"
        assert caught.value.details == {
            "reason": "target_not_unique",
            "match_count": 2,
        }

    await _with_client(handler, operation)
    assert mutations == []


@pytest.mark.asyncio
async def test_select_rejects_unique_unfocused_target_without_mutating() -> None:
    mutations: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/status":
            return _status("session-1")
        if path == "/session/session-1/appium/settings":
            return _ok({})
        if path == "/session/session-1/elements":
            return _ok([{"element-6066-11e4-a52e-4f735466cecf": "target"}])
        if path == "/session/session-1/element/target/attribute/focused":
            return _ok(False)
        if path.endswith("/click") or path.endswith("/wda/pressButton"):
            mutations.append(path)
            return _ok({})
        raise AssertionError(request.url)

    async def operation(client: WDAClient) -> None:
        with pytest.raises(WDAError, match="not focused") as caught:
            await client.select("Bluetooth")
        assert caught.value.code.value == "verification_failed"
        assert caught.value.details == {"reason": "target_not_focused"}

    await _with_client(handler, operation)
    assert mutations == []


@pytest.mark.asyncio
async def test_select_focused_target_sends_one_remote_select_without_clicking() -> None:
    queries: list[dict[str, object]] = []
    presses: list[dict[str, object]] = []
    clicks: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/status":
            return _status("session-1")
        if path == "/session/session-1/appium/settings":
            return _ok({})
        if path == "/session/session-1/elements":
            queries.append(json.loads(request.content))
            return _ok([{"element-6066-11e4-a52e-4f735466cecf": "target"}])
        if path == "/session/session-1/element/target/attribute/focused":
            return _ok(True)
        if path == "/session/session-1/wda/pressButton":
            presses.append(json.loads(request.content))
            return _ok({})
        if path.endswith("/click"):
            clicks.append(path)
            return _ok({})
        raise AssertionError(request.url)

    result = await _with_client(
        handler,
        lambda client: client.select("Bluetooth", role="Button"),
    )
    assert result.operation == "select"
    assert result.acknowledged is True
    assert queries == [
        {
            "using": "predicate string",
            "value": 'type == "XCUIElementTypeButton" AND label == "Bluetooth"',
        }
    ]
    assert presses == [{"name": "select"}]
    assert clicks == []


@pytest.mark.asyncio
async def test_mutation_timeout_is_uncertain_and_never_retried() -> None:
    presses = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal presses
        path = request.url.path
        if path == "/status":
            return _status("session-1")
        if path == "/session/session-1/appium/settings":
            return _ok({})
        if path == "/session/session-1/wda/pressButton":
            presses += 1
            await asyncio.sleep(0.05)
            return _ok({})
        raise AssertionError(request.url)

    async def operation(client: WDAClient) -> None:
        with pytest.raises(WDAMutationUncertain, match="observe before"):
            await client.press("down")

    await _with_client(handler, operation, request_timeout_s=0.01)
    assert presses == 1


@pytest.mark.asyncio
async def test_press_accepts_official_tv_os_playpause_key() -> None:
    payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/status":
            return _status("session-1")
        if path == "/session/session-1/appium/settings":
            return _ok({})
        if path == "/session/session-1/wda/pressButton":
            payloads.append(json.loads(request.content))
            return _ok({})
        raise AssertionError(request.url)

    result = await _with_client(handler, lambda client: client.press("playpause"))
    assert result.operation == "press"
    assert result.acknowledged is True
    assert payloads == [{"name": "playpause"}]


@pytest.mark.asyncio
async def test_screenshot_remains_available_when_semantic_source_times_out() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/screenshot":
            return _ok(base64.b64encode(PNG).decode())
        if path == "/status":
            return _status("session-1")
        if path == "/session/session-1/appium/settings":
            return _ok({})
        if path == "/wda/activeAppInfo":
            return _ok({"bundleId": "app.one"})
        if path == "/session/session-1/source":
            await asyncio.sleep(0.05)
            return _ok(_tree("Late"))
        raise AssertionError(request.url)

    observation = await _with_client(
        handler,
        lambda client: client.observe(include_image=True, timeout_s=0.08),
        request_timeout_s=0.01,
    )
    assert observation.image is not None
    assert observation.image.png_bytes == PNG
    assert "semantic_observation_failed" in observation.errors
    assert "png_bytes" not in observation.model_dump(mode="json")["image"]


@pytest.mark.asyncio
async def test_slow_optional_screenshot_cannot_exceed_observation_budget() -> None:
    active_apps = iter(["app.one", "app.one"])

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/screenshot":
            await asyncio.sleep(0.2)
            return _ok(base64.b64encode(PNG).decode())
        if path == "/status":
            return _status("session-1")
        if path == "/session/session-1/appium/settings":
            return _ok({})
        if path == "/wda/activeAppInfo":
            return _ok({"bundleId": next(active_apps)})
        if path == "/session/session-1/source":
            return _ok(_tree("Ready"))
        if path == "/session/session-1/element/active":
            return _ok({"element-6066-11e4-a52e-4f735466cecf": "focused-1"})
        if path.endswith("/attribute/label"):
            return _ok("Ready")
        raise AssertionError(request.url)

    started = time.monotonic()
    observation = await _with_client(
        handler,
        lambda client: client.observe(include_image=True, timeout_s=0.03),
        request_timeout_s=1.0,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 0.1
    assert observation.image is None
    assert "image_unavailable" in observation.warnings


@pytest.mark.asyncio
async def test_expected_app_wait_respects_overall_timeout_budget() -> None:
    sources = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sources
        path = request.url.path
        if path == "/status":
            return _status("session-1")
        if path == "/session/session-1/appium/settings":
            return _ok({})
        if path == "/wda/activeAppInfo":
            await asyncio.sleep(0.015)
            return _ok({"bundleId": "wrong.app"})
        if path == "/session/session-1/source":
            sources += 1
            return _ok(_tree("Should not read"))
        raise AssertionError(request.url)

    started = time.monotonic()
    observation = await _with_client(
        handler,
        lambda client: client.observe(expected_app="target.app", timeout_s=0.045),
        request_timeout_s=0.1,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 0.15
    assert sources == 0
    assert observation.expected_app_verified is False
    assert "expected_app_not_observed" in observation.errors


@pytest.mark.asyncio
async def test_changed_server_session_is_reconfigured_before_next_mutation() -> None:
    sessions = iter(["session-1", "session-2"])
    configured: list[str] = []
    pressed: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/status":
            return _status(next(sessions))
        if path.endswith("/appium/settings"):
            configured.append(path.split("/")[2])
            return _ok({})
        if path.endswith("/wda/pressButton"):
            pressed.append(path.split("/")[2])
            return _ok({})
        raise AssertionError(request.url)

    async def operation(client: WDAClient):  # noqa: ANN202
        first = await client.press("down")
        second = await client.press("up")
        return first, second

    first, second = await _with_client(handler, operation)
    assert configured == ["session-1", "session-2"]
    assert pressed == ["session-1", "session-2"]
    assert first.identity.generation == 0
    assert second.identity.generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["press", "select", "set_text"])
async def test_stale_expected_identity_blocks_before_lookup_or_mutation(action: str) -> None:
    unsafe_requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/status":
            return _status("session-current")
        if path == "/session/session-current/appium/settings":
            return _ok({})
        unsafe_requests.append(path)
        return _ok({})

    async def operation(client: WDAClient) -> None:
        current = (await client.status()).identity
        stale = WDAIdentity(
            helper_build_id=current.helper_build_id,
            session_id="session-stale",
            generation=current.generation,
        )
        with pytest.raises(WDAError, match="changed since the observation") as caught:
            if action == "press":
                await client.press("down", expected_identity=stale)
            elif action == "select":
                await client.select("Result", expected_identity=stale)
            else:
                await client.set_text("Query", expected_identity=stale)
        assert caught.value.code.value == "stale_endpoint"

    await _with_client(handler, operation)
    assert unsafe_requests == []


@pytest.mark.asyncio
async def test_set_text_uses_tv_os_active_element_value_route() -> None:
    payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/status":
            return _status("session-1")
        if path == "/session/session-1/appium/settings":
            return _ok({})
        if path == "/session/session-1/wda/keys":
            payloads.append(json.loads(request.content))
            return _ok({})
        raise AssertionError(request.url)

    result = await _with_client(handler, lambda client: client.set_text("Severance"))
    assert result.operation == "set_text"
    assert result.acknowledged is True
    assert payloads == [{"value": list("Severance")}]


@pytest.mark.asyncio
async def test_error_never_includes_endpoint_or_private_response_body() -> None:
    private = "PRIVATE-UDID-IN-SERVER-BODY"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"value": {"message": private}})

    async def operation(client: WDAClient) -> None:
        with pytest.raises(WDAError) as caught:
            await client.status()
        rendered = str(caught.value)
        assert private not in rendered
        assert "wda.test" not in rendered

    await _with_client(handler, operation)
