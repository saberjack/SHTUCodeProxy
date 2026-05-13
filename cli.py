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

from config_store import AppConfig, CODEX_SANDBOX_MODES, DEFAULT_CODEX_SANDBOX_MODE, MODEL_ENV_KEYS, config_path, ensure_builtin_model_routes, load_config, save_config
from platform_utils import launch_script_filename, launch_script_text
from proxy import ProxyHandler, ThreadingHTTPServer
from safe_io import atomic_write_text, backup_existing_file, restore_latest_backup, snapshot_original_file


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
    snapshot_original_file(settings_path)
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
    if parsed.get("sandbox_mode") not in CODEX_SANDBOX_MODES:
        raise ValueError("Codex sandbox_mode must be a supported Codex sandbox mode")
    features = parsed.get("features", {})
    if not isinstance(features, dict) or features.get("hooks") is not True:
        raise ValueError("Codex features.hooks must be enabled")
    if "codex_hooks" in features:
        raise ValueError("Codex features.codex_hooks is deprecated; use features.hooks")
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
    sandbox_mode = getattr(config, "codex_sandbox_mode", DEFAULT_CODEX_SANDBOX_MODE)
    if sandbox_mode not in CODEX_SANDBOX_MODES:
        sandbox_mode = DEFAULT_CODEX_SANDBOX_MODE
    return "\n".join([
        f'model = "{codex_model}"',
        f'model_provider = "{provider}"',
        f'sandbox_mode = "{sandbox_mode}"',
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


def toml_header_name(line: str) -> str:
    stripped = line.strip()
    if not is_toml_section_header(stripped):
        return ""
    return stripped.strip("[]").strip()


def is_managed_codex_section(header_name: str) -> bool:
    return header_name in {
        "features",
        "windows",
        "model_providers.shtu_proxy",
        "profiles.shtu_proxy",
        "model_providers.custom",
    }


def is_stale_model_selection_line(header_name: str, line: str) -> bool:
    if header_name.startswith("profiles."):
        return False
    stripped = line.strip()
    return stripped.startswith("model =") or stripped.startswith("model_provider =")


def format_toml_value(value: object) -> str:
    return json.dumps(value) if isinstance(value, str) else str(value).lower()


def parse_flat_toml_section(existing: str, section_name: str) -> dict[str, object]:
    values: dict[str, object] = {}
    in_section = False
    for line in existing.splitlines():
        stripped = line.strip()
        if is_toml_section_header(stripped):
            in_section = toml_header_name(stripped) == section_name
            continue
        if not in_section or not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        try:
            parsed_line = tomllib.loads(stripped)
        except tomllib.TOMLDecodeError:
            continue
        if key in parsed_line:
            values[key] = parsed_line[key]
    return values


def codex_preserved_config_block(existing: str) -> str:
    try:
        parsed = tomllib.loads(existing) if existing.strip() else {}
    except tomllib.TOMLDecodeError:
        parsed = {}
    lines: list[str] = []
    unmanaged_root, unmanaged_sections = codex_unmanaged_config_parts(existing)
    if unmanaged_root:
        lines.append(unmanaged_root)
        lines.append("")
    features = parsed.get("features") if isinstance(parsed.get("features"), dict) else parse_flat_toml_section(existing, "features")
    feature_values = dict(features)
    feature_values.pop("codex_hooks", None)
    feature_values["hooks"] = True
    lines.append("[features]")
    for key, value in feature_values.items():
        lines.append(f"{key} = {format_toml_value(value)}")
    windows = parsed.get("windows") if isinstance(parsed.get("windows"), dict) else parse_flat_toml_section(existing, "windows")
    windows_values = dict(windows)
    if os.name == "nt":
        windows_values["sandbox"] = "elevated"
    if windows_values:
        lines.append("")
        lines.append("[windows]")
        for key, value in windows_values.items():
            lines.append(f"{key} = {format_toml_value(value)}")
    if unmanaged_sections:
        lines.append("")
        lines.append(unmanaged_sections)
    return "\n".join(lines).strip()


def codex_unmanaged_config_parts(existing: str) -> tuple[str, str]:
    lines = existing.splitlines()
    blocks: list[str] = []
    root_lines: list[str] = []
    block: list[str] = []
    current_header = ""
    managed_root_keys = {"model", "model_provider", "sandbox_mode"}
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if is_toml_section_header(stripped):
            if block and current_header and not is_managed_codex_section(current_header):
                blocks.append("\n".join(block).rstrip())
            current_header = toml_header_name(stripped)
            block = [line]
            index += 1
            continue
        if current_header:
            if not is_stale_model_selection_line(current_header, line):
                block.append(line)
        elif stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
            if key not in managed_root_keys:
                root_lines.append(line)
        elif root_lines:
            root_lines.append(line)
        index += 1
    if block and current_header and not is_managed_codex_section(current_header):
        blocks.append("\n".join(block).rstrip())
    root = "\n".join(root_lines).strip()
    sections = "\n\n".join(block for block in blocks if block.strip())
    return root, sections


def write_codex_config(config: AppConfig) -> Path:
    config_path_value = getattr(config, "codex_config_path", "") or str(Path.home() / ".codex" / "config.toml")
    target = Path(config_path_value).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    snapshot_original_file(target)
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
    snapshot_original_file(target)
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


def restore_client_backup(path_value: str, *, original: bool = False) -> Path:
    target = Path(path_value).expanduser()
    restored = restore_latest_backup(target, original=original)
    if restored is None:
        kind = "original" if original else "recent"
        raise FileNotFoundError(f"No {kind} backup found for {target}")
    return restored


def codex_health_report(config: AppConfig) -> tuple[bool, list[str]]:
    ensure_builtin_model_routes(config)
    messages: list[str] = []
    ok = True
    config_target = Path(getattr(config, "codex_config_path", "") or Path.home() / ".codex" / "config.toml").expanduser()
    auth_target = Path(getattr(config, "codex_auth_path", "") or Path.home() / ".codex" / "auth.json").expanduser()
    if not config_target.exists():
        return False, [f"Missing config.toml: {config_target}"]
    try:
        text = config_target.read_text(encoding="utf-8-sig")
        parsed = tomllib.loads(text)
        validate_codex_config(text)
        messages.append("config.toml TOML syntax: OK")
    except Exception as exc:
        return False, [f"config.toml invalid: {exc}"]
    expected_model = getattr(config, "codex_model_id", "") or config.default_model_id
    if parsed.get("model") == expected_model:
        messages.append(f"root model: OK ({expected_model})")
    else:
        ok = False
        messages.append(f"root model mismatch: {parsed.get('model')!r} != {expected_model!r}")
    if parsed.get("sandbox_mode") in CODEX_SANDBOX_MODES:
        messages.append(f"sandbox_mode: OK ({parsed.get('sandbox_mode')})")
    else:
        ok = False
        messages.append("sandbox_mode: invalid or missing")
    features = parsed.get("features", {}) if isinstance(parsed.get("features"), dict) else {}
    if features.get("hooks") is True and "codex_hooks" not in features:
        messages.append("features.hooks: OK")
    else:
        ok = False
        messages.append("features.hooks: missing or deprecated codex_hooks still present")
    provider = parsed.get("model_providers", {}).get("shtu_proxy", {}) if isinstance(parsed.get("model_providers"), dict) else {}
    if provider.get("wire_api") == "responses" and provider.get("base_url") == f"http://{config.host}:{config.port}/v1" and provider.get("requires_openai_auth") is True:
        messages.append("shtu_proxy provider: OK")
    else:
        ok = False
        messages.append("shtu_proxy provider: mismatch")
    profile = parsed.get("profiles", {}).get("shtu_proxy", {}) if isinstance(parsed.get("profiles"), dict) else {}
    if profile.get("model_provider") == "shtu_proxy" and profile.get("model") == expected_model:
        messages.append("shtu_proxy profile: OK")
    else:
        ok = False
        messages.append("shtu_proxy profile: mismatch")
    mcp_servers = parsed.get("mcp_servers", {})
    if isinstance(mcp_servers, dict) and mcp_servers:
        messages.append(f"MCP servers preserved: {', '.join(sorted(mcp_servers.keys()))}")
    else:
        messages.append("MCP servers: none configured")
    projects = parsed.get("projects", {})
    if isinstance(projects, dict) and projects:
        messages.append(f"trusted project entries: {len(projects)}")
    if auth_target.exists():
        try:
            auth = json.loads(auth_target.read_text(encoding="utf-8-sig"))
            if auth.get("auth_mode") == "apikey" and auth.get("OPENAI_API_KEY"):
                messages.append("auth.json API key mode: OK")
            else:
                ok = False
                messages.append("auth.json API key mode: missing")
        except Exception as exc:
            ok = False
            messages.append(f"auth.json invalid: {exc}")
    else:
        ok = False
        messages.append(f"Missing auth.json: {auth_target}")
    return ok, messages


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
