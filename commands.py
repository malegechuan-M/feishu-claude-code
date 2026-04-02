"""
斜杠命令解析与处理。
返回要发送给用户的回复文本。
"""

import getpass
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from typing import Optional, Tuple

from bot_config import CLAUDE_CLI, DEFAULT_CWD
from session_store import (
    SessionStore,
    scan_cli_sessions,
    generate_summary,
    _get_api_token,
    _write_custom_title,
)

PLUGINS_DIR = os.path.expanduser("~/.claude/plugins")


VALID_MODES = {
    "default": "每次工具调用需确认",
    "acceptEdits": "自动接受文件编辑，其余需确认",
    "plan": "只规划不执行工具",
    "bypassPermissions": "全部自动执行（无确认）",
    "dontAsk": "全部自动执行（静默）",
}

MODE_ALIASES = {
    "bypass": "bypassPermissions",
    "accept": "acceptEdits",
    "auto": "bypassPermissions",
}

MODEL_ALIASES = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

HELP_TEXT = """\
📖 **可用命令**

**Bot 管理：**
`/help` — 显示此帮助
`/stop` — 停止当前正在运行的任务
`/new` 或 `/clear` — 开始新 session
`/resume` — 查看历史 sessions / `/resume [序号]` 恢复
`/model [名称]` — 切换模型（opus / sonnet / haiku 或完整 ID）
`/mode [模式]` — 切换权限模式（default / plan / acceptEdits / bypassPermissions）
`/status` — 显示当前 session 信息
`/cd [路径]` — 切换工具执行的工作目录
`/ls [路径]` — 查看当前工作目录下的文件/目录
 `/workspace` 或 `/ws` — 保存/切换群组工作空间
 `/reindex` — 触发知识库增量索引 / `reindex force` 全量重建
 `/tasks` — 查看进行中的长任务列表
 `/resume-task [任务ID]` — 恢复指定的长任务继续执行

**查看能力：**
`/skills` — 列出已安装的 Claude Skills
`/mcp` — 列出已配置的 MCP Servers
`/usage` — 查看 Claude Max 订阅用量百分比和重置时间


**Claude Skills（直接转发给 Claude 执行）：**
`/commit` — 提交代码
其他 `/xxx` — 自动转发给 Claude 处理

**MCP 工具：** 已配置的 MCP servers 自动可用，直接对话即可调用。

**发送任意普通消息即可与 Claude 对话。**\
"""


def parse_command(text: str) -> Optional[Tuple[str, str]]:
    """
    尝试解析斜杠命令。
    返回 (command, args) 或 None（不是命令）。
    """
    text = text.strip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split(None, 1)
    cmd = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return cmd, args


# Bot 自身处理的命令，其余 /xxx 转发给 Claude
BOT_COMMANDS = {
    "help",
    "h",
    "new",
    "clear",
    "resume",
    "model",
    "mode",
    "status",
    "cd",
    "ls",
    "workspace",
    "ws",
    "skills",
    "mcp",
    "usage",
    "stop",
    "schedule",
    "reindex",
    "tasks",
    "resume-task",
    "quota",
    "contact",
    "group-memory",
    "instinct",
    "review",
    "install",
    "skill-create",
    "skills",
}


async def _build_session_list(
    user_id: str, chat_id: str, store: SessionStore
) -> list[dict]:
    """构建合并、去重、排序后的 session 列表（不含当前 session）。
    /resume 列表展示和 /resume N 选择都用这一个函数，保证索引一致。"""
    cur_sid = (await store.get_current_raw(user_id, chat_id)).get("session_id")

    cli_all = scan_cli_sessions(30)
    cli_preview_map = {s["session_id"]: s for s in cli_all}

    feishu_sessions = [
        {**s, "source": "feishu"} for s in await store.list_sessions(user_id, chat_id)
    ]
    for s in feishu_sessions:
        cli_info = cli_preview_map.get(s["session_id"])
        if cli_info and cli_info.get("preview"):
            s["preview"] = cli_info["preview"]

    feishu_ids = {s["session_id"] for s in feishu_sessions}
    cli_sessions = [
        s
        for s in cli_all
        if s["session_id"] not in feishu_ids and len(s.get("preview", "")) > 5
    ]
    all_sessions = feishu_sessions + cli_sessions

    seen = set()
    if cur_sid:
        seen.add(cur_sid)
    deduped = []
    for s in all_sessions:
        sid = s["session_id"]
        if sid not in seen:
            seen.add(sid)
            deduped.append(s)

    deduped.sort(key=lambda s: s.get("started_at", ""), reverse=True)
    return deduped[:15]


async def _format_session_list(user_id: str, chat_id: str, store: SessionStore) -> str:
    """生成历史 sessions 列表（去重 + 手机友好格式），含当前 session"""
    from session_store import _clean_preview

    cur = await store.get_current_raw(user_id, chat_id)
    cur_sid = cur.get("session_id")

    cli_all = scan_cli_sessions(30)
    cli_preview_map = {s["session_id"]: s for s in cli_all}

    all_sessions = await _build_session_list(user_id, chat_id, store)

    def _fmt_time(raw: str) -> str:
        t = raw[:16].replace("T", " ")
        if len(t) >= 16:
            t = t[5:16].replace("-", "/")
        return t

    # 收集所有需要展示的 session_id
    all_sids = []
    if cur_sid:
        all_sids.append(cur_sid)
    for s in all_sessions:
        all_sids.append(s["session_id"])

    # 懒加载：为缺失摘要的 session 生成（限制 5 个，避免太慢）
    summaries = {}
    missing = []
    for sid in all_sids:
        cached = store.get_summary(user_id, sid)
        if cached:
            summaries[sid] = cached
        else:
            missing.append(sid)

    if missing:
        token = _get_api_token()
        if token:
            new_summaries = {}
            for sid in missing[:5]:
                s = generate_summary(sid, token=token)
                if s:
                    new_summaries[sid] = s
                    summaries[sid] = s
                    _write_custom_title(sid, s)
            if new_summaries:
                await store.batch_set_summaries(user_id, new_summaries)

    lines = []

    def _strip_md(text: str) -> str:
        """去除 markdown 格式 + 压成单行纯文本"""
        # 换行 → 空格，压成单行
        text = " ".join(text.split())
        # heading 标记
        while text.startswith("#"):
            text = text.lstrip("#").lstrip()
        # bold / italic
        text = text.replace("**", "").replace("__", "")
        # backtick
        text = text.replace("`", "")
        # XML 残留标签名（如 <tool_call>）
        text = text.replace("<", "").replace(">", "")
        return text.strip()

    def _desc(sid: str, preview_raw: str) -> str:
        """用 summary 优先，没有就用 preview，拼成简短描述"""
        s = summaries.get(sid, "")
        if s:
            s = _strip_md(s)
            return s if len(s) <= 40 else s[:37] + "..."
        p = _clean_preview(preview_raw or "")
        if not p:
            return "（无预览）"
        p = _strip_md(p)
        return p if len(p) <= 40 else p[:37] + "..."

    # 当前 session
    if cur_sid:
        cli_info = cli_preview_map.get(cur_sid)
        preview = (
            cli_info.get("preview")
            if cli_info and cli_info.get("preview")
            else cur.get("preview") or ""
        )
        started = _fmt_time(cur.get("started_at", ""))
        lines.append(f"当前  {_desc(cur_sid, preview)} ({started})  #{cur_sid[:8]}")

    if not cur_sid and not all_sessions:
        return "暂无历史 sessions。"

    for i, s in enumerate(all_sessions, 1):
        sid = s["session_id"]
        preview = s.get("preview", "")
        started = _fmt_time(s.get("started_at", ""))
        lines.append(f"{i}. {_desc(sid, preview)} ({started})  #{sid[:8]}")

    if all_sessions:
        lines.append("")
        lines.append("回复 /resume 序号 恢复")
    return "\n".join(lines)


def _list_skills() -> str:
    """扫描 ~/.claude/plugins 目录，列出所有可用的 slash command skills"""
    skills = []
    if not os.path.isdir(PLUGINS_DIR):
        return "暂无已安装的 skills。"

    for root, dirs, files in os.walk(PLUGINS_DIR):
        if os.path.basename(root) != "commands":
            continue
        for fname in files:
            if not fname.endswith(".md"):
                continue
            name = fname[:-3]
            fpath = os.path.join(root, fname)
            desc = ""
            try:
                with open(fpath, encoding="utf-8") as f:
                    in_frontmatter = False
                    for line in f:
                        line = line.strip()
                        if line == "---" and not in_frontmatter:
                            in_frontmatter = True
                            continue
                        if line == "---" and in_frontmatter:
                            break
                        if in_frontmatter and line.startswith("description:"):
                            desc = line[len("description:") :].strip().strip('"')
            except OSError:
                pass
            skills.append((name, desc))

    if not skills:
        return "暂无已安装的 skills。"

    skills.sort(key=lambda x: x[0])
    lines = ["🛠 **可用 Skills**（发送 `/名称` 即可调用）\n"]
    for name, desc in skills:
        desc_str = f" — {desc}" if desc else ""
        lines.append(f"• `/{name}`{desc_str}")
    return "\n".join(lines)


def _get_usage() -> str:
    """
    发一个轻量 API 请求，从响应 headers 获取 Claude Max 订阅用量百分比和重置时间。
    """
    if sys.platform != "darwin":
        return "❌ /usage 目前只支持 macOS"

    import urllib.request
    import urllib.error
    import ssl

    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                "Claude Code-credentials",
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        creds = json.loads(result.stdout.strip())
        token = creds["claudeAiOauth"]["accessToken"]
    except Exception as e:
        return f"❌ 读取凭证失败：{e}"

    body = json.dumps(
        {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            headers = dict(resp.headers)
    except urllib.error.HTTPError as e:
        headers = dict(e.headers)
    except Exception as e:
        return f"❌ 获取用量失败：{e}"

    def h(key):
        return (
            headers.get(key)
            or headers.get(key.lower())
            or headers.get(key.replace("-", "_"))
        )

    def fmt_pct(val):
        if val is None:
            return "未知"
        pct = float(val) * 100
        bar_len = 20
        filled = round(pct / 100 * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        return f"{bar} {pct:.1f}%"

    def fmt_reset(ts):
        if ts is None:
            return "未知"
        try:
            dt = datetime.fromtimestamp(int(ts))
            now = datetime.now()
            diff = dt - now
            hours = int(diff.total_seconds() // 3600)
            minutes = int((diff.total_seconds() % 3600) // 60)
            return f"{dt.strftime('%m/%d %H:%M')}（{hours}h{minutes}m 后）"
        except Exception:
            return ts

    u5h = h("anthropic-ratelimit-unified-5h-utilization")
    u7d = h("anthropic-ratelimit-unified-7d-utilization")
    r5h = h("anthropic-ratelimit-unified-5h-reset")
    r7d = h("anthropic-ratelimit-unified-7d-reset")
    s5h = h("anthropic-ratelimit-unified-5h-status") or "unknown"
    s7d = h("anthropic-ratelimit-unified-7d-status") or "unknown"

    if u5h is None and u7d is None:
        return "📊 **Usage**\n\n未能获取用量数据（响应中无用量 headers）。"

    lines = ["📊 **Claude Max 用量**\n"]
    lines.append(f"**5小时窗口**（状态：{s5h}）")
    lines.append(f"{fmt_pct(u5h)}")
    lines.append(f"重置时间：{fmt_reset(r5h)}\n")
    lines.append(f"**7天窗口**（状态：{s7d}）")
    lines.append(f"{fmt_pct(u7d)}")
    lines.append(f"重置时间：{fmt_reset(r7d)}")

    return "\n".join(lines)


def _list_mcp() -> str:
    """调用 claude mcp list 获取已配置的 MCP servers"""
    try:
        result = subprocess.run(
            [CLAUDE_CLI, "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = result.stdout.strip()
    except Exception as e:
        return f"❌ 获取 MCP 列表失败：{e}"

    if not output:
        return "暂无已配置的 MCP servers。\n\n用 `claude mcp add` 在终端添加。"

    return f"🔌 **已配置的 MCP Servers**\n\n{output}"


async def _list_directory(
    user_id: str, chat_id: str, store: SessionStore, args: str
) -> str:
    cur = await store.get_current_raw(user_id, chat_id)
    base_dir = cur.get("cwd", DEFAULT_CWD)
    raw_target = args.strip()

    if not raw_target:
        target = base_dir
        display_target = "."
    elif os.path.isabs(raw_target):
        target = os.path.expanduser(raw_target)
        display_target = target
    else:
        target = os.path.abspath(os.path.join(base_dir, os.path.expanduser(raw_target)))
        display_target = raw_target

    if not os.path.exists(target):
        return f"❌ 路径不存在：`{display_target}`\n当前工作目录：`{base_dir}`"

    if not os.path.isdir(target):
        return f"❌ 目标不是目录：`{display_target}`"

    try:
        entries = []
        with os.scandir(target) as it:
            for entry in it:
                suffix = "/" if entry.is_dir() else ""
                entries.append(
                    (not entry.is_dir(), entry.name.lower(), f"`{entry.name}{suffix}`")
                )
    except OSError as e:
        return f"❌ 读取目录失败：{e}"

    entries.sort()
    preview = [item[2] for item in entries[:50]]
    hidden_count = max(0, len(entries) - len(preview))

    lines = [
        "📁 **目录内容**",
        f"请求路径：`{display_target}`",
        f"绝对路径：`{target}`",
    ]
    if not preview:
        lines.append("（空目录）")
        return "\n".join(lines)

    lines.append("")
    lines.extend(preview)
    if hidden_count:
        lines.append("")
        lines.append(f"…… 还有 {hidden_count} 项未显示")
    return "\n".join(lines)


async def _format_workspace_list(
    user_id: str, chat_id: str, store: SessionStore
) -> str:
    cur = await store.get_current_raw(user_id, chat_id)
    current_name = cur.get("workspace", "")
    current_cwd = cur.get("cwd", "~")
    workspaces = store.list_workspaces(user_id)

    lines = ["🗂 **工作空间**"]
    lines.append(
        f"当前绑定：`{current_name}`" if current_name else "当前绑定：（未命名）"
    )
    lines.append(f"当前目录：`{current_cwd}`")

    if workspaces:
        lines.append("")
        lines.append("已保存：")
        for name, path in workspaces.items():
            marker = " ← 当前群组" if name == current_name else ""
            lines.append(f"• `{name}` → `{path}`{marker}")
    else:
        lines.append("")
        lines.append("还没有已保存的工作空间。")

    lines.append("")
    lines.append("用法：")
    lines.append("`/ws save 名称 [路径]` 保存工作空间")
    lines.append("`/ws use 名称` 绑定当前群组到该工作空间")
    lines.append("`/ws set 路径` 直接设置当前群组目录")
    lines.append("`/ws remove 名称` 删除已保存的工作空间")
    return "\n".join(lines)


async def _handle_workspace_command(
    args: str,
    user_id: str,
    chat_id: str,
    store: SessionStore,
) -> str:
    if not args:
        return await _format_workspace_list(user_id, chat_id, store)

    try:
        parts = shlex.split(args)
    except ValueError as e:
        return f"❌ 参数解析失败：{e}"

    if not parts:
        return await _format_workspace_list(user_id, chat_id, store)

    action = parts[0].lower()

    if action in {"list", "ls"}:
        return await _format_workspace_list(user_id, chat_id, store)

    if action in {"save", "add"}:
        if len(parts) < 2:
            return "⚠️ 用法：`/ws save 名称 [路径]`"
        name = parts[1]
        path = (await store.get_current_raw(user_id, chat_id)).get("cwd", DEFAULT_CWD)
        if len(parts) >= 3:
            path = os.path.expanduser(parts[2])
        if not os.path.isdir(path):
            return f"❌ 路径不存在：`{path}`"
        await store.save_workspace(user_id, name, path)
        return f"✅ 已保存工作空间 `{name}` → `{path}`"

    if action == "use":
        if len(parts) != 2:
            return "⚠️ 用法：`/ws use 名称`"
        name = parts[1]
        path = await store.bind_workspace(user_id, chat_id, name)
        if not path:
            return f"❌ 未找到工作空间：`{name}`，先用 `/ws save {name} 路径` 保存。"
        return (
            f"✅ 当前群组已绑定工作空间 `{name}`\n"
            f"工作目录：`{path}`\n"
            "如需清空旧上下文，可继续发送 `/new`。"
        )

    if action == "set":
        if len(parts) != 2:
            return "⚠️ 用法：`/ws set 路径`"
        path = os.path.expanduser(parts[1])
        if not os.path.isdir(path):
            return f"❌ 路径不存在：`{path}`"
        old_name = (await store.get_current_raw(user_id, chat_id)).get("workspace", "")
        await store.set_cwd(user_id, chat_id, path)
        suffix = "，并解除原工作空间绑定" if old_name else ""
        return f"✅ 当前群组工作目录已切换为 `{path}`{suffix}"

    if action in {"remove", "delete", "rm"}:
        if len(parts) != 2:
            return "⚠️ 用法：`/ws remove 名称`"
        name = parts[1]
        if not await store.delete_workspace(user_id, name):
            return f"❌ 未找到工作空间：`{name}`"
        return f"✅ 已删除工作空间 `{name}`"

    return f"❌ 未知子命令：`{action}`\n可用：`list`、`save`、`use`、`set`、`remove`"


async def handle_command(
    cmd: str,
    args: str,
    user_id: str,
    chat_id: str,
    store: SessionStore,
) -> Optional[str]:
    """处理命令，返回回复文本。返回 None 表示不是 bot 命令，应转发给 Claude。"""

    if cmd not in BOT_COMMANDS:
        return None  # 不认识的 /xxx → 转发给 Claude（如 /commit 等 skill）

    if cmd == "ws":
        cmd = "workspace"

    if cmd in ("help", "h"):
        return HELP_TEXT

    elif cmd in ("new", "clear"):
        old_title = await store.new_session(user_id, chat_id)
        if old_title:
            return f"✅ 已开始新 session。\n上个会话：「{old_title}」"
        return "✅ 已开始新 session，之前的对话历史已清除。"

    elif cmd == "resume":
        if not args:
            return await _format_session_list(user_id, chat_id, store)
        # 如果是数字序号，先在合并列表中找到对应 session_id
        try:
            idx = int(args) - 1
            all_sessions = await _build_session_list(user_id, chat_id, store)
            if 0 <= idx < len(all_sessions):
                args = all_sessions[idx]["session_id"]
            else:
                return f"❌ 序号 {int(args)} 超出范围（共 {len(all_sessions)} 条）。"
        except ValueError:
            pass  # 直接用 session ID 字符串
        session_id, old_title = await store.resume_session(user_id, chat_id, args)
        if not session_id:
            return f"❌ 未找到 session：`{args}`，用 `/resume` 查看列表。"
        reply = f"✅ 已恢复 session `{session_id[:8]}...`，继续对话吧。"
        if old_title:
            reply += f"\n上个会话：「{old_title}」"
        return reply

    elif cmd == "model":
        if not args:
            cur = await store.get_current(user_id, chat_id)
            return f"当前模型：`{cur.model}`\n可用：opus / sonnet / haiku 或完整模型 ID"
        model = MODEL_ALIASES.get(args.lower(), args)
        await store.set_model(user_id, chat_id, model)
        return f"✅ 已切换模型为 `{model}`"

    elif cmd == "status":
        cur = await store.get_current_raw(user_id, chat_id)
        sid = cur.get("session_id") or "（新 session）"
        model = cur.get("model", "未知")
        cwd = cur.get("cwd", "~")
        workspace = cur.get("workspace") or "（未绑定）"
        started = cur.get("started_at", "")[:16].replace("T", " ")
        mode = cur.get("permission_mode") or "bypassPermissions"
        return (
            f"📊 **当前 Session 状态**\n"
            f"Session ID: `{sid}`\n"
            f"模型: `{model}`\n"
            f"权限模式: `{mode}`\n"
            f"工作空间: `{workspace}`\n"
            f"工作目录: `{cwd}`\n"
            f"开始时间: {started}"
        )

    elif cmd == "mode":
        if not args:
            cur = await store.get_current(user_id, chat_id)
            current_mode = cur.permission_mode
            lines = [
                f"当前模式：**{current_mode}** — {VALID_MODES.get(current_mode, '')}\n"
            ]
            lines.append("**可选模式：**")
            for mode, desc in VALID_MODES.items():
                marker = " ← 当前" if mode == current_mode else ""
                lines.append(f"• `{mode}` — {desc}{marker}")
            lines.append("\n用 `/mode [模式名]` 切换。")
            return "\n".join(lines)
        mode = MODE_ALIASES.get(args.lower(), args)
        if mode not in VALID_MODES:
            return f"❌ 未知模式：`{args}`\n可选：{', '.join(f'`{m}`' for m in VALID_MODES)}"
        await store.set_permission_mode(user_id, chat_id, mode)
        return f"✅ 已切换为 **{mode}** — {VALID_MODES[mode]}"

    elif cmd == "cd":
        if not args:
            return "⚠️ 用法：`/cd [路径]`"
        path = os.path.expanduser(args)
        if not os.path.isdir(path):
            return f"❌ 路径不存在：`{path}`"
        old_name = (await store.get_current_raw(user_id, chat_id)).get("workspace", "")
        await store.set_cwd(user_id, chat_id, path)
        suffix = "，并解除原工作空间绑定" if old_name else ""
        return f"✅ 工作目录已切换为 `{path}`{suffix}"

    elif cmd == "ls":
        return await _list_directory(user_id, chat_id, store, args)

    elif cmd == "workspace":
        return await _handle_workspace_command(args, user_id, chat_id, store)

    elif cmd == "skills":
        return _list_skills()

    elif cmd == "mcp":
        return _list_mcp()

    elif cmd == "usage":
        return _get_usage()

    elif cmd == "stop":
        return "⏹ /stop 命令在消息队列外处理，如果看到这条说明当前没有运行中的任务。"

    elif cmd == "schedule":
        from scheduler import add_schedule, remove_schedule, list_schedules

        if not args:
            tasks = list_schedules(chat_id)
            if not tasks:
                return (
                    "⏰ 当前没有定时任务。\n\n"
                    "**用法：**\n"
                    "`/schedule HH:MM 任务描述` — 每天固定时间\n"
                    "`/schedule every Xm 任务描述` — 每 X 分钟\n"
                    "`/schedule */5 * * * * 任务描述` — cron 表达式\n\n"
                    "**示例：**\n"
                    "`/schedule 08:00 生成今日工作摘要并发给我`\n"
                    "`/schedule del 任务ID` — 删除任务"
                )
            lines = ["⏰ **定时任务列表**\n"]
            for t in tasks:
                status = "✅" if t.get("enabled") else "❌"
                lines.append(f"{status} `{t['id']}` | `{t['cron']}` | {t['task'][:30]}")
            lines.append("\n删除：`/schedule del 任务ID`")
            return "\n".join(lines)

        parts = args.split(None, 1)
        if parts[0].lower() in ("del", "delete", "rm", "remove"):
            if len(parts) < 2:
                return "⚠️ 用法：`/schedule del 任务ID`"
            success = remove_schedule(parts[1].strip())
            return "✅ 已删除" if success else "❌ 未找到该任务 ID"

        # 解析 cron 表达式（支持 HH:MM、every Xm、标准 5 字段 cron）
        import re

        # every Xm 格式
        m = re.match(r"(every\s+\d+m)\s+(.*)", args, re.DOTALL)
        if m:
            cron_expr, task_prompt = m.group(1).strip(), m.group(2).strip()
        # HH:MM 格式
        elif re.match(r"^\d{1,2}:\d{2}\s+", args):
            cron_expr, _, task_prompt = args.partition(" ")
            task_prompt = task_prompt.strip()
        # 标准 5 字段 cron（以空格分隔的 5 段 + 任务描述）
        else:
            cron_parts = args.split(None, 5)
            if len(cron_parts) >= 6:
                cron_expr = " ".join(cron_parts[:5])
                task_prompt = cron_parts[5]
            else:
                return "⚠️ 格式错误。用法：`/schedule HH:MM 任务内容`"

        if not task_prompt:
            return "⚠️ 任务描述不能为空"

        task_id = add_schedule(chat_id, cron_expr, task_prompt)
        return (
            f"✅ **定时任务已创建**\n"
            f"ID：`{task_id}`\n"
            f"时间：`{cron_expr}`\n"
            f"任务：{task_prompt}"
        )

    elif cmd == "memory":
        from memory_local import (
            read_recent_logs,
            read_pending_candidates,
            MEMORY_FILE,
            SOUL_FILE,
        )

        if args == "soul":
            try:
                return f"**SOUL.md**\n\n{SOUL_FILE.read_text(encoding='utf-8')}"
            except Exception:
                return "❌ 读取 SOUL.md 失败"
        if args == "brain":
            try:
                return f"**MEMORY.md**\n\n{MEMORY_FILE.read_text(encoding='utf-8')}"
            except Exception:
                return "❌ 读取 MEMORY.md 失败"
        # 默认：显示最近日志摘要 + 待晋升候选
        logs = read_recent_logs(days=3)
        candidates = read_pending_candidates()
        parts = ["**本地记忆系统**\n"]
        if logs:
            parts.append(f"**最近日志摘要**\n{logs[:1200]}")
        else:
            parts.append("（暂无日志摘要）")
        if candidates:
            parts.append(f"**待晋升候选**\n{candidates[:800]}")
        parts.append("\n子命令：`/memory soul` | `/memory brain` | `/promote 规则内容`")
        return "\n\n".join(parts)

    elif cmd == "promote":
        if not args:
            from memory_local import read_pending_candidates

            candidates = read_pending_candidates()
            if not candidates:
                return "📭 当前没有待晋升的记忆候选。"
            return f"**待晋升候选**（用 `/promote 规则内容` 确认写入）\n\n{candidates[:1500]}"
        from memory_local import promote_to_memory

        ok = promote_to_memory(args)
        return f"✅ 已写入 MEMORY.md：{args}" if ok else "❌ 写入失败"

    elif cmd == "reindex":
        from indexer import build_index
        import threading

        force = args.strip() == "force"

        def _do():
            try:
                count = build_index(force=force)
                print(f"[reindex] 完成，处理了 {count} 块", flush=True)
            except Exception as e:
                print(f"[reindex] 失败: {e}", flush=True)

        threading.Thread(target=_do, daemon=True).start()
        action = "全量重建" if force else "增量索引"
        return f"✅ 已在后台启动 {action}，完成时会输出日志。"

    elif cmd == "tasks":
        from long_task import list_active_tasks, get_task, get_latest_checkpoint

        rows = list_active_tasks(chat_id)
        if not rows:
            return (
                "📋 **进行中的长任务**\n\n暂无进行中的任务。\n"
                "发送任务类消息（如「帮我做个报告」「分析一下XX」）会 "
                "自动创建任务并保存检查点，崩溃可恢复。"
            )
        lines = ["📋 **进行中的长任务**\n"]
        for i, row in enumerate(rows, 1):
            latest = row["latest_step_desc"]
            latest_str = f"\n  ↳ 当前：{latest[:50]}" if latest else ""
            lines.append(
                f"{i}. `{row['id']}` | {row['checkpoint_count']} 个检查点 | "
                f"{row['description'][:40]}{latest_str}"
            )
        lines.append("\n回复 `/resume-task 任务ID` 继续任务")
        lines.append("回复 `/resume-task del 任务ID` 删除任务")
        return "\n".join(lines)

    elif cmd == "resume-task":
        from long_task import get_task, get_latest_checkpoint, get_checkpoints

        if not args:
            return "⚠️ 用法：`/resume-task 任务ID`\n先 `/tasks` 查看任务 ID 列表。"

        parts = args.split(None, 1)
        task_id_or_del = parts[0].strip()

        if task_id_or_del.lower() == "del":
            if len(parts) < 2:
                return "⚠️ 用法：`/resume-task del 任务ID`"
            from long_task import abandon_task

            ok = abandon_task(parts[1].strip())
            return "✅ 已删除" if ok else "❌ 未找到该任务 ID"
        if not args.strip():
            return "⚠️ 用法：`/resume-task 任务ID`"

        task = get_task(task_id_or_del)
        if not task:
            return f"❌ 未找到任务：`{task_id_or_del}`"
        if task["status"] != "active":
            return f"⚠️ 该任务状态为 `{task['status']}`，无法恢复。"

        checkpoints = get_checkpoints(task_id_or_del)
        cp_count = len(checkpoints)
        latest = get_latest_checkpoint(task_id_or_del)
        latest_step = latest["step"] if latest else 0
        latest_desc = latest["step_desc"] if latest else ""

        await store.set_pending_resume_task(user_id, chat_id, task_id_or_del)

        info = (
            f"✅ **任务已找到**（ID: `{task['id']}`）\n"
            f"描述：{task['description'][:80]}\n"
            f"检查点：共 {cp_count} 个，上次停在步骤 {latest_step}\n"
        )
        if latest_desc:
            info += f"上一步：{latest_desc[:60]}\n"
        info += "\n已注入检查点历史到上下文，继续对话即可延续任务。\n如需放弃：`/resume-task del {task_id_or_del}`"
        return info

    elif cmd == "quota":
        # 查看各模型配额状态和降级情况
        try:
            from quota_tracker import tracker as quota_tracker
            return quota_tracker.get_status()
        except Exception as e:
            return f"❌ 获取配额状态失败: {e}"

    elif cmd == "group-memory":
        # chat_id 以 oc_ 开头为群聊，ou_ 开头为私聊
        if not chat_id.startswith("oc_"):
            return "此命令仅在群聊中可用"
        from group_memory import get_group_status
        return get_group_status(chat_id)

    elif cmd == "contact":
        from contact_memory import get_contact
        if args:
            # 查看指定用户
            data = get_contact(args.strip())
        else:
            # 查看发送者自己
            data = get_contact(user_id)

        if not data or data.get("message_count", 0) == 0:
            return "暂无该联系人的记录"

        lines = [f"**👤 联系人档案**\n"]
        lines.append(f"**姓名**: {data.get('name') or '未知'}")
        lines.append(f"**首次交互**: {data.get('first_seen', '未知')[:10]}")
        lines.append(f"**最近交互**: {data.get('last_seen', '未知')[:10]}")
        lines.append(f"**消息总数**: {data.get('message_count', 0)}")

        if data.get("traits"):
            lines.append(f"\n**特征**: {', '.join(data['traits'][-10:])}")
        if data.get("topics"):
            lines.append(f"**常聊话题**: {', '.join(data['topics'][-10:])}")
        if data.get("preferences"):
            for k, v in data["preferences"].items():
                lines.append(f"**偏好 {k}**: {v}")
        if data.get("notes"):
            lines.append(f"\n**最近备注**: {data['notes'][-1]}")
        if data.get("patterns"):
            lines.append(f"**行为模式**: {', '.join(data['patterns'][-5:])}")

        return "\n".join(lines)

    elif cmd == "instinct":
        from instinct_manager import (
            get_instinct_list, activate, reject as reject_instinct,
            create_instinct, deactivate,
        )
        if not args:
            return get_instinct_list()

        parts = args.strip().split(maxsplit=1)
        subcmd = parts[0].lower()
        subarg = parts[1] if len(parts) > 1 else ""

        if subcmd == "list":
            return get_instinct_list()
        elif subcmd == "approve" and subarg:
            ok = activate(subarg.strip())
            return f"✅ 直觉 `{subarg}` 已激活" if ok else f"❌ 未找到 `{subarg}`"
        elif subcmd == "reject" and subarg:
            ok = reject_instinct(subarg.strip())
            return f"✅ 直觉 `{subarg}` 已拒绝删除" if ok else f"❌ 未找到 `{subarg}`"
        elif subcmd == "deactivate" and subarg:
            ok = deactivate(subarg.strip())
            return f"✅ 直觉 `{subarg}` 已停用" if ok else f"❌ 未找到 `{subarg}`"
        elif subcmd == "create" and "|" in subarg:
            trigger, action = subarg.split("|", 1)
            iid = create_instinct(trigger.strip(), action.strip(), source="manual", status="active")
            return f"✅ 已创建直觉 `{iid}`: 当「{trigger.strip()}」→ {action.strip()}"
        elif subcmd == "evolve":
            from instinct_manager import evolve_instincts
            return evolve_instincts()
        else:
            return ("用法:\n"
                    "  `/instinct` — 查看所有\n"
                    "  `/instinct approve <id>` — 审批通过\n"
                    "  `/instinct reject <id>` — 拒绝删除\n"
                    "  `/instinct deactivate <id>` — 停用\n"
                    "  `/instinct create <触发词> | <期望行为>` — 创建新直觉\n"
                    "  `/instinct evolve` — 将相似直觉聚类为技能 SOP")

    elif cmd == "review":
        if not args:
            return ("用法: `/review <文本>` — 审查指定内容\n"
                    "也可在群聊中回复其他 Bot 的消息并输入 `/review`")
        from review_mode import review_output, format_review
        review = review_output(args, source="manual")
        return format_review(review, source="手动审查")

    elif cmd == "install":
        from capability_installer import list_proposals, execute_install, reject_install, propose_install
        if not args:
            return list_proposals()
        parts = args.strip().split(maxsplit=1)
        subcmd = parts[0].lower()
        subarg = parts[1] if len(parts) > 1 else ""
        if subcmd == "list":
            return list_proposals(status=subarg if subarg else None)
        elif subcmd == "approve" and subarg:
            return execute_install(subarg.strip())
        elif subcmd == "reject" and subarg:
            return reject_install(subarg.strip())
        elif subcmd == "propose" and "|" in subarg:
            action, target = subarg.split("|", 1)
            return json.dumps(propose_install(action.strip(), target.strip()), ensure_ascii=False)
        else:
            return ("用法:\n"
                    "  `/install` — 查看提案列表\n"
                    "  `/install approve <id>` — 批准执行\n"
                    "  `/install reject <id>` — 拒绝\n"
                    "  `/install propose <action> | <target>` — 提交提案")

    elif cmd == "skill-create":
        from skill_creator import create_skill_from_git
        repo = args.strip() if args else os.path.expanduser("~/feishu-claude-code")
        return create_skill_from_git(repo_path=repo)

    elif cmd == "skills":
        from skill_creator import list_skills
        return list_skills()

    else:
        return None  # fallback: 转发给 Claude
