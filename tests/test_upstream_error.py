#!/usr/bin/env python3
"""
Test: Verify that upstream errors (e.g. context overflow) are properly
propagated to the downstream client instead of resulting in empty responses.

Usage:
  python tests/test_upstream_error.py [--proxy-url URL] [--api-key KEY]
  python tests/test_upstream_error.py --direct-only --upstream-api-key KEY
"""

from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

DEFAULT_PROXY_URL = "http://127.0.0.1:8091"
DEFAULT_API_KEY = "local-proxy"


def test_context_overflow_raw(proxy_url: str, api_key: str) -> None:
    """Send a request that will cause context overflow and inspect the raw SSE response."""
    large_text = "This is a test sentence. " * 20000  # ~800K chars ≈ 200K tokens

    payload = {
        "model": "deepseek-pro",
        "max_tokens": 16384,
        "stream": True,
        "messages": [
            {"role": "user", "content": large_text}
        ],
    }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "accept": "text/event-stream",
        "authorization": f"Bearer {api_key}",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    url = f"{proxy_url}/v1/messages"
    print(f"[TEST] Sending large context overflow request to {url}")
    print(f"[TEST] Payload size: {len(data) // 1024} KB")

    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        response = urllib.request.urlopen(req, timeout=120)
        status = response.status
        print(f"[TEST] Response status: {status}")

        print(f"[TEST] Raw SSE events:")
        print("-" * 60)
        event_count = 0
        has_error = False
        has_text_content = False
        error_content = None

        while True:
            raw = response.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue

            if line.startswith("event:"):
                event_type = line[6:].strip()
                print(f"  event: {event_type}")
            elif line.startswith("data:"):
                data_str = line[5:].strip()
                try:
                    parsed = json.loads(data_str)
                    event_type = parsed.get("type", "")
                    if event_type in ("error",) or parsed.get("error"):
                        has_error = True
                        error_content = parsed
                        print(f"  ⚠️  ERROR EVENT: {json.dumps(parsed, ensure_ascii=False)[:500]}")
                    elif event_type == "content_block_delta":
                        delta = parsed.get("delta", {})
                        text = delta.get("text", "")
                        if text:
                            has_text_content = True
                            if "[Proxy Error]" in text:
                                has_error = True
                                error_content = parsed
                            print(f"  📝 text delta: {text[:100]}{'...' if len(text) > 100 else ''}")
                        thinking = delta.get("thinking", "")
                        if thinking:
                            print(f"  🧠 thinking delta: {thinking[:80]}...")
                    elif event_type == "message_stop":
                        print(f"  ✅ message_stop")
                    else:
                        summary = json.dumps(parsed, ensure_ascii=False)
                        if len(summary) > 200:
                            summary = summary[:200] + "..."
                        print(f"  📦 {event_type or 'unknown'}: {summary}")
                except json.JSONDecodeError:
                    if data_str == "[DONE]":
                        print(f"  🔚 [DONE]")
                    else:
                        print(f"  ❓ raw data: {data_str[:200]}")
                event_count += 1

        print("-" * 60)
        print(f"[TEST] Total events: {event_count}")
        print(f"[TEST] Has error: {has_error}")
        print(f"[TEST] Has text content: {has_text_content}")

        if not has_error and not has_text_content:
            print()
            print("❌ BUG: Upstream error was NOT propagated to client!")
        elif has_error:
            print()
            print("✅ Error was properly propagated to client.")
        else:
            print()
            print("⚠️  Unexpected: got text content despite context overflow.")

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"[TEST] HTTP Error: {exc.code}")
        print(f"[TEST] Body: {body[:500]}")
        print()
        print("✅ Upstream returned HTTP error (caught by urllib).")
    except Exception as exc:
        print(f"[TEST] Exception: {type(exc).__name__}: {exc}")


def test_context_overflow_direct(upstream_url: str, upstream_model: str, api_key: str) -> None:
    """Send directly to upstream to see the raw error format."""
    large_text = "This is a test sentence. " * 20000

    payload = {
        "model": upstream_model,
        "max_tokens": 16384,
        "stream": True,
        "messages": [
            {"role": "user", "content": large_text}
        ],
        "chat_template_kwargs": {"enable_thinking": True},
    }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "accept": "text/event-stream",
        "authorization": f"Bearer {api_key}",
    }

    print(f"\n[TEST-DIRECT] Sending directly to upstream: {upstream_url}")
    print(f"[TEST-DIRECT] Payload size: {len(data) // 1024} KB")

    try:
        req = urllib.request.Request(upstream_url, data=data, headers=headers, method="POST")
        response = urllib.request.urlopen(req, timeout=120)
        print(f"[TEST-DIRECT] Response status: {response.status}")
        print(f"[TEST-DIRECT] Raw upstream SSE:")
        print("-" * 60)
        line_count = 0
        while line_count < 20:
            raw = response.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line:
                print(f"  {line[:300]}")
                line_count += 1
        while True:
            raw = response.readline()
            if not raw:
                break
        print("-" * 60)

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"[TEST-DIRECT] HTTP Error: {exc.code}")
        print(f"[TEST-DIRECT] Body: {body[:1000]}")
        print()
        print("📋 This is the raw error format the upstream returns.")
    except Exception as exc:
        print(f"[TEST-DIRECT] Exception: {type(exc).__name__}: {exc}")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Test upstream error propagation")
    parser.add_argument("--proxy-url", default=DEFAULT_PROXY_URL)
    parser.add_argument("--upstream-url", default="https://genaiapi.shanghaitech.edu.cn/api/v1/start/chat/completions")
    parser.add_argument("--upstream-model", default="deepseek-pro")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--upstream-api-key", default="")
    parser.add_argument("--direct-only", action="store_true", help="Only test direct upstream")
    args = parser.parse_args()

    if not args.direct_only:
        print("=" * 60)
        print("TEST 1: Via proxy (check if error is propagated)")
        print("=" * 60)
        test_context_overflow_raw(args.proxy_url, args.api_key)

    if args.upstream_api_key:
        print()
        print("=" * 60)
        print("TEST 2: Direct upstream (see raw error format)")
        print("=" * 60)
        test_context_overflow_direct(args.upstream_url, args.upstream_model, args.upstream_api_key)
    elif args.direct_only:
        print("\n⚠️  --upstream-api-key required for direct upstream test")


if __name__ == "__main__":
    main()
