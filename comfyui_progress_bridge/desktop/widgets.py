"""Native PyQt6 widgets for the compact multi-endpoint progress dock."""

from __future__ import annotations

import queue
import threading
from dataclasses import replace
from datetime import datetime

from PyQt6.QtCore import QEvent, QPoint, QRect, QRectF, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QDesktopServices,
    QFontMetrics,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPixmap,
    QResizeEvent,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..monitor.models import (
    EndpointId,
    MonitorState,
    Reduction,
    TaskKey,
    TaskState,
)
from .i18n import LANGUAGES, Translator, localized_result
from .secret_store import SendKeyStore, validate_sendkey
from .settings import (
    AppSettings,
    AudioConfig,
    BackendNotificationSettings,
    EndpointConfig,
    NotificationConfig,
    QQNotificationConfig,
    ServerChanNotificationConfig,
    SettingsStore,
    TelegramNotificationConfig,
    WeixinNotificationConfig,
    WindowPosition,
)

THEMES = {
    "dark": ("#171A21", "#232733", "#F4F6FC", "#AAB1C3"),
    "light": ("#EEF1F7", "#FFFFFF", "#202430", "#60697A"),
    "system": ("palette(window)", "palette(base)", "palette(text)", "palette(mid)"),
}

DOCK_UI_SCALE = 0.75


def _dock_px(value: int) -> int:
    """Scale one dock-only pixel metric using predictable half-up rounding."""
    return max(1, int(value * DOCK_UI_SCALE + 0.5))


class ElidedLabel(QLabel):
    """A fixed-bound label that always preserves its full value as a tooltip."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(), self.sizePolicy().verticalPolicy())
        if text:
            self.set_full_text(text)

    @property
    def full_text(self) -> str:
        return self._full_text

    def set_full_text(self, text: object) -> None:
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._update_elision()

    def _update_elision(self) -> None:
        width = max(0, self.contentsRect().width())
        self.setText(
            QFontMetrics(self.font()).elidedText(
                self._full_text, Qt.TextElideMode.ElideRight, width
            )
        )

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_elision()


class AvatarLabel(QLabel):
    """Paint one PNG clipped to a circular frame."""

    def __init__(self, size: int = _dock_px(54), parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._pixmap = QPixmap()
        self.path = ""

    def set_path(self, path: str) -> None:
        self.path = path
        self.setToolTip(path)
        self._pixmap = QPixmap(path) if path else QPixmap()
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addEllipse(QRectF(self.rect().adjusted(1, 1, -1, -1)))
        painter.setClipPath(clip)
        if not self._pixmap.isNull():
            scaled = self._pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (scaled.width() - self.width()) // 2
            y = (scaled.height() - self.height()) // 2
            painter.drawPixmap(self.rect(), scaled, QRect(x, y, self.width(), self.height()))
        else:
            painter.fillRect(self.rect(), Qt.GlobalColor.transparent)


class DragHandle(QLabel):
    def __init__(self, text: str, window: ProgressWindow) -> None:
        super().__init__(text, window)
        self.window = window
        self.origin: QPoint | None = None
        self.setObjectName("dragHandle")
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.globalPosition().toPoint() - self.window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window.move(event.globalPosition().toPoint() - self.origin)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.origin is not None:
            self.origin = None
            self.window.persist_position()
            event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        """Forward keyboard movement while this accessible focus target is active."""
        self.window.keyPressEvent(event)


class EndpointCard(QFrame):
    CARD_WIDTH = _dock_px(328)

    def __init__(
        self,
        config: EndpointConfig,
        translator: Translator,
        *,
        professional: bool,
        avatar_path: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.translator = translator
        card_font = self.font()
        card_font.setPixelSize(_dock_px(14))
        self.setFont(card_font)
        self.setObjectName("endpointCard")
        self.setFixedWidth(self.CARD_WIDTH)
        self.setStyleSheet(
            f"QFrame#endpointCard {{ border: {_dock_px(2)}px solid {config.color}; "
            f"border-radius: {_dock_px(7)}px; padding: {_dock_px(5)}px; }}"
        )
        outer = QHBoxLayout(self)
        outer.setContentsMargins(*(_dock_px(value) for value in (10, 7, 9, 7)))
        outer.setSpacing(_dock_px(9))
        self.avatar: AvatarLabel | None = None
        if avatar_path is not None:
            self.avatar = AvatarLabel(parent=self)
            self.avatar.set_path(avatar_path)
            outer.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignTop)

        body = QVBoxLayout()
        body.setSpacing(_dock_px(3))
        outer.addLayout(body, 1)
        header = QHBoxLayout()
        self.endpoint_label = ElidedLabel(f"{config.name} · {config.host}:{config.port}")
        self.endpoint_label.setObjectName("endpointLabel")
        header.addWidget(self.endpoint_label, 1)
        self.status_label = QLabel(translator("unknown"))
        self.status_label.setObjectName("connectivityLabel")
        self.status_label.setVisible(professional)
        header.addWidget(self.status_label)
        body.addLayout(header)
        self.queue_label = QLabel(translator("queue_counts", running=0, pending=0))
        self.queue_label.setObjectName("queueLabel")
        body.addWidget(self.queue_label)

        self.details = QWidget(self)
        details = QVBoxLayout(self.details)
        details.setContentsMargins(0, _dock_px(3), 0, 0)
        details.setSpacing(_dock_px(2))
        self.stage_label = ElidedLabel(translator("no_task"))
        self.stage_label.setObjectName("stageLabel")
        self.node_label = ElidedLabel("—")
        self.node_label.setObjectName("nodeLabel")
        details.addWidget(self.stage_label)
        details.addWidget(self.node_label)
        progress_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(_dock_px(14))
        self.progress_label = QLabel("—")
        self.progress_label.setObjectName("progressLabel")
        progress_row.addWidget(self.progress_bar, 1)
        progress_row.addWidget(self.progress_label)
        details.addLayout(progress_row)
        self.timestamp_label = QLabel("—")
        self.timestamp_label.setObjectName("timestampLabel")
        details.addWidget(self.timestamp_label)
        body.addWidget(self.details)
        self.details.setVisible(professional)
        for widget in (
            self.endpoint_label,
            self.status_label,
            self.queue_label,
            self.stage_label,
            self.node_label,
            self.progress_bar,
            self.progress_label,
            self.timestamp_label,
        ):
            widget.setFont(card_font)
        self.setFixedHeight(_dock_px(151 if professional else 67))

    @staticmethod
    def _tasks_for(state: MonitorState, config: EndpointConfig) -> list[TaskState]:
        return [
            task
            for key, task in state.tasks.items()
            if key.endpoint.host == config.host and key.endpoint.port == config.port
        ]

    def update_state(self, state: MonitorState, *, received_at: datetime | None = None) -> None:
        endpoint_state = next(
            (
                value
                for endpoint, value in state.endpoints.items()
                if endpoint.host == self.config.host and endpoint.port == self.config.port
            ),
            None,
        )
        tasks = self._tasks_for(state, self.config)
        running = sum(task.status == "running" for task in tasks)
        pending = sum(task.status == "pending" for task in tasks)
        self.queue_label.setText(self.translator("queue_counts", running=running, pending=pending))
        connectivity = (
            "unknown"
            if endpoint_state is None or endpoint_state.online is None
            else ("online" if endpoint_state.online else "offline")
        )
        self.status_label.setText(self.translator(connectivity))
        self.status_label.setToolTip(
            self.translator("connectivity") + ": " + self.translator(connectivity)
        )
        active = next((task for task in tasks if task.status == "running"), None)
        active = active or next((task for task in tasks if task.status == "pending"), None)
        active = active or next(iter(tasks), None)
        if active is None:
            self.stage_label.set_full_text(self.translator("no_task"))
            self.node_label.set_full_text("—")
            self.progress_label.setText("—")
            self.progress_bar.setValue(0)
        else:
            stage = self.translator(active.stage_key)
            self.stage_label.set_full_text(f"{self.translator('stage')}: {stage}")
            node = active.node_name or "—"
            self.node_label.set_full_text(f"{self.translator('node')}: {node}")
            value, maximum = active.progress_value, active.progress_max
            if value is not None and maximum is not None and maximum > 0:
                percent = max(0, min(100, round(float(value) / float(maximum) * 100)))
                self.progress_bar.setValue(percent)
                self.progress_label.setText(f"{value} / {maximum}")
                self.progress_label.setToolTip(f"{self.translator('steps')}: {value} / {maximum}")
            else:
                self.progress_bar.setValue(0)
                self.progress_label.setText(self.translator(active.status))
        self.timestamp_label.setText(
            f"{self.translator('updated')}: "
            + (received_at.strftime('%H:%M:%S') if received_at is not None else "—")
        )


class _TestActionWorker:
    """One reusable worker with a strict single-action capacity."""

    def __init__(self, finished) -> None:
        self._finished = finished
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._active = False
        self._stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="settings-test-worker", daemon=True)
        self.thread.start()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def submit(self, action) -> bool:
        with self._lock:
            if self._active or self._stopping.is_set():
                return False
            self._active = True
        self._queue.put_nowait(action)
        return True

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                action = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                result = action()
            except Exception:
                from .notifications import SafeResult

                result = SafeResult(False, "network_error", "Test action failed")
            finally:
                self._queue.task_done()
                with self._lock:
                    self._active = False
            if not self._stopping.is_set():
                self._finished(result)

    def shutdown(self, timeout: float = 0.25) -> bool:
        self._stopping.set()
        self.thread.join(max(0.0, timeout))
        return not self.thread.is_alive()


class SettingsDialog(QDialog):
    """Native settings editor, including secure completion actions."""

    test_finished = pyqtSignal(object)

    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
        *,
        notification_sender=None,
        audio_player=None,
        secret_store=None,
    ) -> None:
        super().__init__(parent)
        self.original = settings
        self.notification_sender = notification_sender
        self.audio_player = audio_player
        self._serverchan_store = None
        self._serverchan_delete_pending = False
        self._serverchan_storage_error = False
        try:
            self._serverchan_store = secret_store or SendKeyStore(
                settings.backend_notifications.serverchan.key_file
            )
            self._serverchan_key_present = self._serverchan_store.has_key()
        except (OSError, ValueError):
            self._serverchan_key_present = False
            self._serverchan_storage_error = True
        self.test_finished.connect(self._test_action_finished)
        self._test_worker = _TestActionWorker(self.test_finished.emit)
        self._test_worker_closed = False
        self.t = Translator(settings.language)
        self.setWindowTitle(self.t("settings"))
        self.setSizeGripEnabled(True)
        self.setMinimumSize(480, 340)
        root = QVBoxLayout(self)
        self.settings_scroll = QScrollArea(self)
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_content = QWidget()
        content = QVBoxLayout(self.settings_content)
        self.settings_scroll.setWidget(self.settings_content)
        root.addWidget(self.settings_scroll, 1)
        form = QFormLayout()
        self.language = QComboBox()
        for code in LANGUAGES:
            self.language.addItem(code, code)
        self.language.setCurrentIndex(self.language.findData(settings.language))
        self.mode = QComboBox()
        self.mode.addItem(self.t("simple"), "simple")
        self.mode.addItem(self.t("professional"), "professional")
        self.mode.setCurrentIndex(self.mode.findData(settings.mode))
        self.theme = QComboBox()
        for key in ("dark", "light", "system"):
            self.theme.addItem(self.t(f"theme_{key}"), key)
        self.theme.setCurrentIndex(self.theme.findData(settings.theme))
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(20, 100)
        self.opacity.setValue(settings.opacity)
        self.dock = QCheckBox()
        self.dock.setChecked(settings.dock_enabled)
        self.avatar_enabled = QCheckBox()
        self.avatar_enabled.setChecked(settings.avatar_enabled)
        form.addRow(self.t("language"), self.language)
        form.addRow(self.t("mode"), self.mode)
        form.addRow(self.t("theme"), self.theme)
        form.addRow(self.t("opacity"), self.opacity)
        form.addRow(self.t("dock_enabled"), self.dock)
        form.addRow(self.t("avatar_enabled"), self.avatar_enabled)
        content.addLayout(form)

        self.endpoint_table = QTableWidget(len(settings.endpoints), 11)
        self.endpoint_table.setHorizontalHeaderLabels(
            [
                self.t("name"),
                self.t("host"),
                self.t("port"),
                self.t("color"),
                self.t("ssh_source"),
                self.t("ssh_host"),
                self.t("ssh_user"),
                self.t("ssh_port"),
                self.t("identity_file"),
                self.t("remote_python"),
                self.t("probe_path"),
            ]
        )
        self.endpoint_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for row, endpoint in enumerate(settings.endpoints):
            values = (
                endpoint.name,
                endpoint.host,
                str(endpoint.port),
                endpoint.color,
                "1" if endpoint.ssh_enabled else "0",
                endpoint.ssh_host,
                endpoint.ssh_user,
                str(endpoint.ssh_port),
                endpoint.ssh_identity_file,
                endpoint.ssh_remote_python,
                endpoint.ssh_probe_path,
            )
            for column, value in enumerate(values):
                self.endpoint_table.setItem(row, column, QTableWidgetItem(value))
        content.addWidget(self.endpoint_table)

        self.avatar_edits: list[QLineEdit] = []
        avatar_box = QHBoxLayout()
        for index in range(6):
            edit = QLineEdit(
                settings.avatar_paths[index] if index < len(settings.avatar_paths) else ""
            )
            edit.setPlaceholderText(f"PNG {index + 1}")
            button = QPushButton("…")
            button.clicked.connect(lambda _checked=False, target=edit: self._pick_avatar(target))
            column = QVBoxLayout()
            column.addWidget(edit)
            column.addWidget(button)
            avatar_box.addLayout(column)
            self.avatar_edits.append(edit)
        content.addLayout(avatar_box)

        completion_form = QFormLayout()
        notification = settings.notifications
        self.notifications_enabled = QCheckBox()
        self.notifications_enabled.setChecked(notification.enabled)
        self.env_file = QLineEdit(notification.env_file)
        self.env_file.setEchoMode(QLineEdit.EchoMode.Password)
        self.env_file.setAccessibleDescription(self.t("credential_file"))
        self.timeout = QLineEdit(str(notification.timeout))
        self.telegram_enabled = QCheckBox("Telegram")
        self.telegram_enabled.setChecked(notification.telegram.enabled)
        self.telegram_target = QLineEdit(notification.telegram.chat_id)
        self.telegram_thread = QLineEdit(
            "" if notification.telegram.thread_id is None else str(notification.telegram.thread_id)
        )
        self.weixin_enabled = QCheckBox("Weixin")
        self.weixin_enabled.setChecked(notification.weixin.enabled)
        self.weixin_account = QLineEdit(notification.weixin.account_id)
        self.weixin_target = QLineEdit(notification.weixin.target)
        self.weixin_context_store = QLineEdit(notification.weixin.context_store)
        self.qq_enabled = QCheckBox("QQ")
        self.qq_enabled.setChecked(notification.qq.enabled)
        self.qq_target_type = QComboBox()
        for target_type in ("c2c", "group", "channel"):
            self.qq_target_type.addItem(target_type, target_type)
        self.qq_target_type.setCurrentIndex(
            self.qq_target_type.findData(notification.qq.target_type)
        )
        self.qq_target = QLineEdit(notification.qq.target)
        completion_form.addRow(self.t("notifications_enabled"), self.notifications_enabled)
        completion_form.addRow(self.t("credential_file"), self.env_file)
        completion_form.addRow(self.t("timeout"), self.timeout)
        completion_form.addRow("Telegram", self.telegram_enabled)
        completion_form.addRow(self.t("telegram_target"), self.telegram_target)
        completion_form.addRow(self.t("telegram_thread"), self.telegram_thread)
        completion_form.addRow("Weixin", self.weixin_enabled)
        completion_form.addRow(self.t("weixin_target"), self.weixin_target)
        completion_form.addRow(self.t("weixin_account"), self.weixin_account)
        completion_form.addRow(self.t("context_store"), self.weixin_context_store)
        completion_form.addRow("QQ", self.qq_enabled)
        completion_form.addRow(self.t("qq_target"), self.qq_target)
        completion_form.addRow(self.t("qq_target_type"), self.qq_target_type)

        credential_row = QHBoxLayout()
        self.credential_state = QLabel(self.t("not_configured"))
        self.credential_state.setObjectName("credentialState")
        from .notifications import credential_source_state

        source_state = credential_source_state(notification)
        configured_platforms = [name for name, present in source_state.items() if present]
        if configured_platforms:
            self.credential_state.setText(
                f"{self.t('configured')}: {', '.join(configured_platforms)}"
            )
        credential_row.addWidget(self.credential_state)
        self.notification_test_buttons = {}
        for platform in ("telegram", "weixin", "qq"):
            button = QPushButton(f"{self.t('test_notification')} · {platform}")
            button.clicked.connect(
                lambda _checked=False, selected=platform: self._test_notification(selected)
            )
            credential_row.addWidget(button)
            self.notification_test_buttons[platform] = button
        completion_form.addRow(self.t("credential_state"), credential_row)

        self.audio_enabled = QCheckBox()
        self.audio_enabled.setChecked(settings.audio.enabled)
        self.audio_mode = QComboBox()
        for key, label in (
            ("disabled", "audio_disabled"),
            ("ding", "audio_ding"),
            ("custom", "audio_custom"),
        ):
            self.audio_mode.addItem(self.t(label), key)
        self.audio_mode.setCurrentIndex(self.audio_mode.findData(settings.audio.mode))
        self.audio_path = QLineEdit(settings.audio.wav_path)
        self.audio_test_button = QPushButton(self.t("test_audio"))
        self.audio_test_button.clicked.connect(self._test_audio)
        completion_form.addRow(self.t("audio_enabled"), self.audio_enabled)
        completion_form.addRow(self.t("audio"), self.audio_mode)
        completion_form.addRow(self.t("wav_file"), self.audio_path)
        completion_form.addRow("", self.audio_test_button)
        content.addLayout(completion_form)

        backend = settings.backend_notifications
        backend_form = QFormLayout()
        self.backend_enabled = QCheckBox()
        self.backend_enabled.setChecked(backend.enabled)
        self.backend_name = QLineEdit(backend.name)
        self.backend_env_file = QLineEdit(backend.credentials_file)
        self.backend_env_file.setEchoMode(QLineEdit.EchoMode.Password)
        self.backend_timeout = QLineEdit(str(backend.timeout))
        self.backend_telegram_enabled = QCheckBox("Telegram")
        self.backend_telegram_enabled.setChecked(backend.telegram.enabled)
        self.backend_telegram_target = QLineEdit(backend.telegram.chat_id)
        self.backend_telegram_thread = QLineEdit(
            "" if backend.telegram.thread_id is None else str(backend.telegram.thread_id)
        )
        self.backend_weixin_enabled = QCheckBox("Weixin")
        self.backend_weixin_enabled.setChecked(backend.weixin.enabled)
        self.backend_weixin_account = QLineEdit(backend.weixin.account_id)
        self.backend_weixin_target = QLineEdit(backend.weixin.target)
        self.backend_weixin_context_store = QLineEdit(backend.weixin.context_store)
        backend_form.addRow(self.t("backend_notifications"), self.backend_enabled)
        backend_form.addRow(self.t("backend_name"), self.backend_name)
        self.serverchan_enabled = QCheckBox(self.t("serverchan"))
        self.serverchan_enabled.setChecked(backend.serverchan.enabled)
        self.serverchan_sendkey = QLineEdit()
        self.serverchan_sendkey.setEchoMode(QLineEdit.EchoMode.Password)
        self.serverchan_sendkey.setInputMethodHints(
            Qt.InputMethodHint.ImhSensitiveData | Qt.InputMethodHint.ImhNoPredictiveText
            | Qt.InputMethodHint.ImhNoAutoUppercase
        )
        self.serverchan_sendkey.setMaxLength(1024)
        self.serverchan_sendkey.setAccessibleName(self.t("serverchan_sendkey"))
        self.serverchan_sendkey.setPlaceholderText(self.t("serverchan_key_placeholder"))
        self.serverchan_sendkey.setEnabled(not self._serverchan_key_present)
        self.serverchan_key_status = QLabel(self.t(
            "serverchan_storage_error" if self._serverchan_storage_error else
            "configured" if self._serverchan_key_present else "not_configured"
        ))
        self.serverchan_key_status.setWordWrap(True)
        self.serverchan_replace_key = QPushButton(self.t("serverchan_replace"))
        self.serverchan_delete_key = QPushButton(self.t("serverchan_delete"))
        self.serverchan_delete_key.setEnabled(self._serverchan_key_present)
        self.serverchan_replace_key.clicked.connect(self._replace_serverchan_key)
        self.serverchan_delete_key.clicked.connect(self._delete_serverchan_key)
        self.serverchan_sendkey.textEdited.connect(self._serverchan_key_edited)
        self.serverchan_enabled.toggled.connect(
            lambda enabled: self.backend_enabled.setChecked(True) if enabled else None
        )
        key_actions = QHBoxLayout()
        key_actions.addWidget(self.serverchan_key_status, 1)
        key_actions.addWidget(self.serverchan_replace_key)
        key_actions.addWidget(self.serverchan_delete_key)
        backend_form.addRow("", self.serverchan_enabled)
        backend_form.addRow(self.t("serverchan_sendkey"), self.serverchan_sendkey)
        backend_form.addRow(self.t("credential_state"), key_actions)
        self.serverchan_get_key = QPushButton(self.t("serverchan_get_key"))
        self.serverchan_get_key.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://sct.ftqq.com/sendkey/"))
        )
        self.serverchan_test_button = QPushButton(f"{self.t('test_notification')} · Server酱")
        self.serverchan_test_button.clicked.connect(
            lambda: self._test_notification("serverchan", backend=True)
        )
        serverchan_actions = QHBoxLayout()
        serverchan_actions.addWidget(self.serverchan_get_key)
        serverchan_actions.addWidget(self.serverchan_test_button)
        backend_form.addRow("", serverchan_actions)
        key_note = QLabel(self.t("serverchan_note"))
        key_note.setWordWrap(True)
        backend_form.addRow("", key_note)
        backend_form.addRow(self.t("credential_file"), self.backend_env_file)
        backend_form.addRow(self.t("timeout"), self.backend_timeout)
        backend_form.addRow("Telegram", self.backend_telegram_enabled)
        backend_form.addRow(self.t("telegram_target"), self.backend_telegram_target)
        backend_form.addRow(self.t("telegram_thread"), self.backend_telegram_thread)
        backend_form.addRow("Weixin", self.backend_weixin_enabled)
        backend_form.addRow(self.t("weixin_target"), self.backend_weixin_target)
        backend_form.addRow(self.t("weixin_account"), self.backend_weixin_account)
        backend_form.addRow(self.t("context_store"), self.backend_weixin_context_store)
        backend_test_row = QVBoxLayout()
        self.backend_notification_test_buttons = {"serverchan": self.serverchan_test_button}
        for platform in ("telegram", "weixin"):
            button = QPushButton(f"{self.t('test_notification')} · backend {platform}")
            button.clicked.connect(
                lambda _checked=False, selected=platform: self._test_notification(
                    selected, backend=True
                )
            )
            backend_test_row.addWidget(button)
            self.backend_notification_test_buttons[platform] = button
        backend_form.addRow(self.t("credential_state"), backend_test_row)
        restart_note = QLabel(self.t("backend_restart_required"))
        restart_note.setWordWrap(True)
        backend_form.addRow("", restart_note)
        content.addLayout(backend_form)
        content.addStretch(1)

        self.validation_label = QLabel("")
        self.validation_label.setObjectName("validationError")
        self.validation_label.setWordWrap(True)
        self.validation_label.setStyleSheet("color: #d33;")
        root.addWidget(self.validation_label)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons = self.button_box
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(self.t("save"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self.t("cancel"))
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        self.reset_button = QPushButton(self.t("reset_position"))
        if parent is not None and hasattr(parent, "reset_position"):
            self.reset_button.clicked.connect(parent.reset_position)
        buttons.addButton(self.reset_button, QDialogButtonBox.ButtonRole.ResetRole)
        root.addWidget(buttons)

        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = max(self.minimumWidth(), min(660, available.width() - 80))
            height = max(self.minimumHeight(), min(500, available.height() - 100))
            self.resize(width, height)
        else:
            self.resize(660, 500)

    def _set_test_controls_enabled(self, enabled: bool) -> None:
        for button in self.notification_test_buttons.values():
            button.setEnabled(enabled)
        for button in self.backend_notification_test_buttons.values():
            button.setEnabled(enabled)
        self.audio_test_button.setEnabled(enabled)

    def _show_test_result(self, result) -> None:
        ok = bool(getattr(result, "ok", False))
        code = str(getattr(result, "code", "api_error"))
        prefix = self.t("test_accepted" if code == "accepted" else
                        "test_success" if ok else "test_failure")
        detail = localized_result(self.original.language, code)
        self.validation_label.setStyleSheet("color: #287a36;" if ok else "color: #d33;")
        self.validation_label.setText(f"{prefix}: {detail}")

    def _test_action_finished(self, result) -> None:
        self._set_test_controls_enabled(True)
        self._show_test_result(result)

    def _busy_result(self) -> None:
        from .notifications import SafeResult

        self._show_test_result(SafeResult(False, "busy", "Test action is busy"))

    def _test_notification(self, platform: str, *, backend: bool = False) -> None:
        """Submit an explicit send to the dialog's sole bounded worker."""
        if self._test_worker.active:
            self._busy_result()
            return
        try:
            candidate = self.result_settings()
            if platform == "serverchan":
                self.validate_serverchan_key_action(candidate)
        except (AttributeError, TypeError, ValueError):
            from .notifications import SafeResult

            self._show_test_result(SafeResult(False, "invalid_settings", "Invalid settings"))
            return

        if backend:
            backend_config = candidate.backend_notifications
            candidate = replace(
                candidate,
                notifications=NotificationConfig(
                    enabled=backend_config.enabled,
                    env_file=backend_config.credentials_file,
                    timeout=backend_config.timeout,
                    telegram=backend_config.telegram,
                    weixin=backend_config.weixin,
                    qq=QQNotificationConfig(),
                    serverchan=backend_config.serverchan,
                ),
            )

        # Capture only this explicit test's new key; opening the dialog never loads a saved key.
        test_key = self.serverchan_sendkey.text().strip() if platform == "serverchan" else ""

        def run():
            sender = self.notification_sender
            if sender is None:
                from .notifications import NotificationSender

                sender = NotificationSender(
                    credential_environ={} if backend else None,
                    **({"serverchan_sendkey": test_key} if test_key else {}),
                )
            text = self.t("test_notification")
            return sender.send_platform(platform, text, candidate)

        if not self._test_worker.submit(run):
            self._busy_result()
            return
        self._set_test_controls_enabled(False)

    def _test_audio(self) -> None:
        """Play non-blocking Qt audio only after this explicit action."""
        if self._test_worker.active:
            self._busy_result()
            return
        try:
            config = AudioConfig(
                enabled=self.audio_enabled.isChecked(),
                mode=self.audio_mode.currentData(),
                wav_path=self.audio_path.text().strip(),
            )
        except ValueError:
            from .notifications import SafeResult

            self._show_test_result(
                SafeResult(False, "invalid_settings", "Invalid settings", "audio")
            )
            return
        player = self.audio_player
        if player is None:
            from .audio import CompletionAudio

            player = CompletionAudio()
        self._show_test_result(player.play(config))

    def _shutdown_test_worker(self) -> None:
        if not self._test_worker_closed:
            self._test_worker_closed = True
            self._test_worker.shutdown(0.25)

    def done(self, result: int) -> None:
        if result != QDialog.DialogCode.Accepted:
            self.serverchan_sendkey.clear()
        self._shutdown_test_worker()
        super().done(result)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.serverchan_sendkey.clear()
        self._shutdown_test_worker()
        super().closeEvent(event)

    def _validate_and_accept(self) -> None:
        try:
            candidate = self.result_settings()
            self.validate_serverchan_key_action(candidate)
        except (AttributeError, TypeError, ValueError) as exc:
            message = f"{self.t('error')}: {exc}"
            self.validation_label.setText(message)
            QMessageBox.warning(self, self.t("settings"), message)
            return
        self.validation_label.clear()
        self.accept()

    def _replace_serverchan_key(self) -> None:
        self._serverchan_delete_pending = False
        self.serverchan_sendkey.clear()
        self.serverchan_sendkey.setEnabled(True)
        self.serverchan_sendkey.setFocus()
        self.serverchan_key_status.setText(self.t(
            "configured" if self._serverchan_key_present else "not_configured"
        ))

    def _serverchan_key_edited(self, _text: str) -> None:
        self._serverchan_delete_pending = False

    def _delete_serverchan_key(self) -> None:
        answer = QMessageBox.question(
            self, self.t("serverchan_delete"), self.t("serverchan_delete_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._serverchan_delete_pending = True
        self.serverchan_sendkey.clear()
        self.serverchan_sendkey.setEnabled(False)
        self.serverchan_enabled.setChecked(False)
        if not (self.backend_telegram_enabled.isChecked()
                or self.backend_weixin_enabled.isChecked()):
            self.backend_enabled.setChecked(False)
        self.serverchan_key_status.setText(self.t("serverchan_delete_pending"))

    def validate_serverchan_key_action(self, candidate: AppSettings) -> None:
        key = self.serverchan_sendkey.text()
        if key:
            validate_sendkey(key)
        elif (candidate.backend_notifications.enabled
              and candidate.backend_notifications.serverchan.enabled
              and (self._serverchan_delete_pending or not self._serverchan_key_present)):
            raise ValueError(self.t("serverchan_missing_key"))

    def commit_serverchan_key(self) -> None:
        """Called only after Save and successful persistence of non-secret settings."""
        if self._serverchan_store is None:
            if self._serverchan_delete_pending or self.serverchan_sendkey.text():
                raise ValueError(self.t("serverchan_storage_error"))
            return
        if self._serverchan_delete_pending:
            self._serverchan_store.delete()
        elif self.serverchan_sendkey.text():
            self._serverchan_store.save(validate_sendkey(self.serverchan_sendkey.text()))

    def _pick_avatar(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.t("avatar_files"), "", "PNG (*.png)")
        if path:
            edit.setText(path)

    def result_settings(self) -> AppSettings:
        endpoints = []
        for row in range(self.endpoint_table.rowCount()):

            def value(column: int, current_row: int = row) -> str:
                item = self.endpoint_table.item(current_row, column)
                return item.text().strip()

            endpoints.append(
                EndpointConfig(
                    host=value(1),
                    port=int(value(2)),
                    name=value(0),
                    color=value(3),
                    ssh_enabled=value(4).casefold() in {"1", "true", "yes"},
                    ssh_host=value(5),
                    ssh_user=value(6),
                    ssh_port=int(value(7)),
                    ssh_identity_file=value(8),
                    ssh_remote_python=value(9),
                    ssh_probe_path=value(10),
                )
            )
        paths = tuple(edit.text().strip() for edit in self.avatar_edits if edit.text().strip())
        thread_text = self.telegram_thread.text().strip()
        backend_thread_text = self.backend_telegram_thread.text().strip()
        notification = NotificationConfig(
            enabled=self.notifications_enabled.isChecked(),
            env_file=self.env_file.text().strip(),
            timeout=float(self.timeout.text().strip()),
            serverchan=self.original.notifications.serverchan,
            telegram=TelegramNotificationConfig(
                enabled=self.telegram_enabled.isChecked(),
                chat_id=self.telegram_target.text().strip(),
                thread_id=int(thread_text) if thread_text else None,
            ),
            weixin=WeixinNotificationConfig(
                enabled=self.weixin_enabled.isChecked(),
                account_id=self.weixin_account.text().strip(),
                target=self.weixin_target.text().strip(),
                context_store=self.weixin_context_store.text().strip(),
            ),
            qq=QQNotificationConfig(
                enabled=self.qq_enabled.isChecked(),
                target_type=self.qq_target_type.currentData(),
                target=self.qq_target.text().strip(),
            ),
        )
        audio = AudioConfig(
            enabled=self.audio_enabled.isChecked(),
            mode=self.audio_mode.currentData(),
            wav_path=self.audio_path.text().strip(),
        )
        backend_notifications = BackendNotificationSettings(
            enabled=self.backend_enabled.isChecked(),
            name=self.backend_name.text().strip(),
            credentials_file=self.backend_env_file.text().strip(),
            timeout=float(self.backend_timeout.text().strip()),
            serverchan=ServerChanNotificationConfig(
                enabled=self.serverchan_enabled.isChecked(),
                key_file=self.original.backend_notifications.serverchan.key_file,
            ),
            telegram=TelegramNotificationConfig(
                enabled=self.backend_telegram_enabled.isChecked(),
                chat_id=self.backend_telegram_target.text().strip(),
                thread_id=int(backend_thread_text) if backend_thread_text else None,
            ),
            weixin=WeixinNotificationConfig(
                enabled=self.backend_weixin_enabled.isChecked(),
                account_id=self.backend_weixin_account.text().strip(),
                target=self.backend_weixin_target.text().strip(),
                context_store=self.backend_weixin_context_store.text().strip(),
            ),
        )
        return replace(
            self.original,
            language=self.language.currentData(),
            mode=self.mode.currentData(),
            theme=self.theme.currentData(),
            opacity=self.opacity.value(),
            dock_enabled=self.dock.isChecked(),
            avatar_enabled=self.avatar_enabled.isChecked(),
            endpoints=tuple(endpoints),
            avatar_paths=paths,
            notifications=notification,
            audio=audio,
            backend_notifications=backend_notifications,
        )


class ProgressWindow(QWidget):
    """Frameless, always-on-top, bounded desktop progress dock."""

    settings_applied = pyqtSignal(object)
    max_scroll_height = _dock_px(470)

    def __init__(
        self,
        settings: AppSettings,
        *,
        persisted_settings: AppSettings | None = None,
        store: SettingsStore | None = None,
    ) -> None:
        super().__init__(
            None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.settings = settings
        dock_font = self.font()
        dock_font.setPixelSize(_dock_px(14))
        self.setFont(dock_font)
        # Runtime settings may include process-local CLI substitutions (for
        # example --show or --demo). Never use them as the source of truth for
        # an implicit save: mutable UI state is merged into this persisted base.
        self.persisted_settings = persisted_settings if persisted_settings is not None else settings
        self.store = store or SettingsStore()
        self.translator = Translator(settings.language)
        # Launches always begin with the first configured expression. Only a
        # successful task transition advances it during this process lifetime.
        self.avatar_index = 0
        self._handled_completions: set[TaskKey] = set()
        self._received_at: dict[EndpointId, datetime] = {}
        self._clamp_pending = False
        self._clamping = False
        self.cards: list[EndpointCard] = []
        self.setObjectName("progressWindow")
        self.setWindowTitle(self.translator("app_title"))
        self.setFixedWidth(_dock_px(352))
        self.setWindowOpacity(settings.opacity / 100)
        root = QVBoxLayout(self)
        root.setContentsMargins(*(_dock_px(6) for _ in range(4)))
        root.setSpacing(_dock_px(4))
        bar = QHBoxLayout()
        self.drag_handle = DragHandle("⠿", self)
        self.drag_handle.setToolTip(self.translator("drag"))
        self.drag_handle.setAccessibleName(self.translator("drag"))
        self.drag_handle.setAccessibleDescription("Alt+Arrow keys move the window")
        self.drag_handle.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        bar.addWidget(self.drag_handle, 1)
        self.collapse_button = QPushButton("▾")
        self.collapse_button.setAccessibleName(self.translator("collapse"))
        self.collapse_button.setFixedSize(_dock_px(28), _dock_px(28))
        self.collapse_button.clicked.connect(
            lambda: self.set_collapsed(not self.settings.collapsed)
        )
        bar.addWidget(self.collapse_button)
        self.gear_button = QPushButton("⚙")
        self.gear_button.setFixedSize(_dock_px(28), _dock_px(28))
        self.gear_button.setToolTip(self.translator("settings"))
        self.gear_button.setAccessibleName(self.translator("settings"))
        self.gear_button.setAccessibleDescription(self.translator("settings"))
        self.gear_button.clicked.connect(self.open_settings)
        bar.addWidget(self.gear_button)
        self.close_button = QPushButton("×")
        self.close_button.setFixedSize(_dock_px(28), _dock_px(28))
        self.close_button.setToolTip(self.translator("close"))
        self.close_button.setAccessibleName(self.translator("close"))
        self.close_button.setAccessibleDescription("Hide dock; use the tray icon to restore it")
        self.close_button.clicked.connect(lambda: self.set_dock_enabled(False))
        bar.addWidget(self.close_button)
        root.addLayout(bar)
        self.source_status = QLabel("")
        self.source_status.setObjectName("sourceStatus")
        self.source_status.setWordWrap(True)
        self.source_status.hide()
        root.addWidget(self.source_status)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setMaximumHeight(self.max_scroll_height)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.card_container = QWidget()
        self.card_layout = QVBoxLayout(self.card_container)
        self.card_layout.setContentsMargins(0, 0, 0, 0)
        self.card_layout.setSpacing(_dock_px(8))
        self.card_layout.addStretch(1)
        self.scroll_area.setWidget(self.card_container)
        root.addWidget(self.scroll_area)
        self._build_cards()
        self._apply_theme()
        self.set_collapsed(settings.collapsed, persist=False)
        self._restore_position()
        self._create_tray()
        for screen in QApplication.screens():
            screen.availableGeometryChanged.connect(lambda _geometry: self.schedule_clamp())
        application = QApplication.instance()
        if application is not None:
            application.screenAdded.connect(lambda _screen: self.schedule_clamp())
            application.screenRemoved.connect(lambda _screen: self.schedule_clamp())

    def _create_tray(self) -> None:
        icon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip(self.translator("app_title"))
        self.tray_menu = QMenu()
        menu = self.tray_menu
        self.show_action = QAction(self.translator("dock_enabled"), self)
        self.show_action.triggered.connect(lambda: self.set_dock_enabled(True))
        menu.addAction(self.show_action)
        hide_action = QAction(self.translator("close"), self)
        hide_action.triggered.connect(lambda: self.set_dock_enabled(False))
        menu.addAction(hide_action)
        settings_action = QAction(self.translator("settings"), self)
        settings_action.triggered.connect(self.open_settings)
        menu.addAction(settings_action)
        reset_action = QAction(self.translator("reset_position"), self)
        reset_action.triggered.connect(self.reset_position)
        menu.addAction(reset_action)
        menu.addSeparator()
        self.quit_action = QAction("Quit", self)
        self.quit_action.setShortcut("Ctrl+Q")
        self.quit_action.triggered.connect(QApplication.quit)
        menu.addAction(self.quit_action)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(
            lambda reason: (
                self.set_dock_enabled(True)
                if reason == QSystemTrayIcon.ActivationReason.Trigger
                else None
            )
        )
        self.tray_icon.show()

    def _avatar_path(self) -> str | None:
        if not self.settings.avatar_enabled or not self.settings.avatar_paths:
            return None
        return self.settings.avatar_paths[self.avatar_index % len(self.settings.avatar_paths)]

    def _refresh_card_area(self) -> None:
        """Size the card area from active queues only and hide it when all are idle."""
        visible_cards = [card for card in self.cards if not card.isHidden()]
        content_height = sum(card.height() for card in visible_cards) + (
            self.card_layout.spacing() * max(0, len(visible_cards) - 1)
        )
        self.scroll_area.setFixedHeight(min(self.max_scroll_height, content_height))
        self.scroll_area.setVisible(bool(visible_cards) and not self.settings.collapsed)
        self.adjustSize()
        self.schedule_clamp()

    def _build_cards(self) -> None:
        for card in self.cards:
            card.deleteLater()
        self.cards.clear()
        for index, config in enumerate(self.settings.endpoints):
            card = EndpointCard(
                config,
                self.translator,
                professional=self.settings.mode == "professional",
                avatar_path=self._avatar_path() if index == 0 else None,
                parent=self.card_container,
            )
            card.hide()
            self.card_layout.insertWidget(index, card)
            self.cards.append(card)
        self._refresh_card_area()

    def _apply_theme(self) -> None:
        window, card, text, muted = THEMES[self.settings.theme]
        if self.settings.theme == "system":
            self.setStyleSheet("")
            return
        self.setStyleSheet(
            f"QWidget#progressWindow {{ background: {window}; color: {text}; "
            f"border-radius: {_dock_px(9)}px; }}"
            f" QFrame#endpointCard {{ background: {card}; color: {text}; }}"
            f" QLabel {{ color: {text}; }} QLabel#timestampLabel {{ color: {muted}; }}"
            f" QPushButton {{ background: {card}; color: {text}; border: none; "
            f"padding: {_dock_px(4)}px; }}"
        )

    def mark_record_received(self, endpoint: EndpointId) -> None:
        """Record local receipt time; remote probe clocks need not match this host."""
        self._received_at[endpoint] = datetime.now().astimezone()

    def render(self, reduction: Reduction) -> None:
        """Render reducer state and consume only successful-task transitions for avatars."""
        self._received_at = {
            endpoint: received_at
            for endpoint, received_at in self._received_at.items()
            if endpoint in reduction.state.endpoints
        }
        self._handled_completions.intersection_update(reduction.state.tasks)
        for transition in reduction.transitions:
            if (
                transition.kind == "task_success"
                and transition.task is not None
                and transition.task not in self._handled_completions
            ):
                self._handled_completions.add(transition.task)
                self._rotate_avatar()
        for card in self.cards:
            tasks = card._tasks_for(reduction.state, card.config)
            has_active_queue = any(task.status in {"running", "pending"} for task in tasks)
            card.setVisible(has_active_queue)
            if has_active_queue:
                received_at = next(
                    (
                        value for endpoint, value in self._received_at.items()
                        if endpoint.host == card.config.host and endpoint.port == card.config.port
                    ),
                    None,
                )
                card.update_state(reduction.state, received_at=received_at)
        self._refresh_card_area()

    def _rotate_avatar(self) -> None:
        if not self.settings.avatar_enabled or len(self.settings.avatar_paths) < 2:
            return
        self.avatar_index = (self.avatar_index + 1) % len(self.settings.avatar_paths)
        if self.cards and self.cards[0].avatar is not None:
            self.cards[0].avatar.set_path(self._avatar_path() or "")

    def safe_save(self, settings: AppSettings) -> bool:
        """Persist settings with localized feedback, leaving the UI operational."""
        try:
            self.store.save(settings)
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.warning(
                self,
                self.translator("settings"),
                f"{self.translator('error')}: {exc}",
            )
            return False
        return True

    def _save_internal(self, **changes: object) -> bool:
        """Persist UI state without copying process-local runtime overrides."""
        persisted = replace(self.persisted_settings, **changes)
        if not self.safe_save(persisted):
            return False
        self.persisted_settings = persisted
        self.settings = replace(self.settings, **changes)
        return True

    def set_collapsed(self, collapsed: bool, *, persist: bool = True) -> None:
        if persist:
            if not self._save_internal(collapsed=collapsed):
                return
        else:
            self.settings = replace(self.settings, collapsed=collapsed)
        self.collapse_button.setText("▸" if collapsed else "▾")
        self.collapse_button.setToolTip(self.translator("expand" if collapsed else "collapse"))
        self.collapse_button.setAccessibleName(
            self.translator("expand" if collapsed else "collapse")
        )
        self._refresh_card_area()

    def set_dock_enabled(self, enabled: bool) -> None:
        if not self._save_internal(dock_enabled=enabled):
            return
        self.setVisible(enabled)
        if enabled:
            self.raise_()
            self.activateWindow()
            self.schedule_clamp()
        self.settings_applied.emit(self.settings)

    def apply_settings(self, settings: AppSettings) -> None:
        """Atomically apply already-validated, already-persisted settings to widgets."""
        self.persisted_settings = settings
        self.settings = settings
        self.translator = Translator(settings.language)
        self.avatar_index = 0
        self._handled_completions.clear()
        self.source_status.clear()
        self.source_status.hide()
        self.setWindowTitle(self.translator("app_title"))
        self.drag_handle.setToolTip(self.translator("drag"))
        self.drag_handle.setAccessibleName(self.translator("drag"))
        self.gear_button.setToolTip(self.translator("settings"))
        self.gear_button.setAccessibleName(self.translator("settings"))
        self.close_button.setToolTip(self.translator("close"))
        self.close_button.setAccessibleName(self.translator("close"))
        self.setWindowOpacity(settings.opacity / 100)
        self._build_cards()
        self._apply_theme()
        self.set_collapsed(settings.collapsed, persist=False)
        self.setVisible(settings.dock_enabled)
        self.schedule_clamp()

    def open_settings(self) -> None:
        dialog_settings = replace(
            self.persisted_settings,
            dock_enabled=self.settings.dock_enabled,
        )
        dialog = SettingsDialog(dialog_settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.save_dialog_settings(dialog)

    def save_dialog_settings(self, dialog: SettingsDialog) -> bool:
        """Persist public settings first; revert them if the atomic key operation fails."""
        try:
            candidate = dialog.result_settings()
            dialog.validate_serverchan_key_action(candidate)
        except (AttributeError, TypeError, ValueError):
            dialog.serverchan_sendkey.clear()
            QMessageBox.warning(
                self, self.translator("settings"), self.translator("serverchan_save_error")
            )
            return False
        if not self.safe_save(candidate):
            dialog.serverchan_sendkey.clear()
            return False
        try:
            dialog.commit_serverchan_key()
        except (OSError, ValueError):
            try:
                self.store.save(self.persisted_settings)
                message = self.translator("serverchan_save_error")
            except (OSError, TypeError, ValueError):
                message = self.translator("serverchan_partial_save_error")
            QMessageBox.warning(self, self.translator("settings"), message)
            return False
        finally:
            dialog.serverchan_sendkey.clear()
        self.apply_settings(candidate)
        self.settings_applied.emit(candidate)
        return True

    def clamp_to(self, available: QRect) -> QPoint:
        maximum_x = max(available.left(), available.right() - self.width() + 1)
        maximum_y = max(available.top(), available.bottom() - self.height() + 1)
        point = QPoint(
            min(max(self.x(), available.left()), maximum_x),
            min(max(self.y(), available.top()), maximum_y),
        )
        if self.pos() != point:
            self._clamping = True
            try:
                self.move(point)
            finally:
                self._clamping = False
        return point

    def schedule_clamp(self) -> None:
        if self._clamp_pending or self._clamping:
            return
        self._clamp_pending = True

        def perform() -> None:
            self._clamp_pending = False
            screen = QApplication.screenAt(self.frameGeometry().center())
            screen = screen or QApplication.primaryScreen()
            if screen is not None:
                self.clamp_to(screen.availableGeometry())

        QTimer.singleShot(0, perform)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.schedule_clamp()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        arrows = {
            Qt.Key.Key_Left: QPoint(-10, 0),
            Qt.Key.Key_Right: QPoint(10, 0),
            Qt.Key.Key_Up: QPoint(0, -10),
            Qt.Key.Key_Down: QPoint(0, 10),
        }
        delta = arrows.get(event.key())
        if delta is not None and event.modifiers() & Qt.KeyboardModifier.AltModifier:
            self.move(self.pos() + delta)
            self.persist_position()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Q and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            QApplication.quit()
            event.accept()
            return
        super().keyPressEvent(event)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in {QEvent.Type.LayoutRequest, QEvent.Type.StyleChange}:
            self.schedule_clamp()

    def reset_position(self, available: QRect | None = None) -> QPoint:
        screen = (
            QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        )
        geometry = available or screen.availableGeometry()
        self.adjustSize()
        self.move(geometry.left() + 12, geometry.bottom() - self.height() - 11)
        point = self.clamp_to(geometry)
        screen_name = screen.name() if screen else ""
        self._save_internal(position=WindowPosition(screen_name, point.x(), point.y()))
        return point

    def _restore_position(self) -> None:
        position = self.settings.position
        screens = QApplication.screens()
        screen = next((item for item in screens if item.name() == position.screen), None)
        screen = screen or QApplication.primaryScreen()
        if position.x is None or position.y is None:
            self.reset_position(screen.availableGeometry())
        else:
            self.move(position.x, position.y)
            point = self.clamp_to(screen.availableGeometry())
            restored = WindowPosition(screen.name(), point.x(), point.y())
            if restored != position:
                self._save_internal(position=restored)

    def persist_position(self) -> None:
        screen = (
            QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        )
        point = self.clamp_to(screen.availableGeometry())
        self._save_internal(position=WindowPosition(screen.name(), point.x(), point.y()))

    def show_source_error(self, message_key: str) -> None:
        message = self.translator(message_key)
        self.source_status.setText(message)
        self.source_status.setToolTip(message)
        self.source_status.show()
        for card in self.cards:
            card.status_label.setText(self.translator("error"))
            card.status_label.setToolTip(message)
        self.schedule_clamp()

    def clear_source_error(self) -> None:
        self.source_status.clear()
        self.source_status.setToolTip("")
        self.source_status.hide()
        for card in self.cards:
            card.status_label.setToolTip("")
        layout = self.layout()
        if layout is not None:
            layout.activate()
        self.adjustSize()
        self.schedule_clamp()
