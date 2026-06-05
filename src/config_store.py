from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from platform_utils import app_dir, default_claude_path, default_claude_settings_path, default_codex_auth_path, default_codex_config_path, portable_claude_path, portable_codex_auth_path, portable_codex_config_path, portable_settings_path
from safe_io import atomic_write_text

APP_NAME = "SHTUClaudeProxy"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8082
DEFAULT_RESPONSES_URL = "https://genaiapi.shanghaitech.edu.cn/api/v1/response"
DEFAULT_CHAT_COMPLETIONS_URL = "https://genaiapi.shanghaitech.edu.cn/api/v1/start"
DEFAULT_UPSTREAM_URL = DEFAULT_RESPONSES_URL
DEFAULT_API_FORMAT = "responses"
DEFAULT_MODEL_ID = "GPT-5.5"
CODEX_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_CODEX_SANDBOX_MODE = "danger-full-access"
CODEX_APPROVAL_POLICIES = ("never", "on-failure", "untrusted", "on-request")
DEFAULT_CODEX_APPROVAL_POLICY = "never"
CODEX_PERSONALITIES = ("pragmatic", "friendly", "precise")
DEFAULT_CODEX_PERSONALITY = "pragmatic"
CODEX_REASONING_EFFORTS = ("low", "medium", "high")
DEFAULT_CODEX_REASONING_EFFORT = "high"
MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_REASONING_MODEL",
)


def strip_model_date_suffix(model_id: str) -> str:
    prefix, separator, suffix = model_id.rpartition("-")
    if separator and len(suffix) == 8 and suffix.isdigit():
        return prefix
    return model_id


def bool_from_config(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


@dataclass
class ModelConfig:
    name: str
    model_id: str
    base_url: str
    api_key: str
    upstream_model: str
    api_format: str
    supports_image: bool = False
    supports_audio: bool = False
    supports_video: bool = False
    stream_bridge: bool = False
    max_context_tokens: int = 0  # 0 means unknown/use global default

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelConfig":
        model_id = str(data.get("model_id") or data.get("id") or DEFAULT_MODEL_ID).strip()
        upstream_model = str(data.get("upstream_model") or model_id).strip()
        legacy_multimodal = bool_from_config(data.get("supports_multimodal"), default_supports_image(model_id, upstream_model))
        return cls(
            name=str(data.get("name") or model_id).strip(),
            model_id=model_id,
            base_url=str(data.get("base_url") or DEFAULT_UPSTREAM_URL).strip(),
            api_key=str(data.get("api_key") or "").strip(),
            upstream_model=upstream_model,
            api_format=str(data.get("api_format") or DEFAULT_API_FORMAT).strip(),
            supports_image=bool_from_config(data.get("supports_image"), legacy_multimodal),
            supports_audio=bool_from_config(data.get("supports_audio"), False),
            supports_video=bool_from_config(data.get("supports_video"), False),
            stream_bridge=bool_from_config(data.get("stream_bridge"), "qwen" in f"{model_id} {upstream_model}".lower()),
            max_context_tokens=int(data.get("max_context_tokens") or data.get("max_tokens") or default_max_context_tokens(model_id, upstream_model)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "model_id": self.model_id,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "upstream_model": self.upstream_model,
            "api_format": self.api_format,
            "supports_image": self.supports_image,
            "supports_audio": self.supports_audio,
            "supports_video": self.supports_video,
            "stream_bridge": self.stream_bridge,
            "max_context_tokens": self.max_context_tokens,
        }


def default_supports_image(model_id: Any, upstream_model: Any = None) -> bool:
    model_text = f"{model_id or ''} {upstream_model or ''}".lower()
    return "gpt-5.5" in model_text or "qwen-instruct" in model_text


def default_max_context_tokens(model_id: Any, upstream_model: Any = None) -> int:
    model_text = f"{model_id or ''} {upstream_model or ''}".lower()
    if "gpt-5.5" in model_text:
        return 400000
    if "deepseek-chat" in model_text:
        return 192000
    if "deepseek-pro" in model_text:
        return 128000
    if "glm" in model_text:
        return 200000
    if "qwen" in model_text:
        return 131072
    return 0


@dataclass
class AppConfig:
    host: str
    port: int
    default_model_id: str
    codex_model_id: str
    codex_sandbox_mode: str
    codex_approval_policy: str
    codex_personality: str
    codex_reasoning_effort: str
    model_env: Dict[str, str]
    timeout: int
    claude_path: str
    claude_settings_path: str
    codex_config_path: str
    codex_auth_path: str
    default_stream: bool
    diagnostic_logging: bool
    models: List[ModelConfig]

    @classmethod
    def default(cls) -> "AppConfig":
        result = cls(
            host=DEFAULT_HOST,
            port=DEFAULT_PORT,
            default_model_id=DEFAULT_MODEL_ID,
            codex_model_id=DEFAULT_MODEL_ID,
            codex_sandbox_mode=DEFAULT_CODEX_SANDBOX_MODE,
            codex_approval_policy=DEFAULT_CODEX_APPROVAL_POLICY,
            codex_personality=DEFAULT_CODEX_PERSONALITY,
            codex_reasoning_effort=DEFAULT_CODEX_REASONING_EFFORT,
            model_env={key: DEFAULT_MODEL_ID for key in MODEL_ENV_KEYS},
            timeout=300,
            claude_path=default_claude_path(),
            claude_settings_path=default_claude_settings_path(),
            codex_config_path=default_codex_config_path(),
            codex_auth_path=default_codex_auth_path(),
            default_stream=True,
            diagnostic_logging=False,
            models=[
                ModelConfig(
                    name="Default GPT-5.5",
                    model_id=DEFAULT_MODEL_ID,
                    base_url=DEFAULT_UPSTREAM_URL,
                    api_key="",
                    upstream_model=DEFAULT_MODEL_ID,
                    api_format=DEFAULT_API_FORMAT,
                    supports_image=True,
                )
            ],
        )
        result._loaded_at = time.time()
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        default = cls.default()
        models = [ModelConfig.from_dict(item) for item in data.get("models", []) if isinstance(item, dict)]
        default_model_id = str(data.get("default_model_id") or (models[0].model_id if models else default.default_model_id)).strip()
        codex_model_id = str(data.get("codex_model_id") or default_model_id).strip()
        codex_sandbox_mode = str(data.get("codex_sandbox_mode") or default.codex_sandbox_mode).strip()
        if codex_sandbox_mode not in CODEX_SANDBOX_MODES:
            codex_sandbox_mode = default.codex_sandbox_mode
        codex_approval_policy = str(data.get("codex_approval_policy") or default.codex_approval_policy).strip()
        if codex_approval_policy not in CODEX_APPROVAL_POLICIES:
            codex_approval_policy = default.codex_approval_policy
        codex_personality = str(data.get("codex_personality") or default.codex_personality).strip()
        if codex_personality not in CODEX_PERSONALITIES:
            codex_personality = default.codex_personality
        codex_reasoning_effort = str(data.get("codex_reasoning_effort") or default.codex_reasoning_effort).strip()
        if codex_reasoning_effort not in CODEX_REASONING_EFFORTS:
            codex_reasoning_effort = default.codex_reasoning_effort
        raw_model_env = data.get("model_env") if isinstance(data.get("model_env"), dict) else {}
        model_env = {
            key: str(raw_model_env.get(key) or default_model_id).strip()
            for key in MODEL_ENV_KEYS
        }
        result = cls(
            host=str(data.get("host") or default.host).strip(),
            port=int(data.get("port") or default.port),
            default_model_id=default_model_id,
            codex_model_id=codex_model_id,
            codex_sandbox_mode=codex_sandbox_mode,
            codex_approval_policy=codex_approval_policy,
            codex_personality=codex_personality,
            codex_reasoning_effort=codex_reasoning_effort,
            model_env=model_env,
            timeout=int(data.get("timeout") or default.timeout),
            claude_path=portable_claude_path(str(data.get("claude_path") or default.claude_path)),
            claude_settings_path=portable_settings_path(str(data.get("claude_settings_path") or default.claude_settings_path)),
            codex_config_path=portable_codex_config_path(str(data.get("codex_config_path") or default.codex_config_path)),
            codex_auth_path=portable_codex_auth_path(str(data.get("codex_auth_path") or default.codex_auth_path)),
            default_stream=bool(data.get("default_stream", default.default_stream)),
            diagnostic_logging=bool(data.get("diagnostic_logging", default.diagnostic_logging)),
            models=models or default.models,
        )
        result._loaded_at = time.time()
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "default_model_id": self.default_model_id,
            "codex_model_id": self.codex_model_id,
            "codex_sandbox_mode": self.codex_sandbox_mode,
            "codex_approval_policy": self.codex_approval_policy,
            "codex_personality": self.codex_personality,
            "codex_reasoning_effort": self.codex_reasoning_effort,
            "model_env": self.model_env,
            "timeout": self.timeout,
            "claude_path": self.claude_path,
            "claude_settings_path": self.claude_settings_path,
            "codex_config_path": self.codex_config_path,
            "codex_auth_path": self.codex_auth_path,
            "default_stream": self.default_stream,
            "diagnostic_logging": self.diagnostic_logging,
            "models": [model.to_dict() for model in self.models],
        }

    def find_model(self, requested_model: Optional[str]) -> ModelConfig:
        for candidate in (requested_model, self.default_model_id):
            if not candidate:
                continue
            candidates = [candidate]
            normalized = strip_model_date_suffix(candidate)
            if normalized != candidate:
                candidates.append(normalized)
            for model_candidate in candidates:
                for model in self.models:
                    if model_candidate in (model.model_id, model.name):
                        return model
            for model_candidate in candidates:
                for model in self.models:
                    if model_candidate == model.upstream_model:
                        return model
        return self.models[0]


def config_path() -> Path:
    env_path = os.getenv("CLAUDE_RESPONSES_PROXY_CONFIG")
    if env_path:
        return Path(env_path)
    return app_dir() / "config.json"


def seed_builtin_model_routes(config: AppConfig) -> AppConfig:
    existing = {m.model_id for m in config.models}
    routes = [
        ("DeepSeek Pro", "deepseek-pro", DEFAULT_CHAT_COMPLETIONS_URL, "chat_completions"),
        ("GPT-5.5", "GPT-5.5", DEFAULT_RESPONSES_URL, "responses"),
        ("GLM Chat", "glm-chat", DEFAULT_CHAT_COMPLETIONS_URL, "chat_completions"),
        ("DeepSeek Chat", "deepseek-chat", DEFAULT_CHAT_COMPLETIONS_URL, "chat_completions"),
        ("Qwen Instruct", "qwen-instruct", DEFAULT_CHAT_COMPLETIONS_URL, "chat_completions"),
    ]
    for name, model_id, base_url, api_format in routes:
        if model_id in existing:
            continue
        config.models.append(ModelConfig(
            name=name,
            model_id=model_id,
            base_url=base_url,
            api_key="",
            upstream_model=model_id,
            api_format=api_format,
            supports_image=default_supports_image(model_id, model_id),
        ))
    model_ids = {model.model_id for model in config.models}
    if config.codex_model_id not in model_ids:
        config.codex_model_id = config.default_model_id if config.default_model_id in model_ids else config.models[0].model_id
    return config


def load_config(path: Optional[Path] = None) -> AppConfig:
    target = path or config_path()
    if not target.exists():
        config = AppConfig.default()
        seed_builtin_model_routes(config)
        save_config(config, target)
        return config
    try:
        return AppConfig.from_dict(json.loads(target.read_text(encoding="utf-8-sig")))
    except Exception:
        return seed_builtin_model_routes(AppConfig.default())


def save_config(config: AppConfig, path: Optional[Path] = None) -> None:
    target = path or config_path()
    payload = json.dumps(config.to_dict(), ensure_ascii=False, indent=2)
    atomic_write_text(target, payload, validate=lambda text: json.loads(text))
