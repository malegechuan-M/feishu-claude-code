"""
知识库索引管理：扫描文件 → 分块 → 写入向量库。
支持增量索引（基于文件修改时间）和强制全量重建。
"""

import json
import re
from datetime import datetime
from pathlib import Path

from vector_store import VectorStore

KNOWLEDGE_SOURCES = [
    {
        "name": "obsidian",
        "paths": [
            Path.home() / "Documents" / "Obsidian" / "知识库" / "02-洞察",
            Path.home() / "Documents" / "Obsidian" / "知识库" / "03-经验",
            Path.home() / "Documents" / "Obsidian" / "知识库" / "06-素材",
            Path.home() / "Documents" / "Obsidian" / "知识库" / "06-业务运营",
        ],
        "glob": "*.md",
    },
    {
        "name": "openclaw_memory",
        "paths": [
            Path.home() / "openclaw" / "workspace" / "memory",
        ],
        "glob": "*.md",
    },
    {
        "name": "openclaw_brain",
        "paths": [
            Path.home() / "openclaw" / "workspace" / "agent_brain",
        ],
        "glob": "*.md",
    },
    {
        "name": "openclaw_sops",
        "paths": [
            Path.home() / "openclaw" / "workspace" / "sops",
        ],
        "glob": "*.md",
    },
    {
        "name": "openclaw_projects",
        "paths": [
            Path.home() / "openclaw" / "workspace" / "projects_data",
        ],
        "glob": "*.md",
    },
    {
        "name": "local_memory",
        "paths": [
            Path.home() / ".feishu-claude" / "brain",
            Path.home() / ".feishu-claude" / "learnings",
        ],
        "glob": "*.md",
    },
]

CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
INDEX_STATE_FILE = Path.home() / ".feishu-claude" / "index_state.json"


def chunk_text(
    text: str, max_chars: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
) -> list[str]:
    """
    将长文本按段落分块，保持段落完整性。
    """
    paragraphs = re.split(r"\n{2,}", text)
    chunks = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) + 2 > max_chars and current:
            chunks.append(current.strip())
            current = current[-overlap:] if overlap else ""
        current = current + "\n\n" + p if current else p
    if current.strip():
        chunks.append(current.strip())
    return chunks if chunks else [text[:max_chars]]


def _load_state() -> dict:
    if INDEX_STATE_FILE.exists():
        try:
            return json.loads(INDEX_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    INDEX_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    INDEX_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_index(force: bool = False) -> int:
    """
    构建/更新向量索引。force=True 时全量重建。
    返回处理的总块数。
    """
    store = VectorStore()
    state = _load_state()
    total_added = 0

    for source in KNOWLEDGE_SOURCES:
        source_name = source["name"]
        if force:
            deleted = store.delete_by_source(f"{source_name}:")
            if deleted > 0:
                print(
                    f"[indexer] 已删除 {source_name} 的 {deleted} 个旧文档", flush=True
                )

        for base_path in source["paths"]:
            if not base_path.exists():
                continue
            for fpath in base_path.rglob(source["glob"]):
                rel_path = f"{source_name}:{fpath.relative_to(base_path)}"
                try:
                    mtime = str(fstat(fpath).st_mtime)
                except Exception:
                    continue

                if not force and state.get(rel_path) == mtime:
                    continue

                try:
                    content = fpath.read_text(
                        encoding="utf-8", errors="replace"
                    ).strip()
                    if not content or len(content) < 50:
                        continue

                    chunks = chunk_text(content)
                    for i, chunk in enumerate(chunks):
                        doc_id = f"{source_name}:{fpath.stem}:{i}"
                        store.add(
                            doc_id,
                            chunk,
                            {
                                "title": fpath.stem,
                                "source": str(fpath),
                                "source_name": source_name,
                                "chunk_index": i,
                                "indexed_at": datetime.now().isoformat(),
                            },
                        )
                        total_added += 1

                    state[rel_path] = mtime
                except Exception as e:
                    print(f"[indexer] 索引失败 {fpath}: {e}", flush=True)

    _save_state(state)
    print(
        f"[indexer] 索引完成，新增/更新 {total_added} 块，当前总量 {store.count()}",
        flush=True,
    )
    return total_added


def fstat(path: Path):
    return path.stat()
