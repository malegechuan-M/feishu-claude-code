"""统一上下文构建 — 将分散的上下文拼接逻辑集中管理。

注入优先级（从高到低）：
1. 环境提示（新 session 时）
2. 用户消息 + 任务检查点
3. 群聊上下文（Observer 或 deque）
4. 联系人记忆
5. Brain 长期记忆（三层加载）
6. 最近纠正
7. 行为直觉
8. 共享记忆（mem0 + RAG）
"""
import logging

logger = logging.getLogger(__name__)


def build_context(
    text: str,
    *,
    task_checkpoint_context: str = "",
    group_history: str = "",
    contact_context: str = "",
    brain_context: str = "",
    corrections_context: str = "",
    instinct_context: str = "",
    memory_context: str = "",
    env_hint: str = "",
) -> str:
    """
    按优先级拼接所有上下文，返回完整的 Claude 消息。

    Args:
        text: 用户原始消息
        task_checkpoint_context: 长任务检查点上下文
        group_history: 群聊历史（Observer 或 deque 增量）
        contact_context: 联系人档案
        brain_context: 长期记忆（SOUL + MEMORY + LEARNINGS）
        corrections_context: 最近的高置信度纠正
        instinct_context: 匹配到的行为直觉
        memory_context: 共享记忆（mem0 + RAG 召回）
        env_hint: 环境提示（新 session 首条消息）

    Returns:
        拼接好的完整消息文本
    """
    parts = []

    # 1. 环境提示（最前面，让 Claude 了解交互环境）
    if env_hint:
        parts.append(env_hint)

    # 2. 任务检查点 + 用户消息
    if task_checkpoint_context:
        parts.append(task_checkpoint_context)
    parts.append(text)

    # 3. 群聊上下文
    if group_history:
        parts.append(group_history)

    # 4. 联系人记忆
    if contact_context:
        parts.append(contact_context)

    # 5. Brain 长期记忆
    if brain_context:
        parts.append(brain_context)

    # 6. 最近纠正
    if corrections_context:
        parts.append(corrections_context)

    # 7. 行为直觉
    if instinct_context:
        parts.append(instinct_context)

    # 8. 共享记忆
    if memory_context:
        parts.append(memory_context)

    result = "\n\n".join(parts)

    # 统计
    total_chars = len(result)
    injected = sum(1 for ctx in [
        task_checkpoint_context, group_history, contact_context,
        brain_context, corrections_context, instinct_context, memory_context,
    ] if ctx)
    logger.debug(f"[context_builder] 总 {total_chars} 字符, {injected} 个上下文模块")

    return result
