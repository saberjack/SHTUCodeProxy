from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import cli
import proxy
from config_store import (
    AppConfig,
    CODEX_SANDBOX_MODES,
    DEFAULT_API_FORMAT,
    DEFAULT_CHAT_COMPLETIONS_URL,
    DEFAULT_RESPONSES_URL,
    MODEL_ENV_KEYS,
    ModelConfig,
    config_path,
    default_claude_path,
    default_claude_settings_path,
    load_config,
    save_config,
)
from platform_utils import (
    default_codex_auth_path,
    default_codex_config_path,
    is_windows,
    launch_claude,
    launch_script_text,
    portable_claude_path,
    portable_codex_auth_path,
    portable_codex_config_path,
    portable_settings_path,
)


IOS_QSS = """
* {
  font-family: "Segoe UI", "SF Pro Text", "Arial";
  font-size: 13px;
  color: #000000;
}

QMainWindow, QWidget#Root, QScrollArea, QScrollArea > QWidget > QWidget {
  background: #F2F2F7;
}

QFrame#NavigationBar {
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FFFFFF, stop:1 #E8E8EC);
  border: 0px;
  border-bottom: 1px solid #E5E5EA;
  border-radius: 0px;
}

QLabel#WindowTitle {
  font-size: 18px;
  font-weight: 700;
  color: #000000;
  background: transparent;
}

QLabel#WindowSubtitle, QLabel#Muted, QLabel#CardHint, QLabel#SectionHint {
  color: rgba(60, 60, 67, 153);
  background: transparent;
}

QLabel#SectionTitle, QLabel#CardTitle, QLabel#StatusLabel {
  font-size: 14px;
  font-weight: 700;
  color: #000000;
  background: transparent;
}

QLabel#DangerText {
  color: #D70015;
  font-weight: 700;
  background: transparent;
}

QFrame#GlassCard, QGroupBox#GlassGroup {
  background: rgba(255, 255, 255, 184);
  border: 1px solid rgba(255, 255, 255, 230);
  border-radius: 16px;
}

QGroupBox#GlassGroup {
  margin-top: 18px;
  padding: 18px 16px 16px 16px;
}

QGroupBox#GlassGroup::title {
  subcontrol-origin: margin;
  subcontrol-position: top left;
  left: 16px;
  padding: 0px 8px;
  color: #000000;
  font-size: 14px;
  font-weight: 700;
  background: #F2F2F7;
}

QGroupBox#CompactGroup {
  background: rgba(255, 255, 255, 184);
  border: 1px solid rgba(255, 255, 255, 230);
  border-radius: 14px;
  margin-top: 16px;
  padding: 12px 14px 10px 14px;
}

QGroupBox#CompactGroup::title {
  subcontrol-origin: margin;
  subcontrol-position: top left;
  left: 16px;
  padding: 0px 8px;
  color: #000000;
  font-size: 13px;
  font-weight: 700;
  background: #F2F2F7;
}

QFrame#AdvancedCard {
  background: rgba(255, 255, 255, 184);
  border: 1px solid rgba(255, 255, 255, 230);
  border-radius: 14px;
}

QLabel#InlineSectionTitle {
  color: #000000;
  font-size: 13px;
  font-weight: 700;
}

QPushButton {
  min-height: 34px;
  padding: 7px 14px;
  border-radius: 10px;
  border: 1px solid rgba(255, 255, 255, 230);
  color: #003A70;
  font-weight: 650;
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FFFFFF, stop:1 #EFEFF4);
}

QPushButton:hover {
  background: #E8F2FF;
  border: 1px solid rgba(0, 122, 255, 110);
}

QPushButton:pressed {
  background: #D1D1D6;
  margin-top: 1px;
  margin-bottom: -1px;
}

QPushButton#PrimaryButton {
  color: #FFFFFF;
  border: 1px solid #007AFF;
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2997FF, stop:1 #007AFF);
}

QPushButton#PrimaryButton:hover {
  background: #E8F2FF;
  color: #003A70;
  border: 1px solid rgba(0, 122, 255, 160);
}

QPushButton#DangerButton {
  color: #D70015;
  border: 1px solid rgba(215, 0, 21, 90);
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FFFFFF, stop:1 #FFF4F5);
}

QPushButton#NeutralButton {
  color: #2C2C2E;
  border: 1px solid #D1D1D6;
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FFFFFF, stop:1 #F2F2F7);
}

QPushButton#NeutralButton:hover {
  color: #003A70;
  background: #E8F2FF;
  border: 1px solid rgba(0, 122, 255, 110);
}

QPushButton#SecondaryButton {
  color: #003A70;
  border: 1px solid rgba(0, 122, 255, 90);
  background: rgba(232, 242, 255, 210);
}

QPushButton#SecondaryButton:hover {
  background: #DCEBFF;
  border: 1px solid rgba(0, 122, 255, 150);
}

QPushButton#RunButton {
  color: #0A3A5F;
  border: 1px solid rgba(0, 122, 255, 70);
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #F8FBFF, stop:1 #EAF4FF);
}

QPushButton#RunButton:hover {
  background: #E8F2FF;
  border: 1px solid rgba(0, 122, 255, 130);
}

QFrame#AdvancedActions {
  background: transparent;
  border: none;
}

QPushButton#OptionalButton {
  min-height: 38px;
  min-width: 172px;
  color: #003A70;
  border: 1px solid rgba(0, 122, 255, 145);
  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FFFFFF, stop:1 #EDF6FF);
}

QPushButton#OptionalButton:hover {
  color: #001D35;
  background: #DCEBFF;
  border: 1px solid rgba(0, 122, 255, 180);
}

QPushButton#OptionalButton:pressed {
  background: #D1D1D6;
  margin-top: 1px;
  margin-bottom: -1px;
}

QLineEdit, QComboBox, QTextEdit, QListWidget, QTableWidget {
  background: rgba(255, 255, 255, 225);
  border: 1px solid #E5E5EA;
  border-radius: 10px;
  padding: 7px 10px;
  selection-background-color: #007AFF;
  selection-color: #FFFFFF;
}

QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QListWidget:focus {
  border: 1px solid #007AFF;
}

QTableWidget {
  gridline-color: #E5E5EA;
  alternate-background-color: rgba(242, 242, 247, 160);
}

QTableWidget::item {
  padding: 7px 8px;
  border-bottom: 1px solid #E5E5EA;
}

QTableWidget::item:selected {
  background: #E8F2FF;
  color: #007AFF;
}

QHeaderView::section {
  background: rgba(242, 242, 247, 220);
  color: rgba(60, 60, 67, 153);
  border: 0px;
  border-bottom: 1px solid #E5E5EA;
  padding: 7px 8px;
  font-weight: 700;
}

QLineEdit[readOnly="true"] {
  background: rgba(242, 242, 247, 190);
  color: rgba(60, 60, 67, 153);
}

QComboBox::drop-down {
  width: 26px;
  border: 0px;
  background: transparent;
}

QComboBox::down-arrow {
  image: none;
  width: 0px;
  height: 0px;
}

QRadioButton {
  background: rgba(255, 255, 255, 185);
  border: 1px solid rgba(255, 255, 255, 230);
  border-radius: 14px;
  padding: 15px 22px;
  font-size: 17px;
  font-weight: 700;
  color: #000000;
}

QRadioButton:hover {
  background: #E8F2FF;
}

QRadioButton:checked {
  background: rgba(232, 242, 255, 230);
  color: #007AFF;
  border: 1px solid rgba(0, 122, 255, 150);
}

QRadioButton::indicator {
  width: 0px;
  height: 0px;
}

QListWidget::item {
  min-height: 30px;
  padding: 5px 8px;
  border-radius: 8px;
}

QListWidget::item:selected {
  background: #E8F2FF;
  color: #007AFF;
}

QScrollBar:vertical {
  background: transparent;
  width: 12px;
  margin: 6px 2px 6px 2px;
}

QScrollBar::handle:vertical {
  background: rgba(60, 60, 67, 55);
  border-radius: 6px;
  min-height: 40px;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
  height: 0px;
}
"""


class FocusGlowLineEdit(QLineEdit):
    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self._glow = QGraphicsDropShadowEffect(self)
        self._glow.setBlurRadius(16)
        self._glow.setOffset(0, 0)
        self._glow.setColor(QColor(0, 122, 255, 70))

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.setGraphicsEffect(self._glow)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.setGraphicsEffect(None)


class FocusWheelComboBox(QComboBox):
    def wheelEvent(self, event) -> None:  # noqa: N802
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()


def build_app_icon() -> QIcon:
    icon_path = Path(__file__).resolve().parent / "assets" / "shtucodeproxy.ico"
    if icon_path.exists():
        return QIcon(str(icon_path))
    pixmap = QPixmap(256, 256)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    rect = pixmap.rect().adjusted(10, 10, -10, -10)
    gradient = QLinearGradient(0, 0, 0, 256)
    gradient.setColorAt(0, QColor("#FFFFFF"))
    gradient.setColorAt(1, QColor("#E8F2FF"))
    painter.setBrush(gradient)
    painter.setPen(QPen(QColor(255, 255, 255, 235), 4))
    painter.drawRoundedRect(rect, 56, 56)
    painter.setPen(QPen(QColor(0, 122, 255, 90), 2))
    painter.drawRoundedRect(rect.adjusted(8, 8, -8, -8), 48, 48)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#007AFF"))
    painter.drawRoundedRect(62, 82, 132, 27, 13, 13)
    painter.drawRoundedRect(62, 148, 132, 27, 13, 13)
    painter.setBrush(QColor(255, 255, 255, 232))
    painter.drawRoundedRect(78, 92, 27, 72, 13, 13)
    painter.drawRoundedRect(152, 92, 27, 72, 13, 13)
    painter.setPen(QColor(0, 54, 110, 255))
    painter.setFont(QFont("Segoe UI", 58, QFont.Bold))
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "S")
    painter.end()
    return QIcon(pixmap)


class IosProxyApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SHTUCodeProxy 4.1.2")
        self.setWindowIcon(build_app_icon())
        self.resize(1420, 960)
        self.setMinimumSize(1220, 780)
        self.setStyleSheet(IOS_QSS)

        self.config_data = load_config()
        save_config(self.config_data)
        self.server: ThreadingHTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        self.selected_index: int | None = None
        self.danger_sandbox_confirmed = False
        self.model_env_combos: dict[str, QComboBox] = {}

        self.build_ui()
        self.refresh_model_list()
        self.refresh_connection_status()
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self.refresh_connection_status)
        self.status_timer.start(5000)
        QTimer.singleShot(300, self.show_first_run_tip)

    def add_shadow(self, widget: QWidget, blur: int = 20, y: int = 2) -> None:
        shadow = QGraphicsDropShadowEffect(widget)
        shadow.setBlurRadius(blur)
        shadow.setOffset(0, y)
        shadow.setColor(QColor(0, 0, 0, 15))
        widget.setGraphicsEffect(shadow)

    def card_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("GlassCard")
        self.add_shadow(frame)
        return frame

    def group_card(self, title: str, compact: bool = False) -> QGroupBox:
        group = QGroupBox(title)
        group.setObjectName("CompactGroup" if compact else "GlassGroup")
        self.add_shadow(group)
        return group

    def build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        nav = QFrame()
        nav.setObjectName("NavigationBar")
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(24, 12, 24, 12)
        title_box = QVBoxLayout()
        title = QLabel("SHTUCodeProxy 4.1.2")
        title.setObjectName("WindowTitle")
        subtitle = QLabel("Claude Code and Codex local bridge")
        subtitle.setObjectName("WindowSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        nav_layout.addLayout(title_box)
        nav_layout.addStretch(1)
        root_layout.addWidget(nav)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        main = QVBoxLayout(content)
        main.setContentsMargins(32, 28, 32, 28)
        main.setSpacing(18)
        scroll.setWidget(content)
        root_layout.addWidget(scroll)
        self.setCentralWidget(root)

        self.client_group = QButtonGroup(self)
        mode_card = self.card_frame()
        mode_layout = QHBoxLayout(mode_card)
        mode_layout.setContentsMargins(18, 18, 18, 18)
        mode_layout.setSpacing(16)
        self.claude_radio = QRadioButton("Claude Code")
        self.codex_radio = QRadioButton("Codex CLI / Desktop")
        self.claude_radio.setChecked(True)
        self.client_group.addButton(self.claude_radio)
        self.client_group.addButton(self.codex_radio)
        self.claude_radio.toggled.connect(self.refresh_mode_hint)
        self.codex_radio.toggled.connect(self.refresh_mode_hint)
        mode_layout.addWidget(self.claude_radio)
        mode_layout.addWidget(self.codex_radio)
        main.addWidget(mode_card)

        quick = self.group_card("Quick Start")
        quick_layout = QHBoxLayout(quick)
        quick_layout.setSpacing(14)
        quick_layout.setContentsMargins(16, 22, 16, 16)
        quick_layout.addWidget(self.step_card("Fast Start", "Save + Connect", "Recommended: write selected client config and start proxy.", self.setup_selected_client, primary=True))
        quick_layout.addWidget(self.step_card("1. Save", "Save Config", "Save model, routing, key, URL, and server settings.", self.save, kind="neutral"))
        quick_layout.addWidget(self.step_card("2. Connect Client", "Write Client Config", "One-time setup: write Claude settings or Codex config.toml.", self.write_selected_client_config, kind="secondary"))
        quick_layout.addWidget(self.step_card("3. Run", "Start Proxy / Launch", "Claude mode opens Claude Code; Codex mode starts proxy only.", self.run_selected_client, kind="run"))
        main.addWidget(quick)

        safety = self.group_card("Connection, Safety, and Recovery")
        safety_layout = QGridLayout(safety)
        safety_layout.setContentsMargins(16, 24, 16, 16)
        safety_layout.setHorizontalSpacing(10)
        self.connection_label = QLabel("Proxy status: not checked")
        self.connection_label.setObjectName("StatusLabel")
        safety_layout.addWidget(self.connection_label, 0, 0, 1, 1)
        safety_layout.addWidget(self.button("Refresh Status", self.refresh_connection_status), 0, 1)
        safety_layout.addWidget(self.button("Check Codex Health", self.check_codex_health), 0, 2)
        safety_layout.addWidget(self.button("Restore Recent Client Backup", lambda: self.restore_client_config(False)), 0, 3)
        safety_layout.addWidget(self.button("Restore Original Client Config", lambda: self.restore_client_config(True), danger=True), 0, 4)
        hint = QLabel("Backups cover the selected client mode: Claude settings or Codex config/auth.")
        hint.setObjectName("SectionHint")
        safety_layout.addWidget(hint, 1, 0, 1, 5)
        safety_layout.setColumnStretch(0, 1)
        main.addWidget(safety)

        server = self.group_card("Server")
        server_layout = QGridLayout(server)
        server_layout.setContentsMargins(16, 24, 16, 16)
        server_layout.setHorizontalSpacing(10)
        self.host_edit = FocusGlowLineEdit(self.config_data.host)
        self.port_edit = FocusGlowLineEdit(str(self.config_data.port))
        self.default_model_edit = FocusGlowLineEdit(self.config_data.default_model_id)
        self.default_model_edit.setReadOnly(True)
        self.timeout_edit = FocusGlowLineEdit(str(self.config_data.timeout))
        self.claude_path_edit = FocusGlowLineEdit(self.config_data.claude_path)
        self.claude_settings_path_edit = FocusGlowLineEdit(self.config_data.claude_settings_path)
        self.codex_config_path_edit = FocusGlowLineEdit(self.config_data.codex_config_path)
        self.codex_auth_path_edit = FocusGlowLineEdit(self.config_data.codex_auth_path)
        self.add_field(server_layout, 0, "Host", self.host_edit)
        self.add_field(server_layout, 0, "Port", self.port_edit, 2)
        self.add_field(server_layout, 0, "Current Main Model", self.default_model_edit, 4)
        self.add_field(server_layout, 0, "Timeout", self.timeout_edit, 6)
        self.add_path_row(server_layout, 1, "Claude Code Path", self.claude_path_edit, self.browse_claude_path)
        self.add_path_row(server_layout, 2, "Claude Settings Path", self.claude_settings_path_edit, self.browse_claude_settings_path)
        self.add_path_row(server_layout, 3, "Codex config.toml Path", self.codex_config_path_edit, self.browse_codex_config_path)
        self.add_path_row(server_layout, 4, "Codex auth.json Path", self.codex_auth_path_edit, self.browse_codex_auth_path)
        main.addWidget(server)

        routing = self.group_card("Claude Model Routing")
        routing_layout = QGridLayout(routing)
        routing_layout.setContentsMargins(16, 24, 16, 16)
        routing_layout.setHorizontalSpacing(10)
        routing_hint = QLabel("Choose model routing. Defaults can all be the same.")
        routing_hint.setObjectName("SectionHint")
        routing_layout.addWidget(routing_hint, 0, 0, 1, 5)
        routes = [
            ("Main Model", "ANTHROPIC_MODEL"),
            ("Haiku Model", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
            ("Sonnet Model", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
            ("Opus Model", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
            ("Reasoning Model", "ANTHROPIC_REASONING_MODEL"),
        ]
        for column, (label, key) in enumerate(routes):
            box = QVBoxLayout()
            box.addWidget(QLabel(label))
            combo = FocusWheelComboBox()
            combo.currentTextChanged.connect(self.on_model_route_changed)
            self.model_env_combos[key] = combo
            box.addWidget(combo)
            routing_layout.addLayout(box, 1, column)
        codex_box = QHBoxLayout()
        codex_box.addWidget(QLabel("Codex Model"))
        self.codex_model_combo = FocusWheelComboBox()
        self.codex_model_combo.currentTextChanged.connect(self.on_codex_model_changed)
        codex_box.addWidget(self.codex_model_combo)
        codex_box.addWidget(QLabel("Sandbox"))
        self.codex_sandbox_combo = FocusWheelComboBox()
        self.codex_sandbox_combo.addItems(CODEX_SANDBOX_MODES)
        self.codex_sandbox_combo.setCurrentText(self.config_data.codex_sandbox_mode)
        self.codex_sandbox_combo.currentTextChanged.connect(self.on_codex_sandbox_changed)
        codex_box.addWidget(self.codex_sandbox_combo)
        codex_hint = QLabel("Writes Codex model, auth key, sandbox_mode, and hooks.")
        codex_hint.setObjectName("SectionHint")
        codex_box.addWidget(codex_hint)
        codex_box.addStretch(1)
        routing_layout.addLayout(codex_box, 2, 0, 1, 5)
        self.route_summary = QLabel("")
        self.route_summary.setObjectName("StatusLabel")
        routing_layout.addWidget(self.route_summary, 3, 0, 1, 5)
        main.addWidget(routing)

        body = QHBoxLayout()
        body.setSpacing(18)
        models_group = self.group_card("Models")
        models_layout = QVBoxLayout(models_group)
        models_layout.setContentsMargins(16, 24, 16, 16)
        self.model_table = QTableWidget(0, 2)
        self.model_table.setHorizontalHeaderLabels(("Model ID", "Upstream Model"))
        self.model_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.model_table.verticalHeader().setVisible(False)
        self.model_table.setShowGrid(True)
        self.model_table.setAlternatingRowColors(True)
        self.model_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.model_table.setSelectionMode(QTableWidget.SingleSelection)
        self.model_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.model_table.itemSelectionChanged.connect(self.on_model_table_selection_changed)
        models_layout.addWidget(self.model_table)
        model_buttons = QHBoxLayout()
        model_buttons.addWidget(self.button("New", self.new_model))
        model_buttons.addWidget(self.button("Delete", self.delete_model))
        model_buttons.addStretch(1)
        models_layout.addLayout(model_buttons)
        body.addWidget(models_group, 1)

        edit_group = self.group_card("Model Config")
        edit_layout = QGridLayout(edit_group)
        edit_layout.setContentsMargins(16, 24, 16, 16)
        self.name_edit = FocusGlowLineEdit()
        self.model_id_edit = FocusGlowLineEdit()
        self.base_url_edit = FocusGlowLineEdit()
        self.api_key_edit = FocusGlowLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.upstream_model_edit = FocusGlowLineEdit()
        self.api_format_combo = FocusWheelComboBox()
        self.api_format_combo.addItems(("responses", "chat_completions"))
        self.api_format_combo.currentTextChanged.connect(self.on_api_format_changed)
        for row, (label, widget) in enumerate((
            ("Display Name", self.name_edit),
            ("Model ID for Claude Code", self.model_id_edit),
            ("Base URL", self.base_url_edit),
            ("API Key", self.api_key_edit),
            ("Upstream Model", self.upstream_model_edit),
            ("API Format", self.api_format_combo),
        )):
            edit_layout.addWidget(QLabel(label), row, 0)
            edit_layout.addWidget(widget, row, 1)
        model_hint = QLabel("Step 1: Fill API Key, Base URL, API Format, and Upstream Model. API Format changes Base URL automatically.")
        model_hint.setObjectName("SectionHint")
        edit_layout.addWidget(model_hint, 6, 0, 1, 2)
        important_hint = QLabel("Important: For GPT models, choose API Format = responses.")
        important_hint.setObjectName("DangerText")
        edit_layout.addWidget(important_hint, 7, 0, 1, 2)
        edit_layout.addWidget(self.button("Apply Model Changes", self.apply_model, primary=True), 8, 1, alignment=Qt.AlignRight)
        edit_layout.setColumnStretch(1, 1)
        body.addWidget(edit_group, 2)
        main.addLayout(body)

        bottom = QHBoxLayout()
        self.status_label = QLabel("Status: Stopped")
        self.status_label.setObjectName("StatusLabel")
        bottom.addWidget(self.status_label)
        bottom.addStretch(1)
        bottom.addWidget(self.button("Start Proxy Only", self.start_proxy))
        bottom.addWidget(self.button("Stop Proxy", self.stop_proxy))
        main.addLayout(bottom)

        advanced = QFrame()
        advanced.setObjectName("AdvancedCard")
        self.add_shadow(advanced)
        advanced_layout = QVBoxLayout(advanced)
        advanced_layout.setContentsMargins(16, 10, 16, 14)
        advanced_layout.setSpacing(0)
        advanced_title = QLabel("Advanced / Optional")
        advanced_title.setObjectName("InlineSectionTitle")
        advanced_layout.addWidget(advanced_title)
        advanced_body = QHBoxLayout()
        advanced_body.setContentsMargins(0, 0, 0, 0)
        advanced_body.setSpacing(16)
        advanced_hint = QLabel("Optional: install a manual PowerShell launcher or copy env vars. Most users do not need these.")
        advanced_hint.setObjectName("SectionHint")
        advanced_hint.setWordWrap(True)
        advanced_hint.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        advanced_button_frame = QFrame()
        advanced_button_frame.setObjectName("AdvancedActions")
        advanced_button_frame.setMinimumSize(368, 42)
        advanced_button_frame.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        advanced_button_layout = QHBoxLayout(advanced_button_frame)
        advanced_button_layout.setContentsMargins(0, 0, 0, 0)
        advanced_button_layout.setSpacing(12)
        advanced_button_layout.addWidget(self.button("Copy Claude Config", self.copy_claude_config, kind="optional"))
        advanced_button_layout.addWidget(self.button("Install Launch Script", self.install_launch_script, kind="optional"))
        advanced_body.addWidget(advanced_hint, 1, alignment=Qt.AlignVCenter)
        advanced_body.addWidget(advanced_button_frame, 0, alignment=Qt.AlignRight | Qt.AlignTop)
        advanced_layout.addLayout(advanced_body)
        advanced.setMinimumHeight(96)
        advanced.setMaximumHeight(104)
        main.addWidget(advanced)

        logs = self.group_card("Logs")
        logs_layout = QVBoxLayout(logs)
        logs_layout.setContentsMargins(16, 24, 16, 16)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFixedHeight(120)
        logs_layout.addWidget(self.log_text)
        main.addWidget(logs)

    def button(self, text: str, slot, primary: bool = False, danger: bool = False, kind: str = "default") -> QPushButton:
        button = QPushButton(text)
        if primary:
            button.setObjectName("PrimaryButton")
        elif danger:
            button.setObjectName("DangerButton")
        elif kind == "neutral":
            button.setObjectName("NeutralButton")
        elif kind == "secondary":
            button.setObjectName("SecondaryButton")
        elif kind == "run":
            button.setObjectName("RunButton")
        elif kind == "optional":
            button.setObjectName("OptionalButton")
        button.clicked.connect(slot)
        return button

    def step_card(self, title: str, button_text: str, description: str, slot, primary: bool = False, kind: str = "default") -> QFrame:
        card = self.card_frame()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        title_label = QLabel(title)
        title_label.setObjectName("CardTitle")
        hint = QLabel(description)
        hint.setObjectName("CardHint")
        hint.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(hint, 1)
        layout.addWidget(self.button(button_text, slot, primary=primary, kind=kind))
        return card

    def add_field(self, layout: QGridLayout, row: int, label: str, widget: QWidget, column: int = 0) -> None:
        layout.addWidget(QLabel(label), row, column)
        layout.addWidget(widget, row, column + 1)

    def add_path_row(self, layout: QGridLayout, row: int, label: str, widget: QLineEdit, slot) -> None:
        layout.addWidget(QLabel(label), row, 0)
        layout.addWidget(widget, row, 1, 1, 6)
        layout.addWidget(self.button("Browse", slot), row, 7)

    def append_log(self, message: str) -> None:
        self.log_text.append(message)

    def info(self, title: str, message: str) -> None:
        QMessageBox.information(self, title, message)

    def warning(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    def error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

    def ask(self, title: str, message: str) -> bool:
        return QMessageBox.question(self, title, message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def client_mode(self) -> str:
        return "codex" if self.codex_radio.isChecked() else "claude"

    def model_ids(self) -> list[str]:
        return [model.model_id for model in self.config_data.models]

    def refresh_model_list(self) -> None:
        self.model_table.blockSignals(True)
        self.model_table.setRowCount(0)
        for model in self.config_data.models:
            row = self.model_table.rowCount()
            self.model_table.insertRow(row)
            self.model_table.setItem(row, 0, QTableWidgetItem(model.model_id))
            self.model_table.setItem(row, 1, QTableWidgetItem(model.upstream_model))
        self.model_table.blockSignals(False)
        self.refresh_model_env_choices()
        if self.config_data.models:
            self.model_table.selectRow(0)
            self.load_model(0)

    def on_model_table_selection_changed(self) -> None:
        rows = self.model_table.selectionModel().selectedRows()
        if rows:
            self.load_model(rows[0].row())

    def refresh_model_env_choices(self) -> None:
        ids = self.model_ids()
        if not ids:
            return
        for key, combo in self.model_env_combos.items():
            current = self.config_data.model_env.get(key) or self.config_data.default_model_id
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(ids)
            combo.setCurrentText(current if current in ids else ids[0])
            combo.blockSignals(False)
        self.codex_model_combo.blockSignals(True)
        self.codex_model_combo.clear()
        self.codex_model_combo.addItems(ids)
        self.codex_model_combo.setCurrentText(self.config_data.codex_model_id if self.config_data.codex_model_id in ids else ids[0])
        self.codex_model_combo.blockSignals(False)
        self.default_model_edit.setText(self.model_env_combos["ANTHROPIC_MODEL"].currentText())
        self.update_model_route_summary()

    def update_model_route_summary(self) -> None:
        labels = (
            ("Main", "ANTHROPIC_MODEL"),
            ("Haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
            ("Sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
            ("Opus", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
            ("Reasoning", "ANTHROPIC_REASONING_MODEL"),
        )
        summary = "Effective: " + " | ".join(f"{label}={self.model_env_combos[key].currentText()}" for label, key in labels)
        summary += f" | Codex={self.codex_model_combo.currentText()}"
        self.route_summary.setText(summary)

    def on_model_route_changed(self) -> None:
        self.default_model_edit.setText(self.model_env_combos["ANTHROPIC_MODEL"].currentText())
        self.update_model_route_summary()

    def on_codex_model_changed(self) -> None:
        self.update_model_route_summary()

    def on_codex_sandbox_changed(self, value: str) -> None:
        if value != "danger-full-access":
            self.danger_sandbox_confirmed = False
            return
        if not self.ask("Dangerous sandbox mode", "danger-full-access disables Codex filesystem sandboxing for model-generated commands.\n\nOnly use it when you fully trust the workspace and commands. Continue?"):
            self.codex_sandbox_combo.setCurrentText("workspace-write")
            self.danger_sandbox_confirmed = False
        else:
            self.danger_sandbox_confirmed = True
        self.update_model_route_summary()

    def load_model(self, index: int) -> None:
        if index < 0 or index >= len(self.config_data.models):
            return
        self.selected_index = index
        model = self.config_data.models[index]
        self.name_edit.setText(model.name)
        self.model_id_edit.setText(model.model_id)
        self.base_url_edit.setText(model.base_url)
        self.api_key_edit.setText(model.api_key)
        self.upstream_model_edit.setText(model.upstream_model)
        self.api_format_combo.setCurrentText(getattr(model, "api_format", DEFAULT_API_FORMAT) or DEFAULT_API_FORMAT)

    def on_api_format_changed(self, api_format: str) -> None:
        if api_format == "responses":
            self.base_url_edit.setText(DEFAULT_RESPONSES_URL)
        elif api_format == "chat_completions":
            self.base_url_edit.setText(DEFAULT_CHAT_COMPLETIONS_URL)

    def new_model(self) -> None:
        self.config_data.models.append(ModelConfig("New Model", "new-model-id", DEFAULT_CHAT_COMPLETIONS_URL, "", "GPT-5.5", DEFAULT_API_FORMAT))
        self.refresh_model_list()
        self.model_table.selectRow(len(self.config_data.models) - 1)

    def delete_model(self) -> None:
        if self.selected_index is None or not self.config_data.models:
            return
        if len(self.config_data.models) == 1:
            self.warning("Cannot delete", "Keep at least one model.")
            return
        del self.config_data.models[self.selected_index]
        self.selected_index = None
        self.refresh_model_list()

    def apply_model(self) -> bool:
        if self.selected_index is None:
            return True
        old_model_id = self.config_data.models[self.selected_index].model_id
        model = ModelConfig(
            self.name_edit.text().strip() or self.model_id_edit.text().strip(),
            self.model_id_edit.text().strip(),
            self.base_url_edit.text().strip(),
            self.api_key_edit.text().strip(),
            self.upstream_model_edit.text().strip() or self.model_id_edit.text().strip(),
            self.api_format_combo.currentText().strip() or DEFAULT_API_FORMAT,
        )
        if not model.model_id or not model.base_url:
            self.error("Missing value", "Model ID and Base URL are required.")
            return False
        if old_model_id != model.model_id:
            for combo in self.model_env_combos.values():
                if combo.currentText() == old_model_id:
                    combo.setCurrentText(model.model_id)
        self.config_data.models[self.selected_index] = model
        current = self.selected_index
        self.refresh_model_list()
        self.model_table.selectRow(current)
        self.append_log(f"Applied model {model.model_id}")
        return True

    def selected_model_env(self) -> dict[str, str]:
        ids = self.model_ids()
        fallback = self.config_data.default_model_id if self.config_data.default_model_id in ids else ids[0]
        selected = {}
        for key, combo in self.model_env_combos.items():
            value = combo.currentText().strip()
            selected[key] = value if value in ids else fallback
            combo.setCurrentText(selected[key])
        return selected

    def selected_codex_model_id(self) -> str:
        ids = self.model_ids()
        fallback = self.config_data.default_model_id if self.config_data.default_model_id in ids else ids[0]
        value = self.codex_model_combo.currentText().strip()
        selected = value if value in ids else fallback
        self.codex_model_combo.setCurrentText(selected)
        return selected

    def sync_server_fields(self) -> bool:
        try:
            port = int(self.port_edit.text().strip())
            timeout = int(self.timeout_edit.text().strip())
        except ValueError:
            self.error("Invalid number", "Port and timeout must be numbers.")
            return False
        self.config_data.host = self.host_edit.text().strip() or "127.0.0.1"
        self.config_data.port = port
        self.config_data.model_env = self.selected_model_env()
        self.config_data.default_model_id = self.config_data.model_env["ANTHROPIC_MODEL"]
        self.config_data.codex_model_id = self.selected_codex_model_id()
        sandbox_mode = self.codex_sandbox_combo.currentText().strip()
        if sandbox_mode == "danger-full-access" and self.client_mode() == "codex" and not self.danger_sandbox_confirmed:
            if not self.ask("Confirm danger-full-access", "You selected danger-full-access for Codex. This bypasses normal sandbox restrictions.\n\nKeep this mode for the next Codex config write?"):
                sandbox_mode = "workspace-write"
            else:
                self.danger_sandbox_confirmed = True
        elif sandbox_mode != "danger-full-access":
            self.danger_sandbox_confirmed = False
        self.config_data.codex_sandbox_mode = sandbox_mode if sandbox_mode in CODEX_SANDBOX_MODES else "workspace-write"
        self.codex_sandbox_combo.setCurrentText(self.config_data.codex_sandbox_mode)
        self.config_data.timeout = timeout
        self.config_data.claude_path = portable_claude_path(self.claude_path_edit.text().strip() or default_claude_path())
        self.config_data.claude_settings_path = portable_settings_path(self.claude_settings_path_edit.text().strip() or default_claude_settings_path())
        self.config_data.codex_config_path = portable_codex_config_path(self.codex_config_path_edit.text().strip() or default_codex_config_path())
        self.config_data.codex_auth_path = portable_codex_auth_path(self.codex_auth_path_edit.text().strip() or default_codex_auth_path())
        return True

    def save(self) -> None:
        if not self.apply_model() or not self.sync_server_fields():
            return
        save_config(self.config_data)
        self.append_log(f"Saved config: {config_path()}")

    def start_proxy(self) -> None:
        if self.server:
            self.stop_proxy()
        self.save()
        proxy.ACTIVE_CONFIG = self.config_data
        try:
            self.server = ThreadingHTTPServer((self.config_data.host, self.config_data.port), proxy.ProxyHandler)
        except OSError as exc:
            if cli.restart_existing_listener(self.config_data.host, self.config_data.port):
                try:
                    self.server = ThreadingHTTPServer((self.config_data.host, self.config_data.port), proxy.ProxyHandler)
                    self.append_log(f"Restarted existing listener on port {self.config_data.port}")
                except OSError as retry_exc:
                    self.server = None
                    self.error("Start failed", str(retry_exc))
                    return
            else:
                self.server = None
                self.error("Start failed", str(exc))
                return
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.status_label.setText(f"Status: Running on http://{self.config_data.host}:{self.config_data.port}")
        self.append_log(f"Proxy started on http://{self.config_data.host}:{self.config_data.port}")
        self.refresh_connection_status()

    def stop_proxy(self) -> None:
        if self.server:
            server = self.server
            self.server = None
            server.shutdown()
            server.server_close()
            self.status_label.setText("Status: Stopped")
            self.append_log("Stopped proxy")
        self.refresh_connection_status()

    def refresh_connection_status(self) -> None:
        host = self.host_edit.text().strip() or "127.0.0.1"
        try:
            port = int(self.port_edit.text().strip())
        except ValueError:
            self.connection_label.setText("Proxy status: invalid port")
            return
        owned = self.server is not None
        try:
            with socket.create_connection((host, port), timeout=0.35):
                listening = True
        except OSError:
            listening = False
        if owned and listening:
            status = f"Proxy status: listening on http://{host}:{port} (started by this app)"
        elif owned:
            status = f"Proxy status: starting/stopping on http://{host}:{port} (owned by this app)"
        elif listening:
            status = f"Proxy status: port {port} is already listening (external process)"
        else:
            status = f"Proxy status: not listening on http://{host}:{port}"
        self.connection_label.setText(status)

    def current_client_paths(self) -> list[str]:
        if self.client_mode() == "codex":
            return [self.codex_config_path_edit.text().strip(), self.codex_auth_path_edit.text().strip()]
        return [self.claude_settings_path_edit.text().strip()]

    def restore_client_config(self, original: bool) -> None:
        self.save()
        mode = self.client_mode()
        label = "original" if original else "most recent"
        if not self.ask("Restore client config", f"Restore the {label} backup for {mode} client config?\n\nThe current file will be backed up before restore."):
            return
        restored = []
        errors = []
        for path_value in self.current_client_paths():
            if not path_value:
                continue
            try:
                backup = cli.restore_client_backup(path_value, original=original)
                restored.append(f"{path_value}\n  from {backup}")
            except Exception as exc:
                errors.append(f"{path_value}: {exc}")
        if restored:
            self.append_log(f"Restored {mode} {label} backup")
        message = ""
        if restored:
            message += "Restored:\n" + "\n".join(restored)
        if errors:
            message += ("\n\n" if message else "") + "Not restored:\n" + "\n".join(errors)
        self.info("Restore complete" if restored else "Restore unavailable", message or "No matching backup was found.")

    def check_codex_health(self) -> None:
        self.save()
        ok, messages = cli.codex_health_report(self.config_data)
        title = "Codex health OK" if ok else "Codex health needs attention"
        prefix = "All required Codex proxy settings look valid." if ok else "Some Codex settings need repair. Click Write Client Config to fix managed fields."
        self.append_log(title)
        self.info(title, prefix + "\n\n" + "\n".join(f"- {item}" for item in messages))

    def needs_first_run_setup(self) -> bool:
        return not any(model.api_key.strip() for model in self.config_data.models)

    def show_first_run_tip(self) -> None:
        if self.needs_first_run_setup():
            self.info("First-time setup", "Welcome to SHTUCodeProxy.\n\nFor first-time use, paste your GenAI API Key, confirm Base URL / API Format / Upstream Model, then click Save + Connect.")

    def claude_env(self) -> dict[str, str]:
        if not self.sync_server_fields():
            return {}
        env = {"ANTHROPIC_BASE_URL": f"http://{self.config_data.host}:{self.config_data.port}", "ANTHROPIC_AUTH_TOKEN": "local-proxy"}
        env.update(self.config_data.model_env)
        return env

    def copy_claude_config(self) -> None:
        env = self.claude_env()
        if not env:
            return
        QApplication.clipboard().setText(json.dumps({"env": env, "includeCoAuthoredBy": False}, ensure_ascii=False, indent=2))
        self.append_log("Copied Claude Code config to clipboard")

    def refresh_mode_hint(self) -> None:
        if not hasattr(self, "log_text"):
            return
        if self.client_mode() == "codex":
            self.append_log("Codex mode selected: writes config.toml with wire_api=responses; start Codex manually with profile shtu_proxy.")
        else:
            self.append_log("Claude mode selected: writes Claude settings and can launch Claude Code.")

    def write_claude_settings(self, notify: bool = True) -> bool:
        self.save()
        if self.needs_first_run_setup():
            self.warning("API key required", "Please paste your GenAI API Key before writing Claude settings.")
            return False
        settings_path = cli.write_claude_settings(self.config_data)
        self.append_log(f"Wrote Claude settings env: {settings_path}")
        if not self.server:
            self.start_proxy()
        if notify:
            self.info("Claude settings written", f"Updated env in:\n{settings_path}\n\nProxy is running at http://{self.config_data.host}:{self.config_data.port}. Restart Claude Code to use it.")
        return True

    def write_codex_config(self, notify: bool = True) -> bool:
        self.save()
        if self.needs_first_run_setup():
            self.warning("API key required", "Please paste your GenAI API Key before writing Codex config.")
            return False
        config_path_written, auth_path_written = cli.write_codex_files(self.config_data)
        save_config(self.config_data)
        self.append_log(f"Wrote Codex config: {config_path_written}")
        self.append_log(f"Wrote Codex auth: {auth_path_written}")
        if not self.server:
            self.start_proxy()
        if notify:
            self.info("Codex config written", f"Updated Codex provider/profile and auth key in:\n{config_path_written}\n{auth_path_written}\n\nProxy is running at http://{self.config_data.host}:{self.config_data.port}.\nUse Codex profile: shtu_proxy.")
        return True

    def write_selected_client_config(self) -> bool:
        return self.write_codex_config() if self.client_mode() == "codex" else self.write_claude_settings()

    def setup_selected_client(self) -> None:
        if self.client_mode() == "codex":
            self.write_codex_config()
        else:
            self.setup_and_launch()

    def run_selected_client(self) -> None:
        if self.client_mode() == "codex":
            self.save()
            if not self.server:
                self.start_proxy()
            self.info("Codex ready", "Proxy is running. Start Codex with profile shtu_proxy from Codex CLI/Desktop.")
            return
        self.launch_claude_code()

    def setup_and_launch(self) -> None:
        if self.write_claude_settings(notify=False):
            self.launch_claude_code()

    def launch_claude_code(self) -> None:
        env_values = self.claude_env()
        if not env_values:
            return
        try:
            launch_claude(self.config_data.claude_path or "claude", env_values)
            self.append_log("Launched Claude Code with SHTUCodeProxy environment")
        except Exception as exc:
            self.error("Launch failed", str(exc))

    def install_launch_script(self) -> None:
        self.save()
        target = cli.install_launch_script(self.config_data)
        if not is_windows():
            target.chmod(0o755)
        QApplication.clipboard().setText(str(target))
        self.append_log(f"Installed Claude launch script: {target}")

    def browse_claude_path(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "Select Claude Code executable", str(Path.home()), "Executables (*.exe *.cmd);;All files (*.*)")
        if selected:
            self.claude_path_edit.setText(selected)

    def browse_claude_settings_path(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "Select Claude settings.json", str(Path.home() / ".claude"), "JSON files (*.json);;All files (*.*)")
        if selected:
            self.claude_settings_path_edit.setText(selected)

    def browse_codex_config_path(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "Select Codex config.toml", str(Path.home() / ".codex"), "TOML files (*.toml);;All files (*.*)")
        if selected:
            self.codex_config_path_edit.setText(selected)

    def browse_codex_auth_path(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "Select Codex auth.json", str(Path.home() / ".codex"), "JSON files (*.json);;All files (*.*)")
        if selected:
            self.codex_auth_path_edit.setText(selected)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.stop_proxy()
        event.accept()


def run() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    window = IosProxyApp()
    window.show()
    return app.exec_()
