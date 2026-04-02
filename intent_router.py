"""
意图检测模块：对每条消息做轻量分类，决定后续处理路径。

分类结果：
- trivial   ：寒暄/确认/表情，直接跳过 RAG 和记忆召回
- question ：提问/求助/查询，正常流程 + RAG 知识库增强
- task     ：执行任务/分析/调研，正常流程 + 可能触发长任务检查点
- chat     ：闲聊/讨论，正常流程

意图检测流程：
1. 快速单词移除法（零延迟，不调 LLM）：移除常见寒暄词后内容为空则 trivial
2. Haiku LLM 分类（token 有效时）：复杂消息进一步分类
"""

import re

# ── 快速通道：单词移除法 ──────────────────────────────────────────
# 按长度降序排列，优先匹配最长词
_TRIVIAL_WORDS_SORTED = sorted(
    [
        "好的",
        "你好",
        "hi",
        "hello",
        "ok",
        "嗯嗯",
        "哈哈",
        "谢谢",
        "感谢",
        "666",
        "牛",
        "行",
        "收到",
        "了解",
        "明白",
        "是的",
        "对",
        "可以",
        "没问题",
        "好嘞",
        "好哒",
        "辛苦了",
        "赞",
        "好的呀",
        "好的哈",
        "好的好的",
        "好的呀",
        "辛苦了",
    ],
    key=len,
    reverse=True,
)

# 单字符寒暄集
_TRIVIAL_CHARS = set("好谢哈嗯嗨呵呵嘻行收了对可没赞哟啊嘿喔")


def _is_trivial_word_removal(text: str) -> bool:
    """
    快速 trivial 检测：移除所有寒暄词/字符后内容为空则为 trivial。
    零延迟，零 API 调用。
    """
    remaining = text.strip().rstrip("!。～~? ")
    for w in _TRIVIAL_WORDS_SORTED:
        remaining = remaining.replace(w, "")
    for c in _TRIVIAL_CHARS:
        remaining = remaining.replace(c, "")
    remaining = re.sub(r"^[，,。！!～~\s?]+|[，,。！!～~\s?]+$", "", remaining)
    remaining = re.sub(
        r"^[\U0001F300-\U0001FAFF\U00002702-\U000027B0\U0000FE00-\U0000FE0F]+$",
        "",
        remaining,
    )
    return len(remaining) == 0


# ── Haiku LLM 分类 ──────────────────────────────────────────────
_HAIKU_INTENT_PROMPT = """你是意图分类助手。对用户消息分类，只返回一个词：
- trivial（寒暄/确认/表情/点赞，如"你好""谢谢""好的"）
- question（提问/求助/查询，如"怎么做""是什么""帮我查"）
- task（执行任务/分析/调研，如"帮我写""帮我分析""做个报告"）
- chat（闲聊/讨论/无明确目的）

用户消息："""


async def _classify_with_haiku(text: str) -> str:
    """
    调用 Claude Haiku 做意图分类。
    失败时返回 "chat"（当普通聊天处理，最安全的 fallback）。
    """
    try:
        from llm_client import chat_haiku

        result = await chat_haiku(
            messages=[
                {"role": "system", "content": _HAIKU_INTENT_PROMPT},
                {"role": "user", "content": text[:200]},
            ],
            max_tokens=15,
            temperature=0,
        )
        for intent in ("trivial", "question", "task", "chat"):
            if intent in result.lower():
                return intent
        return "chat"
    except Exception as e:
        print(f"[intent] Haiku 分类失败: {e}", flush=True)
        # fallback 用 question 而不是 chat，确保不会被误降到 Haiku
        return "question"


async def classify(text: str) -> str:
    """
    完整意图分类：快速单词移除法 → Haiku LLM 分类。
    简单消息走单词移除（零延迟零成本），复杂消息才调 Haiku。
    """
    if _is_trivial_word_removal(text):
        return "trivial"
    return await _classify_with_haiku(text)
