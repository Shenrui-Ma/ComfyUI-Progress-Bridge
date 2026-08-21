#!/usr/bin/env python3
"""Print ComfyUI Progress Bridge UDP events as newline-delimited JSON."""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket


def accepts_event(event: object) -> bool:
    """Return whether a decoded JSON value is a schema-v2 event object."""
    return isinstance(event, dict) and event.get("schema") == 2


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=30999)
    args = parser.parse_args()
    host = str(ipaddress.IPv4Address(args.host))

    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind((host, args.port))
    print(json.dumps({"listening": f"udp://{host}:{args.port}", "schema": 2}), flush=True)

    while True:
        payload, _ = receiver.recvfrom(65535)
        try:
            event = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if accepts_event(event):
            print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
