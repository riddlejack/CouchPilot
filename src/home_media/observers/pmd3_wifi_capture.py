"""Optional GPL worker: Wi-Fi remote-pair userspace TCP screenshot.

Invoked only as a subprocess by home-media core (never imported by core logic).
Requires Python >= 3.13 for native TLS-PSK (tvOS 26 / iOS 18.2+ removed QUIC).

Usage:
  <python3.14> -m home_media.observers.pmd3_wifi_capture --udid <id> --out <png>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


async def capture(udid: str, out: Path) -> None:
    from pymobiledevice3.remote import tunnel_service
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot

    services = await tunnel_service.get_remote_pairing_tunnel_services(udid=udid)
    if not services:
        print("no_remote_pairing_tunnel_service", file=sys.stderr)
        raise SystemExit(3)
    # Prefer IPv4; never fall back to an unbound/other device.
    ordered = sorted(services, key=lambda s: (1 if ":" in str(s.hostname) else 0))
    svc = ordered[0]
    tunnel_service.USE_USERSPACE_TUNNEL = True
    try:
        async with svc.start_tcp_tunnel() as tunnel_result:
            tun = tunnel_result.client.tun
            tun.set_peer(tunnel_result.address)
            async with UserspaceDialPlane(tun, tunnel_result.address) as dial_plane:
                rsd = RemoteServiceDiscoveryService(
                    (tunnel_result.address, tunnel_result.port),
                    open_connection=dial_plane.dial,
                )
                await rsd.connect()
                async with DvtProvider(rsd) as dvt, Screenshot(dvt) as screenshot:
                    png = await screenshot.get_screenshot()
                out.write_bytes(png)
                await rsd.close()
    finally:
        tunnel_service.USE_USERSPACE_TUNNEL = False
        await svc.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pmd3_wifi_capture")
    parser.add_argument("--udid", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.udid.strip():
        print("empty_udid", file=sys.stderr)
        return 2
    if sys.version_info < (3, 13):
        print("python_ge_313_required_for_tvos_tcp_psk", file=sys.stderr)
        return 4
    try:
        asyncio.run(capture(args.udid.strip(), args.out))
    except Exception as exc:  # noqa: BLE001 — worker surface
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
