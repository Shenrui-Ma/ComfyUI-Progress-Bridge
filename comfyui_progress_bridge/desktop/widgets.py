"""Native PyQt6 widgets for the compact multi-endpoint progress dock."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from PyQt6.QtCore import QEvent, QPoint, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
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

from comfyui_progress_bridge.monitor.models import (
    MonitorState,
    Reduction,
    TaskKey,
    TaskState,
)

from .i18n import LANGUAGES, Translator
from .settings import AppSettings, EndpointConfig, SettingsStore, WindowPosition

THEMES = {
    "dark": ("#171A21", "#232733", "#F4F6FC", "#AAB1C3"),
    "light": ("#EEF1F7", "#FFFFFF", "#202430", "#60697A"),
    "system": ("palette(window)", "palette(base)", "palette(text)", "palette(mid)"),
}


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

    def __init__(self, size: int = 54, parent: QWidget | None = None) -> None:
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
    CARD_WIDTH = 328

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
        self.setObjectName("endpointCard")
        self.setFixedWidth(self.CARD_WIDTH)
        self.setStyleSheet(
            f"QFrame#endpointCard {{ border: 2px solid {config.color}; "
            "border-radius: 7px; padding: 5px; }"
        )
        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 7, 9, 7)
        outer.setSpacing(9)
        self.avatar: AvatarLabel | None = None
        if avatar_path is not None:
            self.avatar = AvatarLabel(parent=self)
            self.avatar.set_path(avatar_path)
            outer.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignTop)

        body = QVBoxLayout()
        body.setSpacing(3)
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
        details.setContentsMargins(0, 3, 0, 0)
        details.setSpacing(2)
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
        self.setFixedHeight(151 if professional else 67)

    @staticmethod
    def _tasks_for(state: MonitorState, config: EndpointConfig) -> list[TaskState]:
        return [
            task
            for key, task in state.tasks.items()
            if key.endpoint.host == config.host and key.endpoint.port == config.port
        ]

    def update_state(self, state: MonitorState) -> None:
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
            f"{self.translator('updated')}: {datetime.now().astimezone().strftime('%H:%M:%S')}"
        )


class SettingsDialog(QDialog):
    """Native settings editor, including endpoint and six avatar slots."""

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.original = settings
        self.t = Translator(settings.language)
        self.setWindowTitle(self.t("settings"))
        self.resize(690, 520)
        root = QVBoxLayout(self)
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
        root.addLayout(form)

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
        root.addWidget(self.endpoint_table)

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
        root.addLayout(avatar_box)
        self.validation_label = QLabel("")
        self.validation_label.setObjectName("validationError")
        self.validation_label.setWordWrap(True)
        self.validation_label.setStyleSheet("color: #d33;")
        root.addWidget(self.validation_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(self.t("save"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self.t("cancel"))
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        self.reset_button = QPushButton(self.t("reset_position"))
        if parent is not None and hasattr(parent, "reset_position"):
            self.reset_button.clicked.connect(parent.reset_position)
        buttons.addButton(self.reset_button, QDialogButtonBox.ButtonRole.ResetRole)
        root.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        try:
            self.result_settings()
        except (AttributeError, TypeError, ValueError) as exc:
            message = f"{self.t('error')}: {exc}"
            self.validation_label.setText(message)
            QMessageBox.warning(self, self.t("settings"), message)
            return
        self.validation_label.clear()
        self.accept()

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
        )


class ProgressWindow(QWidget):
    """Frameless, always-on-top, bounded desktop progress dock."""

    settings_applied = pyqtSignal(object)
    max_scroll_height = 470

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
        # Runtime settings may include process-local CLI substitutions (for
        # example --show or --demo). Never use them as the source of truth for
        # an implicit save: mutable UI state is merged into this persisted base.
        self.persisted_settings = (
            persisted_settings if persisted_settings is not None else settings
        )
        self.store = store or SettingsStore()
        self.translator = Translator(settings.language)
        # Launches always begin with the first configured expression. Only a
        # successful task transition advances it during this process lifetime.
        self.avatar_index = 0
        self._handled_completions: set[TaskKey] = set()
        self._clamp_pending = False
        self._clamping = False
        self.cards: list[EndpointCard] = []
        self.setObjectName("progressWindow")
        self.setWindowTitle(self.translator("app_title"))
        self.setFixedWidth(352)
        self.setWindowOpacity(settings.opacity / 100)
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)
        bar = QHBoxLayout()
        self.drag_handle = DragHandle("⠿", self)
        self.drag_handle.setToolTip(self.translator("drag"))
        self.drag_handle.setAccessibleName(self.translator("drag"))
        self.drag_handle.setAccessibleDescription("Alt+Arrow keys move the window")
        self.drag_handle.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        bar.addWidget(self.drag_handle, 1)
        self.collapse_button = QPushButton("▾")
        self.collapse_button.setAccessibleName(self.translator("collapse"))
        self.collapse_button.setFixedWidth(28)
        self.collapse_button.clicked.connect(
            lambda: self.set_collapsed(not self.settings.collapsed)
        )
        bar.addWidget(self.collapse_button)
        self.gear_button = QPushButton("⚙")
        self.gear_button.setFixedWidth(28)
        self.gear_button.setToolTip(self.translator("settings"))
        self.gear_button.setAccessibleName(self.translator("settings"))
        self.gear_button.setAccessibleDescription(self.translator("settings"))
        self.gear_button.clicked.connect(self.open_settings)
        bar.addWidget(self.gear_button)
        self.close_button = QPushButton("×")
        self.close_button.setFixedWidth(28)
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
        self.card_layout.setSpacing(8)
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
            lambda reason: self.set_dock_enabled(True)
            if reason == QSystemTrayIcon.ActivationReason.Trigger
            else None
        )
        self.tray_icon.show()

    def _avatar_path(self) -> str | None:
        if not self.settings.avatar_enabled or not self.settings.avatar_paths:
            return None
        return self.settings.avatar_paths[self.avatar_index % len(self.settings.avatar_paths)]

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
            )
            self.card_layout.insertWidget(index, card)
            self.cards.append(card)
        content_height = sum(card.height() for card in self.cards) + 8 * max(0, len(self.cards) - 1)
        self.scroll_area.setFixedHeight(min(self.max_scroll_height, content_height))
        self.adjustSize()
        self.schedule_clamp()

    def _apply_theme(self) -> None:
        window, card, text, muted = THEMES[self.settings.theme]
        if self.settings.theme == "system":
            self.setStyleSheet("")
            return
        self.setStyleSheet(
            f"QWidget#progressWindow {{ background: {window}; color: {text}; border-radius: 9px; }}"
            f" QFrame#endpointCard {{ background: {card}; color: {text}; }}"
            f" QLabel {{ color: {text}; }} QLabel#timestampLabel {{ color: {muted}; }}"
            f" QPushButton {{ background: {card}; color: {text}; border: none; padding: 4px; }}"
        )

    def render(self, reduction: Reduction) -> None:
        """Render reducer state and consume only successful-task transitions for avatars."""
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
            card.update_state(reduction.state)

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
        self.scroll_area.setVisible(not collapsed)
        self.collapse_button.setText("▸" if collapsed else "▾")
        self.collapse_button.setToolTip(self.translator("expand" if collapsed else "collapse"))
        self.collapse_button.setAccessibleName(
            self.translator("expand" if collapsed else "collapse")
        )
        self.adjustSize()
        self.schedule_clamp()

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
        dialog = SettingsDialog(self.persisted_settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            candidate = dialog.result_settings()
        except (AttributeError, TypeError, ValueError) as exc:
            QMessageBox.warning(
                self, self.translator("settings"), f"{self.translator('error')}: {exc}"
            )
            return
        if not self.safe_save(candidate):
            return
        self.apply_settings(candidate)
        self.settings_applied.emit(candidate)

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

    def show_source_error(self, message: str) -> None:
        self.source_status.setText(f"{self.translator('error')}: {message}")
        self.source_status.setToolTip(message)
        self.source_status.show()
        for card in self.cards:
            card.status_label.setText(self.translator("error"))
            card.status_label.setToolTip(message)
        self.schedule_clamp()
