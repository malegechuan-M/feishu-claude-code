"""联系人记忆系统 — 每用户独立档案，自动获取真名，TTL 缓存。"""
import json
import os
import time
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

CONTACTS_DIR = Path.home() / ".feishu-claude" / "memory" / "contacts"

# TTL 缓存：60 秒内返回内存缓存，避免频繁读文件
_cache: dict[str, tuple[float, dict]] = {}
_cache_ttl = 60  # 秒
_locks: dict[str, threading.Lock] = {}
_global_lock = threading.Lock()


def _get_lock(open_id: str) -> threading.Lock:
    """获取 per-user 锁（线程安全）"""
    with _global_lock:
        if open_id not in _locks:
            _locks[open_id] = threading.Lock()
        return _locks[open_id]


def _contact_path(open_id: str) -> Path:
    return CONTACTS_DIR / f"{open_id}.json"


def _default_contact(open_id: str) -> dict:
    """新用户的默认档案"""
    from datetime import datetime
    return {
        "open_id": open_id,
        "name": "",
        "nickname": "",
        "first_seen": datetime.now().isoformat(),
        "last_seen": datetime.now().isoformat(),
        "message_count": 0,
        "traits": [],        # AI 提取的性格特征
        "preferences": {},   # 偏好设置 {"response_style": "简洁", ...}
        "topics": [],        # 常聊话题
        "notes": [],         # 备注（AI 或人工添加）
        "patterns": [],      # 行为模式
    }


def get_contact(open_id: str) -> dict:
    """获取联系人档案，优先使用 TTL 缓存"""
    # 检查缓存
    if open_id in _cache:
        cached_time, cached_data = _cache[open_id]
        if time.time() - cached_time < _cache_ttl:
            return cached_data

    # 从文件读取
    path = _contact_path(open_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = _default_contact(open_id)
    else:
        data = _default_contact(open_id)

    _cache[open_id] = (time.time(), data)
    return data


def update_contact(open_id: str, **kwargs):
    """更新联系人档案（线程安全）"""
    lock = _get_lock(open_id)
    with lock:
        data = get_contact(open_id)

        # 更新简单字段
        for key in ("name", "nickname", "last_seen"):
            if key in kwargs:
                data[key] = kwargs[key]

        # 累加 message_count
        if "message_count_incr" in kwargs:
            data["message_count"] = data.get("message_count", 0) + kwargs["message_count_incr"]

        # 追加列表字段（去重）
        for list_key in ("traits", "topics", "notes", "patterns"):
            if list_key in kwargs:
                existing = data.get(list_key, [])
                for item in kwargs[list_key]:
                    if item not in existing:
                        existing.append(item)
                data[list_key] = existing

        # 合并 preferences
        if "preferences" in kwargs:
            data.setdefault("preferences", {}).update(kwargs["preferences"])

        # 保存到文件
        os.makedirs(CONTACTS_DIR, exist_ok=True)
        _contact_path(open_id).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        # 更新缓存
        _cache[open_id] = (time.time(), data)
        logger.debug(f"[contact] 更新联系人 {open_id}: {list(kwargs.keys())}")


def get_contact_context(open_id: str) -> str:
    """格式化联系人档案为注入 prompt 的文本"""
    data = get_contact(open_id)

    # 没有任何有意义的信息则返回空
    if not data.get("name") and not data.get("traits") and not data.get("topics") and data.get("message_count", 0) < 3:
        return ""

    parts = []
    parts.append(f"[联系人档案]")

    if data.get("name"):
        parts.append(f"姓名: {data['name']}")

    if data.get("message_count"):
        parts.append(f"交互次数: {data['message_count']}")

    if data.get("traits"):
        parts.append(f"特征: {', '.join(data['traits'][-5:])}")

    if data.get("preferences"):
        prefs = "; ".join(f"{k}={v}" for k, v in list(data['preferences'].items())[:3])
        parts.append(f"偏好: {prefs}")

    if data.get("topics"):
        parts.append(f"常聊话题: {', '.join(data['topics'][-5:])}")

    if data.get("notes"):
        parts.append(f"备注: {data['notes'][-1]}")  # 最近一条

    return "\n".join(parts) + "\n"


def resolve_name_from_feishu(open_id: str, feishu_client) -> str:
    """通过飞书 API 获取用户真实姓名，失败则返回空字符串"""
    try:
        info = feishu_client.get_user_info(open_id)
        if info and info.get("name"):
            update_contact(open_id, name=info["name"])
            return info["name"]
    except Exception as e:
        logger.debug(f"[contact] 获取用户名失败 {open_id}: {e}")
    return ""


def record_interaction(open_id: str, feishu_client=None):
    """记录一次交互（消息计数+1，更新 last_seen）"""
    from datetime import datetime

    data = get_contact(open_id)
    updates = {
        "message_count_incr": 1,
        "last_seen": datetime.now().isoformat(),
    }

    # 首次交互且没有名字，尝试获取
    if not data.get("name") and feishu_client:
        resolve_name_from_feishu(open_id, feishu_client)

    update_contact(open_id, **updates)
