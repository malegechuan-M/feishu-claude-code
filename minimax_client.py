"""MiniMax API 客户端 — 配额耗尽时的兜底模型。"""
import os
import json
import logging
import urllib.request
import ssl

logger = logging.getLogger(__name__)

MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
MINIMAX_ENDPOINT = "https://api.minimaxi.com/v1/text/chatcompletion_v2"
MINIMAX_MODEL = "MiniMax-M2.7"


def chat_minimax(user_message: str, system_prompt: str = "", max_tokens: int = 4096) -> str:
    """
    调用 MiniMax API 生成回复。
    用于 Claude 配额耗尽时的兜底。

    Returns:
        回复文本，失败时返回错误说明
    """
    if not MINIMAX_API_KEY:
        return "❌ MiniMax API Key 未配置，无法兜底"

    messages = []
    if system_prompt:
        messages.append({"role": "system", "name": "assistant", "content": system_prompt})
    messages.append({"role": "user", "name": "user", "content": user_message})

    body = json.dumps({
        "model": MINIMAX_MODEL,
        "messages": messages,
        "stream": False,
        "temperature": 0.7,
        "max_completion_tokens": max_tokens,
    }).encode("utf-8")

    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        MINIMAX_ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Bearer {MINIMAX_API_KEY}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
            data = json.loads(resp.read())
            # MiniMax 返回格式
            choices = data.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
            return f"❌ MiniMax 返回异常: {data}"
    except Exception as e:
        logger.error(f"[minimax] API 调用失败: {e}")
        return f"❌ MiniMax 调用失败: {e}"
