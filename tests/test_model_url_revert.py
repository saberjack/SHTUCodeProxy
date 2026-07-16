# -*- coding: utf-8 -*-
"""Regression test for: 修改模型配置点 Apply 后 Base URL 回退成校内 genai 默认 URL.

根因: load_model 中 api_format_combo.setCurrentText 会触发 on_api_format_changed,
后者无条件把 base_url_edit 覆盖成 DEFAULT_*_URL。修复方式是加载时 blockSignals。

WHY this test exists: 用户要能接入外部 URL; 一旦加载一个 API Format 与当前 combo
不同的模型, 已保存的自定义 URL 就被校内默认值覆盖, 再点 Apply 即写回默认 URL。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config_store import AppConfig, ModelConfig
import pyqt_gui
from pyqt_gui import IosProxyApp
from PyQt5.QtWidgets import QApplication

EXTERNAL = "https://external.example.com/v1/chat"


def _build_config() -> AppConfig:
    cfg = AppConfig.default()
    # 模型0: responses + 校内默认 URL (模拟默认配置的 GPT-5.5)
    cfg.models[0].base_url = pyqt_gui.DEFAULT_RESPONSES_URL
    cfg.models[0].api_format = "responses"
    # 模型1: chat_completions + 外部 URL (用户想接入的外部模型)
    cfg.models.append(
        ModelConfig(
            name="External Chat",
            model_id="external-chat",
            base_url=EXTERNAL,
            api_key="sk-test",
            upstream_model="external-chat",
            api_format="chat_completions",
        )
    )
    return cfg


def _make_app() -> IosProxyApp:
    cfg = _build_config()
    # 拦截磁盘读写, 绝不触碰生产 config.json
    with patch.object(pyqt_gui, "load_config", return_value=cfg), \
         patch.object(pyqt_gui, "save_config", lambda *a, **k: None):
        app = IosProxyApp()
    return app


def main() -> int:
    qapp = QApplication.instance() or QApplication(sys.argv)
    app = _make_app()

    failures = []

    # 场景1: 从 responses 模型切到 chat_completions 模型, URL 必须保留为外部值
    app.load_model(0)
    assert app.api_format_combo.currentText() == "responses"
    app.load_model(1)
    got = app.base_url_edit.text().strip()
    if got != EXTERNAL:
        failures.append(f"[switch] expected {EXTERNAL!r}, got {got!r}")
    if app.api_format_combo.currentText() != "chat_completions":
        failures.append(f"[switch] combo not chat_completions: {app.api_format_combo.currentText()!r}")

    # 场景2: 模拟 apply_model 的 refresh_model_list -> load_model(0) -> selectRow(current) -> load_model(current)
    # 即先被模型0刷新 combo, 再加载模型1; 模型1 的 URL 仍必须保留
    app.load_model(0)  # combo -> responses
    app.load_model(1)  # combo -> chat_completions, URL 必须保持外部值
    got2 = app.base_url_edit.text().strip()
    if got2 != EXTERNAL:
        failures.append(f"[apply-reload] expected {EXTERNAL!r}, got {got2!r}")

    # 场景3: Apply 往返 — 改 URL 为新外部值, apply_model(persist=False), 检查 config_data 已保存且字段未回退
    app.load_model(1)
    new_external = "https://other.example.com/v2"
    app.base_url_edit.setText(new_external)
    ok = app.apply_model(persist=False)
    if not ok:
        failures.append("[apply] apply_model returned False")
    saved = app.config_data.models[1].base_url
    if saved != new_external:
        failures.append(f"[apply] config not saved: expected {new_external!r}, got {saved!r}")
    shown = app.base_url_edit.text().strip()
    if shown != new_external:
        failures.append(f"[apply] field reverted after apply: expected {new_external!r}, got {shown!r}")

    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("PASS: model URL preserved across format switch and apply (3 scenarios)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())