"""WDA doctor and exact-process lifecycle tests with no device activity."""

from __future__ import annotations

import os
import plistlib
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from home_media.wda_runtime import (
    WDARuntime,
    WDARuntimeConfig,
    WDARuntimeState,
    discover_cached_xctestruns,
    discover_xcode_developer_dirs,
)

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)
UDID = "private-target-identifier"
BUNDLE_ID = "com.example.WebDriverAgentRunner.xctrunner"


class FakeProcess:
    def __init__(self, *, returncode: int | None = None, pid: int = 4321) -> None:
        self.returncode = returncode
        self.pid = pid
        self.terminated = 0
        self.killed = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = -15
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1
        self.returncode = -15

    def kill(self) -> None:
        self.killed += 1
        self.returncode = -9


class UnstoppableProcess(FakeProcess):
    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired("owned-runner", timeout)


def _record_fake_group_signal(
    process: FakeProcess,
    signals: list[tuple[int, int]],
    pgid: int,
    sig: int,
) -> None:
    signals.append((pgid, sig))
    process.returncode = -sig


def _fixture_tree(tmp_path: Path) -> tuple[WDARuntimeConfig, Path]:
    developer = tmp_path / "Xcode-beta.app" / "Contents" / "Developer"
    xcodebuild = developer / "usr" / "bin" / "xcodebuild"
    xcodebuild.parent.mkdir(parents=True)
    xcodebuild.write_text("binary", encoding="utf-8")
    xcodebuild.chmod(0o755)

    products = tmp_path / "cache" / "build-wda-signed" / "Build" / "Products"
    app = products / "Debug-appletvos" / "WebDriverAgentRunner_tvOS-Runner.app"
    test_bundle = app / "PlugIns" / "WebDriverAgentRunner_tvOS.xctest"
    framework = products / "Debug-appletvos" / "WebDriverAgentLib_tvOS.framework"
    test_bundle.mkdir(parents=True)
    framework.mkdir(parents=True)
    profile = app / "embedded.mobileprovision"
    profile.write_bytes(b"signed-profile-placeholder")
    xctestrun = products / "WebDriverAgentRunner_tvOS_appletvos27.0-arm64.xctestrun"
    xctestrun.write_bytes(
        plistlib.dumps(
            {
                "WebDriverAgentRunner_tvOS": {
                    "TestHostPath": (
                        "__TESTROOT__/Debug-appletvos/WebDriverAgentRunner_tvOS-Runner.app"
                    ),
                    "TestHostBundleIdentifier": BUNDLE_ID,
                    "TestBundlePath": ("__TESTHOST__/PlugIns/WebDriverAgentRunner_tvOS.xctest"),
                    "DependentProductPaths": [
                        "__TESTROOT__/Debug-appletvos/WebDriverAgentLib_tvOS.framework",
                        ("__TESTROOT__/Debug-appletvos/WebDriverAgentRunner_tvOS-Runner.app"),
                        (
                            "__TESTROOT__/Debug-appletvos/"
                            "WebDriverAgentRunner_tvOS-Runner.app/PlugIns/"
                            "WebDriverAgentRunner_tvOS.xctest"
                        ),
                    ],
                },
                "__xctestrun_metadata__": {"FormatVersion": 2},
            }
        )
    )
    config = WDARuntimeConfig(
        udid=UDID,
        xctestrun_path=xctestrun,
        developer_dir=developer,
        cache_dir=tmp_path / "cache",
    )
    return config, profile


def _profile(*, expires_at: datetime, devices: list[str] | None = None) -> bytes:
    return plistlib.dumps(
        {
            "ExpirationDate": expires_at,
            "TimeToLive": 7,
            "ProvisionedDevices": devices if devices is not None else [UDID],
            "Platform": ["tvOS"],
            "Entitlements": {"application-identifier": f"TEAMID.{BUNDLE_ID}"},
        }
    )


def _native_config(
    tmp_path: Path,
    *,
    xctestrun_path: Path | None = None,
    command: tuple[str, ...] | None = None,
) -> WDARuntimeConfig:
    runner = tmp_path / "bin" / "native-runner"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text("runner", encoding="utf-8")
    runner.chmod(0o755)
    return WDARuntimeConfig(
        udid=UDID,
        backend="native",
        bundle_id=BUNDLE_ID,
        native_command=command or (str(runner),),
        xctestrun_path=xctestrun_path,
        cache_dir=tmp_path / "native-cache",
    )


def _runner(profile: bytes, *, codesign_returncode: int = 0):
    def run(argv: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "/usr/bin/codesign":
            return subprocess.CompletedProcess(argv, codesign_returncode, b"", b"")
        if argv[:4] == ("/usr/bin/security", "cms", "-D", "-i"):
            return subprocess.CompletedProcess(argv, 0, profile, b"")
        raise AssertionError(f"unexpected local command: {argv[0]}")

    return run


def test_discover_cached_xctestruns_is_cache_bounded_and_sorted(tmp_path: Path) -> None:
    first = tmp_path / "build-wda-signed" / "Build" / "Products" / "a.xctestrun"
    second = tmp_path / "other" / "b.xctestrun"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    (tmp_path / "link.xctestrun").symlink_to(first)

    assert discover_cached_xctestruns(tmp_path) == (first, second)


def test_discover_xcode_locations_without_changing_global_selection(tmp_path: Path) -> None:
    apps = tmp_path / "Applications"
    beta = apps / "Xcode-beta.app" / "Contents" / "Developer"
    regular = apps / "Xcode.app" / "Contents" / "Developer"
    for developer in (beta, regular):
        xcodebuild = developer / "usr" / "bin" / "xcodebuild"
        xcodebuild.parent.mkdir(parents=True)
        xcodebuild.write_text("binary", encoding="utf-8")
        xcodebuild.chmod(0o755)

    assert discover_xcode_developer_dirs((apps,)) == tuple(sorted((beta, regular), key=str))


def test_default_logs_are_private_target_specific_without_raw_udids(tmp_path: Path) -> None:
    first, _ = _fixture_tree(tmp_path)
    second = replace(first, udid="another-private-target-identifier")

    assert first.resolved_log_path != second.resolved_log_path
    assert first.udid not in str(first.resolved_log_path)
    assert second.udid not in str(second.resolved_log_path)


def test_udid_fingerprints_are_casefolded_and_canonically_normalized(
    tmp_path: Path,
) -> None:
    composed = WDARuntimeConfig(udid="Device-\u00c5", cache_dir=tmp_path)
    decomposed = WDARuntimeConfig(udid=" device-A\u030a ", cache_dir=tmp_path)

    assert composed.process_lock_path == decomposed.process_lock_path
    assert composed.resolved_log_path == decomposed.resolved_log_path


def test_doctor_reports_missing_xcode_without_running_commands(tmp_path: Path) -> None:
    config, _ = _fixture_tree(tmp_path)
    config = WDARuntimeConfig(
        udid=config.udid,
        xctestrun_path=config.xctestrun_path,
        developer_dir=tmp_path / "absent-xcode",
        cache_dir=config.cache_dir,
    )
    report = WDARuntime(
        config,
        command_runner=_runner(_profile(expires_at=NOW + timedelta(days=6))),
        now=lambda: NOW,
    ).doctor()

    assert report.ready is False
    assert "xcode_unavailable" in {issue.code for issue in report.issues}


def test_doctor_reports_missing_profile(tmp_path: Path) -> None:
    config, profile_path = _fixture_tree(tmp_path)
    profile_path.unlink()
    report = WDARuntime(
        config,
        command_runner=_runner(_profile(expires_at=NOW + timedelta(days=6))),
        now=lambda: NOW,
    ).doctor()

    assert report.ready is False
    assert report.profile_valid is False
    assert "profile_missing" in {issue.code for issue in report.issues}


def test_doctor_reports_expired_profile_and_actual_expiry(tmp_path: Path) -> None:
    config, _ = _fixture_tree(tmp_path)
    expiry = NOW - timedelta(hours=1)
    report = WDARuntime(
        config,
        command_runner=_runner(_profile(expires_at=expiry)),
        now=lambda: NOW,
    ).doctor()

    assert report.ready is False
    assert report.profile_expires_at == expiry
    assert report.profile_days_remaining == 0
    assert "profile_expired" in {issue.code for issue in report.issues}


def test_doctor_warns_honestly_for_seven_day_free_profile(tmp_path: Path) -> None:
    config, _ = _fixture_tree(tmp_path)
    expiry = NOW + timedelta(days=6, hours=23)
    report = WDARuntime(
        config,
        command_runner=_runner(_profile(expires_at=expiry)),
        now=lambda: NOW,
    ).doctor()

    assert report.ready is True
    assert report.profile_valid is True
    assert report.profile_expires_at == expiry
    issue = next(issue for issue in report.issues if issue.code == "profile_short_lived")
    assert issue.severity.value == "warning"
    assert "seven days" in issue.remedy


def test_native_doctor_needs_no_xcode_and_reports_unknown_profile_expiry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("home_media.wda_runtime.sys.platform", "darwin")
    report = WDARuntime(_native_config(tmp_path)).doctor()

    assert report.ready is True
    assert report.xcode_available is False
    assert report.xctestrun_valid is False
    assert report.profile_expires_at is None
    issues = {issue.code: issue for issue in report.issues}
    assert set(issues) == {"profile_expiry_unknown"}
    assert issues["profile_expiry_unknown"].severity.value == "warning"
    assert "seven days" in issues["profile_expiry_unknown"].remedy


def test_native_doctor_blocks_missing_runner(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("home_media.wda_runtime.sys.platform", "darwin")
    config = _native_config(
        tmp_path,
        command=(str(tmp_path / "missing-native-runner"),),
    )

    report = WDARuntime(config).doctor()

    assert report.ready is False
    assert "native_runner_unavailable" in {issue.code for issue in report.issues}
    assert "xcode_unavailable" not in {issue.code for issue in report.issues}


def test_native_doctor_blocks_expired_provided_cached_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("home_media.wda_runtime.sys.platform", "darwin")
    xcode_config, _ = _fixture_tree(tmp_path)
    config = _native_config(tmp_path, xctestrun_path=xcode_config.xctestrun_path)
    expiry = NOW - timedelta(minutes=1)

    report = WDARuntime(
        config,
        command_runner=_runner(_profile(expires_at=expiry)),
        now=lambda: NOW,
    ).doctor()

    assert report.ready is False
    assert report.profile_expires_at == expiry
    assert report.xctestrun_valid is True
    assert report.signature_valid is True
    assert "profile_expired" in {issue.code for issue in report.issues}
    assert "xcode_unavailable" not in {issue.code for issue in report.issues}


def test_native_start_uses_exact_pinned_arguments_without_xcode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("home_media.wda_runtime.sys.platform", "darwin")
    prefix = (str(tmp_path / "bin" / "uvx"), "--from", "pymobiledevice3==11.15.5")
    Path(prefix[0]).parent.mkdir(parents=True)
    Path(prefix[0]).write_text("runner", encoding="utf-8")
    Path(prefix[0]).chmod(0o755)
    config = _native_config(tmp_path, command=prefix)
    captured: dict[str, object] = {}
    process = FakeProcess(pid=8765)
    signals: list[tuple[int, int]] = []

    def factory(argv: Sequence[str], env: Mapping[str, str], log: BinaryIO) -> FakeProcess:
        captured["argv"] = tuple(argv)
        captured["log_mode"] = os.fstat(log.fileno()).st_mode & 0o777
        captured["has_developer_dir"] = "DEVELOPER_DIR" in env
        return process

    runtime = WDARuntime(
        config,
        process_factory=factory,
        group_signaler=lambda pgid, sig: _record_fake_group_signal(
            process, signals, pgid, sig
        ),
    )

    status = runtime.start()

    assert status.state is WDARuntimeState.STARTING
    assert captured["argv"] == (
        *prefix,
        "developer",
        "dvt",
        "xcuitest",
        "--native",
        "--udid",
        UDID,
        "--env",
        "USE_PORT=8100",
        "--env",
        "MJPEG_SERVER_PORT=9100",
        "--env",
        f"WDA_PRODUCT_BUNDLE_IDENTIFIER={BUNDLE_ID}",
        BUNDLE_ID,
    )
    assert captured["log_mode"] == 0o600
    assert runtime.mark_ready().state is WDARuntimeState.READY
    assert runtime.stop().state is WDARuntimeState.STOPPED
    assert signals == [(8765, signal.SIGTERM)]
    assert process.terminated == 0


def test_start_uses_exact_target_arguments_selected_xcode_and_private_log(
    tmp_path: Path,
) -> None:
    config, _ = _fixture_tree(tmp_path)
    captured: dict[str, object] = {}
    process = FakeProcess()
    signals: list[tuple[int, int]] = []

    def factory(argv: Sequence[str], env: Mapping[str, str], log: BinaryIO) -> FakeProcess:
        captured["argv"] = tuple(argv)
        captured["developer_dir"] = env["DEVELOPER_DIR"]
        captured["log_mode"] = os.fstat(log.fileno()).st_mode & 0o777
        return process

    runtime = WDARuntime(
        config,
        command_runner=_runner(_profile(expires_at=NOW + timedelta(days=6))),
        process_factory=factory,
        group_signaler=lambda pgid, sig: _record_fake_group_signal(
            process, signals, pgid, sig
        ),
        now=lambda: NOW,
    )
    status = runtime.start()

    assert status.state is WDARuntimeState.STARTING
    assert captured["argv"] == (
        str(config.developer_dir / "usr" / "bin" / "xcodebuild"),
        "-xctestrun",
        str(config.xctestrun_path),
        "-destination",
        f"id={UDID}",
        "test-without-building",
    )
    assert captured["developer_dir"] == str(config.developer_dir)
    assert captured["log_mode"] == 0o600
    assert runtime.mark_ready().state is WDARuntimeState.READY
    stopped = runtime.stop()
    assert stopped.state is WDARuntimeState.STOPPED
    assert signals == [(process.pid, signal.SIGTERM)]
    assert process.terminated == 0
    assert process.killed == 0


def test_startup_failure_redacts_raw_xcode_output(tmp_path: Path) -> None:
    config, _ = _fixture_tree(tmp_path)
    secret = f"device={UDID} team=PRIVATE-TEAM credential=PRIVATE-CREDENTIAL"

    def factory(argv: Sequence[str], env: Mapping[str, str], log: BinaryIO) -> FakeProcess:
        del argv, env
        log.write(secret.encode())
        return FakeProcess(returncode=65)

    runtime = WDARuntime(
        config,
        command_runner=_runner(_profile(expires_at=NOW + timedelta(days=6))),
        process_factory=factory,
        now=lambda: NOW,
    )
    status = runtime.start()

    assert status.state is WDARuntimeState.FAILED
    assert status.issue is not None
    assert status.issue.code == "xcodebuild_exited_before_ready"
    returned = f"{status.issue.code} {status.issue.message} {status.issue.remedy}"
    assert UDID not in returned
    assert "PRIVATE-TEAM" not in returned
    assert "PRIVATE-CREDENTIAL" not in returned
    assert status.log_path.read_text(encoding="utf-8") == secret
    assert status.log_path.stat().st_mode & 0o777 == 0o600


def test_stop_only_signals_the_owned_process(tmp_path: Path) -> None:
    config, _ = _fixture_tree(tmp_path)
    process = FakeProcess()
    signals: list[tuple[int, int]] = []
    runtime = WDARuntime(
        config,
        command_runner=_runner(_profile(expires_at=NOW + timedelta(days=6))),
        process_factory=lambda _argv, _env, _log: process,
        group_signaler=lambda pgid, sig: _record_fake_group_signal(
            process, signals, pgid, sig
        ),
        now=lambda: NOW,
    )
    runtime.start()

    runtime.stop()
    runtime.stop()

    assert signals == [(process.pid, signal.SIGTERM)]
    assert process.terminated == 0
    assert process.killed == 0


def test_runtime_lock_prevents_duplicate_helper_for_exact_target(tmp_path: Path) -> None:
    config, _ = _fixture_tree(tmp_path)
    profile = _profile(expires_at=NOW + timedelta(days=6))
    first_process = FakeProcess(pid=8101)
    second_process = FakeProcess(pid=8102)
    first = WDARuntime(
        config,
        command_runner=_runner(profile),
        process_factory=lambda _argv, _env, _log: first_process,
        group_signaler=lambda _pgid, sig: setattr(first_process, "returncode", -sig),
        now=lambda: NOW,
    )
    second = WDARuntime(
        config,
        command_runner=_runner(profile),
        process_factory=lambda _argv, _env, _log: second_process,
        group_signaler=lambda _pgid, sig: setattr(second_process, "returncode", -sig),
        now=lambda: NOW,
    )

    assert first.start().state is WDARuntimeState.STARTING
    blocked = second.start()

    assert blocked.state is WDARuntimeState.FAILED
    assert blocked.issue is not None
    assert blocked.issue.code == "helper_process_in_use"
    assert second_process.returncode is None

    first.stop()
    assert second.start().state is WDARuntimeState.STARTING
    second.stop()


def test_dead_leader_with_live_child_keeps_lock_and_blocks_replacement(
    tmp_path: Path,
) -> None:
    config, _ = _fixture_tree(tmp_path)
    profile = _profile(expires_at=NOW + timedelta(days=6))
    dead_leader = FakeProcess(returncode=0, pid=8151)
    replacement = FakeProcess(pid=8152)
    group_alive = True
    starts = 0

    def first_factory(
        _argv: Sequence[str], _env: Mapping[str, str], _log: BinaryIO
    ) -> FakeProcess:
        nonlocal starts
        starts += 1
        return dead_leader

    first = WDARuntime(
        config,
        command_runner=_runner(profile),
        process_factory=first_factory,
        group_signaler=lambda _pgid, _sig: None,
        group_probe=lambda _pgid: group_alive,
        now=lambda: NOW,
    )
    second = WDARuntime(
        config,
        command_runner=_runner(profile),
        process_factory=lambda _argv, _env, _log: replacement,
        group_signaler=lambda _pgid, sig: setattr(replacement, "returncode", -sig),
        now=lambda: NOW,
    )

    failed = first.start()
    unchanged = first.start()
    blocked = second.start()

    assert failed.state is WDARuntimeState.FAILED
    assert unchanged.pid == dead_leader.pid
    assert starts == 1
    assert blocked.state is WDARuntimeState.FAILED
    assert blocked.issue is not None
    assert blocked.issue.code == "helper_process_in_use"

    group_alive = False
    assert first.stop().state is WDARuntimeState.STOPPED
    assert second.start().state is WDARuntimeState.STARTING
    second.stop()


def test_failed_stop_cannot_orphan_process_by_starting_replacement(tmp_path: Path) -> None:
    config, _ = _fixture_tree(tmp_path)
    config = replace(config, stop_timeout_s=0.001)
    process = UnstoppableProcess(pid=8201)
    starts = 0
    group_alive = True

    def factory(
        _argv: Sequence[str], _env: Mapping[str, str], _log: BinaryIO
    ) -> UnstoppableProcess:
        nonlocal starts
        starts += 1
        return process

    runtime = WDARuntime(
        config,
        command_runner=_runner(_profile(expires_at=NOW + timedelta(days=6))),
        process_factory=factory,
        group_signaler=lambda _pgid, _sig: None,
        group_probe=lambda _pgid: group_alive,
        now=lambda: NOW,
    )
    assert runtime.start().state is WDARuntimeState.STARTING

    failed = runtime.stop()
    restarted = runtime.start()

    assert failed.state is WDARuntimeState.FAILED
    assert restarted.state is WDARuntimeState.FAILED
    assert restarted.pid == process.pid
    assert starts == 1

    process.returncode = -9
    group_alive = False
    runtime.stop()


def test_stop_kills_child_that_outlives_process_group_leader(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    child_code = (
        "import os, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "with open(sys.argv[1], 'w') as handle:\n"
        "    handle.write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    leader_code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
        "time.sleep(60)\n"
    )
    spawned: subprocess.Popen[bytes] | None = None
    signals: list[int] = []

    def factory(
        _argv: Sequence[str], env: Mapping[str, str], log: BinaryIO
    ) -> subprocess.Popen[bytes]:
        nonlocal spawned
        spawned = subprocess.Popen(  # noqa: S603 - fixed local Python test fixture
            [sys.executable, "-c", leader_code, child_code, str(child_pid_path)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=dict(env),
            close_fds=True,
            start_new_session=True,
        )
        return spawned

    def signal_group(pgid: int, sig: int) -> None:
        signals.append(sig)
        os.killpg(pgid, sig)

    config = replace(_native_config(tmp_path), stop_timeout_s=0.2)
    runtime = WDARuntime(
        config,
        process_factory=factory,
        group_signaler=signal_group,
    )
    try:
        assert runtime.start().state is WDARuntimeState.STARTING
        deadline = time.monotonic() + 2.0
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))

        stopped = runtime.stop()

        assert stopped.state is WDARuntimeState.STOPPED
        assert signals == [signal.SIGTERM, signal.SIGKILL]
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("owned child process survived runtime.stop()")
    finally:
        if spawned is not None:
            with suppress(ProcessLookupError):
                os.killpg(spawned.pid, signal.SIGKILL)
            try:
                spawned.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                spawned.kill()
                spawned.wait(timeout=2.0)
