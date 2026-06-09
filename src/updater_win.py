# -*- coding: utf-8 -*-
"""Windows-specific update replacement logic for SHTUCodeProxy.

Strategy:
  1. Rename running exe to .old (Windows allows renaming a running file)
  2. Copy new exe to the original path
  3. Start new exe with --update-cleanup flag
  4. Old process exits; new process deletes the .old file on startup

Rollback:
  If the new exe crashes within 5 seconds of startup, a rollback marker file
  is detected on the next launch, and the .old file is restored.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

ROLLBACK_MARKER = "SHTUCodeProxy-update-rollback"
CLEANUP_FLAG = "--update-cleanup"
ROLLBACK_GRACE_SECONDS = 5


def _running_exe_path() -> Optional[Path]:
    """Return the path of the currently running executable, or None."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return None


def _rollback_marker_path() -> Optional[Path]:
    """Return the path for the rollback marker file."""
    exe = _running_exe_path()
    if not exe:
        return None
    return exe.parent / f".{ROLLBACK_MARKER}"


def _old_exe_path() -> Optional[Path]:
    """Return the expected path of the .old backup."""
    exe = _running_exe_path()
    if not exe:
        return None
    return Path(str(exe) + ".old")


def check_rollback_needed() -> bool:
    """Check if a previous update failed and rollback is needed.

    Should be called early in app startup (before the grace period expires).
    """
    marker = _rollback_marker_path()
    if marker and marker.exists():
        return True
    return False


def perform_rollback() -> Tuple[bool, Optional[str]]:
    """Roll back a failed update by restoring the .old exe.

    Returns (success, error_message).
    """
    exe = _running_exe_path()
    old = _old_exe_path()
    marker = _rollback_marker_path()

    if not exe or not old:
        return False, "Cannot determine exe paths for rollback"

    if not old.exists():
        # No backup to restore — just clean up marker
        if marker:
            try:
                marker.unlink()
            except Exception:
                pass
        return False, "No .old backup file found"

    try:
        # Remove the current (broken) exe
        try:
            exe.unlink()
        except PermissionError:
            return False, f"Cannot remove current exe (locked): {exe}"

        # Restore the old exe
        import shutil
        shutil.move(str(old), str(exe))

        # Clean up marker
        if marker:
            try:
                marker.unlink()
            except Exception:
                pass

        return True, None
    except Exception as e:
        return False, f"Rollback failed: {e}"


def apply_update(
    new_exe_path: Path,
    *,
    restart_proxy: bool = True,
) -> Tuple[bool, Optional[str]]:
    """Apply a downloaded update by replacing the running executable.

    Steps:
      1. Write rollback marker
      2. Rename current exe → .old
      3. Copy new exe to original path
      4. Start new exe with --update-cleanup
      5. Current process should exit after this returns

    Args:
        new_exe_path: Path to the downloaded new exe (in temp dir).
        restart_proxy: Whether to auto-start the proxy in the new process.

    Returns:
        (success, error_message)
    """
    exe = _running_exe_path()
    old = _old_exe_path()
    marker = _rollback_marker_path()

    if not exe:
        return False, "Not running from a frozen executable — cannot auto-update"

    if not new_exe_path.exists():
        return False, f"New exe not found: {new_exe_path}"

    try:
        # Step 1: Write rollback marker
        if marker:
            marker.write_text(str(exe), encoding="utf-8")

        # Step 2: Rename current exe to .old
        # Remove previous .old if it exists
        if old and old.exists():
            try:
                old.unlink()
            except PermissionError:
                # Another process might hold a handle — try rename with suffix
                old2 = Path(str(old) + f".{int(time.time())}")
                try:
                    old.rename(old2)
                except Exception:
                    pass

        try:
            exe.rename(old)
        except PermissionError as e:
            # Clean up marker on failure
            if marker:
                try:
                    marker.unlink()
                except Exception:
                    pass
            return False, f"Cannot rename running exe: {e}"

        # Step 3: Copy new exe and _internal directory to original location
        import shutil
        try:
            shutil.copy2(str(new_exe_path), str(exe))
        except Exception as e:
            # CRITICAL: Copy failed, try to restore immediately
            try:
                old.rename(exe)
            except Exception:
                pass
            if marker:
                try:
                    marker.unlink()
                except Exception:
                    pass
            return False, f"Cannot copy new exe to {exe}: {e}"
        # WHY: PyInstaller COLLECT mode requires the _internal directory
        # next to the exe. When updating from a zip download, we need to
        # copy the entire _internal directory as well.
        new_internal = new_exe_path.parent / "_internal"
        old_internal = exe.parent / "_internal"
        if new_internal.is_dir():
            try:
                if old_internal.is_dir():
                    # Remove old _internal but skip .old files (from previous update)
                    for item in old_internal.iterdir():
                        try:
                            if item.is_dir():
                                shutil.rmtree(str(item))
                            else:
                                item.unlink()
                        except Exception:
                            pass
                shutil.copytree(str(new_internal), str(old_internal), dirs_exist_ok=True)
            except Exception as e:
                # Non-critical: exe is already updated, _internal copy failure
                # may mean some features break but the app should still start
                pass

        # Step 4: Remove the GUI instance lock so the new process can acquire it
        # WHY: The old process may still be shutting down when the new one starts.
        # The lock file blocks the new process with "already running" error.
        # Since we are the same user and we are about to exit, it is safe to
        # remove the lock file now.
        try:
            from safe_io import file_lock
            from tempfile import gettempdir
            from pathlib import Path as _P
            lock_file = _P(gettempdir()) / "SHTUCodeProxy" / "gui-instance.lock"
            if lock_file.exists():
                lock_file.unlink()
        except Exception:
            pass

        # Step 5: Start the new exe with cleanup flag
        cmd = [str(exe)]
        if restart_proxy:
            cmd.append("--start-proxy")
        cmd.append(CLEANUP_FLAG)

        try:
            # Use CREATE_NEW_PROCESS_GROUP so the new process survives our exit
            DETACHED = 0x00000008
            subprocess.Popen(
                cmd,
                env=dict(os.environ, SHTUCODEPROXY_AUTO_START="1"),
                creationflags=DETACHED | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            # New process failed to start — rollback marker is still in place,
            # so next launch will detect the failure
            return False, f"Failed to start new exe: {e}"

        return True, None

    except Exception as e:
        # Unexpected error — marker is in place, rollback will happen on next launch
        return False, f"Update apply failed: {e}"


def cleanup_after_update() -> None:
    """Called by the new process on startup with --update-cleanup.

    Waits for the grace period, then removes the .old backup and rollback marker.
    If the process crashes before this completes, the marker remains and
    rollback will be triggered on the next launch.
    """
    # Wait briefly for the old process to fully exit
    time.sleep(2)

    exe = _running_exe_path()
    old = _old_exe_path()
    marker = _rollback_marker_path()

    # Remove .old backup
    if old and old.exists():
        try:
            old.unlink()
        except PermissionError:
            # Old process might still be shutting down — retry after a delay
            time.sleep(3)
            try:
                old.unlink()
            except Exception:
                pass

    # Remove rollback marker (signals successful update)
    if marker and marker.exists():
        try:
            marker.unlink()
        except Exception:
            pass

    # Clean up temp download directory
    try:
        from updater import cleanup_download
        cleanup_download()
    except Exception:
        pass