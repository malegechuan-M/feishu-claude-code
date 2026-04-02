"""
自动模型路由 — 根据意图和消息复杂度选择最合适的模型。

路由策略：
- trivial  → 不调 Claude（由 main.py 处理）
- chat     → Haiku（闲聊不需要强模型）
- question → Sonnet（常规提问）
- task     → Sonnet，复杂任务升级 Opus
- 代码/推理 → Opus + extended thinking

用户通过 /model 手动指定的模型优先级最高（锁定模式）。
"""
import re
import logging

logger = logging.getLogger(__name__)

# 模型定义
HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-6"

# 默认路由表（意图 → 模型）
INTENT_MODEL_MAP = {
    "trivial": HAIKU,   # 实际上 trivial 会跳过，这里做兜底
    "chat": HAIKU,
    "question": SONNET,
    "task": SONNET,      # 简单任务用 Sonnet，复杂任务下面会升级
}

# ── 复杂度检测：命中任一规则则升级到 Opus ──────────────────────

# 代码相关关键词
_CODE_PATTERNS = re.compile(
    r"(代码|代码审查|debug|bug|报错|error|重构|refactor|实现.*功能|写一个.*程序"
    r"|function|class\s+\w|def\s+\w|import\s+\w|编程|算法|数据结构"
    r"|API|接口|架构|设计模式|性能优化|并发|异步)",
    re.I
)

# 复杂推理关键词
_REASONING_PATTERNS = re.compile(
    r"(分析.*原因|为什么会|对比.*方案|评估.*风险|设计.*策略|制定.*计划"
    r"|深入分析|全面评估|系统设计|架构设计|技术选型|利弊|优缺点"
    r"|逻辑|推理|证明|反驳|论证|假设.*那么|如果.*会怎样"
    r"|战略|策略|规划|路线图|可行性)",
    re.I
)

# 长文本/复杂任务关键词
_COMPLEX_TASK_PATTERNS = re.compile(
    r"(完整的|详细的|全面的|深度|系统性|一步步|step.by.step"
    r"|帮我写一篇|撰写.*报告|市场调研|竞品分析|商业计划"
    r"|翻译.*全文|总结.*文档|review|审查)",
    re.I
)

# 消息长度阈值：超长消息通常意味着复杂上下文
_LONG_MSG_THRESHOLD = 500


def select_model(intent: str, text: str, user_model: str) -> tuple[str, str]:
    """
    根据意图和消息内容选择最合适的模型。

    Args:
        intent: 意图分类结果（trivial/chat/question/task）
        text: 用户消息原文
        user_model: 用户当前 session 设定的模型

    Returns:
        (selected_model, reason) — 选中的模型和选择原因
    """
    # 1. 用户手动锁定了 Opus → 尊重用户选择，不降级
    if user_model == OPUS:
        return OPUS, "用户指定 Opus"

    # 2. 用户手动选了 Haiku → 尊重用户选择，不升级
    if user_model == HAIKU:
        return HAIKU, "用户指定 Haiku"

    # 3. 自动路由逻辑
    base_model = INTENT_MODEL_MAP.get(intent, SONNET)

    # 复杂度检测：是否需要升级到 Opus
    upgrade_reason = _check_complexity(text, intent)
    if upgrade_reason:
        logger.info(f"[model_router] 升级到 Opus: {upgrade_reason}")
        return OPUS, upgrade_reason

    # 意图为 chat 且消息很短 → Haiku 足够
    if intent == "chat" and len(text) < 50:
        return HAIKU, "短闲聊"

    return base_model, f"意图={intent}"


def _check_complexity(text: str, intent: str) -> str | None:
    """
    检测消息是否属于复杂任务，需要升级到 Opus。

    Returns:
        升级原因字符串，None 表示不需要升级
    """
    # 代码相关
    if _CODE_PATTERNS.search(text):
        return "代码/编程任务"

    # 复杂推理
    if _REASONING_PATTERNS.search(text):
        return "复杂推理/分析"

    # 复杂任务
    if _COMPLEX_TASK_PATTERNS.search(text):
        return "复杂任务/长文撰写"

    # 超长消息 + task 意图
    if intent == "task" and len(text) > _LONG_MSG_THRESHOLD:
        return f"长消息任务({len(text)}字符)"

    return None


def select_effort(model: str, intent: str, text: str) -> str:
    """
    根据模型和复杂度选择 effort 级别（对应 claude CLI --effort 参数）。

    Returns:
        "low" / "medium" / "high" / "max"

    策略：
    - Haiku → low（闲聊不需要深度思考）
    - Sonnet + question → medium
    - Sonnet + task → high
    - Opus + question → high
    - Opus + task → max（启用最深度思考）
    - 深度推理关键词 → max
    """
    # 深度推理关键词 → 无论模型都用 max
    if _REASONING_PATTERNS.search(text):
        return "max"

    if model == HAIKU:
        return "low"

    if model == OPUS:
        if intent == "task":
            return "max"
        if intent == "question":
            return "high"
        return "high"

    # Sonnet
    if intent == "task":
        return "high"
    if intent == "question":
        return "medium"
    return "medium"
