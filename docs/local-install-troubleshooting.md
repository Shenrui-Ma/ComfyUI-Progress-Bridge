# Local and remote UI troubleshooting / 本地与远程界面排查

Progress Bridge has two separate views. The compact **browser panel** is part of the
ComfyUI page, on both local and remote servers. The **PyQt desktop dock** is a separate
process; it can show multiple endpoints and has additional notification/audio/avatar settings.
The README dock screenshot does not mean that opening the browser launches that native UI.

Progress Bridge 有两种界面：**浏览器面板**随本地或远程 ComfyUI 页面加载；
**PyQt 桌面悬浮窗**是独立进程，提供多端点、通知、音频、头像等额外设置。
README 中的桌面截图不是浏览器面板的全功能预览。

## Update / 更新

In the plugin checkout, run `git pull --ff-only`, restart ComfyUI when its queue is
empty, then hard-refresh the browser (Ctrl+Shift+R / Cmd+Shift+R).
Copy the **entire plugin directory** for offline installs: the JS entry point now
imports companion `.mjs` files, so replacing only `progress-panel.js` is insufficient.

在插件目录运行 `git pull --ff-only`，队列为空后重启 ComfyUI，再强制刷新浏览器。
离线安装需复制**完整插件目录**，不能只覆盖一个 JS 文件。

## Browser controls / 浏览器操作

- Drag the header or grip; use arrow keys after focusing the grip. Shift moves faster.
- Click the gear for language, theme, opacity, scale and reset position. Escape closes settings.
- Settings are local to that browser origin (host **and port**); they survive refresh.
- Browser settings do not edit backend Telegram/Weixin credentials. See
  [Backend notifications](backend-notifications.md) for host-side setup.
- The queue number is the server's total remaining count. Node details follow the current
  client's WebSocket and graph. Jobs submitted by another client may show only queue activity.
- Offline/reconnecting state clears stale progress. `Updated` records accepted data time.

拖动标题栏或手柄可移动；手柄获得焦点后也可使用方向键（Shift 加速）。齿轮设置语言、
主题、不透明度、缩放与位置重置，Esc 关闭。设置按浏览器来源（主机与端口）独立保存。
浏览器不会写后端通知凭据。队列显示剩余总数；其他客户端提交的任务可能只有队列状态，
不会跨客户端读取工作流。断线清除旧进度，“更新”显示最后收到有效数据的时间。

## Native dock / 原生桌面窗

Check the ComfyUI log for `desktop launch requested` or `disabled or unavailable`.
`requested` means the child process was spawned; it does not guarantee Qt loaded successfully.
If no dock appears, run it from a terminal to see errors using the **same Python environment**:

```bash
cd /path/to/ComfyUI/custom_nodes/ComfyUI-Progress-Bridge
python -m pip install -e .
python -m comfyui_progress_bridge.desktop --show --endpoint 127.0.0.1:8188
```

For Windows portable ComfyUI, use its `python_embeded\python.exe` instead of a system
Python when installing dependencies. The autostart bootstrap explicitly locates the plugin
checkout even when embedded Python ignores `PYTHONPATH`. PyQt6 must still be installed.

检查启动日志：`requested` 只说明创建了子进程，不保证 Qt 成功启动。
用运行 ComfyUI 的同一 Python 环境执行上述命令，可看到具体错误。
Windows 便携版使用自带 `python_embeded\python.exe` 安装依赖；自动启动现在可处理
忽略 `PYTHONPATH` 的嵌入式 Python。仍需安装 PyQt6。

Hiding the dock keeps monitoring and alerts alive. Quitting the application stops them.
On headless/remote hosts, set `COMFY_PROGRESS_BRIDGE_AUTOSTART=0`; browser monitoring
and opted-in backend notifications remain available.

隐藏悬浮窗会继续监控和提醒，退出应用才停止。无桌面远程服务器使用
`COMFY_PROGRESS_BRIDGE_AUTOSTART=0`，浏览器面板和已启用的后端通知仍可运行。

The native probe sends bounded, display-only workflow metadata (currently at most
three nodes per queued prompt). The dock uses available node titles/types and falls
back to node IDs when that metadata is unavailable; it does not cache node inputs.

原生探针只传输有上限的显示元数据（目前每个排队任务最多三个节点）。桌面窗优先使用
可用的节点标题/类型，缺少元数据时回退为节点 ID，不缓存节点输入内容。

Windows embedded-Python startup is covered by isolated Python regression tests;
this is not a substitute for testing the dock on the affected user's Windows machine.

Windows 嵌入式 Python 的启动路径有隔离回归测试覆盖，但不能替代用户实际 Windows
设备上的 Qt 窗口验证。

## Lightweight developer test / 无模型前端测试

```bash
python tests/browser_preview.py
```

Open `http://127.0.0.1:18089/` and click **Run assertions**. **Small viewport** loads
the same shipped JS in a 280×230 iframe. This fixture sends synthetic browser events only;
it does not install ComfyUI, download models, submit inference, or send external notifications.

打开上述本地地址运行断言或小视口测试。测试页直接加载实际插件 JS，无需安装 ComfyUI、
下载模型或启动推理，不发送外部消息。
