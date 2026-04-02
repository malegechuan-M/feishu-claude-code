"""记忆三层加载策略 — 意图驱动，按需加载不同粒度的长期记忆。"""
import re
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 与 memory_local.py 一致的路径
SOUL_FILE = Path.home() / ".feishu-claude" / "brain" / "SOUL.md"
MEMORY_FILE = Path.home() / ".feishu-claude" / "brain" / "MEMORY.md"
LEARNINGS_FILE = Path.home() / ".feishu-claude" / "learnings" / "LEARNINGS.md"
ERRORS_FILE = Path.home() / ".feishu-claude" / "learnings" / "ERRORS.md"
PROFILE_FILE = Path.home() / ".feishu-claude" / "brain" / "PROFILE.md"
TOOLS_FILE = Path.home() / ".feishu-claude" / "brain" / "TOOLS.md"
PATTERNS_FILE = Path.home() / ".feishu-claude" / "brain" / "PATTERNS.md"
DECISIONS_FILE = Path.home() / ".feishu-claude" / "brain" / "DECISIONS.md"

# 意图关键词组 → 需要加载的记忆文件和层级
# 匹配到关键词时强制使用 Layer 3（完整加载）
INTENT_KEYWORDS = {
    "identity": ["你是谁", "你叫什么", "自我介绍", "你的身份"],
    "memory_ref": ["之前", "上次", "还记得", "我说过", "你说过", "记不记得"],
    "tools": ["工具", "MCP", "mcp", "插件", "skill"],
    "decisions": ["决策", "决定", "为什么这样", "当时为什么"],
    "learnings": ["教训", "经验", "踩坑", "错误", "纠正"],
    "patterns": ["习惯", "偏好", "风格", "喜欢怎样"],
    "evolution": ["进化", "成长", "学到了", "改进"],
}

# Layer 预算（字符）
LAYER_BUDGETS = {
    1: 300,    # Index: 标题 + 首行
    2: 1200,   # Summary: 智能截断
    3: None,   # Full: 不限
}


def select_layer(intent: str, text: str) -> int:
    """
    根据意图和消息内容，选择记忆加载层级。

    Returns:
        0 — 跳过（trivial）
        1 — Index（标题预览）
        2 — Summary（智能截断）
        3 — Full（完整加载）
    """
    # trivial 消息跳过记忆
    if intent == "trivial":
        return 0

    # 检查是否命中深度关键词 → 强制 Layer 3
    text_lower = text.lower()
    for group, keywords in INTENT_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                logger.debug(f"[router_context] 关键词命中 '{kw}' ({group}) → Layer 3")
                return 3

    # 按意图选择默认层级
    layer_map = {
        "chat": 1,      # 闲聊：标题预览即可
        "question": 2,  # 提问：摘要级别
        "task": 3,      # 任务：完整加载
    }
    return layer_map.get(intent, 2)


def _load_file_safe(path: Path) -> str:
    """安全读取文件，失败返回空"""
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _extract_index(content: str) -> str:
    """Layer 1: 提取章节标题 + 每节首行预览"""
    if not content:
        return ""
    lines = content.split("\n")
    result = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            result.append(stripped)
            # 取标题后第一个非空行作为预览
            for j in range(i + 1, min(i + 3, len(lines))):
                preview = lines[j].strip()
                if preview and not preview.startswith("#"):
                    result.append(f"  {preview[:80]}...")
                    break
    return "\n".join(result)


def _extract_summary(content: str, budget: int = 1200) -> str:
    """Layer 2: 智能截断，保留最近的 section，控制在 budget 字符内"""
    if not content:
        return ""
    if len(content) <= budget:
        return content

    # 按 ## 标题拆分 section
    sections = re.split(r"(?=^## )", content, flags=re.M)
    sections = [s.strip() for s in sections if s.strip()]

    if not sections:
        return content[:budget]

    # 从最后一个 section 开始往前加，直到超预算
    result = []
    used = 0
    for section in reversed(sections):
        if used + len(section) > budget and result:
            break
        result.insert(0, section)
        used += len(section)

    return "\n\n".join(result)


def load_context(layer: int) -> str:
    """
    按指定层级加载长期记忆。

    Layer 0: 返回空（跳过）
    Layer 1: 各文件的标题索引
    Layer 2: 各文件的智能截断摘要
    Layer 3: 完整加载（等同于原 read_brain_context）
    """
    if layer <= 0:
        return ""

    files = [
        (SOUL_FILE, "行为原则"),
        (MEMORY_FILE, "长期记忆"),
        (LEARNINGS_FILE, "经验教训"),
        (PROFILE_FILE, "用户画像"),
        (TOOLS_FILE, "工具能力"),
        (PATTERNS_FILE, "行为模式"),
        (DECISIONS_FILE, "决策记录"),
    ]

    parts = []
    for path, label in files:
        content = _load_file_safe(path)
        if not content:
            continue

        if layer == 1:
            processed = _extract_index(content)
        elif layer == 2:
            budget = LAYER_BUDGETS.get(2, 1200)
            processed = _extract_summary(content, budget)
        else:
            processed = content

        if processed:
            parts.append(f"[{label}]\n{processed}")

    if not parts:
        return ""

    total_chars = sum(len(p) for p in parts)
    logger.debug(f"[router_context] Layer {layer} 加载 {total_chars} 字符")

    return (
        "\n\n---\n"
        f"[本地记忆系统 - Layer {layer}]\n\n"
        + "\n\n".join(parts)
        + "\n---\n"
    )
