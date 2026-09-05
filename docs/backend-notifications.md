# Backend queue-drained notifications

The bridge can send Telegram and/or Weixin notifications directly from the Python process that runs
ComfyUI. This path does **not** use the desktop companion, Hermes, an LLM, a workflow node, or a
browser. It is disabled unless the user explicitly enables it in the desktop settings or the host
administrator selects a standalone local config file.

## Local desktop binding

1. Create a dedicated owner-private credential file. It is inert `KEY=value` data and is never
   evaluated as shell code:

   ```text
   TELEGRAM_BOT_TOKEN=replace-me
   WEIXIN_TOKEN=replace-me
   ```

2. Open the progress dock settings and find **Backend queue-complete notifications**. Enable the
   backend itself, then configure Telegram and Weixin independently. Each platform has its own test
   button. QQ remains desktop-only.
3. Point the backend credential field at the file from step 1, save, and restart ComfyUI.

The desktop saves this opt-in under its existing mode-0600 settings file. Tokens and context tokens
are not copied into that JSON. A disabled or missing backend section does not create a sender or
worker. Changing backend settings takes effect on the next ComfyUI start.

## Headless or remote host binding

1. Create an owner-private credentials file on the ComfyUI host. The existing notification sender
   reads this inert `KEY=value` file; it is never evaluated as shell code.

   ```text
   TELEGRAM_BOT_TOKEN=replace-me
   WEIXIN_TOKEN=replace-me
   ```

2. Create an owner-private backend config on the same host. Tokens do not belong in this JSON:

   ```json
   {
     "enabled": true,
     "name": "Render host",
     "language": "en-US",
     "credentials_file": "/home/comfy/.config/comfyui-progress-bridge/credentials.env",
     "timeout": 10,
     "telegram": {
       "enabled": true,
       "chat_id": "-1001234567890",
       "thread_id": null
     },
     "weixin": {
       "enabled": false,
       "account_id": "",
       "target": "",
       "context_store": ""
     }
   }
   ```

3. Restrict both files and select the config before starting ComfyUI:

   ```bash
   chmod 600 /home/comfy/.config/comfyui-progress-bridge/{backend-notifications.json,credentials.env}
   COMFY_PROGRESS_BRIDGE_BACKEND_CONFIG=/home/comfy/.config/comfyui-progress-bridge/backend-notifications.json \
     python main.py --port 8188
   ```

`COMFY_PROGRESS_BRIDGE_BACKEND_CONFIG` takes priority over desktop-managed settings. If the selected
JSON contains `"enabled": false`, no backend notification sender or worker is installed. The config
and credential file must be regular, non-symlink, owner-private files owned by the current user.
Relative `credentials_file` and `context_store` paths are resolved relative to the selected file.

For a remote machine, create and test these files on that machine. A desktop settings file on a
different computer is never copied to the server automatically. Keep the environment variable in
the service definition or launch script that starts that specific ComfyUI instance.

Supported languages are `en-US`, `zh-CN`, `ja-JP`, and `ko-KR`. At least one of Telegram or Weixin
must be enabled. Backend mode intentionally does not enable QQ.

The backend reads credentials exclusively from its configured private file; process-level
Telegram/Weixin environment variables cannot silently override that file. Missing platform
credentials, targets or context tokens fail that platform's send without disabling the other
platform. Shared JSON/schema and credential-file permission errors still disable backend setup.
`enabled` in the startup log means the observer is installed, not that delivery was confirmed.

## Queue semantics and safety

The backend wrapper observes the `status` payloads already emitted through
`PromptServer.send_sync`. A positive `status.exec_info.queue_remaining` arms a busy epoch; the first
subsequent zero enqueues exactly one completion notification. Initial zero, repeated zero, malformed
status, and unrelated events do nothing. A later positive value starts a new epoch.

The original `send_sync` call runs first, with its arguments, return value, and WebSocket routing
unchanged. Notification HTTP work runs in one bounded daemon worker and never in the callback.
Configuration, queueing, and sender failures are fail-open so they cannot prevent ComfyUI startup or
execution. Logs report only enabled/disabled state and never config contents, credentials, API
responses, or target IDs.

Weixin delivery uses the existing `NotificationSender` with its current protocol-aware behavior.
The successful live iLink response is identified by its bounded `message_id` (or a verified zero
business code). Uniform stale-session responses retry once without `context_token`; contradictory
business codes fail closed; `prepare failed` maps to a missing-context-token result; and explicit
rate-limit responses use bounded backoff. The adapter never calls `getupdates`, so it cannot compete
with a gateway poller.

Do not put tokens, chat IDs, or notification configuration in workflow JSON. Keep both local files
out of source control and provision them independently on every ComfyUI host.
