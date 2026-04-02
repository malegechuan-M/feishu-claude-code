"""增强版纠正检测 — 分级置信度，高置信度立即注入下一轮 context。"""
import json
import re
import os
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

CORRECTIONS_QUEUE = Path.home() / ".feishu-claude" / "memory" / "corrections_queue.json"
LEARNINGS_QUEUE = Path.home() / ".feishu-claude" / "memory" / "learnings_queue.json"

# 11 种中英文纠正模式，每种附带置信度基线
CORRECTION_PATTERNS = [
    # 高置信度（>=0.7）— 明确纠正
    (re.compile(r"不[是对].*[而应](该|当)", re.S), 0.85, "explicit_correction"),
    (re.compile(r"你[搞弄]错了"), 0.80, "direct_error"),
    (re.compile(r"错了[，。！]"), 0.75, "error_signal"),
    (re.compile(r"(以后|下次|今后)(别|不要|不用|禁止)"), 0.80, "future_prohibition"),
    (re.compile(r"(记住|记住了)[，：:]"), 0.75, "remember_command"),
    (re.compile(r"wrong|incorrect|that'?s not", re.I), 0.75, "en_error"),
    (re.compile(r"actually[,\s]", re.I), 0.70, "en_actually"),

    # 中置信度（0.4-0.69）— 可能的纠正
    (re.compile(r"你误解了|你理解错了"), 0.65, "misunderstanding"),
    (re.compile(r"不是这样的|不是这个意思"), 0.60, "not_like_this"),
    (re.compile(r"don'?t\s+(do|use|add|make)", re.I), 0.55, "en_dont"),

    # 低置信度（<0.4）— 弱信号
    (re.compile(r"应该是|正确的是"), 0.35, "should_be"),
]

# 高置信度阈值：达到此值的纠正立即注入下一轮
HIGH_CONFIDENCE_THRESHOLD = 0.70


def detect_correction_v2(text: str, prev_response: str = "") -> tuple[float, str, str]:
    """
    增强版纠正检测。

    Args:
        text: 用户当前消息
        prev_response: Bot 上一条回复（用于提取错误上下文）

    Returns:
        (confidence, pattern_name, matched_text)
        confidence=0 表示未检测到纠正
    """
    best_confidence = 0.0
    best_pattern = ""
    best_match = ""

    for pattern, base_confidence, name in CORRECTION_PATTERNS:
        m = pattern.search(text)
        if m:
            # 取最高置信度的匹配
            if base_confidence > best_confidence:
                best_confidence = base_confidence
                best_pattern = name
                best_match = m.group(0)

    return best_confidence, best_pattern, best_match


def process_correction(user_msg: str, prev_response: str, user_id: str):
    """
    处理检测到的纠正：高置信度入 corrections_queue（下轮注入），
    低置信度入 learnings_queue（等每日复盘确认）。
    """
    confidence, pattern, matched = detect_correction_v2(user_msg, prev_response)

    if confidence == 0:
        return

    entry = {
        "timestamp": datetime.now().isoformat(),
        "user_id": user_id,
        "user_msg": user_msg[:500],
        "prev_response": prev_response[:300] if prev_response else "",
        "pattern": pattern,
        "confidence": confidence,
        "matched_text": matched,
    }

    if confidence >= HIGH_CONFIDENCE_THRESHOLD:
        _append_to_queue(CORRECTIONS_QUEUE, entry)
        logger.info(f"[reflect] 高置信度纠正入队: {pattern} ({confidence:.2f})")
        # 高置信度纠正自动创建 pending 直觉
        try:
            from instinct_manager import create_instinct
            trigger = matched[:100] if matched else user_msg[:100]
            action = user_msg[:200]
            create_instinct(trigger, action, domain="correction", source="user_correction", status="pending")
        except Exception as _ie:
            logger.debug(f"[reflect] 创建直觉失败: {_ie}")
    else:
        _append_to_queue(LEARNINGS_QUEUE, entry)
        logger.info(f"[reflect] 低置信度纠正入候选: {pattern} ({confidence:.2f})")


def get_recent_corrections(limit: int = 3) -> str:
    """
    读取最近的高置信度纠正，格式化为注入 prompt 的文本。
    读取后清空队列（一次性注入）。
    """
    if not CORRECTIONS_QUEUE.exists():
        return ""

    try:
        entries = json.loads(CORRECTIONS_QUEUE.read_text(encoding="utf-8"))
    except Exception:
        return ""

    if not entries:
        return ""

    # 取最近 limit 条
    recent = entries[-limit:]

    # 清空队列（已消费）
    try:
        CORRECTIONS_QUEUE.write_text("[]", encoding="utf-8")
    except Exception:
        pass

    lines = ["[最近纠正 — 请在本轮回复中遵循以下修正]"]
    for e in recent:
        lines.append(f"- 用户说: {e.get('user_msg', '')[:200]}")
        if e.get("prev_response"):
            lines.append(f"  你之前回答: {e['prev_response'][:150]}")
        lines.append(f"  (置信度: {e.get('confidence', 0):.0%})")

    return "\n".join(lines) + "\n"


def _append_to_queue(path: Path, entry: dict):
    """追加条目到 JSON 队列文件"""
    os.makedirs(path.parent, exist_ok=True)

    try:
        if path.exists():
            entries = json.loads(path.read_text(encoding="utf-8"))
        else:
            entries = []
    except Exception:
        entries = []

    entries.append(entry)

    # 最多保留 20 条，防止文件无限增长
    if len(entries) > 20:
        entries = entries[-20:]

    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
