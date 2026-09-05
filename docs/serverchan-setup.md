# WeChat via ServerChan Turbo / Server酱微信通知

This optional channel sends queue-complete notifications from the **ComfyUI backend**.
It does not need Hermes, an LLM, or an open desktop window. ServerChan is a third-party
relay, not a Tencent service. Notification text passes through that relay; the plugin
does not include workflows, prompts or generated media. Internet access to
`https://sctapi.ftqq.com` is required. Service quotas and WeChat delivery restrictions apply.

这是可选的 **ComfyUI 后端队列完成通知**通道，不依赖 Hermes、LLM 或常驻桌面窗。
Server酱是第三方中转服务，通知文本会经过它；插件不发送工作流、提示词或生成媒体。
运行 ComfyUI 的机器必须能访问上述 HTTPS 地址，仍受服务额度及微信通道限制。

## First-time setup / 首次设置

1. Open [ServerChan Turbo](https://sct.ftqq.com/sendkey/), sign in and bind a WeChat
   reception channel there. Copy your own **SCT-prefixed SendKey**. ServerChan³ `sctp…`
   keys belong to a different service and are not supported by this channel.
2. Open the native progress dock's **Settings**, scroll to **Backend queue-complete
   notifications**, and enable **WeChat via ServerChan Turbo**. This also turns on
   the backend master switch. The legacy Telegram/Weixin credential-file field is
   not needed for ServerChan-only use.
3. Paste the key into **SendKey (SCT…)**. Click **Test notification · Server酱**
   if you want to send one test now; testing does not save the entered key.
4. A successful API response means **request accepted**, not confirmed WeChat delivery.
   Check your phone before relying on notifications.
5. Click **Save**, then restart ComfyUI when its queue is empty. Run a queue and confirm
   its completion notification. No graph node needs to be added.

1. 在 [Server酱 Turbo 官网](https://sct.ftqq.com/sendkey/) 登录、绑定微信接收通道，
   复制自己的 **SCT 开头 SendKey**。不是 Server酱³ 的 `sctp…` 密钥。
2. 打开原生悬浮窗齿轮 → **后端队列完成通知** → **微信通知 · Server酱 Turbo**。
   勾选后会同步开启后端总开关。仅用 Server酱时，不需要填写旧版“凭据环境文件”。
3. 在 **SendKey（SCT…）** 框输入密钥，可点击 **测试通知 · Server酱**。
   测试只使用本次输入，不会自动保存。
4. “请求已受理”不代表微信必达，请在手机确认实际收到。
5. 点击 **保存**，队列为空时重启 ComfyUI，之后队列完成会由后端自动提醒，无需增加节点。

## Saved keys / 保存后的行为

- Reopening Settings checks file metadata only: it never loads the saved SendKey into
  a widget, tooltip or ordinary settings object. The UI shows a configured status.
- **Replace key** unlocks a blank password field. Blank means **keep the previous key**.
- **Delete key** asks for confirmation and marks deletion pending. It takes effect
  only on **Save**. **Cancel** preserves the previous key and settings.
- Explicit tests may use an unsaved new key. Tests and first-time setup are never
  automatically triggered by opening Settings.
- If key persistence fails, the UI attempts to restore the previous non-secret settings
  and reports failure. These are two separate files, not a crash-atomic database transaction.

再次打开只显示“已配置”，不读取、回填或显示已保存密钥。更换时输入新密钥，留空保留旧值。
删除需确认并保存；取消不改变磁盘上的密钥。打开设置不会自动发送测试通知。
保存失败会尝试恢复原设置并明确报错；普通设置和密钥是两个文件，不保证断电时跨文件事务原子性。

## Storage and limits / 存储与安全边界

The default secret file is `secrets/serverchan.key` under the normal per-user plugin
configuration directory. `settings.json` contains only the enable switch and file path.
No SendKey is stored in the workflow, repository, regular JSON settings or diagnostic messages.

- **macOS/Linux:** plaintext in an owner-private `0600` file inside a dedicated `0700`
  directory. This is filesystem permission protection, **not encryption**. Unsafe permissions,
  symlinks, hardlinks, non-regular files and oversized files are rejected.
- **Windows:** the secret payload is encrypted with user-bound Windows DPAPI. Ordinary
  settings rely on the user's AppData ACLs, not POSIX permission bits. The ComfyUI process
  must run as the same Windows user on the same machine. Native Windows DPAPI execution
  is covered by a Windows-only test, which is skipped on macOS/Linux.
- Updates use atomic replacement of the secret file. Processes running as your user,
  administrators, malware and backups are outside this protection boundary. A SendKey
  must still be treated as a password and revoked at ServerChan if exposed.

默认密钥位于插件用户配置目录下的 `secrets/serverchan.key`，普通 `settings.json` 仅记录
开关和路径。macOS/Linux 为权限隔离的明文文件，Windows 为当前用户 DPAPI 加密。
这不能防御以同一用户运行的恶意进程、管理员或不安全备份；泄露后应在 Server酱官网重置。
重新显示 UI 时隐藏密钥，不等于系统上任何程序都无法读取它。

## Remote/headless ComfyUI / 远程或无桌面服务器

Saving on your laptop does **not** configure a remote ComfyUI server. Provision the
key on each host that will send notifications. Keep both the sender settings and key
outside the repository, and run the commands in the plugin's Python environment.

This prompt reads the key without placing it in shell history or process arguments:

```bash
python -c "import os; os.environ['COMFY_PROGRESS_BRIDGE_COMPANION']='1'; from getpass import getpass; from comfyui_progress_bridge.desktop.settings import config_directory; from comfyui_progress_bridge.desktop.secret_store import SendKeyStore; SendKeyStore(config_directory()/'secrets'/'serverchan.key').save(getpass('ServerChan Turbo SendKey: '))"
```

Create a host-side JSON config (owner-private `0600` on POSIX):

```json
{
  "enabled": true,
  "name": "ComfyUI",
  "language": "en-US",
  "serverchan": {"enabled": true}
}
```

Point `COMFY_PROGRESS_BRIDGE_BACKEND_CONFIG` at that JSON before starting ComfyUI.
The omitted `key_file` uses the per-user default; an explicit relative `key_file` resolves
beside the JSON. Do not copy a Windows DPAPI file to a different user/machine; provision
it there again. Merely saving a key does not enable notifications.

本机填写不会自动同步到远端。请在真正运行 ComfyUI 的主机上通过上述隐藏输入创建密钥，
再创建后端 JSON 并用 `COMFY_PROGRESS_BRIDGE_BACKEND_CONFIG` 指向它。仅保存密钥不会
自动启用提醒。Windows 加密文件不能直接复制给其他用户或机器使用。
