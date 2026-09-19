"""Local WebDriverAgent doctor and exact-device process lifecycle.

This module never builds, signs, provisions, pairs, or discovers a device. It
can launch either an explicitly selected cached ``.xctestrun`` through Xcode or
a previously installed signed helper through pymobiledevice3's native DVT
runner. pymobiledevice3 remains a separate process: core home-media does not
import or redistribute it. Raw runner output may contain private signing and
device details, so it is written only to a local mode-0600 log and is never
copied into returned issues.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Literal, Protocol, Self

DEFAULT_WDA_CACHE_DIR = Path.home() / "Library" / "Caches" / "home-media" / "appium-spike"
DEFAULT_NATIVE_COMMAND = (
    "uvx",
    "--from",
    "pymobiledevice3==11.15.5",
    "pymobiledevice3",
)

_XCTESTRUN_METADATA_KEY = "__xctestrun_metadata__"
_LOCAL_COMMAND_TIMEOUT_S = 10.0
_PROCESS_GROUP_POLL_INTERVAL_S = 0.05
_BUNDLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{2,253}[A-Za-z0-9]")


class WDAIssueSeverity(StrEnum):
    """Severity of one machine-readable doctor or lifecycle issue."""

    ERROR = "error"
    WARNING = "warning"


class WDARuntimeState(StrEnum):
    """Managed state of the one process owned by a runtime instance."""

    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class WDAIssue:
    code: str
    severity: WDAIssueSeverity
    message: str
    remedy: str


@dataclass(frozen=True)
class WDADoctorReport:
    ready: bool
    issues: tuple[WDAIssue, ...]
    xcode_available: bool
    xctestrun_valid: bool
    build_products_present: bool
    signature_valid: bool
    profile_valid: bool
    profile_expires_at: datetime | None
    profile_days_remaining: int | None


@dataclass(frozen=True)
class WDARuntimeStatus:
    state: WDARuntimeState
    pid: int | None
    log_path: Path
    issue: WDAIssue | None = None


@dataclass(frozen=True)
class WDARuntimeConfig:
    """Explicit local inputs for one already-built or installed signed runner."""

    udid: str
    xctestrun_path: Path | None = None
    developer_dir: Path | None = None
    backend: Literal["xcode", "native"] = "xcode"
    bundle_id: str | None = None
    native_command: tuple[str, ...] = DEFAULT_NATIVE_COMMAND
    cache_dir: Path = DEFAULT_WDA_CACHE_DIR
    log_path: Path | None = None
    startup_timeout_s: float = 20.0
    stop_timeout_s: float = 8.0

    @property
    def resolved_log_path(self) -> Path:
        fingerprint = _udid_fingerprint(self.udid)
        filename = f"wda-{self.backend}-{fingerprint}.log"
        return self.log_path or self.cache_dir / "runtime" / filename

    @property
    def process_lock_path(self) -> Path:
        fingerprint = _udid_fingerprint(self.udid)
        return self.cache_dir / "runtime" / f"wda-{fingerprint}.lock"


class ManagedProcess(Protocol):
    pid: int
    returncode: int | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[bytes]]
ProcessFactory = Callable[[Sequence[str], Mapping[str, str], BinaryIO], ManagedProcess]
GroupSignaler = Callable[[int, int], None]
GroupProbe = Callable[[int], bool]
Now = Callable[[], datetime]


def _udid_fingerprint(udid: str) -> str:
    canonical = unicodedata.normalize("NFC", udid.strip()).casefold()
    canonical = unicodedata.normalize("NFC", canonical)
    return sha256(canonical.encode()).hexdigest()[:16]


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _run_local_command(argv: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - fixed executable and argv, never a shell
        list(argv),
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=_LOCAL_COMMAND_TIMEOUT_S,
    )


def _start_local_process(
    argv: Sequence[str], env: Mapping[str, str], log: BinaryIO
) -> ManagedProcess:
    return subprocess.Popen(  # noqa: S603 - fixed executable and argv, never a shell
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=dict(env),
        close_fds=True,
        start_new_session=True,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def discover_cached_xctestruns(
    cache_dir: Path = DEFAULT_WDA_CACHE_DIR,
) -> tuple[Path, ...]:
    """Return existing cached candidates for an explicit setup choice.

    Discovery is intentionally limited to the configured cache tree.  Starting
    WDA still requires the caller to put one exact path in ``WDARuntimeConfig``.
    """
    if not cache_dir.is_dir():
        return ()
    return tuple(
        sorted(
            (
                candidate
                for candidate in cache_dir.rglob("*.xctestrun")
                if candidate.is_file() and not candidate.is_symlink()
            ),
            key=lambda candidate: str(candidate),
        )
    )


def discover_xcode_developer_dirs(
    application_dirs: Sequence[Path] | None = None,
) -> tuple[Path, ...]:
    """List existing Xcode developer directories without changing selection."""
    roots = (
        tuple(application_dirs)
        if application_dirs is not None
        else (Path("/Applications"), Path.home() / "Applications")
    )
    found: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for app in root.glob("Xcode*.app"):
            developer_dir = app / "Contents" / "Developer"
            xcodebuild = developer_dir / "usr" / "bin" / "xcodebuild"
            if xcodebuild.is_file() and os.access(xcodebuild, os.X_OK):
                found.add(developer_dir)
    return tuple(sorted(found, key=lambda candidate: str(candidate)))


def _issue(code: str, message: str, remedy: str, *, warning: bool = False) -> WDAIssue:
    return WDAIssue(
        code=code,
        severity=WDAIssueSeverity.WARNING if warning else WDAIssueSeverity.ERROR,
        message=message,
        remedy=remedy,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _executable_available(command: Sequence[str]) -> bool:
    if not command or not command[0].strip():
        return False
    executable = command[0]
    if os.sep in executable:
        path = Path(executable)
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(executable) is not None


def _plausible_bundle_id(bundle_id: str | None) -> bool:
    if bundle_id is None or "." not in bundle_id:
        return False
    return _BUNDLE_ID.fullmatch(bundle_id) is not None


@dataclass(frozen=True)
class _XCTestRunDetails:
    test_root: Path
    host_path: Path
    product_paths: tuple[Path, ...]
    bundle_id: str


def _resolve_test_path(value: str, *, test_root: Path, host_path: Path | None = None) -> Path:
    if value == "__TESTROOT__":
        candidate = test_root
    elif value.startswith("__TESTROOT__/"):
        candidate = test_root / value.removeprefix("__TESTROOT__/")
    elif host_path is not None and value == "__TESTHOST__":
        candidate = host_path
    elif host_path is not None and value.startswith("__TESTHOST__/"):
        candidate = host_path / value.removeprefix("__TESTHOST__/")
    else:
        raw = Path(value)
        candidate = raw if raw.is_absolute() else test_root / raw
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(test_root.resolve(strict=False)):
        raise ValueError("xctestrun path escapes its build-products root")
    return resolved


def _read_xctestrun(path: Path) -> _XCTestRunDetails:
    payload = plistlib.loads(path.read_bytes())
    if not isinstance(payload, dict):
        raise ValueError("xctestrun root must be a dictionary")
    targets = [
        value
        for key, value in payload.items()
        if key != _XCTESTRUN_METADATA_KEY
        and isinstance(value, dict)
        and isinstance(value.get("TestHostPath"), str)
    ]
    if len(targets) != 1:
        raise ValueError("xctestrun must contain one runnable WDA target")
    target = targets[0]
    test_root = path.parent.resolve(strict=False)
    host_value = target.get("TestHostPath")
    bundle_id = target.get("TestHostBundleIdentifier")
    dependent = target.get("DependentProductPaths")
    test_bundle = target.get("TestBundlePath")
    if not isinstance(host_value, str) or not isinstance(bundle_id, str) or not bundle_id:
        raise ValueError("xctestrun target metadata is incomplete")
    if not isinstance(dependent, list) or not all(isinstance(item, str) for item in dependent):
        raise ValueError("xctestrun dependent products are invalid")
    if not isinstance(test_bundle, str):
        raise ValueError("xctestrun test bundle is invalid")
    host_path = _resolve_test_path(host_value, test_root=test_root)
    products = tuple(
        _resolve_test_path(item, test_root=test_root, host_path=host_path) for item in dependent
    ) + (_resolve_test_path(test_bundle, test_root=test_root, host_path=host_path),)
    return _XCTestRunDetails(
        test_root=test_root,
        host_path=host_path,
        product_paths=products,
        bundle_id=bundle_id,
    )


class WDARuntime:
    """Doctor and lifecycle manager for one exact, already-built WDA runner."""

    def __init__(
        self,
        config: WDARuntimeConfig,
        *,
        command_runner: CommandRunner = _run_local_command,
        process_factory: ProcessFactory = _start_local_process,
        group_signaler: GroupSignaler = os.killpg,
        group_probe: GroupProbe = _process_group_exists,
        now: Now = _utc_now,
    ) -> None:
        self.config = config
        self._command_runner = command_runner
        self._process_factory = process_factory
        self._group_signaler = group_signaler
        self._group_probe = group_probe
        self._now = now
        self._process: ManagedProcess | None = None
        self._owned_pgid: int | None = None
        self._log_file: BinaryIO | None = None
        self._process_lock_file: BinaryIO | None = None
        self._state = WDARuntimeState.STOPPED
        self._last_issue: WDAIssue | None = None

    def doctor(self) -> WDADoctorReport:
        """Validate the selected backend and any available signed-build evidence."""
        issues: list[WDAIssue] = []
        native = self.config.backend == "native"
        developer_dir = self.config.developer_dir
        xcodebuild = (
            developer_dir / "usr" / "bin" / "xcodebuild"
            if developer_dir is not None
            else None
        )
        xcode_available = bool(
            xcodebuild is not None
            and xcodebuild.is_file()
            and os.access(xcodebuild, os.X_OK)
        )
        if native:
            if sys.platform != "darwin":
                issues.append(
                    _issue(
                        "native_backend_requires_macos",
                        "The native WDA runner is supported only on macOS.",
                        "Run the native helper lifecycle on the paired Mac.",
                    )
                )
            if not _executable_available(self.config.native_command):
                issues.append(
                    _issue(
                        "native_runner_unavailable",
                        "The configured native WDA runner command is unavailable.",
                        (
                            "Install uv and keep the pinned uvx command, or configure an existing "
                            "Python pymobiledevice3 process command explicitly."
                        ),
                    )
                )
            if not _plausible_bundle_id(self.config.bundle_id):
                issues.append(
                    _issue(
                        "native_bundle_id_invalid",
                        "A plausible installed WDA runner bundle identifier is required.",
                        (
                            "Use the exact bundle identifier of the previously installed signed "
                            "helper."
                        ),
                    )
                )
        elif not xcode_available:
            issues.append(
                _issue(
                    "xcode_unavailable",
                    "The selected Xcode developer directory has no executable xcodebuild.",
                    "Select an existing Xcode developer directory that includes the tvOS platform.",
                )
            )

        if not self.config.udid.strip():
            issues.append(
                _issue(
                    "target_identifier_missing",
                    "An exact target device identifier is required.",
                    "Choose the already-verified Apple TV identifier during local setup.",
                )
            )

        xctestrun_valid = False
        build_products_present = False
        signature_valid = False
        profile_valid = False
        profile_expires_at: datetime | None = None
        profile_days_remaining: int | None = None
        details: _XCTestRunDetails | None = None

        xctestrun = self.config.xctestrun_path
        if xctestrun is None:
            issues.append(
                _issue(
                    "profile_expiry_unknown" if native else "xctestrun_missing",
                    (
                        "No cached build was provided, so the installed helper profile expiry "
                        "cannot be verified."
                        if native
                        else "The selected xctestrun file is unavailable."
                    ),
                    (
                        "Provide the matching cached xctestrun to audit signing and expiration, "
                        "and plan for free profiles to require renewal about every seven days."
                        if native
                        else (
                            "Choose an existing cached tvOS xctestrun produced by the signed "
                            "WDA build."
                        )
                    ),
                    warning=native,
                )
            )
        elif not xctestrun.is_file() or xctestrun.is_symlink():
            issues.append(
                _issue(
                    "xctestrun_missing",
                    "The selected xctestrun file is unavailable.",
                    "Choose an existing cached tvOS xctestrun produced by the signed WDA build.",
                )
            )
        else:
            try:
                details = _read_xctestrun(xctestrun)
                xctestrun_valid = True
            except (OSError, ValueError, plistlib.InvalidFileException):
                issues.append(
                    _issue(
                        "xctestrun_invalid",
                        "The selected xctestrun file is not a usable single-target WDA plan.",
                        "Recreate the signed WDA build and select its generated tvOS xctestrun.",
                    )
                )

        if details is not None:
            if native and details.bundle_id != self.config.bundle_id:
                issues.append(
                    _issue(
                        "native_bundle_mismatch",
                        "The native bundle identifier does not match the cached signed runner.",
                        "Use the exact bundle identifier recorded by the selected xctestrun.",
                    )
                )
            build_products_present = details.host_path.is_dir() and all(
                product.exists() for product in details.product_paths
            )
            if not build_products_present:
                issues.append(
                    _issue(
                        "build_products_missing",
                        "One or more WDA build products referenced by xctestrun are missing.",
                        "Recreate the signed WDA build without moving its generated products.",
                    )
                )
            else:
                signature_valid = self._verify_signature(details.host_path)
                if not signature_valid:
                    issues.append(
                        _issue(
                            "signature_invalid",
                            (
                                "The cached WDA runner does not pass local code-signature "
                                "verification."
                            ),
                            "Rebuild and sign WDA for the exact Apple TV with a valid profile.",
                        )
                    )

                profile_path = details.host_path / "embedded.mobileprovision"
                (
                    profile_valid,
                    profile_expires_at,
                    profile_days_remaining,
                    profile_issues,
                ) = self._inspect_profile(profile_path, details.bundle_id)
                issues.extend(profile_issues)

        ready = not any(item.severity is WDAIssueSeverity.ERROR for item in issues)
        return WDADoctorReport(
            ready=ready,
            issues=tuple(issues),
            xcode_available=xcode_available,
            xctestrun_valid=xctestrun_valid,
            build_products_present=build_products_present,
            signature_valid=signature_valid,
            profile_valid=profile_valid,
            profile_expires_at=profile_expires_at,
            profile_days_remaining=profile_days_remaining,
        )

    def _verify_signature(self, host_path: Path) -> bool:
        try:
            completed = self._command_runner(
                ("/usr/bin/codesign", "--verify", "--deep", "--strict", str(host_path))
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    def _inspect_profile(
        self, profile_path: Path, bundle_id: str
    ) -> tuple[bool, datetime | None, int | None, tuple[WDAIssue, ...]]:
        if not profile_path.is_file() or profile_path.is_symlink():
            return (
                False,
                None,
                None,
                (
                    _issue(
                        "profile_missing",
                        "The signed WDA runner has no readable embedded provisioning profile.",
                        "Rebuild and sign WDA for the exact Apple TV.",
                    ),
                ),
            )
        try:
            completed = self._command_runner(
                ("/usr/bin/security", "cms", "-D", "-i", str(profile_path))
            )
            if completed.returncode != 0:
                raise ValueError("profile decode failed")
            profile = plistlib.loads(completed.stdout)
        except (OSError, ValueError, plistlib.InvalidFileException, subprocess.SubprocessError):
            return (
                False,
                None,
                None,
                (
                    _issue(
                        "profile_unreadable",
                        "The embedded provisioning profile could not be decoded locally.",
                        "Rebuild WDA with a valid Apple development provisioning profile.",
                    ),
                ),
            )
        if not isinstance(profile, dict):
            return (
                False,
                None,
                None,
                (
                    _issue(
                        "profile_unreadable",
                        "The embedded provisioning profile has an invalid structure.",
                        "Rebuild WDA with a valid Apple development provisioning profile.",
                    ),
                ),
            )

        found_issues: list[WDAIssue] = []
        expiration = profile.get("ExpirationDate")
        expires_at = _as_utc(expiration) if isinstance(expiration, datetime) else None
        days_remaining: int | None = None
        now = _as_utc(self._now())
        if expires_at is None:
            found_issues.append(
                _issue(
                    "profile_expiration_missing",
                    "The provisioning profile has no usable expiration date.",
                    "Rebuild WDA with a current Apple development provisioning profile.",
                )
            )
        else:
            seconds_remaining = (expires_at - now).total_seconds()
            days_remaining = max(0, int(seconds_remaining // 86_400))
            if seconds_remaining <= 0:
                found_issues.append(
                    _issue(
                        "profile_expired",
                        "The WDA provisioning profile has expired.",
                        (
                            "Rebuild and re-sign WDA; free development profiles normally "
                            "require renewal every seven days."
                        ),
                    )
                )
            else:
                ttl = profile.get("TimeToLive")
                if (isinstance(ttl, int) and ttl <= 7) or seconds_remaining <= 7 * 86_400:
                    found_issues.append(
                        _issue(
                            "profile_short_lived",
                            (
                                "The WDA provisioning profile is short-lived and will require "
                                "renewal soon."
                            ),
                            (
                                "Plan to rebuild and re-sign WDA before the displayed expiration; "
                                "free development profiles commonly expire after seven days."
                            ),
                            warning=True,
                        )
                    )

        devices = profile.get("ProvisionedDevices")
        if not isinstance(devices, list) or self.config.udid not in devices:
            found_issues.append(
                _issue(
                    "profile_target_mismatch",
                    "The provisioning profile does not include the exact configured Apple TV.",
                    (
                        "Rebuild WDA for the selected device with automatic device registration "
                        "enabled."
                    ),
                )
            )

        entitlements = profile.get("Entitlements")
        application_identifier = (
            entitlements.get("application-identifier") if isinstance(entitlements, dict) else None
        )
        if not isinstance(application_identifier, str) or not application_identifier.endswith(
            f".{bundle_id}"
        ):
            found_issues.append(
                _issue(
                    "profile_bundle_mismatch",
                    "The provisioning profile does not match the WDA test-host bundle identifier.",
                    "Rebuild WDA with the same bundle identifier used by the generated xctestrun.",
                )
            )

        platform = profile.get("Platform")
        if not isinstance(platform, list) or "tvOS" not in platform:
            found_issues.append(
                _issue(
                    "profile_platform_mismatch",
                    "The provisioning profile is not valid for tvOS.",
                    "Rebuild WDA using a tvOS development target and profile.",
                )
            )

        valid = not any(item.severity is WDAIssueSeverity.ERROR for item in found_issues)
        return valid, expires_at, days_remaining, tuple(found_issues)

    def start(self) -> WDARuntimeStatus:
        """Start the exact runner and return without assuming server readiness."""
        current = self.status()
        if self._process is not None and (
            self._process.poll() is None or self._owned_process_group_exists()
        ):
            # A failed stop or exited launcher may leave an owned child alive.
            # Never overwrite that handle and orphan its process group.
            return current
        if current.state in {WDARuntimeState.STARTING, WDARuntimeState.READY}:
            return current

        report = self.doctor()
        blocking = next(
            (item for item in report.issues if item.severity is WDAIssueSeverity.ERROR), None
        )
        if blocking is not None:
            self._state = WDARuntimeState.FAILED
            self._last_issue = blocking
            return self._snapshot()

        lock_issue = self._acquire_process_lock()
        if lock_issue is not None:
            self._state = WDARuntimeState.FAILED
            self._last_issue = lock_issue
            return self._snapshot()

        try:
            log = self._open_private_log()
        except OSError:
            self._release_process_lock()
            self._state = WDARuntimeState.FAILED
            self._last_issue = _issue(
                "log_unavailable",
                "The private WDA runtime log could not be created.",
                (
                    "Ensure the configured cache directory is writable and contains no symlinked "
                    "log path."
                ),
            )
            return self._snapshot()

        env = dict(os.environ)
        if self.config.backend == "native":
            assert self.config.bundle_id is not None
            argv = (
                *self.config.native_command,
                "developer",
                "dvt",
                "xcuitest",
                "--native",
                "--udid",
                self.config.udid,
                "--env",
                "USE_PORT=8100",
                "--env",
                "MJPEG_SERVER_PORT=9100",
                "--env",
                f"WDA_PRODUCT_BUNDLE_IDENTIFIER={self.config.bundle_id}",
                self.config.bundle_id,
            )
        else:
            assert self.config.developer_dir is not None
            assert self.config.xctestrun_path is not None
            xcodebuild = self.config.developer_dir / "usr" / "bin" / "xcodebuild"
            argv = (
                str(xcodebuild),
                "-xctestrun",
                str(self.config.xctestrun_path),
                "-destination",
                f"id={self.config.udid}",
                "test-without-building",
            )
            env["DEVELOPER_DIR"] = str(self.config.developer_dir)
        try:
            process = self._process_factory(argv, env, log)
        except Exception:  # noqa: BLE001 - return only a fixed, redacted issue
            log.close()
            self._log_file = None
            self._release_process_lock()
            self._state = WDARuntimeState.FAILED
            if self.config.backend == "native":
                self._last_issue = _issue(
                    "native_runner_start_failed",
                    "The configured native WDA runner could not be started.",
                    "Run the doctor remedies, then retry the pinned native runner command.",
                )
            else:
                self._last_issue = _issue(
                    "xcodebuild_start_failed",
                    "xcodebuild could not be started.",
                    "Run the doctor remedies, then retry with the selected Xcode installation.",
                )
            return self._snapshot()

        self._process = process
        # _start_local_process creates a new session, so the leader PID is the
        # exact process group owned by this runtime. This lets native uvx and
        # its Python child stop together without matching or killing by name.
        self._owned_pgid = process.pid
        self._state = WDARuntimeState.STARTING
        self._last_issue = None
        if process.poll() is not None:
            self._record_early_exit()
            if not self._owned_process_group_exists():
                self._close_log()
                self._release_process_lock()
        return self._snapshot()

    def mark_ready(self) -> WDARuntimeStatus:
        """Promote a live owned process after an independent HTTP readiness probe."""
        status = self.status()
        if status.state is WDARuntimeState.STARTING and self._process is not None:
            self._state = WDARuntimeState.READY
        return self._snapshot()

    def status(self) -> WDARuntimeStatus:
        if self._process is None:
            return self._snapshot()
        if self._process.poll() is not None:
            if self._state in {WDARuntimeState.STARTING, WDARuntimeState.READY}:
                self._record_early_exit()
            if not self._owned_process_group_exists():
                self._close_log()
                self._release_process_lock()
        return self._snapshot()

    def stop(self) -> WDARuntimeStatus:
        """Stop the exact process group retained by this runtime instance."""
        process = self._process
        if process is None:
            self._owned_pgid = None
            self._state = WDARuntimeState.STOPPED
            self._last_issue = None
            self._close_log()
            self._release_process_lock()
            return self._snapshot()
        group_exists = self._owned_process_group_exists()
        if process.poll() is None or group_exists:
            self._signal_owned_process_group(signal.SIGTERM)
            if not self._wait_for_owned_process_group_exit(self.config.stop_timeout_s):
                self._signal_owned_process_group(signal.SIGKILL)
                if not self._wait_for_owned_process_group_exit(
                    self.config.stop_timeout_s
                ):
                    self._state = WDARuntimeState.FAILED
                    self._last_issue = _issue(
                        "owned_process_stop_failed",
                        "The owned WDA runner process group did not stop within the deadline.",
                        "Inspect the private runtime log and stop this recorded process manually.",
                    )
                    return self._snapshot()
        self._process = None
        self._owned_pgid = None
        self._state = WDARuntimeState.STOPPED
        self._last_issue = None
        self._close_log()
        self._release_process_lock()
        return self._snapshot()

    def _acquire_process_lock(self) -> WDAIssue | None:
        if self._process_lock_file is not None:
            return None
        path = self.config.process_lock_path
        parent = path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if parent.is_symlink() or path.is_symlink():
                raise OSError("unsafe process lock path")
            with suppress(OSError):
                parent.chmod(0o700)
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            lock_file = os.fdopen(descriptor, "r+b", buffering=0)
            try:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_file.close()
                return _issue(
                    "helper_process_in_use",
                    "Another local runtime already owns this exact WDA helper target.",
                    "Use the existing bridge or stop its managed helper before retrying.",
                )
            except (ImportError, OSError) as exc:
                lock_file.close()
                raise OSError("process locking unavailable") from exc
            self._process_lock_file = lock_file
        except OSError:
            return _issue(
                "helper_process_lock_unavailable",
                "The private helper ownership lock could not be created.",
                "Ensure the configured cache runtime directory is private and writable.",
            )
        return None

    def _release_process_lock(self) -> None:
        if self._process_lock_file is not None:
            self._process_lock_file.close()
            self._process_lock_file = None

    def _signal_owned_process_group(self, sig: int) -> None:
        process = self._process
        pgid = self._owned_pgid
        if process is None or pgid is None:
            return
        try:
            self._group_signaler(pgid, sig)
        except OSError:
            # A custom process factory might not establish a process group.
            # Falling back to the one retained process remains narrowly owned.
            if sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()

    def _owned_process_group_exists(self) -> bool:
        pgid = self._owned_pgid
        if pgid is None:
            return False
        try:
            return self._group_probe(pgid)
        except ProcessLookupError:
            return False
        except OSError:
            # Permission errors and unexpected probe failures must not be
            # mistaken for proof that the owned group is gone.
            return True

    def _wait_for_owned_process_group_exit(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            process = self._process
            leader_exited = process is None or process.poll() is not None
            if leader_exited and not self._owned_process_group_exists():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(_PROCESS_GROUP_POLL_INTERVAL_S, remaining))

    def _open_private_log(self) -> BinaryIO:
        path = self.config.resolved_log_path
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent.is_symlink() or path.is_symlink():
            raise OSError("unsafe log path")
        with suppress(OSError):
            parent.chmod(0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        self._log_file = os.fdopen(descriptor, "w+b", buffering=0)
        return self._log_file

    def _record_early_exit(self) -> None:
        native = self.config.backend == "native"
        runner_name = "native WDA runner" if native else "WDA xcodebuild process"
        if self._state is WDARuntimeState.READY:
            self._state = WDARuntimeState.FAILED
            self._last_issue = _issue(
                "native_runner_exited" if native else "xcodebuild_exited",
                f"The confirmed {runner_name} exited.",
                "Inspect the private mode-0600 runtime log and restart the signed runner.",
            )
            return
        self._state = WDARuntimeState.FAILED
        log_tail = self._safe_log_tail_lower()
        if any(token in log_tail for token in (b"provisioning profile", b"code signing")):
            self._last_issue = _issue(
                "native_runner_signing_failed" if native else "xcodebuild_signing_failed",
                f"The {runner_name} exited before readiness because signing was rejected.",
                "Renew the WDA profile or signing setup, then recreate the cached signed build.",
            )
        elif any(
            token in log_tail for token in (b"unable to find a device", b"device is not available")
        ):
            self._last_issue = _issue(
                "native_runner_target_unavailable" if native else "xcodebuild_target_unavailable",
                f"The {runner_name} could not launch on the exact configured Apple TV.",
                "Confirm the existing developer pairing and that the selected Apple TV is online.",
            )
        else:
            self._last_issue = _issue(
                (
                    "native_runner_exited_before_ready"
                    if native
                    else "xcodebuild_exited_before_ready"
                ),
                f"The {runner_name} exited before a ready server was confirmed.",
                "Inspect the private mode-0600 runtime log, renew signing if needed, and retry.",
            )

    def _safe_log_tail_lower(self) -> bytes:
        path = self.config.resolved_log_path
        try:
            if self._log_file is not None:
                self._log_file.flush()
            with path.open("rb") as handle:
                size = path.stat().st_size
                handle.seek(max(0, size - 65_536))
                return handle.read().lower()
        except OSError:
            return b""

    def _snapshot(self) -> WDARuntimeStatus:
        pid = self._process.pid if self._process is not None else None
        return WDARuntimeStatus(
            state=self._state,
            pid=pid,
            log_path=self.config.resolved_log_path,
            issue=self._last_issue,
        )

    def _close_log(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()
