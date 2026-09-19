"""Read-only tvOS AccessibilityAudit transport probe.

This optional GPL-boundary research script opens the already-paired, exact-UDID
userspace tunnel, prints only redacted service metadata, and tests the
RemoteXPC *transport handshake* for ``remoteAXService``. It also tries the raw
DTX transport used by the tvOS 26 daemon and reads only ``deviceAPIVersion``.
It never changes a setting, moves focus, or presses UI.

Run inside the project's separate pymobiledevice3 environment:

    .venv-pmd3/bin/python research/pmd3_ax_transport_probe.py --udid "$UDID"
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import time
from contextlib import AsyncExitStack, suppress
from typing import Any

AX_REMOTE_XPC = "com.apple.accessibility.axAuditDaemon.remoteAXService"
AX_DTX_SHIM = "com.apple.accessibility.axAuditDaemon.remoteserver.shim.remote"


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def response_shape(value: Any) -> dict[str, Any]:
    """Describe a DTX result without dumping screen bytes or household data."""
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "length": len(value),
            "sha256": hashlib.sha256(value).hexdigest()[:12],
        }
    if isinstance(value, dict):
        return {"type": "dict", "keys": sorted(map(str, value))[:30]}
    if isinstance(value, (list, tuple)):
        return {"type": type(value).__name__, "length": len(value)}
    return {"type": type(value).__name__}


async def close_safely(value: Any) -> None:
    with suppress(Exception):
        await value.close()


async def select_exact(services: list[Any], udid: str) -> Any:
    selected = None
    for candidate in services:
        try:
            matches = str(candidate.remote_identifier) == udid
        except Exception:
            matches = False
        if matches and selected is None:
            selected = candidate
        else:
            await close_safely(candidate)
    if selected is None:
        raise RuntimeError("exact observer identity unavailable")
    return selected


async def probe(udid: str) -> None:
    from pymobiledevice3.dtx import DTXConnection
    from pymobiledevice3.dtx.service import DTXControlService, dtx_on_invoke
    from pymobiledevice3.remote import tunnel_service
    from pymobiledevice3.remote.remote_service_discovery import (
        RemoteServiceDiscoveryService,
    )
    from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane

    services = await tunnel_service.get_remote_pairing_tunnel_services(udid=udid)
    selected = await select_exact(services, udid)
    async with AsyncExitStack() as stack:
        stack.push_async_callback(selected.close)
        stack.callback(setattr, tunnel_service, "USE_USERSPACE_TUNNEL", False)
        tunnel_service.USE_USERSPACE_TUNNEL = True

        tunnel = await stack.enter_async_context(selected.start_tcp_tunnel())
        tunnel.client.tun.set_peer(tunnel.address)
        dial_plane = await stack.enter_async_context(
            UserspaceDialPlane(tunnel.client.tun, tunnel.address)
        )
        rsd = RemoteServiceDiscoveryService(
            (tunnel.address, tunnel.port),
            open_connection=dial_plane.dial,
        )
        await rsd.connect()
        stack.push_async_callback(rsd.close)

        assert rsd.peer_info is not None
        properties = rsd.peer_info.get("Properties", {})
        print(
            "device",
            {
                "udid_fingerprint": fingerprint(str(rsd.udid)),
                "product_type": properties.get("ProductType"),
                "os_version": properties.get("OSVersion"),
            },
        )

        advertised = rsd.peer_info.get("Services", {})
        for name in (AX_REMOTE_XPC, AX_DTX_SHIM):
            metadata = advertised.get(name)
            if metadata is None:
                print("service", {"name": name, "advertised": False})
                continue
            service_properties = metadata.get("Properties", {})
            print(
                "service",
                {
                    "name": name,
                    "advertised": True,
                    "uses_remote_xpc": bool(service_properties.get("UsesRemoteXPC")),
                    "property_keys": sorted(service_properties),
                },
            )

        direct = advertised.get(AX_REMOTE_XPC)
        if direct is None:
            return
        if not direct.get("Properties", {}).get("UsesRemoteXPC", False):
            print("remote_xpc_handshake", "skipped_not_advertised_as_remote_xpc")
            return

        connection = rsd.start_remote_service(AX_REMOTE_XPC)
        try:
            await asyncio.wait_for(connection.connect(), timeout=5.0)
        except TimeoutError:
            print("remote_xpc_handshake", "timeout")
        except Exception as exc:
            print("remote_xpc_handshake", f"failed:{type(exc).__name__}")
        else:
            print("remote_xpc_handshake", "ok")
        finally:
            await close_safely(connection)

        # tvOS 26's axauditd passes the accepted RSD socket directly to
        # DTXSocketTransport, despite advertising UsesRemoteXPC in the service
        # map. Probe that protocol without enabling the AX cache or invoking UI.
        class AXHostControlService(DTXControlService):
            @dtx_on_invoke("hostAPIVersion")
            async def host_api_version(self) -> int:
                return 1

        raw = await rsd.start_lockdown_service_without_checkin(AX_REMOTE_XPC)
        dtx = None
        try:
            await raw._ensure_started()  # noqa: SLF001 - research-only transport probe
            dtx = DTXConnection(raw.reader, raw.writer)
            dtx.sent_capabilities = None
            control_context = dtx.ctx.child(channel=dtx._ctrl_channel)  # noqa: SLF001
            dtx._control_svc = AXHostControlService(control_context)  # noqa: SLF001
            dtx._services[0] = dtx._control_svc  # noqa: SLF001
            await asyncio.wait_for(dtx.connect(), timeout=5.0)
            version = await asyncio.wait_for(
                dtx._ctrl_channel.invoke("deviceAPIVersion"),  # noqa: SLF001
                timeout=5.0,
            )
        except TimeoutError:
            print("raw_dtx_probe", "timeout")
        except Exception as exc:
            print("raw_dtx_probe", f"failed:{type(exc).__name__}")
        else:
            print("raw_dtx_probe", {"connected": True, "device_api_version": version})
        finally:
            if dtx is not None:
                await dtx.aclose()
            else:
                await close_safely(raw)

        # The legacy DTX shim still advertises XADAuditServer, including its
        # read-only screenshot selector. Probe only version and screenshot.
        from pymobiledevice3.services.accessibilityaudit import AccessibilityAudit

        audit = AccessibilityAudit(rsd)
        try:
            await asyncio.wait_for(audit._provider.connect(), timeout=18.0)  # noqa: SLF001
            shim_version = await asyncio.wait_for(
                audit._invoke("deviceApiVersion"),  # noqa: SLF001
                timeout=5.0,
            )
            started = time.perf_counter()
            screenshot = await asyncio.wait_for(
                audit._invoke("deviceCaptureScreenshot"),  # noqa: SLF001
                timeout=10.0,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
        except TimeoutError:
            print("legacy_dtx_shim", "timeout")
        except Exception as exc:
            print("legacy_dtx_shim", f"failed:{type(exc).__name__}")
        else:
            print(
                "legacy_dtx_shim",
                {
                    "device_api_version": shim_version,
                    "screenshot_latency_ms": elapsed_ms,
                    "screenshot": response_shape(screenshot),
                },
            )
        finally:
            await audit.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--udid", required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.udid.strip()))


if __name__ == "__main__":
    main()
