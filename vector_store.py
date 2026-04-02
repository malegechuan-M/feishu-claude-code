"""
RAG 向量知识库：ChromaDB + bge-small-zh 嵌入。
支持增量索引 + LRU 查询缓存。
"""

import hashlib
import time as _time
from collections import OrderedDict
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

PERSIST_PATH = str(Path.home() / ".feishu-claude" / "vector_db")
MODEL_NAME = "BAAI/bge-small-zh-v1.5"
COLLECTION_NAME = "knowledge_base"
CACHE_MAX = 64
CACHE_TTL = 300

_embedder = None


def _get_embedder():
    """懒加载 embedding 模型（首次约 80MB 下载，之后缓存）。"""
    global _embedder
    if _embedder is None:
        _embedder = SentenceTransformer(MODEL_NAME)
    return _embedder


def embed_text(text: str) -> list[float]:
    """生成文本嵌入向量（512 维）。"""
    model = _get_embedder()
    return model.encode(text[:512], normalize_embeddings=True).tolist()


class VectorStore:
    def __init__(self):
        self.client = chromadb.PersistentClient(path=PERSIST_PATH)
        self.collection = self.client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        self._query_cache: OrderedDict = OrderedDict()

    def add(self, doc_id: str, text: str, metadata: dict) -> None:
        """添加/更新文档到向量库。"""
        embedding = embed_text(text)
        self.collection.upsert(
            ids=[doc_id],
            embeddings=[embedding],
            documents=[text[:1000]],
            metadatas=[{k: str(v) for k, v in metadata.items()}],
        )

    def query_similar(self, text: str, top_k: int = 3) -> list[dict]:
        """
        语义搜索，返回最相似的文档。
        带 LRU 缓存（5 分钟 TTL）。
        """
        if self.collection.count() == 0:
            return []

        cache_key = hashlib.md5(f"{text[:200]}:{top_k}".encode()).hexdigest()
        if cache_key in self._query_cache:
            ts, cached = self._query_cache[cache_key]
            if _time.time() - ts < CACHE_TTL:
                self._query_cache.move_to_end(cache_key)
                return cached
            else:
                del self._query_cache[cache_key]

        embedding = embed_text(text)
        results = self.collection.query(
            query_embeddings=[embedding],
            n_results=min(top_k, self.collection.count()),
        )

        similar = []
        if results and results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                meta = (
                    results["metadatas"][0][i] if results["metadatas"] else None
                ) or {}
                similar.append(
                    {
                        "id": doc_id,
                        "title": meta.get("title", ""),
                        "summary": meta.get("summary", ""),
                        "source": meta.get("source", ""),
                        "distance": results["distances"][0][i]
                        if results["distances"]
                        else 0,
                    }
                )

        self._query_cache[cache_key] = (_time.time(), similar)
        if len(self._query_cache) > CACHE_MAX:
            self._query_cache.popitem(last=False)
        return similar

    def delete_by_source(self, source_prefix: str) -> int:
        """按来源前缀删除文档。返回删除数量。"""
        all_ids = self.collection.get()["ids"]
        to_delete = [id for id in all_ids if id.startswith(source_prefix)]
        if to_delete:
            self.collection.delete(ids=to_delete)
        return len(to_delete)

    def count(self) -> int:
        """返回向量库文档总数。"""
        return self.collection.count()
