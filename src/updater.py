# -*- coding: utf-8 -*-
"""Auto-update checker and downloader for SHTUCodeProxy.

Two-phase design:
  Phase 1 (Check)  — query GitHub Releases, compare versions, notify user
  Phase 2 (Apply)  — download new binary, verify SHA256, hand off to platform replacer

This module handles Phase 1 + download + verification.
Platform-specific replacement logic lives in updater_win.py / updater_linux.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import tempfile
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

# GitHub repo that hosts releases
REPO_OWNER = "saberjack"
REPO_NAME = "SHTUCodeProxy"
RELEASES_API = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases"

# Minimum interval between automatic checks (seconds) to avoid hitting rate limits
CHECK_COOLDOWN = 300  # 5 minutes

# Cache file for last check result (stored in app_dir)
CHECK_CACHE_FILENAME = "update_check_cache.json"


def current_version() -> str:
    """Return the running application version from the VERSION file."""
    # In PyInstaller bundles, __file__ points to _MEIPASS where --add-data files are extracted.
    # In source mode, __file__ is in src/, and VERSION is in the project root.
    version_file = Path(__file__).resolve().with_name("VERSION")
    if not version_file.exists():
        # Source mode: try project root (parent of src/)
        version_file = Path(__file__).resolve().parent.parent / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8").strip() or "dev"
    except Exception:
        return "dev"


def parse_version(v: str) -> Tuple[int, ...]:
    """Parse a version string like '4.6.2' or 'v4.6.2' into a comparable tuple."""
    cleaned = v.strip().lstrip("v")
    parts = []
    for part in cleaned.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            # Handle pre-release suffixes like '4.7.0-beta1' — just take the numeric prefix
            num_match = re.match(r"(\d+)", part)
            parts.append(int(num_match.group(1)) if num_match else 0)
    return tuple(parts)


def is_newer(remote_version: str, local_version: str) -> bool:
    """Return True if remote_version is strictly newer than local_version."""
    return parse_version(remote_version) > parse_version(local_version)


@dataclass
class UpdateInfo:
    """Information about an available update."""
    version: str           # e.g. "4.7.0"
    html_url: str          # GitHub release page URL
    body: str              # Release notes / changelog
    download_url: str      # Direct download URL for the platform binary
    sha256_url: str        # URL for SHA256SUMS.txt
    asset_name: str        # Filename of the downloadable asset

    @property
    def display_version(self) -> str:
        return f"v{self.version}"


@dataclass
class CheckResult:
    """Result of an update check."""
    has_update: bool
    update_info: Optional[UpdateInfo] = None
    error: Optional[str] = None
    checked_at: float = 0.0

    @property
    def is_error(self) -> bool:
        return self.error is not None


def _platform_asset_pattern() -> Optional[str]:
    """Return the glob-like pattern for the current platform's release asset."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows" and machine in ("amd64", "x86_64"):
        return "-windows-x64.exe"  # WHY: apply_update expects exe, not zip
    if system == "linux" and machine in ("x86_64", "amd64"):
        return "-linux-x86_64"
    # macOS or other architectures — not supported for auto-update yet
    return None


def _cache_path() -> Path:
    """Return the path for the update check cache file."""
    # Use the same app_dir as config_store
    from platform_utils import app_dir
    return app_dir() / CHECK_CACHE_FILENAME


def _read_check_cache() -> Optional[dict]:
    """Read the last check result from cache, if any."""
    cache = _cache_path()
    if not cache.exists():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        return data
    except Exception:
        return None


def _write_check_cache(result: CheckResult) -> None:
    """Persist check result to cache file."""
    cache = _cache_path()
    cache.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {"checked_at": result.checked_at}
    if result.has_update and result.update_info:
        payload["has_update"] = True
        payload["version"] = result.update_info.version
        payload["download_url"] = result.update_info.download_url
        payload["sha256_url"] = result.update_info.sha256_url
        payload["asset_name"] = result.update_info.asset_name
        payload["html_url"] = result.update_info.html_url
        payload["body"] = result.update_info.body
    else:
        payload["has_update"] = False
    try:
        from safe_io import atomic_write_text
        atomic_write_text(cache, json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception:
        # Cache write failure is non-critical
        pass


def last_check_time() -> Optional[float]:
    """Return the timestamp of the last successful check, or None."""
    cache = _read_check_cache()
    if cache and "checked_at" in cache:
        return float(cache["checked_at"])
    return None


def _fetch_via_gh_cli() -> Optional[dict]:
    """Fall back to gh CLI when the GitHub REST API is rate-limited or unreachable.

    Returns the parsed JSON release dict, or None on failure.
    """
    import shutil
    import subprocess
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        result = subprocess.run(
            [gh, "api", f"repos/{REPO_OWNER}/{REPO_NAME}/releases/latest", "--jq", "."],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception:
        pass
    return None



def _fetch_latest_version_via_redirect() -> Optional[str]:
    """Get the latest release version tag via GitHub releases redirect URL.

    WHY: GitHub REST API has a 60 req/hr rate limit for anonymous requests.
    The /releases/latest redirect URL is NOT rate-limited and always returns
    the correct tag. This serves as a fallback when the API is blocked and
    gh CLI is unavailable (which is the common case for end users).

    Returns:
        The latest version tag string (e.g. "4.6.7"), or None on failure.
    """
    url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases/latest"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SHTUCodeProxy-Updater"})
        resp = urllib.request.urlopen(req, timeout=15)
        final_url = resp.geturl()
        import re as _re
        m = _re.search(r"tag/v?([\d.]+)", final_url)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def check_for_update(*, force: bool = False) -> CheckResult:
    """Check GitHub Releases for a newer version.

    Args:
        force: If True, bypass the cooldown timer and check immediately.

    Returns:
        CheckResult with has_update=True if a newer version exists.
    """
    now = time.time()

    # Cooldown check — avoid hammering GitHub API
    if not force:
        last = last_check_time()
        if last and (now - last) < CHECK_COOLDOWN:
            # Return cached result if still within cooldown
            cache = _read_check_cache()
            if cache:
                if cache.get("has_update"):
                    info = UpdateInfo(
                        version=cache["version"],
                        html_url=cache.get("html_url", ""),
                        body=cache.get("body", ""),
                        download_url=cache["download_url"],
                        sha256_url=cache["sha256_url"],
                        asset_name=cache["asset_name"],
                    )
                    return CheckResult(has_update=True, update_info=info, checked_at=last)
                else:
                    return CheckResult(has_update=False, checked_at=last)

    # Query GitHub API — try direct HTTP first, fall back to gh CLI on rate limit
    release = None
    api_error = None
    try:
        req = urllib.request.Request(
            f"{RELEASES_API}/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "SHTUCodeProxy-Updater"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            release = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            result = CheckResult(has_update=False, checked_at=now)
            _write_check_cache(result)
            return result
        if e.code == 403:
            # Rate limited — try gh CLI as fallback
            release = _fetch_via_gh_cli()
            if release is None:
                # WHY: gh CLI is unavailable for most end users. The GitHub
                # /releases/latest redirect URL is NOT rate-limited, so we
                # can get the latest version tag and construct download URLs.
                remote_tag = _fetch_latest_version_via_redirect()
                if remote_tag and is_newer(remote_tag, current_version()):
                    asset_pattern = _platform_asset_pattern()
                    if asset_pattern:
                        asset_name = f"SHTUCodeProxy-v{remote_tag}{asset_pattern}"
                        download_url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases/download/v{remote_tag}/{asset_name}"
                        sha256_url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases/download/v{remote_tag}/SHA256SUMS.txt"
                        info = UpdateInfo(
                            version=remote_tag,
                            html_url=f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases/tag/v{remote_tag}",
                            body="",
                            download_url=download_url,
                            sha256_url=sha256_url,
                            asset_name=asset_name,
                        )
                        result = CheckResult(has_update=True, update_info=info, checked_at=now)
                        _write_check_cache(result)
                        return result
                return CheckResult(has_update=False, error="GitHub API rate limited. Download manually from https://github.com/saberjack/SHTUCodeProxy/releases", checked_at=now)
        else:
            return CheckResult(has_update=False, error=f"GitHub API HTTP {e.code}", checked_at=now)
    except urllib.error.URLError as e:
        # Network unreachable — try gh CLI as fallback
        release = _fetch_via_gh_cli()
        if release is None:
            return CheckResult(has_update=False, error=f"Network error: {e.reason}", checked_at=now)
    except Exception as e:
        return CheckResult(has_update=False, error=str(e), checked_at=now)

    remote_tag = release.get("tag_name", "").lstrip("v")
    if not remote_tag:
        return CheckResult(has_update=False, error="Empty tag_name in release", checked_at=now)

    local_ver = current_version()
    if not is_newer(remote_tag, local_ver):
        result = CheckResult(has_update=False, checked_at=now)
        _write_check_cache(result)
        return result

    # Find the right asset for this platform
    asset_pattern = _platform_asset_pattern()
    if not asset_pattern:
        return CheckResult(has_update=False, error="Auto-update not supported on this platform", checked_at=now)

    download_url = ""
    sha256_url = ""
    asset_name = ""

    for asset in release.get("assets", []):
        name = asset.get("name", "")
        url = asset.get("browser_download_url", "")
        if name.endswith(asset_pattern):
            download_url = url
            asset_name = name
        elif name == "SHA256SUMS.txt":
            sha256_url = url

    if not download_url:
        return CheckResult(
            has_update=False,
            error=f"No matching asset for pattern '{asset_pattern}' in release v{remote_tag}",
            checked_at=now,
        )

    info = UpdateInfo(
        version=remote_tag,
        html_url=release.get("html_url", ""),
        body=release.get("body", ""),
        download_url=download_url,
        sha256_url=sha256_url,
        asset_name=asset_name,
    )
    result = CheckResult(has_update=True, update_info=info, checked_at=now)
    _write_check_cache(result)
    return result


def _temp_update_dir() -> Path:
    """Return the temporary directory for downloaded update files."""
    d = Path(tempfile.gettempdir()) / "SHTUCodeProxy-update"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fetch_sha256(sha256_url: str, asset_name: str) -> Optional[str]:
    """Download SHA256SUMS.txt and extract the expected hash for our asset."""
    if not sha256_url:
        return None
    try:
        req = urllib.request.Request(sha256_url, headers={"User-Agent": "SHTUCodeProxy-Updater"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8")
    except Exception:
        return None

    for line in text.splitlines():
        line = line.strip()
        # Format: <hash>  <filename>  or  <hash> *<filename>
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == asset_name:
            return parts[0].lower()
    return None


def _sha256_file(path: Path) -> str:
    """Compute SHA256 of a local file, returning hex digest."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)  # 1 MiB
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest().lower()


def download_update(
    info: UpdateInfo,
    *,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Tuple[bool, Optional[Path], Optional[str]]:
    """Download the update binary and verify its SHA256.

    Args:
        info: UpdateInfo from check_for_update.
        progress_callback: Called with (bytes_downloaded, total_bytes) for progress UI.
            total_bytes may be 0 if unknown.

    Returns:
        (success, downloaded_path, error_message)
    """
    update_dir = _temp_update_dir()
    dest = update_dir / info.asset_name

    # If already downloaded and verified, skip re-download
    if dest.exists():
        expected_hash = _fetch_sha256(info.sha256_url, info.asset_name)
        if expected_hash and _sha256_file(dest) == expected_hash:
            return True, dest, None
        # Hash mismatch or unknown — re-download
        try:
            dest.unlink()
        except Exception:
            pass

    try:
        req = urllib.request.Request(info.download_url, headers={"User-Agent": "SHTUCodeProxy-Updater"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(1 << 16)  # 64 KiB
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        try:
                            progress_callback(downloaded, total)
                        except Exception:
                            pass
    except Exception as e:
        # Clean up partial download
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        return False, None, f"Download failed: {e}"

    # SHA256 verification
    expected_hash = _fetch_sha256(info.sha256_url, info.asset_name)
    if expected_hash:
        actual_hash = _sha256_file(dest)
        if actual_hash != expected_hash:
            try:
                dest.unlink()
            except Exception:
                pass
            return False, None, f"SHA256 mismatch: expected {expected_hash}, got {actual_hash}"

    return True, dest, None


def cleanup_download() -> None:
    """Remove the temporary update directory and all its contents."""
    update_dir = _temp_update_dir()
    if update_dir.exists():
        try:
            for f in update_dir.iterdir():
                try:
                    f.unlink()
                except Exception:
                    pass
            update_dir.rmdir()
        except Exception:
            pass