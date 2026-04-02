"""
飞书 × Claude Code Bot
通过飞书 WebSocket 长连接接收私聊/群聊消息，调用本机 claude CLI 回复，支持流式卡片输出。

启动：python main.py
"""

import asyncio
import json
import ssl
import sys
import os
import threading
import time
import traceback
import urllib.request

# 确保项目目录在 sys.path 最前面
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lark_oapi as lark
from lark_oapi.api.im.v1.model import P2ImMessageReceiveV1

import bot_config as config
from feishu_client import FeishuClient
from session_store import SessionStore
from commands import parse_command, handle_command
from claude_runner import run_claude
from run_control import ActiveRun, ActiveRunRegistry, stop_run
from memory_bridge import recall_all, capture_memory
from llm_client import chat_haiku
from prompt_guard import sanitize
from quota_tracker import tracker as quota_tracker
from group_memory import record_message as gm_record, get_group_context

_active_task_by_chat: dict[str, str] = {}
_current_task_info: dict[str, tuple] = {}  # chat_id -> (task_id, step)
from memory_local import (
    read_brain_context,
    read_recent_logs,
    write_daily_log,
    write_error,
    write_learning,
    detect_correction,
)
from contact_memory import get_contact_context, record_interaction
from reflect_detector import process_correction, get_recent_corrections
from router_context import select_layer, load_context
from instinct_manager import get_instinct_context
from context_builder import build_context
from model_router import select_model, select_effort
from internal_debate import should_debate, run_debate, enhance_with_critique, format_debate_log
from collections import deque

# ── 三层消息去重 ──────────────────────────────────────────────
from collections import OrderedDict
import hashlib

# Layer 1: message_id 去重（LRU 200 条）
_seen_msg_ids: OrderedDict[str, float] = OrderedDict()
_SEEN_MSG_MAX = 200

# Layer 2: 内容级去重（sender + content MD5, 120 秒 TTL）
_seen_content: dict[str, float] = {}
_CONTENT_DEDUP_TTL = 120  # 秒

# Layer 3: 去重持久化文件（防止重启后重放）
_SEEN_IDS_FILE = os.path.expanduser("~/.feishu-claude/seen_messages.json")

def _load_seen_ids():
    """启动时从文件恢复已见 message_id"""
    global _seen_msg_ids
    try:
        if os.path.exists(_SEEN_IDS_FILE):
            data = json.loads(open(_SEEN_IDS_FILE, encoding="utf-8").read())
            for mid in data[-_SEEN_MSG_MAX:]:
                _seen_msg_ids[mid] = time.time()
    except Exception:
        pass

def _save_seen_ids():
    """持久化已见 message_id"""
    try:
        os.makedirs(os.path.dirname(_SEEN_IDS_FILE), exist_ok=True)
        with open(_SEEN_IDS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(_seen_msg_ids.keys())[-_SEEN_MSG_MAX:], f)
    except Exception:
        pass

def _is_duplicate(message_id: str, sender_id: str, content: str) -> bool:
    """
    三层去重检查：
    Layer 1: message_id 精确匹配
    Layer 2: sender + content MD5（120 秒 TTL）
    Layer 3: 持久化（防重启后重放）
    """
    now = time.time()

    # Layer 1: message_id
    if message_id in _seen_msg_ids:
        return True
    _seen_msg_ids[message_id] = now
    # LRU 淘汰
    while len(_seen_msg_ids) > _SEEN_MSG_MAX:
        _seen_msg_ids.popitem(last=False)
    # 每 20 条持久化一次
    if len(_seen_msg_ids) % 20 == 0:
        _save_seen_ids()

    # Layer 2: 内容级去重
    content_hash = hashlib.md5(f"{sender_id}:{content[:200]}".encode()).hexdigest()
    if content_hash in _seen_content:
        if now - _seen_content[content_hash] < _CONTENT_DEDUP_TTL:
            return True
    _seen_content[content_hash] = now
    # 清理过期条目
    expired = [k for k, t in _seen_content.items() if now - t > _CONTENT_DEDUP_TTL * 2]
    for k in expired:
        del _seen_content[k]

    return False

# ── 群聊实时消息缓存（WebSocket 推送，有真实卡片内容）────────────
# API 拉历史时卡片内容被飞书降级为占位符，实时推送有原始内容
_group_histories: dict[str, deque] = {}
GROUP_HISTORY_MAX = 50  # 每个群保留最近 N 条
_CACHE_FILE = os.path.expanduser("~/.feishu-claude/group_history_cache.json")

# Plan A：记录每个群"上次 Bot 回复时"deque 的长度，下次只注入增量
_last_reply_deque_len: dict[str, int] = {}


def _save_group_cache():
    """把群聊缓存持久化到文件"""
    try:
        data = {chat_id: list(buf) for chat_id, buf in _group_histories.items()}
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"[cache] 保存群聊缓存失败: {e}", flush=True)


def _load_group_cache():
    """启动时从文件恢复群聊缓存"""
    try:
        if not os.path.exists(_CACHE_FILE):
            return
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for chat_id, entries in data.items():
            _group_histories[chat_id] = deque(entries, maxlen=GROUP_HISTORY_MAX)
        total = sum(len(v) for v in _group_histories.values())
        print(
            f"[cache] 恢复群聊缓存：{len(_group_histories)} 个群，共 {total} 条消息",
            flush=True,
        )
    except Exception as e:
        print(f"[cache] 读取群聊缓存失败: {e}", flush=True)


def _parse_event_content(msg) -> str:
    """从 WebSocket 事件解析消息文本（支持 text / post / interactive 卡片）"""
    if msg.message_type == "text":
        try:
            return json.loads(msg.content).get("text", "").strip()
        except Exception:
            return ""
    elif msg.message_type == "post":
        try:
            post = json.loads(msg.content)
            parts = []
            for block in post.get("zh_cn", {}).get("content", []):
                for el in block:
                    t = el.get("text") or el.get("content", "")
                    if t:
                        parts.append(t)
            return "\n".join(parts).strip()
        except Exception:
            return ""
    elif msg.message_type == "interactive":
        try:
            card = json.loads(msg.content)
            parts = []
            if card.get("schema") == "2.0":
                # Card JSON 2.0（Claude bot 格式）
                for el in card.get("body", {}).get("elements", []):
                    if el.get("tag") == "markdown":
                        parts.append(el.get("content", ""))
            else:
                # 旧版 Card Kit（OpenClaw 格式）
                header = card.get("header", {})
                title = header.get("title", {})
                if isinstance(title, dict) and title.get("content"):
                    parts.append(f"**{title['content']}**")
                for el in card.get("elements", []):
                    sub_els = el if isinstance(el, list) else [el]
                    for sub in sub_els:
                        tag = sub.get("tag", "")
                        if tag == "markdown":
                            parts.append(sub.get("content", ""))
                        elif tag in ("div", "section"):
                            text_obj = sub.get("text", {})
                            if isinstance(text_obj, dict):
                                parts.append(text_obj.get("content", ""))
                        elif tag == "text":
                            t = sub.get("text", "")
                            if t and t != "请升级至最新版本客户端，以查看内容":
                                parts.append(t)
            text = "\n".join(p for p in parts if p).strip()
            return text if text not in ("⏳ 思考中...", "") else ""
        except Exception:
            return ""
    return ""


def _record_group_msg(chat_id: str, sender_name: str, content: str):
    """记录一条群消息到内存缓存，并异步持久化"""
    if not content:
        return
    if chat_id not in _group_histories:
        _group_histories[chat_id] = deque(maxlen=GROUP_HISTORY_MAX)
    _group_histories[chat_id].append((sender_name, content[:3000]))
    # 每10条持久化一次（避免每条都写文件）
    if len(_group_histories[chat_id]) % 10 == 0:
        _save_group_cache()
    # Observer 群聊记忆
    try:
        gm_record(chat_id, sender_name, content)
    except Exception as _gm:
        logger.debug(f"[group_memory] record 失败: {_gm}")


def _get_group_history_text(chat_id: str) -> str:
    """获取格式化的群聊历史文本（增量模式：只返回上次 Bot 回复后的新消息）"""
    buf = _group_histories.get(chat_id)
    if not buf:
        return ""

    buf_list = list(buf)
    last_len = _last_reply_deque_len.get(chat_id, 0)

    if last_len == 0:
        # 首次 / Bot 重启后：取最近 15 条作为初始上下文
        msgs_to_show = buf_list[-15:]
        label = f"群聊最近 {len(msgs_to_show)} 条消息"
    else:
        delta_msgs = buf_list[last_len:]
        if not delta_msgs:
            # 上次回复后暂无新消息（用户紧接着 @ 了两次）
            msgs_to_show = buf_list[-3:]
            label = f"最近 {len(msgs_to_show)} 条消息（无新消息）"
        else:
            # 正常情况：2 条锚点消息 + 增量
            anchor = buf_list[max(0, last_len - 2) : last_len]
            msgs_to_show = anchor + delta_msgs
            label = f"上次回复后的新消息（{len(delta_msgs)} 条）"

    if not msgs_to_show:
        return ""

    lines = [f"{name}: {content}" for name, content in msgs_to_show]
    return f"\n\n---\n[{label}]\n" + "\n".join(lines) + "\n---\n"


def _mark_group_replied(chat_id: str):
    """Bot 回复完成后，记录当前队列长度作为下次 delta 的基准"""
    buf = _group_histories.get(chat_id)
    if buf is not None:
        _last_reply_deque_len[chat_id] = len(buf)


def _get_bot_open_id() -> str:
    """调用飞书 API 获取 Bot 自身的 open_id"""
    ctx = ssl.create_default_context()
    # 先获取 tenant_access_token
    token_body = json.dumps(
        {
            "app_id": config.FEISHU_APP_ID,
            "app_secret": config.FEISHU_APP_SECRET,
        }
    ).encode()
    token_req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=token_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(token_req, context=ctx, timeout=10) as r:
        token = json.loads(r.read())["tenant_access_token"]

    # 获取 bot info
    bot_req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/bot/v3/info",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(bot_req, context=ctx, timeout=10) as r:
        data = json.loads(r.read())
        return data["bot"]["open_id"]


# ── 看门狗：定时重启防止 WebSocket 假死 ──────────────────────

MAX_UPTIME = 24 * 3600  # 最长运行 24 小时后主动重启
_start_time = time.time()
_last_event = time.time()


def _watchdog():
    """后台线程，定期检查进程健康。异常时退出让 launchctl 拉起。"""
    while True:
        time.sleep(300)  # 每 5 分钟检查
        uptime = time.time() - _start_time
        idle = time.time() - _last_event

        if uptime > MAX_UPTIME:
            print(f"[watchdog] 运行 {uptime / 3600:.1f}h，定时重启刷新连接", flush=True)
            _save_group_cache()
            _save_seen_ids()
            os._exit(0)

        print(
            f"[watchdog] uptime={uptime / 3600:.1f}h idle={idle / 60:.0f}min",
            flush=True,
        )


# ── 全局单例 ──────────────────────────────────────────────────

lark_client = (
    lark.Client.builder()
    .app_id(config.FEISHU_APP_ID)
    .app_secret(config.FEISHU_APP_SECRET)
    .log_level(lark.LogLevel.INFO)
    .build()
)

feishu = FeishuClient(
    lark_client, app_id=config.FEISHU_APP_ID, app_secret=config.FEISHU_APP_SECRET
)
store = SessionStore()
_active_runs = ActiveRunRegistry()

# 启动时获取 bot 自己的 open_id，用于群聊中判断是否 @了自己
try:
    BOT_OPEN_ID = _get_bot_open_id()
    print(f"[init] Bot open_id: {BOT_OPEN_ID}", flush=True)
except Exception as e:
    print(f"[warn] 获取 bot open_id 失败: {e}，群聊过滤将退化为响应所有 @", flush=True)
    BOT_OPEN_ID = None

# per-chat 消息队列锁，保证同一群组的消息串行处理，允许不同群组并发处理
_chat_locks: dict[str, asyncio.Lock] = {}
_MAX_CHAT_LOCKS = 200  # 防止无界增长


# ── /stop 命令处理 ───────────────────────────────────────────


async def _announce_stopped_run(active_run: ActiveRun):
    try:
        await feishu.update_card(active_run.card_msg_id, "⏹ 已停止当前任务")
    except Exception as exc:
        print(f"[warn] update stopped card failed: {exc}", flush=True)


async def _handle_stop_command(sender_open_id: str) -> str:
    active_run = _active_runs.get_run(sender_open_id)
    if active_run is None:
        return "当前没有正在运行的任务"
    if active_run.stop_requested:
        return "正在停止当前任务，请稍候"
    stopped = await stop_run(
        _active_runs,
        sender_open_id,
        on_stopped=_announce_stopped_run,
    )
    if not stopped:
        return "当前没有正在运行的任务"
    return "已发送停止请求"


# ── 核心消息处理（async）─────────────────────────────────────


def extract_chat_info(event: P2ImMessageReceiveV1) -> tuple[str, str, bool]:
    """
    Extract user_id, chat_id, and is_group from message event.

    Returns:
        (user_id, chat_id, is_group)
        - For private chat: chat_id = user_id
        - For group chat: chat_id = group's chat_id
    """
    sender = event.event.sender
    user_id = sender.sender_id.open_id

    message = event.event.message
    chat_type = message.chat_type
    chat_id_raw = message.chat_id

    is_group = chat_type == "group"

    if is_group:
        chat_id = chat_id_raw
    else:
        chat_id = user_id

    return user_id, chat_id, is_group


async def handle_message_async(event: P2ImMessageReceiveV1):
    """异步处理一条飞书消息"""
    msg = event.event.message
    print(f"[收到消息] type={msg.message_type} chat={msg.chat_type}", flush=True)

    # Extract chat info (supports both private and group chats)
    user_id, chat_id, is_group = extract_chat_info(event)
    print(
        f"[Chat Info] user={user_id[:8]}... chat={chat_id[:8]}... is_group={is_group}",
        flush=True,
    )

    # 三层消息去重
    message_id = msg.message_id or ""
    raw_content = _parse_event_content(msg)
    if _is_duplicate(message_id, user_id, raw_content):
        print(f"[dedup] 重复消息跳过: {message_id[:16]}", flush=True)
        return

    # /stop 命令在锁外处理（不需要排队）
    if msg.message_type == "text":
        try:
            _text = json.loads(msg.content).get("text", "").strip()
        except Exception:
            _text = ""
        if _text.lower() in ("/stop", "@_user_1 /stop") or _text.strip().endswith(
            "/stop"
        ):
            reply = await _handle_stop_command(user_id)
            if is_group:
                await feishu.reply_card(msg.message_id, content=reply, loading=False)
            else:
                await feishu.send_card_to_user(user_id, content=reply, loading=False)
            return

    # 群聊消息处理：非 @我 的消息记录到实时缓存
    if is_group:
        mentions = getattr(msg, "mentions", None) or []
        mentioned_me = False
        if BOT_OPEN_ID and mentions:
            mentioned_me = any(
                getattr(getattr(m, "id", None), "open_id", None) == BOT_OPEN_ID
                for m in mentions
            )
        elif not BOT_OPEN_ID and mentions:
            mentioned_me = True  # 降级：有 @mention 就响应

        if not mentioned_me:
            # 不是 @我：解析内容并记录到群聊历史缓存
            content = _parse_event_content(msg)
            sender_name = config.GROUP_KNOWN_NAMES.get(
                user_id, user_id[:8] if user_id else "未知"
            )
            _record_group_msg(chat_id, sender_name, content)
            print(
                f"[group cache] 记录消息 chat={chat_id[:8]} sender={sender_name} len={len(content)}",
                flush=True,
            )
            return  # 不是@我，不调用 Claude

    # 获取该群组的队列锁，保证同一群组消息串行处理，不同群组可并发
    if chat_id not in _chat_locks:
        # 简单的 LRU 清理：超出上限时清掉所有锁（已释放的锁丢弃无害）
        if len(_chat_locks) >= _MAX_CHAT_LOCKS:
            _chat_locks.clear()
        _chat_locks[chat_id] = asyncio.Lock()
    lock = _chat_locks[chat_id]

    async with lock:
        try:
            await _process_message(user_id, chat_id, is_group, msg)
        except Exception as e:
            print(f"[error] 消息处理异常: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()


async def _process_message(user_id: str, chat_id: str, is_group: bool, msg):
    """实际处理消息的逻辑，在 per-chat lock 保护下执行"""
    print(
        f"[处理消息] user={user_id[:8]}... chat={chat_id[:8]}... is_group={is_group}",
        flush=True,
    )
    text = ""
    img_path = None

    if msg.message_type == "text":
        try:
            text = json.loads(msg.content).get("text", "").strip()
        except Exception:
            return
        if not text:
            return

        # 群聊：只去掉 @本bot 的占位符，保留其他 @mention 内容
        if is_group:
            mentions = getattr(msg, "mentions", None) or []
            for mention in mentions:
                mention_open_id = getattr(getattr(mention, "id", None), "open_id", None)
                key = getattr(mention, "key", "")
                if key and (not BOT_OPEN_ID or mention_open_id == BOT_OPEN_ID):
                    text = text.replace(key, "").strip()
            if not text:
                return

        print(f"[文本] {text[:50]}", flush=True)

    elif msg.message_type == "image":
        try:
            image_key = json.loads(msg.content).get("image_key", "")
            if not image_key:
                return
            img_path = await feishu.download_image(msg.message_id, image_key)
            text = f"[用户发送了一张图片，路径：{img_path}，请读取并分析这张图片，直接回复用中文]"
        except Exception as e:
            print(f"[error] 下载图片失败: {e}")
            if is_group:
                try:
                    await feishu.reply_card(
                        msg.message_id, content=f"❌ 下载图片失败：{e}", loading=False
                    )
                except Exception:
                    pass
            else:
                await feishu.send_text_to_user(user_id, f"❌ 下载图片失败：{e}")
            return

    elif msg.message_type == "post":
        # 飞书富文本消息：提取所有文本内容
        try:
            raw_content = msg.content or ""
            print(f"[post-raw] {raw_content[:500]}", flush=True)
            post_data = json.loads(raw_content)
            parts = []
            # 尝试所有可能的语言 key + 顶层 content
            content_found = None
            for lang_key in ("zh_cn", "en_us", "ja_jp"):
                lang_content = post_data.get(lang_key, {})
                if isinstance(lang_content, dict):
                    content_found = lang_content.get("content", [])
                elif isinstance(lang_content, list):
                    content_found = lang_content
                if content_found:
                    break
            # 兜底：直接看顶层 content
            if not content_found:
                content_found = post_data.get("content", [])
            # 兜底2：如果 post_data 本身就是 title+content
            if not content_found and "title" in post_data:
                parts.append(post_data.get("title", ""))

            for paragraph in (content_found or []):
                if isinstance(paragraph, list):
                    for element in paragraph:
                        if not isinstance(element, dict):
                            continue
                        tag = element.get("tag", "")
                        if tag == "text":
                            parts.append(element.get("text", ""))
                        elif tag == "a":
                            parts.append(element.get("text", element.get("href", "")))
                        elif tag == "at":
                            at_id = element.get("user_id", "")
                            if at_id != BOT_OPEN_ID:
                                parts.append(f"@{element.get('user_name', at_id)}")
                        elif tag == "code_block":
                            lang = element.get("language", "")
                            code = element.get("text", "")
                            parts.append(f"```{lang}\n{code}\n```")
                        elif tag == "code":
                            parts.append(f"`{element.get('text', '')}`")
                        elif tag == "img":
                            parts.append("[图片]")
                        elif tag == "media":
                            parts.append("[媒体]")
                elif isinstance(paragraph, str):
                    parts.append(paragraph)

            text = " ".join(p for p in parts if p).strip()
            print(f"[post-parsed] len={len(text)} text={text[:100]}", flush=True)
            if not text:
                print(f"[post] 解析后为空，跳过", flush=True)
                return
        except Exception as e:
            print(f"[error] 解析 post 消息失败: {e}", flush=True)
            # 最终兜底：尝试把整个 content 当纯文本
            try:
                text = json.loads(msg.content or "{}").get("text", "")
                if not text:
                    return
                print(f"[post-fallback] {text[:80]}", flush=True)
            except Exception:
                return

    else:
        print(f"[skip] 不支持的消息类型: {msg.message_type}", flush=True)
        return  # 不支持的消息类型

    # ── Prompt Injection 防御 ─────────────────────────────────
    try:
        text, was_filtered = sanitize(text, is_group=is_group)
        if was_filtered:
            import logging
            logging.getLogger(__name__).warning(
                f"[prompt_guard] Injection detected from {user_id}"
            )
    except Exception as _pg_err:
        print(f"[prompt_guard] 检测失败（忽略）: {_pg_err}", flush=True)

    # ── 斜杠命令 ──────────────────────────────────────────────
    parsed = parse_command(text)
    if parsed:
        cmd, args = parsed
        reply = await handle_command(cmd, args, user_id, chat_id, store)
        if reply is not None:
            if cmd == "resume" and not args:
                # /resume 命令特殊处理：发送文本消息
                if is_group:
                    await feishu.reply_card(
                        msg.message_id, content=reply, loading=False
                    )
                else:
                    await feishu.send_text_to_user(user_id, reply)
            else:
                if is_group:
                    await feishu.reply_card(
                        msg.message_id, content=reply, loading=False
                    )
                else:
                    await feishu.send_card_to_user(
                        user_id, content=reply, loading=False
                    )
            return
        # reply is None → 不是 bot 命令，当作普通消息（含 /xxx）转发给 Claude

    # ── 意图检测（轻量分类，决定后续处理路径）────────────────
    intent = "chat"  # 默认
    try:
        from intent_router import classify

        intent = await classify(text)
        print(f"[intent] {intent}: {text[:50]}", flush=True)
    except Exception as e:
        print(f"[intent] 分类失败: {e}", flush=True)

    # 根据意图决定是否跳过 RAG/记忆召回（trivial 消息走快速路径）
    skip_rag = intent == "trivial"
    skip_memory = intent == "trivial"

    # 记录联系人交互（用消息事件中的 sender name 作为 fallback）
    try:
        _sender_name = ""
        try:
            _sender = getattr(getattr(event.event, "sender", None), "sender_id", None)
            _sender_name = getattr(getattr(event.event, "sender", None), "sender_type", "")
            # 尝试从 mentions 中获取发送者名字
            _mentions = getattr(msg, "mentions", None) or []
            for _m in _mentions:
                _m_id = getattr(getattr(_m, "id", None), "open_id", None)
                if _m_id == user_id:
                    _sender_name = getattr(_m, "name", "") or ""
                    break
        except Exception:
            pass
        from contact_memory import update_contact
        if _sender_name:
            update_contact(user_id, name=_sender_name)
        record_interaction(user_id, feishu_client=None)  # 跳过 get_user_info API 调用
    except Exception as _ci:
        print(f"[contact] record_interaction 失败: {_ci}", flush=True)

    # ── 普通消息 → 调用 Claude ──────────────────────────────
    session = await store.get_current(user_id, chat_id)
    print(f"[Claude] session={session.session_id} model={session.model}", flush=True)

    # ── 长任务检查点管理 ────────────────────────────────────
    # 1. pending resume task → 注入检查点上下文
    task_checkpoint_context = ""
    current_task_id = ""
    current_step = 0
    if session.pending_resume_task_id:
        task_id = session.pending_resume_task_id
        from long_task import build_checkpoint_context, get_latest_checkpoint

        task_checkpoint_context = build_checkpoint_context(task_id)
        latest = get_latest_checkpoint(task_id)
        current_step = latest["step"] if latest else 0
        current_task_id = task_id
        print(
            f"[task] 恢复任务 {task_id[:16]}... step={current_step} ctx={len(task_checkpoint_context)} chars",
            flush=True,
        )
        await store.clear_pending_resume_task(user_id, chat_id)
    # 2. intent=task → 创建或延续任务
    elif intent == "task":
        task_id = _active_task_by_chat.get(chat_id, "")
        if not task_id:
            from long_task import start_task

            task_id = start_task(chat_id, user_id, text[:80])
            _active_task_by_chat[chat_id] = task_id
            print(f"[task] 新建任务 {task_id}: {text[:40]}", flush=True)
        else:
            print(f"[task] 继续任务 {task_id[:16]}...", flush=True)
        latest = _current_task_info.get(chat_id)
        current_step = latest[1] if latest else 0
        current_task_id = task_id

    # ── 群聊历史（紧随 session 初始化之后）───────────────────
    group_history = ""
    if is_group:
        # 先把本条 @我 的消息也记一下（保持历史连贯）
        my_content = _parse_event_content(msg)
        sender_name = config.GROUP_KNOWN_NAMES.get(
            user_id, user_id[:8] if user_id else "用户"
        )
        _record_group_msg(chat_id, sender_name, my_content)

        # 群聊上下文：优先使用 Observer 观察笔记，为空时 fallback 到 deque
        try:
            group_ctx = get_group_context(chat_id)
        except Exception:
            group_ctx = ""

        if group_ctx:
            group_history = f"\n\n{group_ctx}"
            print(
                f"[history] 注入群聊 Observer 记忆 {len(group_history)} 字符",
                flush=True,
            )
        else:
            # fallback: 使用 deque 增量历史
            group_history = _get_group_history_text(chat_id)
            if group_history:
                print(
                    f"[history] 注入群聊历史 {len(group_history)} 字符（实时缓存 {len(_group_histories.get(chat_id, []))} 条）",
                    flush=True,
                )
            else:
                print(f"[history] 群聊历史缓存为空（Bot 刚启动或无历史消息）", flush=True)

    # 0b. 本地长期记忆（三层加载）—— 根据意图选择加载粒度，每 session 只注入一次
    brain_context = ""
    # context_injected 超过 24h 自动刷新（让长 session 也能获得最新记忆）
    _ctx_stale = False
    if session.context_injected:
        try:
            from datetime import datetime, timedelta
            injected_at = getattr(session, 'context_injected_at', None)
            if injected_at and datetime.now() - datetime.fromisoformat(injected_at) > timedelta(hours=24):
                _ctx_stale = True
                print(f"[brain] context 已超 24h，强制刷新", flush=True)
        except Exception:
            pass
    if skip_memory or (session.context_injected and not _ctx_stale):
        reason = "trivial 跳过" if skip_memory else "session 已有上下文"
        print(f"[brain] {reason}，跳过注入", flush=True)
    else:
        try:
            layer = select_layer(intent, text)
            brain_context = await asyncio.get_event_loop().run_in_executor(
                None, load_context, layer
            )
            if brain_context:
                print(f"[brain] Layer {layer} 注入 {len(brain_context)} 字符", flush=True)
            else:
                print(f"[brain] Layer {layer} 无内容", flush=True)
        except Exception as e:
            print(f"[brain] 三层加载失败，fallback 全量: {e}", flush=True)
            try:
                brain_context = await asyncio.get_event_loop().run_in_executor(
                    None, read_brain_context
                )
            except Exception:
                pass

    # 0c. 召回共享记忆（mem0 + OpenClaw 本地知识库）—— trivial 消息跳过，每 session 只注入一次
    memory_context = ""
    if skip_rag or session.context_injected:
        reason = "trivial 跳过 RAG" if skip_rag else "session 已有上下文"
        print(f"[memory] {reason}，跳过召回", flush=True)
    else:
        try:
            memory_context = await asyncio.get_event_loop().run_in_executor(
                None, recall_all, text
            )
            if memory_context:
                print(f"[memory] 召回 {len(memory_context)} 字符上下文", flush=True)
        except Exception as e:
            print(f"[memory] recall 异常: {e}", flush=True)

    # 1. 发送"思考中"占位卡片，拿到 message_id
    try:
        if is_group:
            card_msg_id = await feishu.reply_card(msg.message_id, loading=True)
        else:
            card_msg_id = await feishu.send_card_to_user(user_id, loading=True)
        print(f"[卡片] card_msg_id={card_msg_id}", flush=True)
    except Exception as e:
        print(f"[error] 发送占位卡片失败: {e}", flush=True)
        if is_group:
            try:
                await feishu.reply_card(
                    msg.message_id, content=f"❌ 发送消息失败：{e}", loading=False
                )
            except Exception:
                pass
        else:
            await feishu.send_text_to_user(user_id, f"❌ 发送消息失败：{e}")
        return

    active_run = _active_runs.start_run(user_id, card_msg_id)

    # 2. 流式回调
    accumulated = ""
    chars_since_push = 0  # 保留供 on_tool_use 引用

    async def push(content: str):
        try:
            await feishu.update_card(card_msg_id, content)
        except Exception as push_err:
            print(f"[warn] push 失败: {push_err}", flush=True)

    async def on_tool_use(name: str, inp: dict):
        nonlocal accumulated, chars_since_push
        # AskUserQuestion: 需要用户看到问题，必须更新卡片
        if name.lower() == "askuserquestion":
            question = inp.get("question", inp.get("text", ""))
            if question:
                accumulated += f"\n\n❓ **等待回复：**\n{question}"
                chars_since_push = 0
                await push(accumulated)
                return
        # 其他工具：只写日志，不更新卡片（避免触发飞书编辑上限）
        tool_line = _format_tool(name, inp)
        print(f"[tool] {tool_line}", flush=True)

    async def on_text_chunk(chunk: str):
        nonlocal accumulated, chars_since_push
        accumulated += chunk
        chars_since_push += len(chunk)
        # 不做中间推送，等 Claude 完成后一次性输出

    # 3. 运行 Claude — 统一构建上下文
    # 收集各上下文模块（安全获取，失败返回空）
    contact_ctx = ""
    try:
        contact_ctx = get_contact_context(user_id)
    except Exception as _ce:
        print(f"[contact] context 获取失败: {_ce}", flush=True)

    corrections_ctx = ""
    try:
        corrections_ctx = get_recent_corrections()
    except Exception as _re:
        print(f"[reflect] corrections 获取失败: {_re}", flush=True)

    instinct_ctx = ""
    try:
        instinct_ctx = get_instinct_context(text)
        if instinct_ctx:
            print(f"[instinct] 注入直觉 {len(instinct_ctx)} 字符", flush=True)
    except Exception as _ie:
        print(f"[instinct] 获取失败: {_ie}", flush=True)

    env_hint = ""
    if not session.session_id:
        group_rule = ""
        if is_group:
            group_rule = (
                "\n\n[群聊@人规则] "
                "当你需要安排任务给其他人或Bot时，必须在回复文本中直接写「@麦克斯」（不是 @_user_1 这种占位符）。"
                "系统会自动把文本中的「@麦克斯」转换为真正的飞书@mention。"
                "可用的@对象：@麦克斯（OpenClaw Bot，负责执行采集/分析/调研等任务）。"
                "示例：'@麦克斯 请去做小红书关键词采集，关键词是空间香氛'。"
            )
        env_hint = (
            "[环境：用户通过飞书发送消息，无交互式UI。"
            "当需要用户做选择时，用编号列表呈现选项（1. 2. 3.），"
            "最后加一个「其他（请说明）」选项，用户回复数字即可。"
            f"简单确认用 Y/N。{group_rule}]"
        )

    claude_msg = build_context(
        text,
        task_checkpoint_context=task_checkpoint_context,
        group_history=group_history,
        contact_context=contact_ctx,
        brain_context=brain_context,
        corrections_context=corrections_ctx,
        instinct_context=instinct_ctx,
        memory_context=memory_context,
        env_hint=env_hint,
    )
    # ── 自动模型路由 ─────────────────────────────────────────────
    model_to_use = session.model
    try:
        routed_model, route_reason = select_model(intent, text, session.model)
        if routed_model != session.model:
            print(f"[model_router] {session.model} → {routed_model} ({route_reason})", flush=True)
        model_to_use = routed_model
    except Exception as _mr:
        print(f"[model_router] 路由失败，使用默认: {_mr}", flush=True)

    # ── 配额检查与自动降级 ─────────────────────────────────────
    try:
        if not quota_tracker.check_quota(model_to_use):
            fallback = quota_tracker.get_fallback_model(model_to_use)
            if fallback == "external":
                # 所有 Claude 模型配额耗尽，使用 MiniMax 兜底
                print(f"[quota] 所有 Claude 模型耗尽，切换 MiniMax 兜底", flush=True)
                model_to_use = "minimax"  # 特殊标记，后面特殊处理
            else:
                print(
                    f"[quota] 配额预警，降级 {model_to_use} → {fallback}", flush=True
                )
                model_to_use = fallback
    except Exception as _qe:
        print(f"[quota] check_quota 失败（忽略）: {_qe}", flush=True)

    # ── 自动 effort 选择 ────────────────────────────────────────
    effort_level = "medium"
    try:
        effort_level = select_effort(model_to_use, intent, text)
        print(f"[model_router] model={model_to_use} effort={effort_level}", flush=True)
    except Exception as _ef:
        print(f"[model_router] effort 选择失败: {_ef}", flush=True)

    # MiniMax 兜底分支（跳过 Claude CLI）
    if model_to_use == "minimax":
        try:
            from minimax_client import chat_minimax
            full_text = chat_minimax(claude_msg)
            new_session_id = None
            used_fresh_session_fallback = True
            # 直接更新卡片并返回
            await feishu.update_card(card_msg_id, f"🤖 [MiniMax 兜底]\n\n{full_text}")
            _active_runs.clear_run(user_id)
            # 后处理
            try:
                write_daily_log(f"[MiniMax兜底] Q: {text[:100]} A: {full_text[:200]}", tag="minimax")
            except Exception:
                pass
            return
        except Exception as mm_err:
            print(f"[minimax] 兜底失败: {mm_err}", flush=True)
            await feishu.update_card(card_msg_id, f"❌ 所有模型均不可用: {mm_err}")
            _active_runs.clear_run(user_id)
            return

    try:
        full_text, new_session_id, used_fresh_session_fallback = await run_claude(
            message=claude_msg,
            session_id=session.session_id,
            model=model_to_use,
            cwd=session.cwd,
            permission_mode=session.permission_mode,
            effort=effort_level,
            on_text_chunk=on_text_chunk,
            on_tool_use=on_tool_use,
            on_process_start=lambda proc: _active_runs.attach_process(user_id, proc),
        )
        print(f"[run_claude] 完成, session={new_session_id}", flush=True)
        # 调用成功后记录用量
        try:
            quota_tracker.record_call(model_to_use)
        except Exception as _re:
            print(f"[quota] record_call 失败（忽略）: {_re}", flush=True)
    except Exception as e:
        if active_run.stop_requested:
            return
        print(f"[error] Claude 运行失败: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        try:
            await feishu.update_card(
                card_msg_id, f"❌ Claude 执行出错：{type(e).__name__}: {e}"
            )
        except Exception:
            pass
        return

    # 3.5 内部自辩（方案类输出自动审视，结果只写日志，不暴露给用户）
    try:
        if full_text and should_debate(text, full_text):
            print(f"[debate] 触发内部自辩...", flush=True)
            debate_result = await asyncio.get_event_loop().run_in_executor(
                None, run_debate, text, full_text
            )
            verdict = debate_result.get("verdict", "pass")
            print(f"[debate] 审查结果: {verdict}", flush=True)
            if debate_result.get("critique"):
                print(f"[debate] 审查意见: {debate_result['critique'][:200]}", flush=True)
            # 只写日志，不修改 full_text
            try:
                write_daily_log(format_debate_log(debate_result), tag="debate")
            except Exception:
                pass
    except Exception as _db:
        print(f"[debate] 自辩失败（忽略）: {_db}", flush=True)

    # 4. 最终更新卡片为完整内容
    final = full_text or "（无输出）"
    if used_fresh_session_fallback:
        final = (
            "⚠️ 检测到工作目录已变化，旧会话无法继续。"
            "本次已自动切换到新 session。\n\n" + final
        )
    # 在末尾附加模型和 effort 标签
    model_short = model_to_use.split("/")[-1].replace("claude-", "").replace("-20251001", "")
    final += f"\n\n`{model_short} · {effort_level}`"
    try:
        await feishu.update_card(card_msg_id, final)
    except Exception as e:
        print(f"[error] 更新卡片失败: {e}", flush=True)

    # 4.5 回复完成后的状态标记
    # Plan C：标记 brain/memory 已注入，下一轮 session 跳过
    if brain_context or memory_context:
        try:
            await store.mark_context_injected(user_id, chat_id)
        except Exception as e:
            print(f"[session] mark_context_injected 失败: {e}", flush=True)
    # Plan A：更新群聊 delta 基准（下次只注入新增消息）
    if is_group:
        _mark_group_replied(chat_id)

    # 5. 群聊中检测 @其他 Bot，通过 OpenClaw CLI 调用 agent
    #    两种模式：工作模式（派发→验收→返工）/ 讨论模式（多轮辩论→Claude 总结）
    if is_group and full_text:
        import re
        import json as _json
        import subprocess as _sp

        OPENCLAW_MAX_ROUNDS = 5

        # 检测是否需要调用 OpenClaw agent
        _matched_trigger = None
        _matched_agent = None
        for trigger, agent_id in config.OPENCLAW_AGENTS.items():
            if trigger in full_text:
                _matched_trigger = trigger
                _matched_agent = agent_id
                break
        # 兜底：检测 @_user_N 占位符
        if not _matched_trigger and re.search(r'@_user_\d+', full_text):
            _matched_trigger = re.search(r'@_user_\d+', full_text).group(0)
            _matched_agent = "agent-a-coo"
            print(f"[openclaw] 检测到占位符 {_matched_trigger}，映射到 {_matched_agent}", flush=True)

        if _matched_trigger and _matched_agent:
            # 提取任务内容（去掉触发词和占位符）
            relay_text = full_text
            for t in config.OPENCLAW_AGENTS:
                relay_text = relay_text.replace(t, "").strip()
            relay_text = re.sub(r'@_user_\d+', '', relay_text).strip()

            if relay_text:
                reply_target = config.OPENCLAW_GROUP_TARGETS.get(chat_id)

                # ── 公共工具函数 ──

                async def _call_oc(agent_id, message, deliver=True):
                    """调用 OpenClaw agent，返回 (result_text, result_obj, oc_model) 或 None"""
                    cmd = [
                        config.OPENCLAW_CLI, "agent",
                        "--agent", agent_id,
                        "--message", message,
                        "--json",
                        "--timeout", "300",
                    ]
                    if deliver and reply_target:
                        cmd += ["--deliver", "--reply-channel", "feishu", "--reply-to", reply_target]
                    try:
                        _cmd = list(cmd)
                        proc = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda c=_cmd: _sp.run(c, capture_output=True, text=True, timeout=360)
                        )
                    except _sp.TimeoutExpired:
                        print(f"[openclaw] 超时", flush=True)
                        return None
                    except Exception as e:
                        print(f"[openclaw] 异常: {e}", flush=True)
                        return None
                    if proc.returncode != 0:
                        print(f"[openclaw] 失败: {proc.stderr[:200]}", flush=True)
                        return None
                    result_text = ""
                    result_obj = {}
                    oc_model = ""
                    try:
                        result_obj = _json.loads(proc.stdout)
                        payloads = result_obj.get("result", {}).get("payloads", [])
                        result_text = "\n".join(p.get("text", "") for p in payloads if p.get("text"))
                        _meta = result_obj.get("result", {}).get("meta", {}).get("agentMeta", {})
                        oc_model = _meta.get("model", "")
                    except Exception:
                        result_text = proc.stdout[:2000] if proc.stdout else ""
                    return (result_text, result_obj, oc_model)

                # 复杂度标记，由 _classify_mode 设置
                _task_complexity = "simple"  # simple → Sonnet, complex → Opus

                async def _call_claude(prompt, effort="low", force_opus=False):
                    """调用 Claude 并返回文本结果，复杂任务自动升级 Opus"""
                    model = "claude-opus-4-6" if (force_opus or _task_complexity == "complex") else "claude-sonnet-4-6"
                    # 复杂任务 effort 也相应提升
                    if model == "claude-opus-4-6" and effort == "low":
                        effort = "medium"
                    try:
                        result, _, _ = await run_claude(
                            message=prompt,
                            model=model,
                            effort=effort,
                        )
                        print(f"[openclaw] Claude 调用完成, model={model}", flush=True)
                        return result.strip()
                    except Exception as e:
                        print(f"[openclaw] Claude 调用失败 ({model}): {e}", flush=True)
                        return None

                # ── 模式 + 复杂度判断（一次 Haiku 调用） ──

                async def _classify_mode():
                    """用 Haiku API 同时判断模式和复杂度"""
                    nonlocal _task_complexity
                    try:
                        result = await chat_haiku(
                            messages=[{
                                "role": "user",
                                "content": (
                                    f"判断以下内容的两个属性，用一行回复，格式：MODE COMPLEXITY\n\n"
                                    f"MODE（二选一）：\n"
                                    f"- TASK：执行任务、查数据、做事情\n"
                                    f"- DISCUSS：讨论、分析、辩论、评估\n\n"
                                    f"COMPLEXITY（二选一）：\n"
                                    f"- SIMPLE：简单查询、日常任务、单一问题\n"
                                    f"- COMPLEX：多维度分析、商业决策、战略评估、需要深度推理\n\n"
                                    f"内容：{relay_text[:300]}\n\n"
                                    f"只回复两个词，如：TASK SIMPLE 或 DISCUSS COMPLEX"
                                ),
                            }],
                            max_tokens=10,
                            temperature=0.0,
                        )
                        if result:
                            upper = result.upper()
                            if "COMPLEX" in upper:
                                _task_complexity = "complex"
                                print(f"[openclaw] 复杂度: complex → 使用 Opus", flush=True)
                            mode = "discuss" if "DISCUSS" in upper else "task"
                            return mode
                    except Exception as e:
                        print(f"[openclaw] Haiku 分类失败，默认 task/simple: {e}", flush=True)
                    return "task"

                # ── 工作模式：派发 → 验收 → 返工 ──

                async def _task_loop():
                    task_desc = (
                        f"{relay_text}\n\n"
                        f"---\n"
                        f"[协同提示] 这是 Claude Bot 派发的任务。请先判断任务类型：\n"
                        f"- 如果涉及市场调研、数据采集、竞品分析 → 考虑调用 agent-b-research\n"
                        f"- 如果涉及流程优化、自动化 → 考虑调用 agent-e-workflow\n"
                        f"- 如果涉及合规、法务 → 考虑调用 agent-f-legal\n"
                        f"- 如果是简单问答或你能直接完成的 → 直接回复即可\n"
                        f"请根据任务复杂度自行决定是否需要子 agent 协助。\n\n"
                        f"[格式要求] 回复开头必须加标注：「[麦克斯 → Claude] 等待 Claude 验收」"
                    )
                    agent_id = _matched_agent

                    for round_n in range(1, OPENCLAW_MAX_ROUNDS + 1):
                        round_label = f"第{round_n}轮" if round_n > 1 else "首轮"
                        print(f"[openclaw][工作] {round_label}: {task_desc[:80]}...", flush=True)

                        oc_result = await _call_oc(agent_id, task_desc)
                        if not oc_result or not oc_result[0].strip():
                            await feishu.send_card_to_group(chat_id, f"[Claude → 老大]\n⚠️ 麦克斯返回空结果（{round_label}），停止协同")
                            return
                        result_text, result_obj, oc_model = oc_result

                        # Claude 验收
                        review = await _call_claude(
                            f"你是任务验收官。以下是你之前派给麦克斯（OpenClaw）的任务和他的执行结果。\n\n"
                            f"【原始任务】\n{relay_text}\n\n"
                            f"【麦克斯的执行结果（{round_label}）】\n{result_text[:3000]}\n\n"
                            f"请验收这个结果：\n"
                            f"1. 先给出验收结论：PASS 或 REVISE\n"
                            f"2. 如果 PASS：简要说明为什么合格\n"
                            f"3. 如果 REVISE：具体说明哪里不合格，需要怎么改\n\n"
                            f"严格按格式回复，第一行必须是 PASS 或 REVISE"
                        )
                        if not review:
                            await feishu.send_card_to_group(chat_id, f"[Claude → 老大]\n⚠️ 验收环节出错（{round_label}）")
                            return

                        is_pass = review.upper().startswith("PASS")
                        verdict_icon = "✅" if is_pass else "🔄"
                        model_line = f"📎 验收: claude-sonnet-4-6 | 执行: {oc_model or '未知'}"
                        if is_pass:
                            direction = "[Claude → 老大] 验收完成，请老大过目"
                        else:
                            direction = "[Claude → 麦克斯] 验收不通过，等待麦克斯返工"
                        await feishu.send_card_to_group(
                            chat_id,
                            f"{direction}\n**{verdict_icon} Claude 验收（{round_label}）**\n{model_line}\n\n{review[:2000]}"
                        )
                        print(f"[openclaw][工作] 验收: {'PASS' if is_pass else 'REVISE'}", flush=True)

                        if is_pass:
                            return

                        if round_n >= OPENCLAW_MAX_ROUNDS:
                            await feishu.send_card_to_group(chat_id, f"[Claude → 老大]\n⚠️ 已达最大轮次（{OPENCLAW_MAX_ROUNDS}），停止返工")
                            return

                        task_desc = (
                            f"这是返工指令（第{round_n+1}轮）。\n\n"
                            f"【原始任务】\n{relay_text}\n\n"
                            f"【你上一轮的结果被驳回，验收意见如下】\n{review[:2000]}\n\n"
                            f"请根据验收意见修改并重新提交结果。\n"
                            f"提示：如果上一轮你独自完成但质量不够，考虑调用子 agent（如 agent-b-research）协助。\n\n"
                            f"[格式要求] 回复开头必须加标注：「[麦克斯 → Claude] 等待 Claude 验收」"
                        )

                # ── 讨论模式：Claude 观点 → 麦克斯回应 → Claude 回应 → ... → Claude 总结 ──

                async def _discuss_loop():
                    agent_id = _matched_agent
                    discussion_log = []  # 记录完整讨论过程

                    # Claude 的初始观点就是 relay_text（Claude 已经回复到群里了）
                    claude_position = relay_text
                    discussion_log.append(("Claude", claude_position))

                    for round_n in range(1, OPENCLAW_MAX_ROUNDS + 1):
                        round_label = f"第{round_n}轮"

                        # ── 麦克斯回应 Claude ──
                        discuss_prompt = (
                            f"你正在与 Claude 进行专业讨论。请认真审视对方观点，给出你的独立见解。\n\n"
                            f"【讨论主题】\n{relay_text}\n\n"
                            f"【Claude 的观点】\n{claude_position[:2000]}\n\n"
                            f"请回应：\n"
                            f"1. 你同意哪些部分？为什么？\n"
                            f"2. 你不同意或有补充的部分？给出你的理由和依据\n"
                            f"3. 有没有 Claude 遗漏的重要角度？\n\n"
                            f"请直接表达你的专业观点，不要客套。\n"
                            f"重要：如果需要数据支撑或专业视角，请调用子 agent（如 agent-b-research）协助。\n"
                            f"回复时请明确标注哪些观点来自你自己，哪些来自子 agent，格式如：\n"
                            f"「我认为 X。另外，研究员补充了 Y 数据，支持/反驳了 Z 观点。」\n\n"
                            f"[格式要求] 回复开头必须加标注：「[麦克斯 → Claude] 等待 Claude 回应」"
                        )
                        print(f"[openclaw][讨论] {round_label} 麦克斯回应中...", flush=True)

                        oc_result = await _call_oc(agent_id, discuss_prompt)
                        if not oc_result or not oc_result[0].strip():
                            await feishu.send_card_to_group(chat_id, f"[Claude → 老大]\n⚠️ 麦克斯无回应（{round_label}），结束讨论")
                            break
                        oc_response, _, oc_model = oc_result
                        discussion_log.append(("麦克斯", oc_response))

                        # ── Claude 回应麦克斯 ──
                        claude_prompt = (
                            f"你正在与麦克斯（OpenClaw, 模型 {oc_model or 'MiniMax'})进行专业讨论。\n\n"
                            f"【讨论主题】\n{relay_text}\n\n"
                            f"【近期讨论记录（最近 2 轮）】\n"
                        )
                        # 只注入最近 4 条发言（2 轮对话），防止上下文膨胀
                        recent_log = discussion_log[-4:] if len(discussion_log) > 4 else discussion_log
                        for speaker, content in recent_log:
                            claude_prompt += f"--- {speaker} ---\n{content[:800]}\n\n"
                        claude_prompt += (
                            f"【麦克斯最新回应（{round_label}）】\n{oc_response[:2000]}\n\n"
                            f"请回应麦克斯的观点：\n"
                            f"1. 他说得对的地方，坦然接受并优化你的观点\n"
                            f"2. 他说得不对的地方，给出反驳和依据\n"
                            f"3. 综合讨论，你的观点有什么更新或深化？\n\n"
                            f"另外，判断讨论是否已经充分收敛（双方基本达成共识或各自论点已充分展开）。\n"
                            f"如果已收敛，第一行写 CONVERGED；如果还需继续，第一行写 CONTINUE。\n"
                            f"然后再写你的回应内容。"
                        )
                        claude_response = await _call_claude(claude_prompt, effort="medium")
                        if not claude_response:
                            await feishu.send_card_to_group(chat_id, f"[Claude → 老大]\n⚠️ Claude 回应失败（{round_label}）")
                            break

                        is_converged = claude_response.upper().startswith("CONVERGED")
                        # 去掉 CONVERGED/CONTINUE 标记
                        claude_clean = re.sub(r'^(CONVERGED|CONTINUE)\s*', '', claude_response, flags=re.IGNORECASE).strip()
                        discussion_log.append(("Claude", claude_clean))

                        # 发送 Claude 的回应到群
                        model_line = f"📎 Claude: claude-sonnet-4-6 | 麦克斯: {oc_model or '未知'}"
                        if is_converged:
                            direction = "[Claude → 老大] 讨论已收敛，正在生成总结"
                        else:
                            direction = "[Claude → 麦克斯] 等待麦克斯回应"
                        await feishu.send_card_to_group(
                            chat_id,
                            f"{direction}\n**💬 Claude 回应（{round_label}）**\n{model_line}\n\n{claude_clean[:2000]}"
                        )
                        print(f"[openclaw][讨论] {round_label} Claude 回应完成, converged={is_converged}", flush=True)

                        if is_converged:
                            break  # 自然收敛，跳出后自动总结

                        if round_n >= OPENCLAW_MAX_ROUNDS:
                            # 5 轮未收敛，问用户
                            # 先做一个简要的当前状态梳理
                            status_prompt = (
                                f"你和麦克斯讨论了 {OPENCLAW_MAX_ROUNDS} 轮但未达成共识。\n"
                                f"请用 3-5 句话概括当前讨论状态：双方各持什么观点，分歧在哪。\n\n"
                                f"【讨论主题】\n{relay_text}\n\n"
                                f"【讨论记录】\n"
                            )
                            for speaker, content in discussion_log[-4:]:
                                status_prompt += f"--- {speaker} ---\n{content[:500]}\n\n"
                            status_summary = await _call_claude(status_prompt, effort="low")
                            status_text = status_summary or "（状态概括失败）"

                            await feishu.send_card_to_group(
                                chat_id,
                                f"[Claude → 老大] 等待老大定夺\n"
                                f"**⏸ 讨论暂停（已 {OPENCLAW_MAX_ROUNDS} 轮未收敛）**\n"
                                f"📎 模型: claude-sonnet-4-6\n\n"
                                f"{status_text}\n\n"
                                f"---\n"
                                f"老大请定夺：\n"
                                f"• 回复「继续讨论」→ 再进行 {OPENCLAW_MAX_ROUNDS} 轮\n"
                                f"• 回复你的决定/方向 → Claude 据此出最终总结\n"
                                f"• 不回复 → 讨论到此为止"
                            )
                            print(f"[openclaw][讨论] {OPENCLAW_MAX_ROUNDS}轮未收敛，等待用户定夺", flush=True)
                            return  # 不自动总结，等用户回复

                        # 更新 Claude 最新观点，用于下一轮
                        claude_position = claude_clean

                    # ── 自然收敛后的最终总结 ──
                    summary_prompt = (
                        f"以下是你和麦克斯（OpenClaw）关于某个话题的完整讨论记录。\n"
                        f"讨论已自然收敛，请为主人写一份最终总结报告。\n\n"
                        f"【讨论主题】\n{relay_text}\n\n"
                        f"【完整讨论记录】\n"
                    )
                    for speaker, content in discussion_log:
                        summary_prompt += f"--- {speaker} ---\n{content[:400]}\n\n"
                    summary_prompt += (
                        f"请输出：\n"
                        f"1. **共识**：双方达成一致的要点\n"
                        f"2. **分歧**：仍有争议的点，以及你的最终判断\n"
                        f"3. **结论与建议**：综合讨论，你给主人的最终建议\n"
                    )
                    summary = await _call_claude(summary_prompt, effort="medium")
                    if summary:
                        await feishu.send_card_to_group(
                            chat_id,
                            f"[Claude → 老大] 讨论完成，请老大过目\n"
                            f"**📋 讨论总结（{len(discussion_log)} 轮发言，自然收敛）**\n📎 模型: claude-sonnet-4-6\n\n{summary[:3000]}"
                        )
                    print(f"[openclaw][讨论] 总结完成，共 {len(discussion_log)} 轮发言", flush=True)

                # ── 主调度：判断模式并执行 ──

                async def _openclaw_dispatch():
                    mode = await _classify_mode()
                    print(f"[openclaw] 模式判断: {mode}", flush=True)
                    if mode == "discuss":
                        await _discuss_loop()
                    else:
                        await _task_loop()

                # 后台执行，不阻塞当前消息处理
                asyncio.ensure_future(_openclaw_dispatch())

    # 6. 保存对话到共享记忆（后台执行，不阻塞）
    if full_text:
        try:
            asyncio.get_event_loop().run_in_executor(
                None, capture_memory, text, full_text
            )
        except Exception as e:
            print(f"[memory] capture 异常: {e}", flush=True)

    # 6b. 本地记忆：写日志 + 检测纠正 + Context DAG 持久化
    if full_text:

        def _local_memory_tasks():
            try:
                # 写每日日志（只记录有实质内容的对话）
                if len(full_text) > 50:
                    log_entry = f"用户：{text[:200]}\nClaude：{full_text[:400]}"
                    write_daily_log(log_entry, tag="对话")
                # 纠正检测（增强版，旧版已移除）
                try:
                    process_correction(text, full_text, user_id)
                except Exception as _re:
                    print(f"[reflect] 纠正检测失败: {_re}", flush=True)
                # Context DAG：持久化对话轮次（后台线程，崩溃不中断）
                from context_dag import ingest as dag_ingest

                dag_ingest(chat_id, "user", text[:2000], user_name="用户")
                dag_ingest(chat_id, "assistant", full_text[:2000], user_name="Claude")
                # 长任务检查点：intent=task 或 resume task 时保存
                if current_task_id:
                    from long_task import add_checkpoint, extract_step_desc

                    next_step = current_step + 1
                    step_desc = extract_step_desc(full_text)
                    accumulated = f"用户：{text[:500]}\nClaude：{full_text[:2000]}"
                    cp_id = add_checkpoint(
                        current_task_id, next_step, step_desc, accumulated
                    )
                    _current_task_info[chat_id] = (current_task_id, next_step)
                    print(
                        f"[task] 保存检查点 task={current_task_id[:16]} step={next_step} id={cp_id}",
                        flush=True,
                    )
            except Exception as e:
                print(f"[brain] 本地记忆写入失败: {e}", flush=True)

        asyncio.get_event_loop().run_in_executor(None, _local_memory_tasks)

    # 7. 更新 session 状态
    if new_session_id:
        await store.on_claude_response(user_id, chat_id, new_session_id, text)


def _format_tool(name: str, inp: dict) -> str:
    """格式化工具调用的进度提示"""
    n = name.lower()
    if n == "bash":
        cmd = inp.get("command", "")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return f"🔧 **执行命令：** `{cmd}`" if cmd else f"🔧 **执行命令...**"
    elif n in ("read_file", "read"):
        return f"📄 **读取：** `{inp.get('file_path', inp.get('path', ''))}`"
    elif n in ("write_file", "write"):
        return f"✏️ **写入：** `{inp.get('file_path', inp.get('path', ''))}`"
    elif n in ("edit_file", "edit"):
        return f"✂️ **编辑：** `{inp.get('file_path', inp.get('path', ''))}`"
    elif n in ("glob",):
        return f"🔍 **搜索文件：** `{inp.get('pattern', '')}`"
    elif n in ("grep",):
        return f"🔎 **搜索内容：** `{inp.get('pattern', '')}`"
    elif n == "task":
        return f"🤖 **子任务：** {inp.get('description', inp.get('prompt', '')[:40])}"
    elif n == "webfetch":
        return f"🌐 **抓取网页...**"
    elif n == "websearch":
        return f"🔍 **搜索：** {inp.get('query', '')}"
    else:
        return f"⚙️ **{name}**"


# ── 飞书事件回调（同步）→ 调度异步任务 ───────────────────────


def on_message_receive(data: P2ImMessageReceiveV1) -> None:
    """
    飞书 SDK 同步回调。
    ws.Client 内部运行 asyncio loop，此处用 ensure_future 调度异步任务。
    """
    global _last_event
    _last_event = time.time()
    asyncio.ensure_future(handle_message_async(data))


# ── 启动 ──────────────────────────────────────────────────────


def main():
    # 启动时恢复群聊缓存
    _load_group_cache()
    _load_seen_ids()
    print("🚀 飞书 Claude Bot 启动中...")
    print(f"   App ID      : {config.FEISHU_APP_ID}")
    print(f"   默认模型    : {config.DEFAULT_MODEL}")
    print(f"   默认工作目录: {config.DEFAULT_CWD}")
    print(f"   权限模式    : {config.PERMISSION_MODE}")

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message_receive)
        .build()
    )

    ws_client = lark.ws.Client(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )

    # 启动看门狗线程
    t = threading.Thread(target=_watchdog, daemon=True)
    t.start()

    # 启动定时任务调度器
    from scheduler import run_scheduler

    async def _scheduler_trigger(chat_id: str, task_prompt: str):
        """定时任务触发：调用 Claude 并把结果发到对应 chat"""
        try:
            session = await store.get_current("__scheduler__", chat_id)
            card_msg_id = await feishu.send_card_to_user(chat_id, loading=True)
            full_text, new_session_id, _ = await run_claude(
                message=task_prompt,
                session_id=session.session_id,
                model=session.model,
                cwd=session.cwd,
                permission_mode=session.permission_mode,
            )
            await feishu.update_card(card_msg_id, full_text or "（定时任务无输出）")
            if new_session_id:
                await store.on_claude_response(
                    "__scheduler__", chat_id, new_session_id, task_prompt
                )
        except Exception as e:
            print(f"[scheduler_trigger] 失败: {e}", flush=True)

    def _start_scheduler():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_scheduler(_scheduler_trigger))

    sched_thread = threading.Thread(target=_start_scheduler, daemon=True)
    sched_thread.start()

    # 启动时增量索引（后台线程，不阻塞 Bot 启动）
    def _start_indexer():
        try:
            from indexer import build_index

            count = build_index()
            print(f"[indexer] 启动索引完成，{count} 块", flush=True)
        except Exception as e:
            print(f"[indexer] 启动索引失败: {e}", flush=True)

    indexing_thread = threading.Thread(target=_start_indexer, daemon=True)
    indexing_thread.start()

    print("✅ 连接飞书 WebSocket 长连接（自动重连）...")
    ws_client.start()  # 阻塞，内部运行 asyncio loop


if __name__ == "__main__":
    main()
