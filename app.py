from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure src/ is on the import path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from pyqt_gui import run  # noqa: E402 - needed for PyInstaller detection
from cli import main  # noqa: E402 - needed for PyInstaller detection


def has_display() -> bool:
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return True
    return bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(main(sys.argv[1:]))

    if not has_display():
        print("SHTUCodeProxy GUI requires a display server.")
        print("Use X11 forwarding, for example: ssh -X user@host")
        print("Or use CLI mode, for example:")
        print("  SHTUCodeProxy status")
        print("  SHTUCodeProxy configure-model --model-id MODEL --api-key KEY --upstream-model MODEL --default --codex")
        print("  SHTUCodeProxy start")
        raise SystemExit(2)

    raise SystemExit(run())