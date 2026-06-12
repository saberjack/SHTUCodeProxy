"""SHTUCodeProxy — 日志模块

职责: 日志级别控制、日志写入、orjson 封装、JSON 工具函数

日志级别:
  -1 = 不启用（不写日志文件，仅 stderr）
   0 = 静默（不输出任何日志）
   1 = 仅错误
   2 = 信息（默认）
   3 = 详细

优先级: config.json log_level > 环境变量 SHTU_LOG_LEVEL > 默认值 2
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

from platform_utils import app_dir
from config_store import AppConfig

# 性能优化: 优先使用 orjson (2-10x faster than stdlib json)
try:
    import orjson as _orjson
    _HAS_ORJSON = True
except ImportError:  # pragma: no cover
    import json as _orjson  # type: ignore
    _HAS_ORJSON = False


# ---------------------------------------------------------------------------
# orjson 封装
# ---------------------------------------------------------------------------

def _orjson_dumps(obj: Any) -> bytes:
    """统一的 JSON 序列化入口 (返回 bytes)."""
    if _HAS_ORJSON:
        return _orjson.dumps(obj)
    return _orjson.dumps(obj).encode("utf-8")


def _orjson_dumps_str(obj: Any) -> str:
    """返回字符串形式的 JSON (用于需要 str 而非 bytes 的场景)."""
    if _HAS_ORJSON:
        return _orjson.dumps(obj).decode("utf-8")
    return _orjson.dumps(obj)


def _orjson_loads(data: Any) -> Any:
    """统一的 JSON 解析入口."""
    if _HAS_ORJSON:
        if isinstance(data, (bytes, bytearray)):
            return _orjson.loads(data)
        if isinstance(data, str):
            return _orjson.loads(data.encode("utf-8"))
        return _orjson.loads(data)
    return _orjson.loads(data)


def json_dumps_compact(value: Any) -> str:
    """紧凑 JSON 输出 (orjson 默认即紧凑, stdlib 需 separators)."""
    if _HAS_ORJSON:
        return _orjson_dumps_str(value)
    import json
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 日志级别
# ---------------------------------------------------------------------------

LOG_FILE_MAX_BYTES = 5 * 1024 * 1024

# 模块级缓存: current_config() 尚未就绪时使用
_LOG_LEVEL = int(os.getenv("SHTU_LOG_LEVEL", "2"))

# 由 proxy 模块在启动时注册，避免循环导入
_ACTIVE_CONFIG_REF = None


def register_active_config(config_getter) -> None:
    """由 proxy 模块调用，注册获取当前配置的回调。"""
    global _ACTIVE_CONFIG_REF
    _ACTIVE_CONFIG_REF = config_getter


def current_config() -> AppConfig:
    """获取当前活跃配置。通过 register_active_config 注册的回调获取。"""
    if _ACTIVE_CONFIG_REF is not None:
        return _ACTIVE_CONFIG_REF()
    return AppConfig.default()


def _get_log_level() -> int:
    """动态获取日志级别, 优先 config.json, 其次环境变量, 默认 2."""
    try:
        cfg = current_config()
        cl = getattr(cfg, "log_level", -1)
        if isinstance(cl, int) and -1 <= cl <= 3:
            return cl
    except Exception:
        pass
    return int(os.getenv("SHTU_LOG_LEVEL", "2"))


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------

def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# 日志写入
# ---------------------------------------------------------------------------

def _write_log(line: str) -> None:
    """底层日志写入，-1 时不写文件仅 stderr。"""
    print(line, file=sys.stderr, flush=True)
    if _get_log_level() == -1:
        return  # -1: 不启用日志文件
    try:
        target_dir = app_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "proxy.log"
        if target.exists() and target.stat().st_size > LOG_FILE_MAX_BYTES:
            backup = target_dir / "proxy.log.1"
            if backup.exists():
                backup.unlink()
            target.rename(backup)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def log(message: str) -> None:
    """向后兼容的无条件日志输出。新代码请用 log_error/log_info/log_debug。"""
    if _get_log_level() <= 0:
        return  # 0=静默, -1=不启用
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    _write_log(line)


def log_error(message: str) -> None:
    """仅 log_level>=1 时输出 (错误级别)。"""
    if _get_log_level() < 1:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    _write_log(line)


def log_info(message: str) -> None:
    """仅 log_level>=2 时输出 (信息级别，默认)。"""
    if _get_log_level() < 2:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    _write_log(line)


def log_debug(message: str) -> None:
    """仅 log_level>=3 时输出 (详细日志)。"""
    if _get_log_level() < 3:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    _write_log(line)


def usage_cache_debug(usage: Any) -> str:
    if not isinstance(usage, dict):
        return ""
    candidates = {
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cached_tokens": usage.get("cached_tokens"),
    }
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details")
    if isinstance(input_details, dict):
        candidates["details_cached_tokens"] = input_details.get("cached_tokens")
    present = {key: value for key, value in candidates.items() if value is not None}
    if not present:
        return ""
    # 性能优化: 用 orjson 替代 json
    return " cache_usage=" + _orjson_dumps_str(present)
