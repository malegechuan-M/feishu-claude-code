"""Prompt injection defense for Feishu messages."""
import re
import logging

logger = logging.getLogger(__name__)

# 11+ 种中英文注入模式，覆盖常见 prompt injection 攻击手法
INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
    re.compile(r"ignore\s+(all\s+)?above", re.I),
    re.compile(r"you\s+are\s+now\s+", re.I),
    re.compile(r"act\s+as\s+(if\s+you\s+are\s+)?", re.I),
    re.compile(r"system\s*:", re.I),
    re.compile(r"<system>", re.I),
    re.compile(r"<\|im_start\|>", re.I),
    re.compile(r"忽略(之前|上面|以上)(的|所有)?(指令|规则|要求)", re.I),
    re.compile(r"你现在(是|扮演|变成)", re.I),
    re.compile(r"从现在起.*角色", re.I),
    re.compile(r"new\s+instructions?\s*:", re.I),
    re.compile(r"override\s+(system|instructions)", re.I),
    re.compile(r"forget\s+(all\s+)?(previous|your|the)\s+", re.I),
    re.compile(r"disregard\s+(all\s+)?(previous|above)", re.I),
]


def sanitize(text: str, is_group: bool = False) -> tuple[str, bool]:
    """
    检测并过滤 prompt 注入。

    私聊模式下：检测到注入模式时记录警告日志，但仍返回原始文本（宽松策略）。
    群聊模式下：强制将匹配段替换为 [FILTERED]，防止群聊中恶意用户注入。

    Args:
        text: 用户消息文本
        is_group: 是否群聊（群聊强制启用过滤）

    Returns:
        (cleaned_text, was_filtered) — 过滤后的文本和是否触发过滤标志
    """
    was_filtered = False
    cleaned = text

    for pattern in INJECTION_PATTERNS:
        if pattern.search(cleaned):
            was_filtered = True
            if is_group:
                # 群聊：强制替换匹配内容，防止注入
                cleaned = pattern.sub("[FILTERED]", cleaned)
                logger.warning(
                    f"[prompt_guard] 群聊注入模式已过滤: pattern={pattern.pattern!r} "
                    f"original_snippet={text[:80]!r}"
                )
            else:
                # 私聊：仅记录，不强制过滤（信任私聊用户）
                logger.warning(
                    f"[prompt_guard] 私聊检测到注入模式（未过滤）: "
                    f"pattern={pattern.pattern!r} snippet={text[:80]!r}"
                )

    return cleaned, was_filtered
