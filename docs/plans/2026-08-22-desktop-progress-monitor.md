# Desktop Progress Monitor Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Turn the server-side ComfyUI event bridge into a complete multi-process desktop progress monitor with a configurable four-language UI, avatars, completion notifications, and sound.

**Architecture:** All ComfyUI instances send schema-v2 envelopes to one shared loopback UDP listener; envelopes carry HTTP port, process instance UUID, and sequence number, so ports and prompt IDs cannot collide. A transport-neutral reducer owns endpoint-qualified queue/task state and emits one-shot queue-drained transitions. A standalone frameless PyQt6 desktop window renders one card per endpoint and owns settings, notifications, audio, drag/restore, and avatar rotation without making the existing Silverwolf overlay interactive.

**Tech Stack:** Python 3.10+, stdlib networking/JSON/dataclasses, PyQt6, pytest, Ruff, GitHub Actions.

---

### Task 1: Schema-v2 shared event transport

**Objective:** Eliminate UDP binding collisions and identify every event by Comfy process instance.

**Files:**
- Modify: `comfyui_progress_bridge/bridge.py`
- Modify: `comfyui_progress_bridge/__init__.py`
- Modify: `examples/receive_progress.py`
- Modify: `tests/test_progress_bridge.py`

**Steps:**
1. Write failing tests asserting ports 8189 and 9189 share one listener safely because each envelope includes its exact `endpoint.port`, unique `instance_id`, monotonic `sequence`, `schema=2`, and timestamp.
2. Add `execution_success` to supported terminal events.
3. Change the default target to shared loopback UDP `30999`; retain an explicit environment override.
4. Preserve the 8192-byte datagram limit, allowlist, idempotence, fail-open behavior, original WebSocket routing, and return value.
5. Update the receiver example and compatibility docs.
6. Run `uv run pytest tests/test_progress_bridge.py -q` and `uv run ruff check .`.

### Task 2: Endpoint-qualified reducer and retention

**Objective:** Model multiple Comfy processes without shared prompt or queue state.

**Files:**
- Create: `comfyui_progress_bridge/monitor/models.py`
- Create: `comfyui_progress_bridge/monitor/stages.py`
- Create: `comfyui_progress_bridge/monitor/reducer.py`
- Test: `tests/test_monitor_reducer.py`

**Steps:**
1. Write failing tests for identical prompt IDs on two endpoints, one endpoint draining while another stays busy, offline versus empty, duplicate terminal packets, stale sequence rejection, process UUID changes, and 30-second terminal retention.
2. Define immutable `EndpointId(host, port, instance_id)` and endpoint-qualified task keys.
3. Implement pure snapshot/event reducers and transition objects.
4. Emit `queue_completed` exactly once on authoritative online busy→empty snapshots.
5. Preserve terminal success/error/interrupted cards for 30 seconds and expire deterministically from an injected clock.
6. Emit semantic stage keys rather than hard-coded Chinese.
7. Run reducer tests and full suite.

### Task 3: Concurrent remote probe and SSH monitor

**Objective:** Feed the reducer with complete per-endpoint snapshots and shared UDP events.

**Files:**
- Create: `comfyui_progress_bridge/monitor/remote_probe.py`
- Create: `comfyui_progress_bridge/monitor/source.py`
- Test: `tests/test_remote_probe.py`
- Test: `tests/test_source.py`

**Steps:**
1. Write failing tests for endpoint demultiplexing, explicit offline records, concurrent polling, schema hello, malformed NDJSON, and restartable source lifecycle.
2. Poll `/queue` concurrently per endpoint and emit explicit online/offline snapshots.
3. Bind one UDP socket on 30999 and route events by envelope endpoint metadata.
4. Trigger immediate queue reconciliation after terminal events.
5. Support local subprocess and persistent SSH source commands without putting secrets in arguments.
6. Run tests and compile checks.

### Task 4: Settings, localization, and native desktop UI

**Objective:** Provide the configurable standalone progress window.

**Files:**
- Create: `comfyui_progress_bridge/desktop/settings.py`
- Create: `comfyui_progress_bridge/desktop/i18n.py`
- Create: `comfyui_progress_bridge/desktop/widgets.py`
- Create: `comfyui_progress_bridge/desktop/app.py`
- Create: `comfyui_progress_bridge/desktop/__main__.py`
- Test: `tests/test_settings.py`
- Test: `tests/test_i18n.py`
- Test: `tests/test_desktop_ui.py`

**Steps:**
1. Write failing tests for Chinese/Japanese/English/Korean strings, secure atomic settings persistence, invalid config recovery, and offscreen widget behavior.
2. Render one non-overlapping card per endpoint in a bounded scroll area.
3. Add simple mode (endpoint and queue counts only) and professional mode (stage/node/steps/progress/connectivity).
4. Elide long node/stage text with a middle/right ellipsis and fixed card bounds.
5. Add color/theme choice, opacity slider, collapse button, UI enable switch, drag handle, persisted per-screen position, and reset-to-default with screen clamping.
6. Put one optional circular avatar only on the top card; rotate among configured PNG files after each completed task; provide enable/disable and file-picker controls.
7. Run offscreen UI tests and manually inspect a synthetic multi-port demo.

### Task 5: Notifications and queue-complete audio

**Objective:** Send one-shot completion notifications and optional sounds without competing with Hermes gateway pollers.

**Files:**
- Create: `comfyui_progress_bridge/desktop/notifications.py`
- Create: `comfyui_progress_bridge/desktop/audio.py`
- Modify: `comfyui_progress_bridge/desktop/widgets.py`
- Test: `tests/test_notifications.py`
- Test: `tests/test_audio.py`

**Steps:**
1. Write HTTP-mock tests for Telegram `sendMessage`, Weixin iLink `sendmessage`, QQ access-token and explicit target endpoints, redaction, disabled backends, and network errors.
2. Read secrets from the app's mode-0600 settings or optionally from Hermes environment/account stores; never print tokens.
3. For Weixin, reuse persisted context tokens and never call `getupdates`.
4. Add per-platform enable switches and UI test buttons.
5. Add disabled/ding/custom-voice queue-complete modes and a master audio switch.
6. Fire notifications/audio only from reducer `queue_completed`, once per endpoint busy epoch.
7. Run unit tests, then send one explicit live Telegram test and one Weixin test through the new adapter; QQ remains mocked until credentials exist.

### Task 6: Local Silverwolf asset preset

**Objective:** Configure the user's installation with six local avatars and one non-autoplay Index-TTS voice preset.

**Files:**
- Local data only: `~/.config/comfyui-progress-bridge/avatars/*.png`
- Local data only: `~/.config/comfyui-progress-bridge/audio/queue-complete-silverwolf.wav`
- Modify: `.gitignore`
- Modify: `README.md`

**Steps:**
1. Downscale six existing local Silverwolf expression PNGs to small RGBA assets without adding copyrighted files to the public repository.
2. Generate exactly `您的当前队列运行完毕喵` using the Silverwolf Index-TTS API and six-second reference without invoking playback.
3. Verify output codec, duration, size, and nonempty path.
4. Point local settings at the deterministic default avatar and voice preset.
5. Document generic local avatar/audio overrides and copyright-safe public defaults.

### Task 7: Deployment and real integration

**Objective:** Replace the duplicated old bridge, deploy the shared probe/UI, and verify real ports.

**Files:**
- Deploy packaged bridge to each monitored ComfyUI `custom_nodes` directory on server 41.
- Deploy `monitor/remote_probe.py` to a versioned user path on server 41.
- Create a macOS LaunchAgent for the standalone desktop monitor.
- Disable the legacy embedded Silverwolf Comfy dock after successful cutover.

**Steps:**
1. Query every configured `/queue`; restart each Comfy process only when its own running and pending counts are zero.
2. Verify every process logs schema-v2 bridge startup and retains HTTP 200 health.
3. Start the standalone UI and verify at least two simultaneous endpoint cards never overlap or overwrite identical prompt IDs.
4. Run real short workflows and capture named node, sampler progress, terminal state, 30-second retention, avatar rotation, and one queue-drained notification/audio event.
5. Verify Silverwolf/Yvonne/Rei overlays and Hermes gateways remain untouched.

### Task 8: Final review and release

**Objective:** Ship a verified public release.

**Files:**
- Create: `.github/workflows/tests.yml`
- Modify: `README.md`
- Modify: `pyproject.toml`

**Steps:**
1. Run all tests on available Python versions, Ruff, compileall, build wheel/sdist, and isolated wheel import.
2. Run independent specification review, then independent security/quality review; fix all blocking findings.
3. Add GitHub Actions now that the PAT has `workflow` scope.
4. Commit with a verified feature commit, push `main`, and verify remote SHA and Actions status.
