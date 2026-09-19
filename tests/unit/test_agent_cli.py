"""Agent setup CLI tests with no device, pairing, or helper activity."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from home_media import agent_cli
from home_media.agent_control import (
    AgentConfig,
    AgentDevice,
    HelperConfig,
    load_agent_config,
    save_agent_config,
)
from home_media.models import DeviceKind, DiscoveredEndpoint, PairingSession
from home_media.wda_runtime import WDADoctorReport, WDARuntimeState, WDARuntimeStatus

runner = CliRunner()
UDID = "00000000-0000-4000-8000-000000000099"
BUNDLE_ID = "com.example.WebDriverAgentRunner.xctrunner"


def _device(
    device_id: str,
    *,
    name: str = "Office",
    address: str = "192.0.2.10",
    protocols: list[str] | None = None,
    identifiers: list[str] | None = None,
) -> DiscoveredEndpoint:
    return DiscoveredEndpoint(
        device_id=device_id,
        kind=DeviceKind.APPLE_TV,
        name=name,
        address=address,
        model="Apple TV 4K",
        os="tvOS",
        protocols=protocols or ["companion", "airplay"],
        raw={"all_identifiers": identifiers or [device_id]},
    )


def _fake_setup_agent(
    monkeypatch: pytest.MonkeyPatch,
    devices: list[DiscoveredEndpoint],
) -> list[tuple[str, str]]:
    pair_calls: list[tuple[str, str]] = []
    sessions: dict[str, tuple[str, str]] = {}

    class FakeAdapter:
        async def discover(self) -> list[DiscoveredEndpoint]:
            return devices

        async def pair_start(self, device_id: str, protocol: str) -> PairingSession:
            pair_calls.append((device_id, protocol))
            session_id = f"session-{len(pair_calls)}"
            sessions[session_id] = (device_id, protocol)
            return PairingSession(
                session_id=session_id,
                room_key="",
                device_id=device_id,
                protocol=protocol,
            )

        async def pair_finish(self, session_id: str, pin: str) -> PairingSession:
            device_id, protocol = sessions[session_id]
            pair_calls.append((protocol, pin))
            return PairingSession(
                session_id=session_id,
                room_key="",
                device_id=device_id,
                protocol=protocol,
                state="completed",
            )

    class FakeAgent:
        def __init__(self, _config: AgentConfig) -> None:
            self.adapter = FakeAdapter()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(agent_cli, "AppleTVAgent", FakeAgent)
    return pair_calls


def test_setup_requires_explicit_number_for_ambiguous_names_and_saves_privately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "private" / "agent.yaml"
    monkeypatch.setenv("APPLE_TV_AGENT_CONFIG", str(config_path))
    first_id = "11111111-1111-4111-8111-111111111111"
    second_id = "22222222-2222-4222-8222-222222222222"
    pair_calls = _fake_setup_agent(
        monkeypatch,
        [
            _device(first_id, name="Apple TV", address="192.0.2.21"),
            _device(second_id, name="Apple TV", address="192.0.2.22"),
        ],
    )

    result = runner.invoke(
        agent_cli.app,
        ["setup", "--no-pair"],
        input="2\nDen Television\n\n",
    )

    assert result.exit_code == 0, result.output
    assert "Select device number" in result.output
    assert "192.0.2.21" in result.output
    assert "192.0.2.22" in result.output
    saved = load_agent_config(config_path).devices[0]
    assert saved.stable_id == second_id
    assert saved.key == "den-television"
    assert pair_calls == []
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert first_id not in result.output
    assert second_id not in result.output
    assert "codex mcp add apple-tv-agent -- apple-tv-agent-mcp" in result.output
    assert "claude mcp add apple-tv-agent -- apple-tv-agent-mcp" in result.output


def test_setup_default_slug_avoids_an_existing_device_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "private" / "agent.yaml"
    monkeypatch.setenv("APPLE_TV_AGENT_CONFIG", str(config_path))
    save_agent_config(
        AgentConfig(
            devices=[
                AgentDevice(
                    key="office",
                    name="Other Office",
                    stable_id="existing-stable-id",
                )
            ]
        ),
        config_path,
    )
    _fake_setup_agent(monkeypatch, [_device("new-stable-id")])

    result = runner.invoke(
        agent_cli.app,
        ["setup", "--no-pair"],
        input="1\n\n\n",
    )

    assert result.exit_code == 0, result.output
    saved = load_agent_config(config_path)
    assert {device.key for device in saved.devices} == {"office", "office-2"}
    assert next(device for device in saved.devices if device.key == "office-2").stable_id == (
        "new-stable-id"
    )


def test_setup_preserves_visual_route_for_matching_stable_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "private" / "agent.yaml"
    monkeypatch.setenv("APPLE_TV_AGENT_CONFIG", str(config_path))
    stable_id = "matching-stable-id"
    helper = HelperConfig(
        udid=UDID,
        backend="native",
        bundle_id=BUNDLE_ID,
    )
    save_agent_config(
        AgentConfig(
            devices=[
                AgentDevice(
                    key="old-key",
                    name="Old Name",
                    stable_id=stable_id,
                    wda_endpoint="http://192.0.2.40:8100",
                    wda_identity="verified-helper-identity",
                    helper=helper,
                )
            ],
            country="CA",
            subscriptions=["Netflix"],
        ),
        config_path,
    )
    _fake_setup_agent(monkeypatch, [_device(stable_id, name="Discovered Name")])

    result = runner.invoke(
        agent_cli.app,
        ["setup", "--no-pair"],
        input="1\nUpdated Name\nupdated-key\n",
    )

    assert result.exit_code == 0, result.output
    saved_config = load_agent_config(config_path)
    assert saved_config.country == "CA"
    assert saved_config.subscriptions == ["Netflix"]
    assert len(saved_config.devices) == 1
    saved = saved_config.devices[0]
    assert saved.key == "updated-key"
    assert saved.name == "Updated Name"
    assert saved.stable_id == stable_id
    assert saved.wda_endpoint == "http://192.0.2.40:8100"
    assert saved.wda_identity == "verified-helper-identity"
    assert saved.helper == helper
    assert "Existing visual-helper binding preserved" in result.output
    assert UDID not in result.output
    assert BUNDLE_ID not in result.output


def test_setup_matches_discovery_alias_and_preserves_configured_stable_identifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "private" / "agent.yaml"
    monkeypatch.setenv("APPLE_TV_AGENT_CONFIG", str(config_path))
    configured_id = "AA:BB:CC:DD:EE:FF"
    preferred_id = "33333333-3333-4333-8333-333333333333"
    helper = HelperConfig(udid=UDID, backend="native", bundle_id=BUNDLE_ID)
    save_agent_config(
        AgentConfig(
            devices=[
                AgentDevice(
                    key="media-room",
                    name="Media Room",
                    stable_id=configured_id,
                    wda_endpoint="http://192.0.2.40:8100",
                    wda_identity="verified-helper-identity",
                    helper=helper,
                )
            ]
        ),
        config_path,
    )
    _fake_setup_agent(
        monkeypatch,
        [
            _device(
                preferred_id,
                name="Bedroom",
                identifiers=[preferred_id, configured_id.casefold()],
            )
        ],
    )

    result = runner.invoke(
        agent_cli.app,
        ["setup", "--no-pair"],
        input="1\n\n\n",
    )

    assert result.exit_code == 0, result.output
    saved = load_agent_config(config_path).devices
    assert len(saved) == 1
    assert saved[0].key == "media-room"
    assert saved[0].name == "Media Room"
    assert saved[0].stable_id == configured_id
    assert saved[0].wda_identity == "verified-helper-identity"
    assert saved[0].helper == helper
    assert "media-room-2" not in result.output


def test_setup_rejects_multiple_configured_alias_matches_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "private" / "agent.yaml"
    monkeypatch.setenv("APPLE_TV_AGENT_CONFIG", str(config_path))
    configured_id = "AA:BB:CC:DD:EE:FF"
    preferred_id = "44444444-4444-4444-8444-444444444444"
    save_agent_config(
        AgentConfig(
            devices=[
                AgentDevice(key="first", name="First", stable_id=configured_id),
                AgentDevice(key="second", name="Second", stable_id=preferred_id),
            ]
        ),
        config_path,
    )
    before = config_path.read_bytes()
    pair_calls = _fake_setup_agent(
        monkeypatch,
        [
            _device(
                preferred_id,
                identifiers=[preferred_id, configured_id],
            )
        ],
    )

    result = runner.invoke(
        agent_cli.app,
        ["setup", "--no-pair"],
        input="1\n",
    )

    assert result.exit_code == 1
    assert '"error": "ambiguous_device_identifiers"' in result.output
    assert "No changes were made" in result.output
    assert config_path.read_bytes() == before
    assert pair_calls == []


def test_setup_never_uses_a_matching_display_name_as_device_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "private" / "agent.yaml"
    monkeypatch.setenv("APPLE_TV_AGENT_CONFIG", str(config_path))
    save_agent_config(
        AgentConfig(
            devices=[
                AgentDevice(
                    key="bedroom",
                    name="Bedroom",
                    stable_id="old-unrelated-id",
                    wda_endpoint="http://192.0.2.50:8100",
                    wda_identity="old-helper-identity",
                )
            ]
        ),
        config_path,
    )
    new_id = "55555555-5555-4555-8555-555555555555"
    _fake_setup_agent(monkeypatch, [_device(new_id, name="Bedroom")])

    result = runner.invoke(
        agent_cli.app,
        ["setup", "--no-pair"],
        input="1\n\n\n",
    )

    assert result.exit_code == 0, result.output
    saved = load_agent_config(config_path)
    assert len(saved.devices) == 2
    new_device = next(device for device in saved.devices if device.stable_id == new_id)
    assert new_device.key == "bedroom-2"
    assert new_device.wda_endpoint is None
    assert new_device.wda_identity is None


def test_setup_prompt_abort_exits_without_saving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "private" / "agent.yaml"
    monkeypatch.setenv("APPLE_TV_AGENT_CONFIG", str(config_path))
    _fake_setup_agent(monkeypatch, [_device("interrupt-stable-id")])
    prompt_count = 0

    def abort_at_key(*_args: object, **_kwargs: object) -> object:
        nonlocal prompt_count
        prompt_count += 1
        if prompt_count == 1:
            return 1
        if prompt_count == 2:
            return "Friendly Name"
        raise agent_cli.typer.Abort

    monkeypatch.setattr(agent_cli.typer, "prompt", abort_at_key)

    result = runner.invoke(agent_cli.app, ["setup", "--no-pair"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert '"error"' not in result.output
    assert not config_path.exists()


@pytest.mark.asyncio
async def test_configure_start_waits_for_worker_before_propagating_cancellation() -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class BlockingRuntime:
        def start(self) -> WDARuntimeStatus:
            entered.set()
            assert release.wait(timeout=1)
            finished.set()
            return WDARuntimeStatus(
                state=WDARuntimeState.STARTING,
                pid=4321,
                log_path=Path("/private/test-wda.log"),
            )

    task = asyncio.create_task(
        agent_cli._start_runtime_for_configure(BlockingRuntime())  # type: ignore[arg-type]
    )
    assert await asyncio.to_thread(entered.wait, 1)

    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished.is_set()


@pytest.mark.asyncio
async def test_configure_stop_waits_for_worker_before_propagating_cancellation() -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class BlockingRuntime:
        def stop(self) -> WDARuntimeStatus:
            entered.set()
            assert release.wait(timeout=1)
            finished.set()
            return WDARuntimeStatus(
                state=WDARuntimeState.STOPPED,
                pid=None,
                log_path=Path("/private/test-wda.log"),
            )

    task = asyncio.create_task(
        agent_cli._stop_runtime_for_configure(BlockingRuntime())  # type: ignore[arg-type]
    )
    assert await asyncio.to_thread(entered.wait, 1)

    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished.is_set()


@pytest.mark.parametrize("stop_state", [WDARuntimeState.STOPPED, WDARuntimeState.FAILED])
def test_configure_serializes_temporary_helper_cleanup_before_saving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop_state: WDARuntimeState,
) -> None:
    config_path = tmp_path / "private" / "agent.yaml"
    monkeypatch.setenv("APPLE_TV_AGENT_CONFIG", str(config_path))
    events: list[str] = []

    class FakeClient:
        def __init__(self, _endpoint: str) -> None:
            self.status_calls = 0

        async def status(self) -> dict[str, bool]:
            self.status_calls += 1
            events.append(f"status-{self.status_calls}")
            if self.status_calls == 1:
                raise OSError("offline until helper starts")
            return {"ready": True}

        async def device_identity(self) -> str:
            events.append("identity")
            return "verified-helper-identity"

        async def aclose(self) -> None:
            events.append("client-close")

    class FakeRuntime:
        def __init__(self, _config: object) -> None:
            pass

        def start(self) -> WDARuntimeStatus:
            events.append("runtime-start")
            return self._status(WDARuntimeState.STARTING, pid=4321)

        def mark_ready(self) -> WDARuntimeStatus:
            events.append("runtime-ready")
            return self._status(WDARuntimeState.READY, pid=4321)

        def stop(self) -> WDARuntimeStatus:
            events.append("runtime-stop")
            pid = None if stop_state is WDARuntimeState.STOPPED else 4321
            return self._status(stop_state, pid=pid)

        @staticmethod
        def _status(state: WDARuntimeState, *, pid: int | None) -> WDARuntimeStatus:
            return WDARuntimeStatus(
                state=state,
                pid=pid,
                log_path=Path("/private/test-wda.log"),
            )

    monkeypatch.setattr(agent_cli, "WDAClient", FakeClient)
    monkeypatch.setattr(agent_cli, "WDARuntime", FakeRuntime)

    result = runner.invoke(
        agent_cli.app,
        [
            "configure",
            "office",
            "--name",
            "Office",
            "--stable-id",
            "stable-device",
            "--endpoint",
            "http://192.0.2.40:8100",
            "--backend",
            "native",
            "--udid",
            UDID,
            "--bundle-id",
            BUNDLE_ID,
        ],
    )

    assert events == [
        "status-1",
        "runtime-start",
        "status-2",
        "runtime-ready",
        "identity",
        "client-close",
        "runtime-stop",
    ]
    if stop_state is WDARuntimeState.STOPPED:
        assert result.exit_code == 0, result.output
        assert load_agent_config(config_path).devices[0].wda_identity == (
            "verified-helper-identity"
        )
    else:
        assert result.exit_code == 1
        assert '"error": "helper_cleanup_failed"' in result.output
        assert not config_path.exists()


def test_pair_prompt_abort_closes_session_without_waiting_on_input_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "private" / "agent.yaml"
    monkeypatch.setenv("APPLE_TV_AGENT_CONFIG", str(config_path))
    save_agent_config(
        AgentConfig(
            devices=[
                AgentDevice(
                    key="office",
                    name="Office",
                    stable_id="stable-device",
                )
            ]
        ),
        config_path,
    )
    state = {"closed": False, "finished": False}

    class FakeAdapter:
        async def pair_start(self, device_id: str, protocol: str) -> PairingSession:
            return PairingSession(room_key="", device_id=device_id, protocol=protocol)

        async def pair_finish(self, _session_id: str, _pin: str) -> PairingSession:
            state["finished"] = True
            raise AssertionError("pair_finish must not run after prompt abort")

    class FakeAgent:
        def __init__(self, config: AgentConfig) -> None:
            self.config = config
            self.adapter = FakeAdapter()

        def device(self, key: str) -> AgentDevice:
            return next(device for device in self.config.devices if device.key == key)

        async def aclose(self) -> None:
            state["closed"] = True

    monkeypatch.setattr(agent_cli, "AppleTVAgent", FakeAgent)
    monkeypatch.setattr(
        agent_cli.typer,
        "prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(agent_cli.typer.Abort()),
    )

    result = runner.invoke(agent_cli.app, ["pair", "office"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert '"error"' not in result.output
    assert state == {"closed": True, "finished": False}


def test_setup_pairs_companion_with_hidden_pin_and_airplay_only_when_chosen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "private" / "agent.yaml"
    monkeypatch.setenv("APPLE_TV_AGENT_CONFIG", str(config_path))
    stable_id = "pairing-stable-id"
    pair_calls = _fake_setup_agent(monkeypatch, [_device(stable_id)])

    result = runner.invoke(
        agent_cli.app,
        ["setup"],
        input="1\n\n\nn\n2468\nn\n",
    )

    assert result.exit_code == 0, result.output
    assert pair_calls == [(stable_id, "companion"), ("companion", "2468")]
    assert "2468" not in result.output
    assert load_agent_config(config_path).devices[0].stable_id == stable_id
    assert '"companion_pairing": "paired"' in result.output
    assert '"airplay_pairing": "not_requested"' in result.output


def test_setup_can_skip_existing_companion_pair_and_pair_airplay_explicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "private" / "agent.yaml"
    monkeypatch.setenv("APPLE_TV_AGENT_CONFIG", str(config_path))
    stable_id = "existing-pair-stable-id"
    pair_calls = _fake_setup_agent(monkeypatch, [_device(stable_id)])

    result = runner.invoke(
        agent_cli.app,
        ["setup", "--use-existing-pairing", "--pair-airplay"],
        input="1\n\n\n9753\n",
    )

    assert result.exit_code == 0, result.output
    assert pair_calls == [(stable_id, "airplay"), ("airplay", "9753")]
    assert "9753" not in result.output
    assert '"companion_pairing": "existing_unverified"' in result.output
    assert '"airplay_pairing": "paired"' in result.output


def test_setup_no_devices_returns_actionable_error_without_creating_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "private" / "agent.yaml"
    monkeypatch.setenv("APPLE_TV_AGENT_CONFIG", str(config_path))
    _fake_setup_agent(monkeypatch, [])

    result = runner.invoke(agent_cli.app, ["setup"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "no_tvos_devices"
    assert "same local network" in payload["remedy"]
    assert not config_path.exists()


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (["--backend", "native", "--bundle-id", BUNDLE_ID], "--udid"),
        (["--backend", "native", "--udid", UDID], "--bundle-id"),
        (
            [
                "--backend",
                "native",
                "--udid",
                UDID,
                "--bundle-id",
                BUNDLE_ID,
                "--developer-dir",
                "/tmp/Xcode/Developer",
            ],
            "does not use an Xcode developer directory",
        ),
        (["--udid", UDID], "Xcode helper requires xctestrun and developer-dir"),
        (
            [
                "--udid",
                UDID,
                "--bundle-id",
                BUNDLE_ID,
                "--xctestrun",
                "/tmp/WDA.xctestrun",
                "--developer-dir",
                "/tmp/Xcode/Developer",
            ],
            "Bundle ID is used only with the native helper backend",
        ),
    ],
)
def test_configure_rejects_invalid_backend_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: list[str],
    expected: str,
) -> None:
    config_path = tmp_path / "private" / "agent.yaml"
    monkeypatch.setenv("APPLE_TV_AGENT_CONFIG", str(config_path))

    result = runner.invoke(
        agent_cli.app,
        [
            "configure",
            "office",
            "--name",
            "Office",
            "--stable-id",
            "stable-device",
            *extra,
        ],
    )

    assert result.exit_code == 2
    assert expected in " ".join(result.output.replace("│", " ").split())
    assert not config_path.exists()


def test_native_config_is_private_and_xctestrun_evidence_is_optional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "private" / "agent.yaml"
    monkeypatch.setenv("APPLE_TV_AGENT_CONFIG", str(config_path))

    result = runner.invoke(
        agent_cli.app,
        [
            "configure",
            "office",
            "--name",
            "Office",
            "--stable-id",
            "stable-device",
            "--backend",
            "native",
            "--udid",
            UDID,
            "--bundle-id",
            BUNDLE_ID,
        ],
    )

    assert result.exit_code == 0
    assert config_path.stat().st_mode & 0o777 == 0o600
    saved = load_agent_config(config_path).devices[0]
    assert saved.helper is not None
    assert saved.helper.backend == "native"
    assert saved.helper.udid == UDID
    assert saved.helper.bundle_id == BUNDLE_ID
    assert saved.helper.xctestrun_path is None
    assert saved.helper.developer_dir is None
    assert UDID not in result.output
    assert BUNDLE_ID not in result.output


def test_endpoint_identity_is_read_and_pinned_instead_of_guessed_from_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "private" / "agent.yaml"
    monkeypatch.setenv("APPLE_TV_AGENT_CONFIG", str(config_path))
    verified_identity = "identity-returned-by-running-helper"

    class FakeClient:
        def __init__(self, endpoint: str) -> None:
            assert endpoint == "http://127.0.0.1:8100"

        async def status(self) -> dict[str, object]:
            return {"ready": True}

        async def device_identity(self) -> str:
            return verified_identity

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(agent_cli, "WDAClient", FakeClient)

    result = runner.invoke(
        agent_cli.app,
        [
            "configure",
            "bedroom",
            "--name",
            "A Friendly Name That Is Not An Identity",
            "--endpoint",
            "http://127.0.0.1:8100",
        ],
    )

    assert result.exit_code == 0
    saved = load_agent_config(config_path).devices[0]
    assert saved.wda_identity == verified_identity
    assert saved.wda_identity != saved.name
    assert verified_identity not in result.output


def test_doctor_forwards_native_backend_and_bundle_to_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "private" / "agent.yaml"
    monkeypatch.setenv("APPLE_TV_AGENT_CONFIG", str(config_path))
    save_agent_config(
        AgentConfig(
            devices=[
                AgentDevice(
                    key="office",
                    name="Office",
                    stable_id="stable-device",
                    helper=HelperConfig(
                        udid=UDID,
                        backend="native",
                        bundle_id=BUNDLE_ID,
                    ),
                )
            ]
        ),
        config_path,
    )
    captured = []

    class FakeRuntime:
        def __init__(self, config) -> None:
            captured.append(config)

        def doctor(self) -> WDADoctorReport:
            return WDADoctorReport(
                ready=True,
                issues=(),
                xcode_available=False,
                xctestrun_valid=False,
                build_products_present=False,
                signature_valid=False,
                profile_valid=False,
                profile_expires_at=None,
                profile_days_remaining=None,
            )

    monkeypatch.setattr(agent_cli, "WDARuntime", FakeRuntime)
    monkeypatch.setattr(agent_cli, "discover_xcode_developer_dirs", lambda: ())
    monkeypatch.setattr(agent_cli, "discover_cached_xctestruns", lambda: ())

    result = runner.invoke(agent_cli.app, ["doctor"])

    assert result.exit_code == 0
    assert len(captured) == 1
    runtime_config = captured[0]
    assert runtime_config.udid == UDID
    assert runtime_config.backend == "native"
    assert runtime_config.bundle_id == BUNDLE_ID
    assert runtime_config.xctestrun_path is None
    assert runtime_config.developer_dir is None
    payload = json.loads(result.output)
    assert payload["devices"][0]["helper"]["ready"] is True
