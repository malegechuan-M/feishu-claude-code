"""
共享记忆桥接层：连接 mem0 云记忆 + OpenClaw 本地知识库。
让 Claude 飞书 Bot 能读取 OpenClaw 的全部积累。
"""

import os
import sqlite3
import traceback
from pathlib import Path
from typing import Optional

# ── mem0 配置 ──────────────────────────────────────────────────
MEM0_API_KEY = os.getenv("MEM0_API_KEY", "")
MEM0_USER_ID = os.getenv("MEM0_USER_ID", "your_user_id")

# ── OpenClaw 本地路径 ─────────────────────────────────────────
OPENCLAW_MEMORY_DIR = Path.home() / ".openclaw" / "memory"
OPENCLAW_WORKSPACE_MEMORY = Path.home() / "openclaw" / "workspace" / "memory"

# ── 初始化 mem0 客户端 ────────────────────────────────────────
_mem0_client = None


def _get_mem0():
    global _mem0_client
    if _mem0_client is None:
        try:
            from mem0 import MemoryClient

            _mem0_client = MemoryClient(api_key=MEM0_API_KEY)
            print("[memory_bridge] mem0 客户端初始化成功", flush=True)
        except Exception as e:
            print(f"[memory_bridge] mem0 初始化失败: {e}", flush=True)
    return _mem0_client


# ── mem0 记忆召回 ─────────────────────────────────────────────

# OpenClaw mem0 使用命名空间格式: wuxianbaoshi:agent:{agentId}
# Claude Bot 使用自己的命名空间，同时搜索时也查 OpenClaw 的
MEM0_CLAUDE_USER_ID = f"{MEM0_USER_ID}:agent:claude-feishu"
MEM0_OPENCLAW_AGENTS = [
    "agent-a-coo",         # 总控官（主要记忆来源）
    "agent-b-research",    # 市场研究员
    "agent-c-trading",     # 交易/电商
    "agent-d-intern",      # 实习生/执行
    "agent-e-workflow",    # 工作流
    "agent-f-legal",       # 法务
    "agent-asii",          # ASII Agent
    "xiaohongshu-expert",  # 小红书专家
]


def recall_memories(query: str, limit: int = 5) -> str:
    """从 mem0 云端搜索相关记忆（Claude 自身 + OpenClaw 各 Agent 命名空间）。"""
    client = _get_mem0()
    if not client:
        return ""
    memories = []
    # 搜索 Claude 自身命名空间
    user_ids = [MEM0_CLAUDE_USER_ID] + [
        f"{MEM0_USER_ID}:agent:{a}" for a in MEM0_OPENCLAW_AGENTS
    ]
    for uid in user_ids:
        try:
            results = client.search(query, filters={"user_id": uid}, limit=limit)
            items = results.get("results", []) if isinstance(results, dict) else results
            for item in items:
                text = item.get("memory", "") if isinstance(item, dict) else str(item)
                if text and text not in [
                    m.split("] ", 1)[-1] if "] " in m else m for m in memories
                ]:
                    source = uid.split(":")[-1] if ":" in uid else uid
                    memories.append(f"  - [{source}] {text}")
        except Exception as e:
            print(f"[memory_bridge] mem0 recall ({uid}) 失败: {e}", flush=True)
    return "\n".join(memories[:limit]) if memories else ""


def capture_memory(user_message: str, assistant_response: str) -> None:
    """将对话关键信息保存到 mem0 Claude 命名空间（失败静默）。"""
    client = _get_mem0()
    if not client:
        return
    try:
        messages = [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_response[:2000]},
        ]
        client.add(messages, user_id=MEM0_CLAUDE_USER_ID)
    except Exception as e:
        print(f"[memory_bridge] mem0 capture 失败: {e}", flush=True)


# ── OpenClaw FTS5 本地搜索 ────────────────────────────────────


def search_openclaw_fts(
    query: str,
    agent_id: str = "agent-a-coo",
    limit: int = 5,
) -> str:
    """搜索 OpenClaw Agent 的 SQLite FTS5 索引，返回相关文本片段。"""
    db_path = OPENCLAW_MEMORY_DIR / f"{agent_id}.sqlite"
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(str(db_path))
        # FTS5 MATCH 查询（表名 chunks_fts）
        rows = conn.execute(
            """
            SELECT c.text, c.path, c.start_line, c.end_line
            FROM chunks_fts AS f
            JOIN chunks AS c ON c.rowid = f.rowid
            WHERE chunks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        parts = []
        for text, path, start, end in rows:
            source = os.path.basename(path) if path else "unknown"
            parts.append(f"  [{source}:{start}-{end}] {text[:300]}")
        return "\n".join(parts)
    except Exception as e:
        print(f"[memory_bridge] FTS5 搜索失败 ({agent_id}): {e}", flush=True)
        return ""


def search_openclaw_workspace_memory(query: str, limit: int = 3) -> str:
    """搜索 OpenClaw workspace/memory/ 下最近的日记忆文件。"""
    if not OPENCLAW_WORKSPACE_MEMORY.exists():
        return ""
    try:
        # 按修改时间倒序，取最近的文件
        md_files = sorted(
            OPENCLAW_WORKSPACE_MEMORY.glob("*.md"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )[:10]  # 只扫最近 10 个文件

        keywords = query.lower().split()
        matches = []
        for f in md_files:
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                # 简单关键词匹配
                if any(kw in content.lower() for kw in keywords):
                    # 提取包含关键词的段落
                    for line in content.split("\n"):
                        if (
                            any(kw in line.lower() for kw in keywords)
                            and len(line.strip()) > 10
                        ):
                            matches.append(f"  [{f.name}] {line.strip()[:200]}")
                            if len(matches) >= limit:
                                break
                if len(matches) >= limit:
                    break
            except Exception:
                continue
        return "\n".join(matches) if matches else ""
    except Exception as e:
        print(f"[memory_bridge] workspace memory 搜索失败: {e}", flush=True)
        return ""


# ── 统一搜索入口 ──────────────────────────────────────────────


def search_claude_local_memory(query: str, limit: int = 3) -> str:
    """搜索 Claude 本地记忆：近期日志摘要 + ERRORS.md 关键词匹配。"""
    from pathlib import Path

    memory_dir = Path.home() / ".feishu-claude" / "memory"
    learnings_dir = Path.home() / ".feishu-claude" / "learnings"

    keywords = query.lower().split()
    matches = []

    # 搜索近期日志摘要（daily-summary）
    try:
        summaries = sorted(memory_dir.glob("*-daily-summary.md"), reverse=True)[:5]
        for f in summaries:
            content = f.read_text(encoding="utf-8", errors="replace")
            for line in content.split("\n"):
                if (
                    any(kw in line.lower() for kw in keywords)
                    and len(line.strip()) > 10
                ):
                    matches.append(f"  [{f.stem}] {line.strip()[:200]}")
                    if len(matches) >= limit:
                        break
            if len(matches) >= limit:
                break
    except Exception:
        pass

    # 搜索 ERRORS.md
    try:
        errors_content = (learnings_dir / "ERRORS.md").read_text(
            encoding="utf-8", errors="replace"
        )
        for line in errors_content.split("\n"):
            if any(kw in line.lower() for kw in keywords) and len(line.strip()) > 10:
                matches.append(f"  [ERRORS] {line.strip()[:200]}")
                if len(matches) >= limit:
                    break
    except Exception:
        pass

    return "\n".join(matches[:limit]) if matches else ""


def recall_all(query: str) -> str:
    """
    统一搜索：向量知识库 + Claude 本地记忆 + mem0 云记忆 + OpenClaw FTS5。
    返回组合后的上下文文本，可直接注入到 Claude 提示词中。
    """
    sections = []

    # 0. 向量知识库（优先级最高，语义搜索）
    try:
        from vector_store import VectorStore

        vs = VectorStore()
        if vs.count() > 0:
            results = vs.query_similar(query, top_k=3)
            if results:
                parts = []
                for r in results:
                    if r["distance"] < 0.6:
                        parts.append(f"  [{r['title']}] {r['summary'][:200]}")
                if parts:
                    sections.append("[向量知识库]\n" + "\n".join(parts))
    except Exception as e:
        print(f"[memory_bridge] 向量搜索失败: {e}", flush=True)

    # 1. Claude 本地记忆（日志摘要 + 错误教训）
    local_result = search_claude_local_memory(query)
    if local_result:
        sections.append(f"[Claude 本地记忆]\n{local_result}")

    # 2. mem0 云端记忆
    mem0_result = recall_memories(query)
    if mem0_result:
        sections.append(f"[共享记忆 - mem0]\n{mem0_result}")

    # 3. OpenClaw 知识库（搜索所有 Agent 的 FTS5 索引）
    fts_agents = ["agent-a-coo", "agent-b-research", "agent-c-trading", "agent-d-intern", "main"]
    fts_parts = []
    for agent_id in fts_agents:
        result = search_openclaw_fts(query, agent_id=agent_id, limit=3)
        if result:
            fts_parts.append(result)
    if fts_parts:
        sections.append(f"[OpenClaw 知识库]\n" + "\n".join(fts_parts))

    # 4. OpenClaw workspace 近期记忆
    ws_result = search_openclaw_workspace_memory(query)
    if ws_result:
        sections.append(f"[OpenClaw 近期执行记录]\n{ws_result}")

    if not sections:
        return ""

    return (
        "\n\n---\n"
        "以下是从共享记忆系统中召回的相关上下文（仅供参考）：\n\n"
        + "\n\n".join(sections)
        + "\n---\n"
    )
