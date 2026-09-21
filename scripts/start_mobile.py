"""Start the existing single-user service for phones on a trusted local network."""

import argparse
import ipaddress
import socket

import uvicorn
from src.config import Settings


def lan_addresses() -> list[str]:
    addresses = {item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    return sorted(
        a for a in addresses if ipaddress.ip_address(a).is_private and not ipaddress.ip_address(a).is_loopback
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    settings = Settings()
    if settings.api_access_token.get_secret_value() or not settings.enable_ui:
        raise SystemExit(
            "The APK uses /ui/. This configuration has disabled the UI; use an authenticated web deployment instead."
        )
    print("Mobile access: phone and PC must use the same trusted Wi-Fi.", flush=True)
    print(
        "This is a single-user service shared with devices on this network. Keep this window open.",
        flush=True,
    )
    for address in lan_addresses():
        print(f"Server address for APK: http://{address}:{args.port}", flush=True)
    print(
        "If blocked by Windows Firewall, allow this Python server on your private network only.", flush=True
    )
    uvicorn.run("src.api.app:app", host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
