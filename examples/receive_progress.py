#!/usr/bin/env python3
"""Print ComfyUI Progress Bridge UDP events as newline-delimited JSON."""

from __future__ import annotations

import argparse
import json
import socket


def bridge_port(comfy_port: int) -> int:
    return 30000 + (comfy_port % 1000)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    parser.add_argument("--comfy-port", type=int, default=8188)
    args = parser.parse_args()
    port = args.port if args.port is not None else bridge_port(args.comfy_port)

    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind((args.host, port))
    print(json.dumps({"listening": f"udp://{args.host}:{port}"}), flush=True)

    while True:
        payload, _ = receiver.recvfrom(65535)
        try:
            event = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
