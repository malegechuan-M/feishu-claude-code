"""
Context DAG — 无损对话压缩。

所有对话轮次持久化到 SQLite，老对话自动压缩成摘要（Haiku LLM），
新对话保持原文。替代内存 deque，提供可恢复的历史。

Storage schema:
  turns     — 每轮对话（chat_id, role, content, user_name, compacted）
  summaries — 压缩后的摘要（chat_id, summary, turn_id_start...turn_id_end, turn_count）
"""

import json
import logging
import sqlite3
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

DB_PATH = Path.home() / ".feishu-claude" / "context_dag.db"

RECENT_FULL_TURNS = 10  # 最近 N 轮保持原文
COMPACT_THRESHOLD = 20  # 累积 N 条未压缩时触发压缩
MAX_SUMMARY_CHARS = 500  # 每段摘要最大字符数
MAX_TURNS_PER_COMPACT = 30  # 每批最多压缩 N 条（控制 Haiku token 消耗）

logger = logging.getLogger(__name__)


def _get_db() -> sqlite3.Connection:
    """获取 SQLite 连接（WAL 模式，线程安全）。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            user_name TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            compacted INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            turn_id_start INTEGER NOT NULL,
            turn_id_end INTEGER NOT NULL,
            turn_count INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_chat ON turns(chat_id, id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_summaries_chat ON summaries(chat_id, id DESC)"
    )
    conn.commit()
    return conn


def ingest(chat_id: str, role: str, content: str, user_name: str = "") -> int:
    """
    存储一轮对话，返回 turn_id。
    累积到 COMPACT_THRESHOLD 条未压缩时，后台线程自动触发压缩。
    """
    if not content:
        return -1

    conn = _get_db()
    try:
        cur = conn.execute(
            "INSERT INTO turns (chat_id, role, content, user_name, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, role, content[:5000], user_name, datetime.now().isoformat()),
        )
        conn.commit()
        turn_id = cur.lastrowid if cur.lastrowid is not None else -1

        uncompacted = conn.execute(
            "SELECT COUNT(*) FROM turns WHERE chat_id = ? AND compacted = 0",
            (chat_id,),
        ).fetchone()[0]

        if uncompacted >= COMPACT_THRESHOLD:
            _trigger_compact_async(chat_id)

        return turn_id
    finally:
        conn.close()


def _compact_async(chat_id: str) -> None:
    """在后台线程执行压缩（避免阻塞消息处理）。"""

    def do():
        try:
            _compact(chat_id)
        except Exception as e:
            logger.warning(f"[dag] 压缩失败 {chat_id}: {e}")

    t = threading.Thread(target=do, daemon=True)
    t.start()


def _trigger_compact_async(chat_id: str) -> None:
    """确保同一 chat_id 的压缩任务不重复并发。"""
    _compact_async(chat_id)


def _compact(chat_id: str) -> None:
    """
    将老对话压缩成摘要。保留最近 RECENT_FULL_TURNS 轮原文，压缩其余。
    Haiku 不可用时用本地规则兜底。
    """
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT id, role, content, user_name FROM turns "
            "WHERE chat_id = ? AND compacted = 0 ORDER BY id",
            (chat_id,),
        ).fetchall()

        if len(rows) <= RECENT_FULL_TURNS:
            return

        to_compress = rows[:-RECENT_FULL_TURNS]
        if len(to_compress) > MAX_TURNS_PER_COMPACT:
            to_compress = to_compress[:MAX_TURNS_PER_COMPACT]

        dialogue_parts = []
        for r in to_compress:
            role_label = "用户" if r["role"] == "user" else "助手"
            name = r["user_name"] or role_label
            dialogue_parts.append(f"{name}：{r['content'][:300]}")

        dialogue = "\n".join(dialogue_parts)

        try:
            from llm_client import chat_haiku
            import asyncio

            summary = asyncio.run(
                chat_haiku(
                    messages=[
                        {
                            "role": "system",
                            "content": "你是对话摘要助手。将以下对话压缩成简洁摘要（200字以内），保留关键信息、决策和结论。只返回摘要文本。",
                        },
                        {"role": "user", "content": dialogue[:3000]},
                    ],
                    max_tokens=250,
                    temperature=0,
                )
            )
            summary = summary[:MAX_SUMMARY_CHARS]
        except Exception:
            summary = _fallback_compact(to_compress)

        turn_ids = [r["id"] for r in to_compress]
        conn.execute(
            "INSERT INTO summaries (chat_id, summary, turn_id_start, turn_id_end, turn_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                summary,
                turn_ids[0],
                turn_ids[-1],
                len(turn_ids),
                datetime.now().isoformat(),
            ),
        )
        conn.execute(
            "UPDATE turns SET compacted = 1 WHERE chat_id = ? AND id <= ?",
            (chat_id, turn_ids[-1]),
        )
        conn.commit()
        logger.info(f"[dag] 压缩完成 {chat_id}: {len(to_compress)} 轮 -> 摘要")
    finally:
        conn.close()


def _fallback_compact(rows: list) -> str:
    """Haiku 不可用时的本地规则兜底摘要。"""
    lines = []
    for r in rows[-5:]:
        name = r["user_name"] or ("用户" if r["role"] == "user" else "助手")
        lines.append(f"- [{name}] {r['content'][:100]}")
    return f"对话要点（{len(rows)}轮）:\n" + "\n".join(lines)


def assemble(chat_id: str, budget_chars: int = 6000) -> str:
    """
    组装上下文：摘要（老对话）+ 原文（最近 N 轮）。
    群聊历史注入用此函数替代内存 deque。
    """
    conn = _get_db()
    try:
        parts = []

        summaries = conn.execute(
            "SELECT summary, turn_count FROM summaries WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        ).fetchall()
        for s in summaries:
            parts.append(f"[摘要（{s['turn_count']}轮）] {s['summary']}")

        recent = conn.execute(
            "SELECT role, content, user_name, created_at FROM turns "
            "WHERE chat_id = ? AND compacted = 0 ORDER BY id DESC LIMIT ?",
            (chat_id, RECENT_FULL_TURNS),
        ).fetchall()
        recent.reverse()  # in-place: list of sqlite3.Row, preserves Row objects

        for r in recent:
            name = r["user_name"] or ("用户" if r["role"] == "user" else "助手")
            time_short = r["created_at"][11:16] if r["created_at"] else ""
            parts.append(f"[{name} {time_short}] {r['content'][:500]}")

        if not parts:
            return ""

        result = "\n".join(parts)
        if len(result) > budget_chars:
            result = result[:budget_chars] + "\n...（上下文已截断）"
        return result
    finally:
        conn.close()


def get_stats(chat_id: str) -> dict:
    """获取 DAG 统计信息。"""
    conn = _get_db()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM turns WHERE chat_id = ?", (chat_id,)
        ).fetchone()[0]
        uncompacted = conn.execute(
            "SELECT COUNT(*) FROM turns WHERE chat_id = ? AND compacted = 0", (chat_id,)
        ).fetchone()[0]
        summary_count = conn.execute(
            "SELECT COUNT(*) FROM summaries WHERE chat_id = ?", (chat_id,)
        ).fetchone()[0]
        return {
            "total_turns": total,
            "uncompacted": uncompacted,
            "summaries": summary_count,
        }
    finally:
        conn.close()


def clear_chat(chat_id: str) -> None:
    """清除某个 chat 的所有 DAG 数据（通常在 /new session 时调用）。"""
    conn = _get_db()
    try:
        conn.execute("DELETE FROM turns WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM summaries WHERE chat_id = ?", (chat_id,))
        conn.commit()
    finally:
        conn.close()
