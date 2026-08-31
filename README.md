# ComfyUI Progress Bridge

A lightweight, server-side [ComfyUI](https://github.com/comfyanonymous/ComfyUI) extension that mirrors live execution events to an external visual progress monitor over UDP.

It is designed for desktop companions, status docks, dashboards, and other observers that need the real active node and sampler progress without hijacking the WebSocket used by the client that submitted the prompt.

<p align="center">
  <img src="docs/images/comfyui-progress-dock.png" width="528" alt="ComfyUI Progress Bridge desktop dock showing queue, active node, and sampler progress">
</p>
<p align="center"><sub>Transparent desktop dock with live queue, node, and sampler progress.</sub></p>

## Why this exists

ComfyUI normally sends `executing` and `progress` WebSocket events to the submitting client's `client_id`. A second WebSocket opened by an external monitor therefore may see the queue but miss node-level events.

This extension preserves ComfyUI's original delivery and emits a second, best-effort UDP copy on loopback:

```text
ComfyUI PromptServer.send_sync
        ├── original WebSocket delivery (unchanged)
        └── compact UDP event → external visual monitor
```

## Features

- Mirrors `executing`, `progress`, `execution_error`, `execution_interrupted`, and `execution_success`
- Preserves the original `PromptServer.send_sync` return value and client routing
- Sends only a compact allowlist of useful fields
- Uses loopback by default; no network service is exposed
- Automatically starts one native PyQt6 desktop progress dock when ComfyUI imports the extension
- Is idempotent and fail-open: monitoring or desktop-launch errors never stop ComfyUI execution
- Provides a server extension only; it intentionally adds no workflow node

## Installation

```bash
cd /path/to/ComfyUI/custom_nodes
git clone https://github.com/Shenrui-Ma/ComfyUI-Progress-Bridge.git
cd ComfyUI-Progress-Bridge
python -m pip install -r requirements.txt
```

ComfyUI Manager installs `requirements.txt` automatically; the final command is only needed for a
manual clone and must use the same Python environment that runs ComfyUI.

Restart that ComfyUI instance. On a desktop system, importing the custom node now starts the
progress dock automatically—no workflow node, separate terminal command, or first inference is
required. Its startup log should include, for example:

```text
[ComfyUI Progress Bridge] schema 2 UDP 127.0.0.1:30999
[ComfyUI Progress Bridge] desktop launch requested
```

The importing ComfyUI HTTP port is passed to the dock automatically, including custom ports such
as `8189`; this launch-time endpoint does not overwrite saved desktop settings. A secure per-endpoint
lock prevents repeated imports from opening duplicate dock windows while allowing different local
ComfyUI ports to have their own monitors.

For a headless server, or when a separately managed monitor is preferred, disable automatic UI
startup before launching ComfyUI:

```bash
COMFY_PROGRESS_BRIDGE_AUTOSTART=0 python main.py --port 8188
```

Every ComfyUI process sends to the shared loopback listener on UDP port `30999`.
The schema-v2 envelope carries the exact ComfyUI HTTP port and a process instance UUID,
so ports such as `8189` and `9189` and identical prompt IDs cannot collide.

> Each running ComfyUI process must be restarted once after installation. Wait for `running=0` and `pending=0` before restarting a production instance.

## Receive events

Run the included receiver:

```bash
python examples/receive_progress.py
```

Then submit a normal ComfyUI workflow. The receiver prints newline-delimited JSON such as:

```json
{"schema":2,"endpoint":{"host":"127.0.0.1","port":8189},"instance_id":"70f12e92-a03d-4770-b080-1f90c9f1ed88","sequence":1,"observed_at":1787414400.0,"type":"executing","data":{"prompt_id":"abc","node":"4"}}
{"schema":2,"endpoint":{"host":"127.0.0.1","port":8189},"instance_id":"70f12e92-a03d-4770-b080-1f90c9f1ed88","sequence":2,"observed_at":1787414400.1,"type":"progress","data":{"prompt_id":"abc","node":"24","value":9,"max":12}}
```

An external monitor can join `prompt_id` and `node` with the workflow graph returned by ComfyUI's `/queue` endpoint to display friendly stages such as model loading, sampling, VAE decode, and saving.

## Configuration

Environment variables must be set before starting ComfyUI:

- `COMFY_PROGRESS_BRIDGE_HOST`: numeric IPv4 UDP destination, default `127.0.0.1` (hostnames are rejected to prevent DNS work on the event path)
- `COMFY_PROGRESS_BRIDGE_PORT`: explicit UDP destination port, default `30999`
- `COMFY_PROGRESS_BRIDGE_ENDPOINT_HOST`: numeric IPv4 host recorded in `endpoint.host`, default `127.0.0.1`

Older releases derived a separate UDP port from each HTTP port. Schema v2 intentionally
replaces that behavior with one shared listener. Existing deployments that require a fixed
legacy destination can set `COMFY_PROGRESS_BRIDGE_PORT` explicitly, but receivers must read
the schema-v2 envelope.

Example:

```bash
COMFY_PROGRESS_BRIDGE_PORT=41000 python main.py --port 8189
```

## Event contract

Only these event types are mirrored:

- `executing`
- `progress`
- `execution_error`
- `execution_interrupted`
- `execution_success`

Only these data fields are eligible for forwarding:

- `prompt_id`
- `node`, `node_id`, `display_node`
- `value`, `max`
- `exception_message`, `node_type`

Binary previews, workflow inputs, prompts, model names, images, and generated media are not forwarded.
Each datagram is capped at 8192 bytes. `prompt_id` is limited to 256 characters, ordinary
forwarded strings to 1024 characters, and error text to 4096 characters.

## Security and compatibility

- The default destination is loopback. Setting a non-loopback destination can expose prompt IDs, node IDs, progress, and error messages to that network destination.
- UDP is intentionally best-effort. A dropped progress packet does not affect inference.
- The implementation wraps an internal ComfyUI method, `PromptServer.send_sync`. The wrapper is small and covered by tests, but upstream internal API changes may require an update.
- This repository targets Python 3.10+ and current ComfyUI releases.

## Development

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
```

## Comfy Registry

The repository follows the standard ComfyUI custom-node layout and includes PEP 621 metadata. Registry-specific `[tool.comfy]` publisher metadata is intentionally not guessed; it should be added after the owner creates a Publisher ID at [Comfy Registry](https://registry.comfy.org/).

## License

MIT
