from __future__ import annotations

import os
import hashlib
import shutil
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from tempfile import gettempdir
from typing import Callable, Optional


ORIGINAL_BACKUP_SUFFIX = ".original.bak"
ORIGINAL_MISSING_SUFFIX = ".original.missing"


def _process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in result.stdout
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@contextmanager
def file_lock(lock_name: str, *, timeout: float = 5.0):
    lock_dir = Path(gettempdir()) / "SHTUCodeProxy"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_dir / f"{lock_name}.lock"
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            break
        except FileExistsError:
            try:
                pid_text = lock.read_text(encoding="utf-8").strip()
                stale = not pid_text.isdigit() or not _process_is_running(int(pid_text))
            except OSError:
                stale = False
            if stale:
                try:
                    lock.unlink()
                    continue
                except OSError:
                    pass
            if time.time() >= deadline:
                raise TimeoutError(f"Timed out waiting for lock: {lock}")
            time.sleep(0.05)
    try:
        yield lock
    finally:
        try:
            if lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
                lock.unlink()
        except OSError:
            pass


def powershell_copy_text(source: Path, target: Path) -> None:
    script = source.with_name(f".{target.name}.copy.ps1")
    script.write_text(
        "param([string]$src,[string]$dst)\n"
        "$parent=Split-Path -Parent $dst\n"
        "if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }\n"
        "$content=Get-Content -LiteralPath $src -Raw -Encoding UTF8\n"
        "Set-Content -LiteralPath $dst -Value $content -Encoding UTF8 -Force\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), str(source), str(target)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        script.unlink()
    except OSError:
        pass
    if result.returncode != 0:
        raise PermissionError(result.stderr.strip() or result.stdout.strip() or f"Could not write {target}")


def backup_existing_file(target: Path) -> Optional[Path]:
    if not target.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = target.with_name(f"{target.name}.bak_{timestamp}")
    data = target.read_bytes()
    try:
        backup.write_bytes(data)
        return backup
    except PermissionError:
        fallback_dir = Path(__file__).resolve().parent / "backups"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        fallback = fallback_dir / f"{target.name}.bak_{timestamp}"
        fallback.write_bytes(data)
        return fallback


def snapshot_original_file(target: Path) -> Optional[Path]:
    missing_marker = target.with_name(f"{target.name}{ORIGINAL_MISSING_SUFFIX}")
    if not target.exists():
        if not missing_marker.exists():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                missing_marker.write_text("missing before first SHTUClaudeProxy write\n", encoding="utf-8")
            except PermissionError:
                fallback_dir = Path(__file__).resolve().parent / "backups"
                fallback_dir.mkdir(parents=True, exist_ok=True)
                missing_marker = fallback_dir / f"{target.name}{ORIGINAL_MISSING_SUFFIX}"
                if not missing_marker.exists():
                    missing_marker.write_text("missing before first SHTUClaudeProxy write\n", encoding="utf-8")
        return missing_marker
    original = target.with_name(f"{target.name}{ORIGINAL_BACKUP_SUFFIX}")
    if original.exists():
        return original
    data = target.read_bytes()
    try:
        original.write_bytes(data)
        return original
    except PermissionError:
        fallback_dir = Path(__file__).resolve().parent / "backups"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        fallback = fallback_dir / f"{target.name}{ORIGINAL_BACKUP_SUFFIX}"
        if not fallback.exists():
            fallback.write_bytes(data)
        return fallback


def latest_backup_for(target: Path, *, original: bool = False) -> Optional[Path]:
    if original:
        candidates = [target.with_name(f"{target.name}{ORIGINAL_BACKUP_SUFFIX}")]
        missing_candidates = [target.with_name(f"{target.name}{ORIGINAL_MISSING_SUFFIX}")]
        fallback = Path(__file__).resolve().parent / "backups" / f"{target.name}{ORIGINAL_BACKUP_SUFFIX}"
        candidates.append(fallback)
        for candidate in candidates:
            if candidate.exists():
                return candidate
        missing_candidates.append(Path(__file__).resolve().parent / "backups" / f"{target.name}{ORIGINAL_MISSING_SUFFIX}")
        for candidate in missing_candidates:
            if candidate.exists():
                return candidate
        return None
    candidates = list(target.parent.glob(f"{target.name}.bak_*")) if target.parent.exists() else []
    fallback_dir = Path(__file__).resolve().parent / "backups"
    if fallback_dir.exists():
        candidates.extend(fallback_dir.glob(f"{target.name}.bak_*"))
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def restore_latest_backup(target: Path, *, original: bool = False) -> Optional[Path]:
    backup = latest_backup_for(target, original=original)
    if backup is None:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup_existing_file(target)
    if backup.name.endswith(ORIGINAL_MISSING_SUFFIX):
        if target.exists():
            target.unlink()
        return backup
    try:
        shutil.copy2(backup, target)
    except PermissionError:
        if target.drive or str(target).startswith("\\\\"):
            powershell_copy_text(backup, target)
        else:
            raise
    return backup


def atomic_write_text(
    target: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    validate: Optional[Callable[[str], None]] = None,
    backup: bool = True,
) -> Optional[Path]:
    digest = hashlib.sha256(str(target.expanduser().resolve()).encode("utf-8")).hexdigest()[:16]
    lock_name = f"write-{digest}"
    with file_lock(lock_name):
        if validate:
            validate(text)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            try:
                if target.read_text(encoding=encoding) == text:
                    return None
            except UnicodeDecodeError:
                pass
        backup_path = backup_existing_file(target) if backup else None
        temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            try:
                temp.write_text(text, encoding=encoding)
            except PermissionError:
                fallback_dir = Path(__file__).resolve().parent / "backups"
                fallback_dir.mkdir(parents=True, exist_ok=True)
                temp = fallback_dir / f".{target.name}.{os.getpid()}.tmp"
                temp.write_text(text, encoding=encoding)
            if validate:
                validate(temp.read_text(encoding=encoding))
            try:
                temp.replace(target)
            except PermissionError:
                try:
                    shutil.copy2(temp, target)
                except PermissionError:
                    if target.drive or str(target).startswith("\\\\"):
                        powershell_copy_text(temp, target)
                    else:
                        raise
        finally:
            if temp.exists():
                temp.unlink()
        return backup_path
