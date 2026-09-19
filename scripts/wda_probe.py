#!/usr/bin/env python3
"""Experimental real-tvOS WDA probe; requires an already-running signed helper.

Uses only the Python standard library. This is a spike tool, not the production
MCP backend. Images and UI text can contain personal data; save them outside git.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Probe:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self.timings: list[dict[str, Any]] = []
        self.session = ""

    def request(self, route: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.monotonic()
        request = urllib.request.Request(
            self.url + route,
            data=None if data is None else json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"WDA HTTP {exc.code}: {exc.read().decode()[:1500]}") from exc
        finally:
            self.timings.append({"route": route, "seconds": time.monotonic() - started})
        value = result.get("value")
        if isinstance(value, dict) and value.get("error"):
            raise RuntimeError(f"WDA {value['error']}: {value.get('message')}")
        return result

    def connect(self) -> None:
        status = self.request("/status")
        if status["value"].get("os", {}).get("name") != "tvOS":
            raise RuntimeError("Expected a tvOS helper; refusing to control another platform")
        sid = status.get("sessionId")
        if not sid:
            result = self.request(
                "/session", {"capabilities": {"alwaysMatch": {"shouldTerminateApp": False}}}
            )
            sid = result["sessionId"]
        self.session = f"/session/{sid}"
        # Animated tvOS screens may never become idle. Verify destinations with
        # observed labels instead of waiting for global application quiescence.
        self.request(
            self.session + "/appium/settings",
            {"settings": {"waitForIdleTimeout": 0, "animationCoolOffTimeout": 0}},
        )

    def screenshot(self, destination: Path) -> dict[str, Any]:
        started = datetime.now(UTC).isoformat()
        data = base64.b64decode(self.request("/screenshot")["value"], validate=True)
        destination.write_bytes(data)
        return {
            "image_request_started_at": started,
            "image_received_at": datetime.now(UTC).isoformat(),
            "screenshot_path": str(destination.resolve()),
        }

    def observe(
        self, expect: str | None, screenshot: Path | None, expected_app: str | None
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 10
        while True:
            started_at = datetime.now(UTC).isoformat()
            active_before = self.request("/wda/activeAppInfo")["value"]["bundleId"]
            if expected_app is not None and active_before != expected_app:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Expected app {expected_app!r}, observed {active_before!r}")
                continue
            self.request(
                self.session + "/appium/settings",
                {"settings": {"defaultActiveApplication": active_before}},
            )
            tree = self.request(self.session + "/source?format=json")["value"]
            visible = visible_nodes(tree)
            active_after = self.request("/wda/activeAppInfo")["value"]["bundleId"]
            same_app = active_before == active_after
            matched = expect is None or any(n["label"] == expect for n in visible)
            if same_app and matched:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Observation did not reach expected visible label {expect!r}")
        focused = self.request(self.session + "/element/active")["value"]
        element_id = focused.get("element-6066-11e4-a52e-4f735466cecf", focused.get("ELEMENT"))
        focus_label = self.request(self.session + f"/element/{element_id}/attribute/label")["value"]
        result: dict[str, Any] = {
            "observation_started_at": started_at,
            "observation_finished_at": datetime.now(UTC).isoformat(),
            "active_app": active_after,
            "focused_label": focus_label,
            "visible": visible,
            "expected_label": expect,
            "expected_app": expected_app,
            "expected_label_verified": expect is not None and matched,
            "note": "Sequential samples, not an atomic snapshot; timings are request durations.",
        }
        if screenshot is not None:
            result.update(self.screenshot(screenshot))
        return result


def visible_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = []
    if node.get("label") and str(node.get("isVisible")) == "1":
        nodes.append({key: node.get(key) for key in ("type", "label", "value")})
    for child in node.get("children", []):
        nodes.extend(visible_nodes(child))
    return nodes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Exact confirmed Apple TV WDA endpoint")
    parser.add_argument("--expect", help="Wait for this exact visible label after the command")
    parser.add_argument("--expect-app", help="Wait for this app before reading its UI tree")
    parser.add_argument("--screenshot", type=Path, help="Save a fresh WDA screenshot outside git")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("observe")
    commands.add_parser("screenshot", help="Image-only fallback independent of UI-tree requests")
    press = commands.add_parser("press")
    press.add_argument("button", choices=["up", "down", "left", "right", "menu", "home", "select"])
    select = commands.add_parser("select")
    select.add_argument("label", help="Exact label of a unique visible cell, button, or icon")
    select.add_argument("--type", choices=["Cell", "Button", "Icon"], default="Cell")
    activate = commands.add_parser("activate")
    activate.add_argument("bundle_id", help="Bundle ID verified on the target device")
    args = parser.parse_args()
    probe = Probe(args.url)
    if args.command == "status":
        print(json.dumps(probe.request("/status"), indent=2))
        return
    if args.command == "screenshot":
        if args.screenshot is None:
            parser.error("screenshot requires --screenshot PATH")
        print(json.dumps(probe.screenshot(args.screenshot), indent=2))
        return
    probe.connect()
    if args.command == "press":
        probe.request(probe.session + "/wda/pressButton", {"name": args.button})
    elif args.command == "select":
        predicate = (
            f'type == "XCUIElementType{args.type}" AND label == '
            f"{json.dumps(args.label, ensure_ascii=False)} AND visible == 1"
        )
        elements = probe.request(
            probe.session + "/elements", {"using": "predicate string", "value": predicate}
        )["value"]
        if len(elements) != 1:
            raise RuntimeError(f"Expected exactly one visible target, found {len(elements)}")
        element_id = elements[0]["element-6066-11e4-a52e-4f735466cecf"]
        probe.request(probe.session + f"/element/{element_id}/click", {})
    elif args.command == "activate":
        probe.request(probe.session + "/wda/apps/activate", {"bundleId": args.bundle_id})
    expected_app = args.bundle_id if args.command == "activate" else args.expect_app
    result = probe.observe(args.expect, args.screenshot, expected_app)
    result["command_acknowledged"] = args.command != "observe"
    result["request_timings"] = probe.timings
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "outcome": "unverified",
                    "recovery": "Observe or screenshot first; do not blindly repeat the action.",
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
