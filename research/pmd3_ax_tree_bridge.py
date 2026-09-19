"""Read-only bridge from pmd3's userspace tunnel to the native AX host PoC.

The Objective-C helper contains no action selectors. This script opens one
exact-UDID ``remoteAXService`` byte stream, proxies it over localhost, and emits
the helper's bounded JSON tree.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import Any

AX_REMOTE_SERVICE = "com.apple.accessibility.axAuditDaemon.remoteAXService"


async def close_safely(value: Any) -> None:
    with suppress(Exception):
        await value.close()


async def select_exact(services: list[Any], udid: str) -> Any:
    selected = None
    for candidate in services:
        with suppress(Exception):
            if str(candidate.remote_identifier) == udid and selected is None:
                selected = candidate
                continue
        await close_safely(candidate)
    if selected is None:
        raise RuntimeError("exact observer identity unavailable")
    return selected


async def relay(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    byte_counts: dict[str, int],
    direction: str,
) -> None:
    try:
        while chunk := await reader.read(65536):
            byte_counts[direction] += len(chunk)
            writer.write(chunk)
            await writer.drain()
    except (BrokenPipeError, ConnectionError):
        pass


async def probe(udid: str, helper: Path, timeout: float) -> int:
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

        advertised = (rsd.peer_info or {}).get("Services", {})
        if AX_REMOTE_SERVICE not in advertised:
            raise RuntimeError("remoteAXService is not advertised")
        print(
            "observer",
            {
                "udid_fingerprint": hashlib.sha256(udid.encode()).hexdigest()[:12],
                "service": AX_REMOTE_SERVICE,
            },
        )

        remote = await rsd.start_lockdown_service_without_checkin(AX_REMOTE_SERVICE)
        await remote._ensure_started()  # noqa: SLF001 - research transport bridge
        stack.push_async_callback(remote.close)

        local_connection: asyncio.Future[
            tuple[asyncio.StreamReader, asyncio.StreamWriter]
        ] = asyncio.get_running_loop().create_future()

        async def accept_once(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            if local_connection.done():
                writer.close()
                await writer.wait_closed()
                return
            local_connection.set_result((reader, writer))

        server = await asyncio.start_server(accept_once, "127.0.0.1", 0)
        stack.push_async_callback(server.wait_closed)
        stack.callback(server.close)
        port = int(server.sockets[0].getsockname()[1])

        process = await asyncio.create_subprocess_exec(
            str(helper),
            "--port",
            str(port),
            "--timeout",
            str(timeout),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        local_reader, local_writer = await asyncio.wait_for(
            local_connection, timeout=5.0
        )
        stack.callback(local_writer.close)

        byte_counts = {"host_to_tv": 0, "tv_to_host": 0}
        relays = [
            asyncio.create_task(
                relay(local_reader, remote.writer, byte_counts, "host_to_tv")
            ),
            asyncio.create_task(
                relay(remote.reader, local_writer, byte_counts, "tv_to_host")
            ),
        ]
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout + 10.0
            )
        finally:
            for task in relays:
                task.cancel()
            await asyncio.gather(*relays, return_exceptions=True)

        if stdout:
            print(stdout.decode(errors="replace").rstrip())
        if stderr:
            print(stderr.decode(errors="replace").rstrip())
        print("transport_bytes", byte_counts)
        return int(process.returncode or 0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--udid", required=True)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()
    if not args.helper.is_file():
        parser.error("--helper must name the compiled native helper")
    raise SystemExit(asyncio.run(probe(args.udid.strip(), args.helper, args.timeout)))


if __name__ == "__main__":
    main()
