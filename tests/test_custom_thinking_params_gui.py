# -*- coding: utf-8 -*-
"""GUI regression for per-model custom thinking parameters.

WHY: the custom JSON is an explicit upstream contract. A malformed value must
not silently replace the existing behavior, and clearing the field must restore
the legacy enable_thinking request.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pyqt_gui
from pyqt_gui import IosProxyApp
from PyQt5.QtWidgets import QApplication, QLabel


CUSTOM_PARAMS = {
    "chat_template_kwargs": {"thinking": True},
    "reasoning_effort": "max",
}


def _make_app() -> IosProxyApp:
    config = pyqt_gui.load_config()
    with patch.object(pyqt_gui, "load_config", return_value=config), \
         patch.object(pyqt_gui, "save_config", lambda *args, **kwargs: None):
        return IosProxyApp()


def main() -> int:
    qapp = QApplication.instance() or QApplication(sys.argv)
    app = _make_app()
    failures = []

    app.load_model(0)
    app.thinking_params_edit.setPlainText(json.dumps(CUSTOM_PARAMS))
    if not app.apply_model(persist=False):
        failures.append("valid custom JSON should apply successfully")
    saved = app.config_data.models[0].upstream_thinking_params
    if saved != CUSTOM_PARAMS:
        failures.append(f"valid custom JSON was not saved: {saved!r}")

    app.load_model(0)
    if app.thinking_params_edit.toPlainText() != json.dumps(CUSTOM_PARAMS, ensure_ascii=False, indent=2):
        failures.append("saved custom JSON was not reloaded into the editor")

    app.thinking_params_edit.setPlainText("[]")
    captured = {}
    with patch.object(pyqt_gui.QMessageBox, "critical", lambda *args, **kwargs: captured.setdefault("called", True)):
        if app.apply_model(persist=False):
            failures.append("non-object JSON should be rejected")
    if captured.get("called") is not True:
        failures.append("non-object JSON should show an error message")
    if app.config_data.models[0].upstream_thinking_params != CUSTOM_PARAMS:
        failures.append("rejected invalid JSON should not overwrite the saved model")

    app.load_model(0)
    app.thinking_params_edit.setPlainText("")
    if not app.apply_model(persist=False):
        failures.append("clearing custom JSON should apply successfully")
    if app.config_data.models[0].upstream_thinking_params != {}:
        failures.append("clearing custom JSON should reset custom params to empty")

    # WHY: the JSON editor has a fixed minimum height; without explicit row
    # offsets it can silently overlap the hint text and hide guidance.
    app.show()
    qapp.processEvents()
    editor_rect = app.thinking_params_edit.geometry()
    sibling_hints = [
        widget.geometry()
        for widget in app.findChildren(QLabel)
        if widget.parentWidget() is app.thinking_params_edit.parentWidget()
    ]
    if any(editor_rect.intersects(hint_rect) for hint_rect in sibling_hints):
        failures.append(f"thinking editor overlaps a hint label: {editor_rect!r}")

    if failures:
        print("FAIL:")
        for failure in failures:
            print("  -", failure)
        return 1
    print("PASS: custom thinking params load, apply, reject invalid input, and clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
