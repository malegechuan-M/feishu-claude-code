"""
轻量 LLM 调用层：封装 Claude Haiku API 供意图检测和 DAG 压缩复用。
优先用 credentials 文件中的 token（来自 Claude Pro/Max 订阅），无需额外 API Key。
"""

import json
import os
import ssl
import subprocess
import time
import urllib.request
from typing import Optional

# 代理地址（启动时读一次，避免重复查环境变量）
_PROXY_URL = os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY") or os.environ.get("https_proxy") or ""

# token 缓存（带时间戳，定期刷新）
_cached_token: Optional[str] = None
_token_fetched_at: float = 0
_TOKEN_TTL = 1800  # 30 分钟后重新获取 token


def _get_api_token(force_refresh: bool = False) -> Optional[str]:
    """获取 Claude API token，带 TTL 缓存，过期或失败时自动刷新"""
    global _cached_token, _token_fetched_at

    if not force_refresh and _cached_token and (time.time() - _token_fetched_at < _TOKEN_TTL):
        return _cached_token

    # 清除旧缓存
    _cached_token = None

    # 1. 试 credentials 文件
    creds_path = os.path.expanduser("~/.claude/.credentials.json")
    if os.path.isfile(creds_path):
        try:
            with open(creds_path) as f:
                creds = json.load(f)
            _cached_token = creds["claudeAiOauth"]["accessToken"]
            _token_fetched_at = time.time()
            return _cached_token
        except Exception:
            pass

    # 2. 试 keychain
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            creds = json.loads(result.stdout.strip())
            _cached_token = creds["claudeAiOauth"]["accessToken"]
            _token_fetched_at = time.time()
            return _cached_token
    except Exception:
        pass

    return None


def _build_opener():
    """构建带代理的 urllib opener"""
    ctx = ssl.create_default_context()
    if _PROXY_URL:
        proxy_handler = urllib.request.ProxyHandler({"https": _PROXY_URL, "http": _PROXY_URL})
        return urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


async def chat_haiku(
    messages: list[dict],
    max_tokens: int = 100,
    temperature: float = 0.1,
    system: str = "",
) -> str:
    """
    调用 Claude Haiku 做轻量任务（意图检测 / DAG 压缩）。
    失败时抛出异常，由调用方决定 fallback 策略。
    注意：system prompt 必须通过 system 参数传，不能放在 messages 里。
    """
    # 兼容处理：如果 messages 里有 role=system，自动提取出来
    clean_messages = []
    for m in messages:
        if m.get("role") == "system":
            if not system:
                system = m["content"]
        else:
            clean_messages.append(m)

    for attempt in range(2):
        token = _get_api_token(force_refresh=(attempt > 0))
        if not token:
            raise RuntimeError("无法获取 Claude API token")

        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": max_tokens,
            "messages": clean_messages,
            "temperature": temperature,
        }
        if system:
            payload["system"] = system

        body = json.dumps(payload).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        opener = _build_opener()
        try:
            with opener.open(req, timeout=30) as resp:
                result = json.loads(resp.read())
                blocks = result.get("content", [])
                if blocks and blocks[0].get("type") == "text":
                    return blocks[0]["text"].strip()
                return ""
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403) and attempt == 0:
                # token 可能过期，清除缓存重试
                print(f"[llm_client] Haiku API {e.code}，刷新 token 重试...", flush=True)
                continue
            raise
