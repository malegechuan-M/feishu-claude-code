"""
Long Task Checkpoints — 多步骤任务的检查点持久化与崩溃恢复。

功能：
- 检测到 intent=task 时，自动为每个任务创建 checkpoint record
- 每个 Claude 响应完成后自动保存检查点（步骤描述 + 累计上下文）
- 用户可随时查看 /tasks 列表、/resume-task 恢复中断的任务
- Bot 重启后任务不丢失（SQLite 持久化）

Schema:
  tasks       — (id, chat_id, user_id, description, status, created_at, updated_at)
  checkpoints — (id, task_id, step, step_desc, accumulated_context, created_at)
"""

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / ".feishu-claude" / "tasks.db"

logger = logging.getLogger(__name__)

_STATUS_ACTIVE = "active"
_STATUS_DONE = "done"
_STATUS_ABANDONED = "abandoned"


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            step INTEGER NOT NULL,
            step_desc TEXT NOT NULL DEFAULT '',
            accumulated_context TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON checkpoints(task_id, step DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_chat ON tasks(chat_id, status)")
    conn.commit()
    return conn


def _new_id() -> str:
    return f"task_{int(time.time() * 1000)}"


def start_task(chat_id: str, user_id: str, description: str) -> str:
    """开启一个新任务，返回 task_id。"""
    task_id = _new_id()
    now = datetime.now().isoformat()
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO tasks (id, chat_id, user_id, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, chat_id, user_id, description, _STATUS_ACTIVE, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info(f"[task] 开启任务 {task_id}: {description[:50]}")
    return task_id


def add_checkpoint(
    task_id: str,
    step: int,
    step_desc: str,
    accumulated_context: str,
) -> int:
    """保存检查点，返回 checkpoint id。"""
    if not task_id:
        return -1
    now = datetime.now().isoformat()
    conn = _get_db()
    try:
        cur = conn.execute(
            "INSERT INTO checkpoints (task_id, step, step_desc, accumulated_context, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, step, step_desc, accumulated_context[:10000], now),
        )
        conn.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?",
            (now, task_id),
        )
        conn.commit()
        return cur.lastrowid if cur.lastrowid is not None else -1
    finally:
        conn.close()


def get_latest_checkpoint(task_id: str) -> Optional[sqlite3.Row]:
    """获取某任务最新的检查点。"""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT * FROM checkpoints WHERE task_id = ? ORDER BY step DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return row
    finally:
        conn.close()


def list_active_tasks(chat_id: str) -> list[sqlite3.Row]:
    """列出某 chat 所有 active 任务（按更新时间倒序）。"""
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT id, description, created_at, updated_at, "
            "(SELECT COUNT(*) FROM checkpoints WHERE task_id = tasks.id) as checkpoint_count, "
            "(SELECT step_desc FROM checkpoints WHERE task_id = tasks.id ORDER BY step DESC LIMIT 1) as latest_step_desc "
            "FROM tasks WHERE chat_id = ? AND status = ? ORDER BY updated_at DESC",
            (chat_id, _STATUS_ACTIVE),
        ).fetchall()
        return rows
    finally:
        conn.close()


def get_task(task_id: str) -> Optional[sqlite3.Row]:
    """获取任务详情。"""
    conn = _get_db()
    try:
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()


def get_checkpoints(task_id: str) -> list[sqlite3.Row]:
    """获取某任务所有检查点（按步骤顺序）。"""
    conn = _get_db()
    try:
        return conn.execute(
            "SELECT * FROM checkpoints WHERE task_id = ? ORDER BY step ASC",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()


def complete_task(task_id: str) -> bool:
    """标记任务完成。"""
    conn = _get_db()
    try:
        cur = conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (_STATUS_DONE, datetime.now().isoformat(), task_id, _STATUS_ACTIVE),
        )
        conn.commit()
        ok = cur.rowcount > 0
        if ok:
            logger.info(f"[task] 任务完成 {task_id}")
        return ok
    finally:
        conn.close()


def abandon_task(task_id: str) -> bool:
    """放弃/删除任务。"""
    conn = _get_db()
    try:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def build_checkpoint_context(task_id: str, max_chars: int = 4000) -> str:
    """
    为 resume 场景构建检查点上下文。
    返回格式化的检查点历史，供注入到 Claude prompt。
    """
    checkpoints = get_checkpoints(task_id)
    if not checkpoints:
        return ""

    lines = ["[检查点历史 - 请延续之前的工作]\n"]
    for cp in checkpoints:
        lines.append(f"--- 步骤 {cp['step']} ---")
        if cp["step_desc"]:
            lines.append(f"本步骤：{cp['step_desc']}")
        if cp["accumulated_context"]:
            ctx = cp["accumulated_context"]
            if len(ctx) > 800:
                ctx = ctx[:800] + "..."
            lines.append(f"累计上下文：{ctx}")
    lines.append("---")

    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n...（检查点历史已截断）"
    return result


# ── 主动检测步骤描述 ──────────────────────────────────────────
# 从 Claude 回复中提取步骤描述的启发式规则
_TASK_STEP_MARKERS = [
    "完成",
    "已做好",
    "已生成",
    "已写入",
    "已创建",
    "已保存",
    "已发送",
    "正在",
    "接下来",
    "步骤",
    "下一步",
    "已进入",
    "已执行",
    "已实现",
    "已输出",
]


def extract_step_desc(full_text: str) -> str:
    """从 Claude 回复中提取简短的步骤描述。"""
    if not full_text:
        return ""
    text = full_text.strip()
    if len(text) < 10:
        return text
    lines = text.split("\n")
    for line in reversed(lines):
        line = line.strip().lstrip("-*▶️📍🔔✅")
        line = line.strip()
        if len(line) > 5:
            for marker in _TASK_STEP_MARKERS:
                if marker in line:
                    return line[:100]
    return text[:80]
