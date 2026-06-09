from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure src/ is on the import path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# Lazy import to avoid loading PyQt5/sip in CLI/serve mode (sip crashes on Python 3.14)
def _import_pyqt_gui():
    from pyqt_gui import run
    return run

from cli import main  # noqa: E402 - needed for PyInstaller detection


def has_display() -> bool:
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return True
    return bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))


if __name__ == "__main__":
    # Handle --update-cleanup: called by the new exe after an auto-update
    # WHY: Run cleanup in a background thread so the GUI starts immediately
    # instead of blocking 2-5 seconds while waiting for the old process to exit.
    # The user sees the window appear right away instead of thinking the restart failed.
    if "--update-cleanup" in sys.argv:
        import threading
        import platform as _pf
        if _pf.system() == "Windows":
            from updater_win import cleanup_after_update
        else:
            from updater_linux import cleanup_after_update
        # Remove rollback marker immediately so crashes during GUI startup don't trigger rollback
        # (the exe replacement was already successful if we got this far)
        import platform as _pf_marker
        if _pf_marker.system() == "Windows":
            from updater_win import _rollback_marker_path
            _marker = _rollback_marker_path()
            if _marker and _marker.exists():
                try:
                    _marker.unlink()
                except Exception:
                    pass
        threading.Thread(target=cleanup_after_update, daemon=True).start()

    # Handle --start-proxy: auto-start proxy after update (then launch GUI normally)
    # This flag is set by updater when restarting after an update
    _auto_start_proxy = "--start-proxy" in sys.argv
    # Remove our internal flags so they don't propagate to CLI parser
    sys.argv = [a for a in sys.argv if a not in ("--update-cleanup", "--start-proxy")]

    # Also handle rollback on startup — if a previous update crashed, restore the old exe
    if getattr(sys, "frozen", False):
        import platform as _pf2
        if _pf2.system() == "Windows":
            from updater_win import check_rollback_needed, perform_rollback
        else:
            from updater_linux import check_rollback_needed, perform_rollback
        if check_rollback_needed():
            ok, err = perform_rollback()
            if ok:
                print("Previous update failed — rolled back to the previous version.")
            else:
                print(f"Rollback attempt failed: {err}")
            # Either way, continue running (with whatever version we have)

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

    # Pass auto-start flag to GUI if set
    if _auto_start_proxy:
        os.environ["SHTUCODEPROXY_AUTO_START"] = "1"

    raise SystemExit(_import_pyqt_gui()())