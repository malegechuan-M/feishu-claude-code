"""群聊观察记忆 — Observer 模式，每 N 轮提取观察笔记，替代全量历史注入。"""
import json
import os
import time
import logging
from pathlib import Path
from collections import deque

logger = logging.getLogger(__name__)

GROUPS_DIR = Path.home() / ".feishu-claude" / "memory" / "groups"

# Observer 触发间隔：每 N 个有意义消息触发一次分析
OBSERVE_INTERVAL = 8  # 每 8 个有意义 turn 触发

# 噪音消息过滤（不计入 turn_counter）
NOISE_PATTERNS = {
    # 纯 emoji 或短回复
    "哈哈", "哈哈哈", "笑死", "好的", "收到", "嗯嗯", "嗯", "ok", "OK", "Ok",
    "+1", "👍", "🤣", "😂", "❤️", "666", "牛", "强", "对", "是的",
    "谢谢", "thanks", "thx", "了解", "明白", "知道了", "好嘞",
}

# 消息缓冲区：存储自上次 observe 以来的消息（用于分析）
_msg_buffers: dict[str, list] = {}
MAX_BUFFER = 30  # 每群最多缓存 30 条待分析


def _group_path(chat_id: str) -> Path:
    return GROUPS_DIR / f"{chat_id}.json"


def _default_group(chat_id: str) -> dict:
    """新群的默认数据"""
    return {
        "chat_id": chat_id,
        "name": "",
        "observations": [],   # 观察笔记（最多 50 条）
        "topics": [],         # 群聊话题标签（最多 30 个）
        "group_profile": {},  # 群定位 {"type": "", "description": ""}
        "turn_counter": 0,    # 有意义消息计数
        "last_analysis_turn": 0,
        "updated_at": "",
    }


def _load_group(chat_id: str) -> dict:
    path = _group_path(chat_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _default_group(chat_id)


def _save_group(chat_id: str, data: dict):
    os.makedirs(GROUPS_DIR, exist_ok=True)
    from datetime import datetime
    data["updated_at"] = datetime.now().isoformat()
    _group_path(chat_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def _is_noise(content: str) -> bool:
    """判断消息是否为噪音（不计入有意义 turn）"""
    text = content.strip()
    # 纯 emoji 检测
    if all(c in "😀😂🤣❤️👍🎉🔥💯✅❌⭐🙏👏🥰😭😤🤔💪🎊🎯📌" or not c.strip() for c in text):
        return True
    # 短噪音词
    if text in NOISE_PATTERNS:
        return True
    # 过短消息（<=2 字符且非中文句子）
    if len(text) <= 2:
        return True
    return False


def record_message(chat_id: str, sender: str, content: str) -> bool:
    """
    记录群聊消息。噪音消息不计入 turn_counter。
    当 turn_counter 达到 OBSERVE_INTERVAL 时触发异步 Observer 分析。

    Returns:
        True if observation was triggered
    """
    # 噪音过滤
    if _is_noise(content):
        return False

    # 写入消息缓冲区（供 observer 分析用）
    if chat_id not in _msg_buffers:
        _msg_buffers[chat_id] = []
    _msg_buffers[chat_id].append({"sender": sender, "content": content[:500]})
    if len(_msg_buffers[chat_id]) > MAX_BUFFER:
        _msg_buffers[chat_id] = _msg_buffers[chat_id][-MAX_BUFFER:]

    # 更新 turn_counter
    data = _load_group(chat_id)
    data["turn_counter"] = data.get("turn_counter", 0) + 1
    _save_group(chat_id, data)

    # 检查是否触发 observer
    turns_since_last = data["turn_counter"] - data.get("last_analysis_turn", 0)
    if turns_since_last >= OBSERVE_INTERVAL:
        import threading
        threading.Thread(target=_run_observation_sync, args=(chat_id,), daemon=True).start()
        return True

    return False


def _run_observation_sync(chat_id: str):
    """在后台线程中同步运行 observer（处理 async chat_haiku）"""
    import asyncio
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_trigger_observation(chat_id))
    except Exception as e:
        logger.error(f"[group_memory] Observer 线程失败: {e}")
    finally:
        loop.close()


async def _trigger_observation(chat_id: str):
    """触发 Observer：用 Haiku 分析最近的消息，提取观察笔记"""
    msgs = _msg_buffers.get(chat_id, [])
    if not msgs:
        return

    # 格式化消息供 Haiku 分析
    msg_text = "\n".join(f"{m['sender']}: {m['content']}" for m in msgs[-15:])

    try:
        from llm_client import chat_haiku

        prompt = f"""分析以下群聊消息，提取 2-3 条关键观察笔记和话题标签。

要求：
1. 观察笔记：记录有价值的信息（讨论的项目进展、做出的决定、有趣的观点等）
2. 话题标签：1-3 个关键词标签
3. 用 JSON 格式返回

格式示例：
{{"observations": ["某某提到项目deadline是下周五", "团队倾向于使用方案B"], "topics": ["项目进度", "方案评估"]}}

群聊消息：
{msg_text}

请只返回 JSON，不要其他内容："""

        result = await chat_haiku([
            {"role": "user", "content": prompt}
        ], max_tokens=300, temperature=0.1)

        if result:
            # 解析 JSON
            # 尝试提取 JSON（可能被包裹在代码块中）
            import re
            json_match = re.search(r'\{.*\}', result, re.S)
            if json_match:
                parsed = json.loads(json_match.group())

                data = _load_group(chat_id)

                # 追加 observations（最多 50 条）
                new_obs = parsed.get("observations", [])
                from datetime import datetime
                timestamp = datetime.now().strftime("%m-%d %H:%M")
                for obs in new_obs:
                    data["observations"].append(f"[{timestamp}] {obs}")
                data["observations"] = data["observations"][-50:]

                # 追加 topics（最多 30 个，去重）
                new_topics = parsed.get("topics", [])
                existing_topics = set(data.get("topics", []))
                for t in new_topics:
                    existing_topics.add(t)
                data["topics"] = list(existing_topics)[-30:]

                # 更新 last_analysis_turn
                data["last_analysis_turn"] = data["turn_counter"]
                _save_group(chat_id, data)

                logger.info(f"[group_memory] 群 {chat_id[:8]} 新增 {len(new_obs)} 条观察")

                # 清空已分析的缓冲区
                _msg_buffers[chat_id] = []

    except Exception as e:
        logger.error(f"[group_memory] Observer 分析失败: {e}")


def get_group_context(chat_id: str) -> str:
    """
    格式化群聊观察记忆为注入 prompt 的文本。
    返回最近的观察笔记和话题标签。
    """
    data = _load_group(chat_id)

    observations = data.get("observations", [])
    topics = data.get("topics", [])

    if not observations and not topics:
        return ""

    parts = ["[群聊记忆]"]

    if observations:
        # 只取最近 10 条观察笔记
        recent = observations[-10:]
        parts.append("近期观察:")
        for obs in recent:
            parts.append(f"  - {obs}")

    if topics:
        parts.append(f"话题标签: {', '.join(topics[-10:])}")

    if data.get("group_profile", {}).get("description"):
        parts.append(f"群定位: {data['group_profile']['description']}")

    return "\n".join(parts) + "\n"


def get_group_status(chat_id: str) -> str:
    """格式化群聊记忆状态，供 /group-memory 命令使用"""
    data = _load_group(chat_id)

    lines = [f"**🧠 群聊观察记忆**\n"]
    lines.append(f"**消息计数**: {data.get('turn_counter', 0)} 条有意义消息")
    lines.append(f"**分析次数**: {data.get('last_analysis_turn', 0) // OBSERVE_INTERVAL}")
    lines.append(f"**观察笔记**: {len(data.get('observations', []))} 条")
    lines.append(f"**话题标签**: {len(data.get('topics', []))} 个")

    if data.get("observations"):
        lines.append(f"\n**最近 5 条观察**:")
        for obs in data["observations"][-5:]:
            lines.append(f"  - {obs}")

    if data.get("topics"):
        lines.append(f"\n**话题**: {', '.join(data['topics'][-15:])}")

    if data.get("updated_at"):
        lines.append(f"\n**最后更新**: {data['updated_at'][:19]}")

    return "\n".join(lines)
