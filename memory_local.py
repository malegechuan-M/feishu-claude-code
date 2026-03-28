"""
本地三层记忆系统
Layer 1: 每日运行日志 (~/.feishu-claude/memory/YYYY-MM-DD.md)
Layer 2: 晋升候选    (~/.feishu-claude/memory/YYYY-MM-DD-promotion-candidates.md)
Layer 3: 长期记忆    (~/.feishu-claude/brain/MEMORY.md + SOUL.md)
Layer 4: 自学习      (~/.feishu-claude/learnings/LEARNINGS.md + ERRORS.md)
"""

import os
import re
from datetime import datetime
from pathlib import Path

BRAIN_DIR = Path.home() / ".feishu-claude" / "brain"
MEMORY_DIR = Path.home() / ".feishu-claude" / "memory"
LEARNINGS_DIR = Path.home() / ".feishu-claude" / "learnings"

MEMORY_FILE = BRAIN_DIR / "MEMORY.md"
SOUL_FILE = BRAIN_DIR / "SOUL.md"
LEARNINGS_FILE = LEARNINGS_DIR / "LEARNINGS.md"
ERRORS_FILE = LEARNINGS_DIR / "ERRORS.md"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _daily_log_path() -> Path:
    return MEMORY_DIR / f"{_today()}.md"


def _promotion_candidates_path() -> Path:
    return MEMORY_DIR / f"{_today()}-promotion-candidates.md"


# ── Layer 1: 每日运行日志 ─────────────────────────────────────────

def write_daily_log(content: str, tag: str = "事件") -> None:
    """向当日日志追加一条记录"""
    path = _daily_log_path()
    if not path.exists():
        path.write_text(f"# {_today()} 对话日志\n\n", encoding="utf-8")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n## [{_now()}] {tag}\n{content}\n")


def read_recent_logs(days: int = 3) -> str:
    """读取最近 N 天的日志摘要"""
    files = sorted(MEMORY_DIR.glob("????-??-??-daily-summary.md"), reverse=True)[:days]
    if not files:
        # 没有摘要就读原始日志
        files = sorted(MEMORY_DIR.glob("????-??-??.md"), reverse=True)[:days]
    if not files:
        return ""
    parts = []
    for f in reversed(files):
        try:
            content = f.read_text(encoding="utf-8")
            # 只取前 800 字避免过长
            if len(content) > 800:
                content = content[:800] + "…"
            parts.append(f"[{f.stem}]\n{content}")
        except Exception:
            continue
    return "\n\n".join(parts)


# ── Layer 2: 晋升候选 ─────────────────────────────────────────────

def write_promotion_candidate(rule_text: str, source: str = "", target_file: str = "MEMORY.md") -> None:
    """写入一条晋升候选规则（等待用户确认）"""
    path = _promotion_candidates_path()
    if not path.exists():
        path.write_text(f"# {_today()} 晋升候选\n\n> 以下规则等待用户确认后晋升到长期记忆\n\n", encoding="utf-8")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n### [{_now()}]\n")
        f.write(f"- **目标文件**：{target_file}\n")
        if source:
            f.write(f"- **来源**：{source}\n")
        f.write(f"- **建议内容**：{rule_text}\n")


def read_pending_candidates() -> str:
    """读取所有待确认的晋升候选"""
    files = sorted(MEMORY_DIR.glob("????-??-??-promotion-candidates.md"), reverse=True)[:7]
    parts = []
    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
            parts.append(content)
        except Exception:
            continue
    return "\n\n---\n".join(parts) if parts else ""


# ── Layer 3: 长期记忆读写 ─────────────────────────────────────────

def read_brain_context() -> str:
    """读取长期记忆（SOUL.md + MEMORY.md），注入对话上下文"""
    parts = []
    for path, label in [(SOUL_FILE, "行为原则"), (MEMORY_FILE, "长期记忆")]:
        try:
            content = path.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"[{label}]\n{content}")
        except Exception:
            continue
    if not parts:
        return ""
    return (
        "\n\n---\n"
        "[本地记忆系统 - 以下是长期积累的上下文]\n\n"
        + "\n\n".join(parts)
        + "\n---\n"
    )


def promote_to_memory(content: str, section: str = "经验教训") -> bool:
    """用户确认后，将规则写入 MEMORY.md（追加到指定章节）"""
    try:
        text = MEMORY_FILE.read_text(encoding="utf-8")
        if f"## {section}" in text:
            text = text.replace(
                f"## {section}",
                f"## {section}\n- [{_today()}] {content}"
            )
        else:
            text += f"\n## {section}\n- [{_today()}] {content}\n"
        MEMORY_FILE.write_text(text, encoding="utf-8")
        return True
    except Exception as e:
        print(f"[memory_local] promote_to_memory 失败: {e}", flush=True)
        return False


# ── Layer 4: 自学习 ───────────────────────────────────────────────

def _next_id(path: Path, prefix: str) -> str:
    """生成下一个条目 ID，如 ERR-20260328-003"""
    date = datetime.now().strftime("%Y%m%d")
    try:
        content = path.read_text(encoding="utf-8")
        pattern = rf"{prefix}-{date}-(\d+)"
        ids = [int(m) for m in re.findall(pattern, content)]
        n = max(ids) + 1 if ids else 1
    except Exception:
        n = 1
    return f"{prefix}-{date}-{n:03d}"


def write_error(user_msg: str, wrong_behavior: str, correction: str) -> None:
    """记录用户纠正的错误（自动写入，无需确认）"""
    entry_id = _next_id(ERRORS_FILE, "ERR")
    entry = (
        f"\n## {entry_id} [{_now()}]\n"
        f"**触发**：{user_msg[:100]}\n"
        f"**错误行为**：{wrong_behavior}\n"
        f"**正确做法**：{correction}\n"
    )
    with open(ERRORS_FILE, "a", encoding="utf-8") as f:
        f.write(entry)
    print(f"[memory_local] 错误已记录: {entry_id}", flush=True)


def write_learning(summary: str, root_cause: str = "", fix: str = "") -> None:
    """记录一条经验教训"""
    entry_id = _next_id(LEARNINGS_FILE, "LRN")
    entry = (
        f"\n## {entry_id} [{_now()}] | status: pending\n"
        f"**摘要**：{summary}\n"
    )
    if root_cause:
        entry += f"**根因**：{root_cause}\n"
    if fix:
        entry += f"**改进**：{fix}\n"
    with open(LEARNINGS_FILE, "a", encoding="utf-8") as f:
        f.write(entry)
    print(f"[memory_local] 经验已记录: {entry_id}", flush=True)


# ── 纠正检测 ─────────────────────────────────────────────────────

CORRECTION_SIGNALS = [
    "不是", "不对", "错了", "你搞错了", "不要这样", "别这样",
    "说过了", "我说的是", "你没理解", "重新来", "不是这个意思",
]


def detect_correction(user_text: str) -> bool:
    """检测用户消息是否包含纠正信号"""
    return any(signal in user_text for signal in CORRECTION_SIGNALS)
