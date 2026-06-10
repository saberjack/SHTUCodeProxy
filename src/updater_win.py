# -*- coding: utf-8 -*-
"""Windows-specific update replacement logic for SHTUCodeProxy.

Strategy:
  1. Rename running exe to .old (Windows allows renaming a running file)
  2. Copy new exe to the original path
  3. Start new exe with --update-cleanup flag
  4. Old process exits; new process cleans up in cleanup_after_update()

Rollback:
  If the new exe crashes within 30 seconds of startup, a rollback marker file
  is detected on the next launch, and the .old file is restored.
  The marker includes a timestamp - only markers older than 30s trigger rollback,
  giving the new process time to start and remove the marker.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

ROLLBACK_MARKER = "SHTUCodeProxy-update-rollback"
CLEANUP_FLAG = "--update-cleanup"
ROLLBACK_GRACE_SECONDS = 30


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
    return exe.parent / ("." + ROLLBACK_MARKER)


def _old_exe_path() -> Optional[Path]:
    """Return the expected path of the .old backup."""
    exe = _running_exe_path()
    if not exe:
        return None
    return Path(str(exe) + ".old")


def check_rollback_needed() -> bool:
    """Check if a previous update failed and rollback is needed.

    Should be called early in app startup.
    WHY: Time-based check prevents false rollback when the new process
    is still starting up. Only markers older than ROLLBACK_GRACE_SECONDS
    trigger rollback.
    """
    marker = _rollback_marker_path()
    if not marker or not marker.exists():
        return False

    # Read timestamp from marker
    try:
        content = marker.read_text(encoding="utf-8").strip()
        # Format: "PID TIMESTAMP EXE_PATH" or just "EXE_PATH" (old format)
        parts = content.split()
        if len(parts) >= 2:
            marker_time = float(parts[1])
            age = time.time() - marker_time
            if age < ROLLBACK_GRACE_SECONDS:
                # Marker is recent - new process may still be starting
                return False
        # Old format or marker is old enough - rollback needed
    except Exception:
        pass

    return True


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
        # No backup to restore - just clean up marker
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
            return False, "Cannot remove current exe (locked): " + str(exe)

        # Restore the old exe
        shutil.move(str(old), str(exe))

        # Clean up marker
        if marker:
            try:
                marker.unlink()
            except Exception:
                pass

        return True, None
    except Exception as e:
        return False, "Rollback failed: " + str(e)


def apply_update(
    new_exe_path: Path,
    *,
    restart_proxy: bool = True,
) -> Tuple[bool, Optional[str]]:
    """Apply a downloaded update by replacing the running executable.

    Steps:
      1. Write rollback marker (with PID + timestamp)
      2. Rename current exe -> .old
      3. Copy new exe to original path
      4. Write staging info for cleanup_after_update
      5. Start new exe with --update-cleanup
      6. Current process should exit after this returns

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
        return False, "Not running from a frozen executable - cannot auto-update"

    if not new_exe_path.exists():
        return False, "New exe not found: " + str(new_exe_path)

    try:
        # Step 1: Write rollback marker with PID and timestamp
        # WHY: Including PID and timestamp allows the new process to wait
        # for the old one to exit, and prevents false rollback during startup
        if marker:
            marker.write_text(
                str(os.getpid()) + " " + str(time.time()) + " " + str(exe),
                encoding="utf-8",
            )

        # Step 2: Rename current exe to .old
        # Remove previous .old if it exists
        if old and old.exists():
            try:
                old.unlink()
            except PermissionError:
                # Another process might hold a handle - try rename with suffix
                old2 = Path(str(old) + "." + str(int(time.time())))
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
            return False, "Cannot rename running exe: " + str(e)

        # Step 3: Copy new exe to original location
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
            return False, "Cannot copy new exe to " + str(exe) + ": " + str(e)

        # Step 4: Write staging info for cleanup_after_update
        # WHY: We do NOT try to delete or move _internal/ here because
        # the old process still has DLLs loaded from it (file locks).
        # Instead, we write the source path and old PID so cleanup_after_update
        # can handle _internal/ copy/deletion AFTER the old process exits.
        staging_file = exe.parent / ".shtucodeproxy_update_staging"
        try:
            staging_file.write_text(
                str(new_exe_path.parent) + "\n" + str(os.getpid()),
                encoding="utf-8",
            )
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
            # New process failed to start - rollback marker is still in place,
            # so next launch will detect the failure
            return False, "Failed to start new exe: " + str(e)

        return True, None

    except Exception as e:
        # Unexpected error - marker is in place, rollback will happen on next launch
        return False, "Update apply failed: " + str(e)



def cleanup_stale_internal() -> Tuple[bool, Optional[str]]:
    if not getattr(sys, 'frozen', False):
        return False, None
    exe = _running_exe_path()
    if not exe:
        return False, None
    dest_internal = exe.parent / '_internal'
    if not dest_internal.is_dir():
        return False, None
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        try:
            if Path(meipass).resolve() == dest_internal.resolve():
                return False, None
        except Exception:
            pass
    try:
        shutil.rmtree(str(dest_internal))
        return True, None
    except Exception as e:
        return False, str(e)

def _wait_for_pid_exit(pid: int, timeout: float = 10.0) -> bool:
    """Wait for a process with the given PID to exit.

    Returns True if the process exited within the timeout, False otherwise.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "PID eq " + str(pid), "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if str(pid) not in result.stdout:
                return True
        except Exception:
            # If we can not check, assume it exited
            return True
        time.sleep(0.5)
    return False


def cleanup_after_update() -> None:
    """Called by the new process on startup with --update-cleanup.

    Waits for the old process to exit, then removes the .old backup,
    handles _internal/ directory, and removes staging files.
    """
    exe = _running_exe_path()
    old = _old_exe_path()
    marker = _rollback_marker_path()

    # Read old PID from staging file to wait for it to exit
    old_pid = None
    source_dir = None
    if exe:
        staging_file = exe.parent / ".shtucodeproxy_update_staging"
        if staging_file.exists():
            try:
                content = staging_file.read_text(encoding="utf-8").strip()
                lines = content.split("\n")
                if len(lines) >= 1:
                    source_dir = Path(lines[0].strip())
                if len(lines) >= 2:
                    old_pid = int(lines[1].strip())
            except Exception:
                pass

    # Wait for old process to exit (up to 15 seconds)
    # WHY: We MUST wait for the old process to fully exit before touching
    # _internal/ or .old because the old process holds file locks on them.
    if old_pid and old_pid != os.getpid():
        _wait_for_pid_exit(old_pid, timeout=15.0)

    # Additional brief wait for file handles to be released by the OS
    time.sleep(1)

    # Handle _internal/ directory
    # WHY: If the user originally installed from a folder-based .zip, they have
    # _internal/ next to the exe. The auto-update downloads a single-file exe.
    # The stale _internal/ can cause PyInstaller to load old modules/VERSION,
    # leading to version mismatch and DLL conflicts.
    dest_internal = exe.parent / "_internal" if exe else None
    if dest_internal and dest_internal.is_dir():
        if exe and source_dir:
            source_internal = source_dir / "_internal"
            if source_internal.is_dir():
                # Source has _internal - folder-based update
                for item in dest_internal.iterdir():
                    try:
                        if item.is_dir():
                            shutil.rmtree(str(item))
                        else:
                            item.unlink()
                    except Exception:
                        pass
                try:
                    shutil.copytree(
                        str(source_internal), str(dest_internal), dirs_exist_ok=True
                    )
                except Exception:
                    pass
            else:
                # Source has NO _internal but destination does
                try:
                    shutil.rmtree(str(dest_internal))
                except Exception:
                    pass
        else:
            # No source_dir info (staging file missing) - remove stale _internal
            try:
                shutil.rmtree(str(dest_internal))
            except Exception:
                pass

    # Remove staging file
    if exe:
        staging_file = exe.parent / ".shtucodeproxy_update_staging"
        if staging_file.exists():
            try:
                staging_file.unlink()
            except Exception:
                pass

    # Remove .old backup
    if old and old.exists():
        try:
            old.unlink()
        except PermissionError:
            # Old process might still be shutting down - retry after a delay
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