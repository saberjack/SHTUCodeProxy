from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Dict

from config_store import AppConfig, MODEL_ENV_KEYS, config_path, ensure_builtin_model_routes, load_config, save_config
from platform_utils import launch_script_filename, launch_script_text
from proxy import ProxyHandler, ThreadingHTTPServer
from safe_io import atomic_write_text, backup_existing_file


def claude_env(config: AppConfig) -> Dict[str, str]:
    env = {
        "ANTHROPIC_BASE_URL": f"http://{config.host}:{config.port}",
        "ANTHROPIC_AUTH_TOKEN": "local-proxy",
    }
    env.update(config.model_env)
    return env


def write_claude_settings(config: AppConfig) -> Path:
    settings_path = Path(config.claude_settings_path).expanduser()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, object] = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        except Exception:
            backup_existing_file(settings_path)
            existing = {}
    env = existing.get("env") if isinstance(existing.get("env"), dict) else {}
    env.update(claude_env(config))
    existing["env"] = env
    existing["includeCoAuthoredBy"] = False
    payload = json.dumps(existing, ensure_ascii=False, indent=2)
    atomic_write_text(settings_path, payload, validate=lambda text: json.loads(text))
    return settings_path


def validate_codex_config(text: str) -> None:
    parsed = tomllib.loads(text)
    if parsed.get("model_provider") != "shtu_proxy":
        raise ValueError("Codex root model_provider was not written correctly")
    if not isinstance(parsed.get("model"), str) or not parsed.get("model"):
        raise ValueError("Codex root model was not written correctly")
    if parsed.get("sandbox_mode") != "workspace-write":
        raise ValueError("Codex sandbox_mode must be workspace-write")
    features = parsed.get("features", {})
    if not isinstance(features, dict) or features.get("hooks") is not True:
        raise ValueError("Codex features.hooks must be enabled")
    windows = parsed.get("windows", {})
    if os.name == "nt" and (not isinstance(windows, dict) or windows.get("sandbox") != "elevated"):
        raise ValueError("Codex windows.sandbox must be elevated on Windows")
    provider = parsed.get("model_providers", {}).get("shtu_proxy", {})
    if "env_key" in provider:
        raise ValueError("Codex shtu_proxy provider should use auth.json instead of requiring an environment variable")
    if provider.get("wire_api") != "responses":
        raise ValueError("Codex shtu_proxy provider must use responses wire_api")
    if provider.get("base_url") is None:
        raise ValueError("Codex shtu_proxy provider is missing base_url")
    profile = parsed.get("profiles", {}).get("shtu_proxy", {})
    if profile.get("model_provider") != "shtu_proxy":
        raise ValueError("Codex shtu_proxy profile was not written correctly")


def codex_config_block(config: AppConfig) -> str:
    return codex_root_config_block(config) + codex_provider_profile_block(config)


def codex_root_config_block(config: AppConfig) -> str:
    provider = "shtu_proxy"
    codex_model = getattr(config, "codex_model_id", "") or config.default_model_id
    return "\n".join([
        f'model = "{codex_model}"',
        f'model_provider = "{provider}"',
        'sandbox_mode = "workspace-write"',
        "",
    ])


def codex_provider_profile_block(config: AppConfig) -> str:
    provider = "shtu_proxy"
    profile = "shtu_proxy"
    codex_model = getattr(config, "codex_model_id", "") or config.default_model_id
    return "\n".join([
        f"[model_providers.{provider}]",
        'name = "SHTUClaudeProxy"',
        f'base_url = "http://{config.host}:{config.port}/v1"',
        'wire_api = "responses"',
        'requires_openai_auth = true',
        "",
        f"[profiles.{profile}]",
        f'model_provider = "{provider}"',
        f'model = "{codex_model}"',
        "",
    ])


def is_toml_section_header(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("[") and stripped.endswith("]")


def is_codex_projects_header(line: str) -> bool:
    stripped = line.strip()
    if not is_toml_section_header(stripped):
        return False
    inner = stripped.strip("[]").strip()
    return inner == "projects" or inner.startswith("projects.")


def codex_preserved_project_blocks(existing: str) -> str:
    lines = existing.splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not is_codex_projects_header(line):
            index += 1
            continue
        block: list[str] = [line]
        index += 1
        while index < len(lines) and not is_toml_section_header(lines[index]):
            block.append(lines[index])
            index += 1
        blocks.append("\n".join(block).rstrip())
    return "\n\n".join(block for block in blocks if block.strip())


def codex_preserved_config_block(existing: str) -> str:
    try:
        parsed = tomllib.loads(existing) if existing.strip() else {}
    except tomllib.TOMLDecodeError:
        parsed = {}
    lines: list[str] = []
    for key in (
        "stream",
        "model_context_window",
        "model_auto_compact_token_limit",
        "model_reasoning_effort",
        "disable_response_storage",
    ):
        if key in parsed:
            lines.append(f"{key} = {json.dumps(parsed[key]) if isinstance(parsed[key], str) else str(parsed[key]).lower()}")
    features = parsed.get("features") if isinstance(parsed.get("features"), dict) else {}
    feature_values = dict(features)
    feature_values["hooks"] = True
    lines.append("")
    lines.append("[features]")
    for key, value in feature_values.items():
        lines.append(f"{key} = {json.dumps(value) if isinstance(value, str) else str(value).lower()}")
    windows = parsed.get("windows") if isinstance(parsed.get("windows"), dict) else {}
    windows_values = dict(windows)
    if os.name == "nt":
        windows_values["sandbox"] = "elevated"
    if windows_values:
        lines.append("")
        lines.append("[windows]")
        for key, value in windows_values.items():
            lines.append(f"{key} = {json.dumps(value) if isinstance(value, str) else str(value).lower()}")
    project_blocks = codex_preserved_project_blocks(existing)
    if project_blocks:
        lines.append("")
        lines.append(project_blocks)
    return "\n".join(lines).strip()


def remove_named_toml_section(text: str, section_name: str) -> str:
    lines = text.splitlines()
    kept: list[str] = []
    skipping = False
    target = f"[{section_name}]"
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            skipping = stripped == target
        if not skipping:
            kept.append(line)
    return "\n".join(kept).rstrip()


def remove_root_toml_key(text: str, key: str) -> str:
    kept: list[str] = []
    in_section = False
    prefix = f"{key} ="
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = True
        if not in_section and stripped.startswith(prefix):
            continue
        kept.append(line)
    return "\n".join(kept).rstrip()


def remove_stale_codex_keys(text: str, config: AppConfig) -> str:
    codex_model = getattr(config, "codex_model_id", "") or config.default_model_id
    model_line = f'model = "{codex_model}"'
    provider_line = 'model_provider = "shtu_proxy"'
    lines = text.splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        current = lines[index].strip()
        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        previous = lines[index - 1].strip() if index > 0 else ""
        if current == model_line and next_line == provider_line:
            index += 2
            continue
        if current == provider_line and previous == model_line:
            index += 1
            continue
        kept.append(lines[index])
        index += 1
    return "\n".join(kept).rstrip()


def write_codex_config(config: AppConfig) -> Path:
    config_path_value = getattr(config, "codex_config_path", "") or str(Path.home() / ".codex" / "config.toml")
    target = Path(config_path_value).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if target.exists():
        existing = target.read_text(encoding="utf-8-sig")
    root = codex_root_config_block(config)
    provider_profile = codex_provider_profile_block(config)
    preserved = codex_preserved_config_block(existing)
    combined = root + (preserved + "\n\n" if preserved else "") + provider_profile
    atomic_write_text(target, combined, validate=validate_codex_config)
    return target


def codex_api_key(config: AppConfig) -> str:
    selected = config.find_model(getattr(config, "codex_model_id", "") or config.default_model_id)
    return selected.api_key or "local-proxy"


def write_codex_auth(config: AppConfig) -> Path:
    auth_path_value = getattr(config, "codex_auth_path", "") or str(Path.home() / ".codex" / "auth.json")
    target = Path(auth_path_value).expanduser()
    payload: dict[str, object] = {}
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8-sig"))
            if isinstance(existing, dict):
                payload.update(existing)
        except Exception:
            pass
    payload["auth_mode"] = "apikey"
    payload["OPENAI_API_KEY"] = codex_api_key(config)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    atomic_write_text(target, text, validate=lambda value: json.loads(value))
    return target


def write_codex_files(config: AppConfig) -> tuple[Path, Path]:
    ensure_builtin_model_routes(config)
    if not getattr(config, "codex_model_id", ""):
        config.codex_model_id = config.default_model_id
    return write_codex_config(config), write_codex_auth(config)


def install_launch_script(config: AppConfig) -> Path:
    target_dir = Path.home() / "shtu-claude-proxy"
    target = target_dir / launch_script_filename()
    atomic_write_text(target, launch_script_text(claude_env(config), config.claude_path))
    if os.name != "nt":
        target.chmod(0o755)
    return target


def print_env(config: AppConfig) -> None:
    for key, value in claude_env(config).items():
        if os.name == "nt":
            print(f"$env:{key} = {json.dumps(value)}")
        else:
            safe = value.replace("'", "'\\''")
            print(f"export {key}='{safe}'")


def show_config(config: AppConfig) -> None:
    print(f"Config path: {config_path()}")
    print(f"Proxy URL: http://{config.host}:{config.port}")
    print(f"Claude path: {config.claude_path}")
    print(f"Claude settings: {config.claude_settings_path}")
    print(f"Codex model: {config.codex_model_id}")
    print(f"Codex config: {config.codex_config_path}")
    print(f"Codex auth: {config.codex_auth_path}")
    print("Model routing:")
    for key in MODEL_ENV_KEYS:
        print(f"  {key}: {config.model_env.get(key, config.default_model_id)}")
    print("Models:")
    for model in config.models:
        has_key = "yes" if model.api_key else "no"
        print(f"  - {model.model_id} -> {model.upstream_model} ({model.api_format}, key={has_key})")


def is_port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def restart_existing_listener(host: str, port: int) -> bool:
    if not is_port_listening(host, port) or os.name != "nt":
        return False
    command = f"(Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue).OwningProcess | Select-Object -Unique"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=10,
    )
    pids = [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]
    current_pid = os.getpid()
    stopped = False
    for pid in pids:
        if pid == current_pid:
            continue
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, text=True, timeout=10)
        stopped = True
    return stopped


def serve(config: AppConfig) -> None:
    import proxy

    proxy.ACTIVE_CONFIG = config
    try:
        server = ThreadingHTTPServer((config.host, config.port), ProxyHandler)
    except OSError:
        if restart_existing_listener(config.host, config.port):
            server = ThreadingHTTPServer((config.host, config.port), ProxyHandler)
        else:
            raise
    print(f"SHTUClaudeProxy listening on http://{config.host}:{config.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping proxy")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SHTUClaudeProxy command-line tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("show-config", help="Show resolved config and model routing")
    subparsers.add_parser("print-env", help="Print shell commands for Claude Code environment variables")
    subparsers.add_parser("write-settings", help="Write Claude Code settings.json env block")
    codex_parser = subparsers.add_parser("write-codex-config", help="Write Codex config.toml Responses provider/profile")
    codex_parser.add_argument("--model", help="Codex model id to write, e.g. glm-chat, deepseek-chat, qwen-instruct")
    subparsers.add_parser("install-launch-script", help="Install claude-shtu launch script")
    subparsers.add_parser("serve", help="Run proxy server without GUI")

    args = parser.parse_args(argv)
    config = load_config()

    if args.command == "show-config":
        show_config(config)
    elif args.command == "print-env":
        print_env(config)
    elif args.command == "write-settings":
        path = write_claude_settings(config)
        print(f"Wrote Claude settings: {path}")
    elif args.command == "write-codex-config":
        if getattr(args, "model", None):
            config.codex_model_id = args.model
            save_config(config)
        config_file, auth_file = write_codex_files(config)
        print(f"Wrote Codex config: {config_file}")
        print(f"Wrote Codex auth: {auth_file}")
    elif args.command == "install-launch-script":
        path = install_launch_script(config)
        print(f"Installed launch script: {path}")
    elif args.command == "serve":
        serve(config)
    else:
        parser.error(f"Unknown command: {args.command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
