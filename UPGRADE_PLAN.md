# 飞书 Bot 升级执行计划

> **执行说明**：本计划由 Claude Opus 4.6 规划，由 Claude Sonnet 4.6 逐步执行。
> 每完成一个 Phase 请打勾确认，遇到错误立即停止并报告。

---

## 环境现状（执行前必读）

| 项目 | 路径/值 |
|------|---------|
| Bot 根目录 | `/Users/wuxianbaoshi/feishu-claude-code/` |
| Python venv | `/Users/wuxianbaoshi/feishu-claude-code/.venv/bin/python` |
| Bot 主程序 | `/Users/wuxianbaoshi/feishu-claude-code/main.py` |
| 飞书 App ID | `cli_a94f54d23fb81cd4` |
| 飞书 App Secret | `WnnzLKfeCbGUze1T6GADIdwnJGc0h1pl` |
| Obsidian 知识库 | `/Users/wuxianbaoshi/Documents/Obsidian/知识库` |
| Claude 全局设置 | `~/.claude/settings.json` |
| MCP 配置文件 | `~/.claude/mcp.json` |
| 已有 MCP | `xhs-pro-mcp` |
| Claude 版本 | `2.1.83` |

---

## Phase 1：持续在线（launchd 守护进程）

**目标**：Bot 开机自启，崩溃自动重启，永久在线。

### Step 1.1 — 停止当前手动运行的 Bot

```bash
# 找到当前进程并停止
pkill -f "main.py" || true
sleep 2
# 确认已停止
ps aux | grep "main.py" | grep -v grep || echo "已停止"
```

### Step 1.2 — 创建 launchd plist 文件

创建文件 `~/Library/LaunchAgents/com.feishu-claude.bot.plist`，内容如下：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.feishu-claude.bot</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/wuxianbaoshi/feishu-claude-code/.venv/bin/python</string>
        <string>/Users/wuxianbaoshi/feishu-claude-code/main.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/wuxianbaoshi/feishu-claude-code</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>/Users/wuxianbaoshi/feishu-claude-code/bot.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/wuxianbaoshi/feishu-claude-code/bot.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>/Users/wuxianbaoshi</string>
    </dict>
</dict>
</plist>
```

### Step 1.3 — 加载并启动服务

```bash
# 加载服务（开机自启 + 立刻启动）
launchctl load ~/Library/LaunchAgents/com.feishu-claude.bot.plist

# 等待 3 秒后验证
sleep 3
launchctl list | grep feishu-claude
tail -20 ~/feishu-claude-code/bot.log
```

**验证标准**：log 中出现 `connected to wss://msg-frontier.feishu.cn` 即为成功。

### 常用管理命令（记录备用）

```bash
# 停止
launchctl unload ~/Library/LaunchAgents/com.feishu-claude.bot.plist
# 重启
launchctl unload ~/Library/LaunchAgents/com.feishu-claude.bot.plist && launchctl load ~/Library/LaunchAgents/com.feishu-claude.bot.plist
# 查看状态
launchctl list | grep feishu-claude
# 查看日志
tail -f ~/feishu-claude-code/bot.log
```

---

## Phase 2：强大记忆（三层记忆架构）

**目标**：跨会话记忆不丢失，Claude 能回忆起所有历史对话。

### Step 2.1 — 安装 claude-mem 插件

在终端（不是飞书）执行 Claude Code 命令：

```bash
claude "/plugin marketplace add thedotmack/claude-mem"
claude "/plugin install claude-mem"
```

**或者**在 Claude Code 交互界面输入：
```
/plugin marketplace add thedotmack/claude-mem
/plugin install claude-mem
```

**验证**：
```bash
ls ~/.claude/plugins/ | grep claude-mem
```
出现 `claude-mem` 目录即为成功。

### Step 2.2 — 创建全局 CLAUDE.md

创建文件 `~/.claude/CLAUDE.md`，这是用户级别的永久记忆，所有会话都会读取：

```markdown
# 用户基本信息

## 身份
- 内容创作者 + 一人公司，主攻小红书平台
- 业务：空间香氛、电商运营、市场调研
- 沟通语言：中文

## 工作目录
- 主工作目录：/Users/wuxianbaoshi/claude_code
- 飞书 Bot 目录：/Users/wuxianbaoshi/feishu-claude-code
- Obsidian 知识库：/Users/wuxianbaoshi/Documents/Obsidian/知识库

## 技术栈
- 主力 AI：Claude Pro 订阅（非 Max，非 API Key）
- 通信平台：飞书（已配置 Bot）
- 知识库工具：Obsidian
- 已有 MCP：xhs-pro-mcp（小红书数据采集）

## 飞书 Bot 信息
- App ID：cli_a94f54d23fb81cd4
- Bot 代码位置：/Users/wuxianbaoshi/feishu-claude-code/
- 通过 launchd 守护进程运行，开机自启

## 工作风格偏好
- 结果导向，不需要过多解释
- 执行前先确认高风险操作
- 回复用中文
- 更新知识库时直接执行 ingest.py，无需确认

## 知识库接入
- Obsidian vault 路径：/Users/wuxianbaoshi/Documents/Obsidian/知识库
- 可直接 Read 该目录下的 .md 文件进行知识检索
- 知识库结构：01-输入源/ 02-洞察/ 03-经验/ 04-项目/ 05-模板/ 06-素材/

## 记忆管理规则
- 每次对话结束时，如果学到了新的用户偏好或项目信息，主动更新本文件
- 重要决策和原因要记录在对应项目的 CLAUDE.md 里
- 失败的方案要注明原因，避免重复踩坑
```

### Step 2.3 — 验证记忆系统

重启一个新的 Claude Code 会话，确认：
1. claude-mem 插件自动加载（session 启动时有提示）
2. CLAUDE.md 内容被读取（第一条消息 Claude 知道用户背景）

---

## Phase 3：飞书生态调用（官方 MCP）

**目标**：让 Claude 能操作飞书云文档、多维表格、发消息等。

### Step 3.1 — 安装飞书官方 MCP

```bash
# 验证 node 可用
node --version
npx --version

# 测试飞书 MCP 是否能运行（会自动下载）
npx -y @larksuiteoapi/lark-mcp mcp \
  -a cli_a94f54d23fb81cd4 \
  -s WnnzLKfeCbGUze1T6GADIdwnJGc0h1pl \
  --help 2>&1 | head -20
```

### Step 3.2 — 注册到 Claude MCP 配置

读取现有 `~/.claude/mcp.json`，在 `mcpServers` 下**追加** `feishu-lark` 条目（保留原有的 `xhs-pro-mcp`）：

```json
{
  "mcpServers": {
    "xhs-pro-mcp": {
      "command": "node",
      "args": ["/Users/wuxianbaoshi/openclaw/workspace/projects_data/xhs-pro-mcp/dist/index.js"]
    },
    "feishu-lark": {
      "command": "npx",
      "args": [
        "-y",
        "@larksuiteoapi/lark-mcp",
        "mcp",
        "-a", "cli_a94f54d23fb81cd4",
        "-s", "WnnzLKfeCbGUze1T6GADIdwnJGc0h1pl"
      ]
    }
  }
}
```

**重要**：这里只修改 `~/.claude/mcp.json`，不动 `~/.claude/settings.json`。

### Step 3.3 — 验证飞书 MCP

```bash
# 重新启动 Claude Code 后检查 MCP 列表
claude mcp list
```

应该能看到 `feishu-lark` 出现在列表中。

### Step 3.4 — 在飞书 Bot 中测试

在飞书给 Bot 发送：
```
帮我用飞书 MCP 列出我的云文档列表
```
Claude 应该能调用飞书 API 返回结果。

---

## Phase 4：知识库接入（Obsidian）

**目标**：Claude 能直接检索 Obsidian 知识库内容。

### Step 4.1 — 直接读取方式（无需额外工具）

由于 CLAUDE.md 已经告知知识库路径，Claude 可以直接用 Read/Glob/Grep 工具检索。

在飞书测试：
```
帮我在知识库里搜索关于小红书爆款的内容
```
Claude 会自动 Grep `/Users/wuxianbaoshi/Documents/Obsidian/知识库` 目录。

### Step 4.2 — 添加知识库快捷命令（可选增强）

在 `~/.claude/commands/` 目录创建文件 `search-kb.md`：

```markdown
---
description: 搜索 Obsidian 知识库
---

在知识库 /Users/wuxianbaoshi/Documents/Obsidian/知识库 中搜索以下内容：

$ARGUMENTS

使用 Grep 工具进行全文搜索，返回最相关的 5 条结果，包含文件名和匹配段落。
```

之后在飞书发 `/search-kb 空间香氛` 即可直接搜索。

---

## Phase 5：定时任务

**目标**：Bot 能执行定时任务，如每日摘要、定时提醒等。

### Step 5.1 — 创建 scheduler.py 模块

在 `/Users/wuxianbaoshi/feishu-claude-code/` 目录创建 `scheduler.py`：

```python
"""
简单的持久化定时任务调度器。
任务存储在 ~/.feishu-claude/schedules.json
支持 cron 表达式和一次性延时任务。
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

SCHEDULES_FILE = os.path.expanduser("~/.feishu-claude/schedules.json")


def _load_schedules() -> list[dict]:
    if not os.path.exists(SCHEDULES_FILE):
        return []
    try:
        with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_schedules(schedules: list[dict]):
    os.makedirs(os.path.dirname(SCHEDULES_FILE), exist_ok=True)
    with open(SCHEDULES_FILE, "w", encoding="utf-8") as f:
        json.dump(schedules, f, ensure_ascii=False, indent=2)


def add_schedule(chat_id: str, cron_expr: str, task: str) -> str:
    """添加一个定时任务，返回任务 ID。"""
    schedules = _load_schedules()
    task_id = f"task_{int(time.time())}"
    schedules.append({
        "id": task_id,
        "chat_id": chat_id,
        "cron": cron_expr,
        "task": task,
        "created_at": datetime.now().isoformat(),
        "enabled": True,
    })
    _save_schedules(schedules)
    return task_id


def remove_schedule(task_id: str) -> bool:
    """删除定时任务，返回是否成功。"""
    schedules = _load_schedules()
    new = [s for s in schedules if s["id"] != task_id]
    if len(new) == len(schedules):
        return False
    _save_schedules(new)
    return True


def list_schedules(chat_id: Optional[str] = None) -> list[dict]:
    """列出定时任务（可按 chat_id 过滤）。"""
    schedules = _load_schedules()
    if chat_id:
        return [s for s in schedules if s["chat_id"] == chat_id]
    return schedules


def _cron_matches(cron_expr: str, now: datetime) -> bool:
    """
    简化的 cron 匹配，支持：
    - "every Xm" — 每 X 分钟
    - "HH:MM"   — 每天固定时间
    - "*/5 * * * *" 等标准 5 字段 cron（分 时 日 月 周）
    """
    cron_expr = cron_expr.strip()

    # 每 X 分钟
    m = re.match(r"every\s+(\d+)m", cron_expr)
    if m:
        interval = int(m.group(1))
        return now.minute % interval == 0 and now.second < 60

    # 每天固定时间 HH:MM
    m = re.match(r"^(\d{1,2}):(\d{2})$", cron_expr)
    if m:
        return now.hour == int(m.group(1)) and now.minute == int(m.group(2))

    # 标准 5 字段 cron（分 时 日 月 周）
    parts = cron_expr.split()
    if len(parts) != 5:
        return False

    def _match_field(field: str, value: int, min_v: int, max_v: int) -> bool:
        if field == "*":
            return True
        if field.startswith("*/"):
            step = int(field[2:])
            return value % step == 0
        if "-" in field:
            a, b = field.split("-")
            return int(a) <= value <= int(b)
        return int(field) == value

    minute, hour, day, month, weekday = parts
    return (
        _match_field(minute, now.minute, 0, 59)
        and _match_field(hour, now.hour, 0, 23)
        and _match_field(day, now.day, 1, 31)
        and _match_field(month, now.month, 1, 12)
        and _match_field(weekday, now.weekday(), 0, 6)
    )


async def run_scheduler(on_trigger: Callable[[str, str], None]):
    """
    后台循环，每分钟整点检查一次触发。
    on_trigger(chat_id, task_prompt) 由调用方实现（发消息给 Claude）。
    """
    print("[scheduler] 定时任务调度器已启动", flush=True)
    while True:
        # 等到下一分钟整点
        now = datetime.now()
        seconds_to_next = 60 - now.second
        await asyncio.sleep(seconds_to_next)

        now = datetime.now()
        schedules = _load_schedules()
        for s in schedules:
            if not s.get("enabled", True):
                continue
            try:
                if _cron_matches(s["cron"], now):
                    print(f"[scheduler] 触发任务 {s['id']}: {s['task'][:40]}", flush=True)
                    await asyncio.coroutine(on_trigger)(s["chat_id"], s["task"]) \
                        if asyncio.iscoroutinefunction(on_trigger) \
                        else on_trigger(s["chat_id"], s["task"])
            except Exception as e:
                print(f"[scheduler] 任务 {s['id']} 触发失败: {e}", flush=True)
```

### Step 5.2 — 集成到 main.py

在 `main.py` 中的 `main()` 函数里，在启动看门狗之后、`ws_client.start()` 之前，加入调度器启动代码：

**在 main.py 的 `main()` 函数末尾，`ws_client.start()` 之前插入：**

```python
    # 启动定时任务调度器
    from scheduler import run_scheduler

    async def _scheduler_trigger(chat_id: str, task_prompt: str):
        """定时任务触发时，模拟一条用户消息发给 Claude"""
        session = await store.get_current("__scheduler__", chat_id)
        card_msg_id = await feishu.send_card_to_user(chat_id, loading=True)
        # 简单调用 Claude，结果发回对应 chat
        from claude_runner import run_claude
        full_text, new_session_id, _ = await run_claude(
            message=task_prompt,
            session_id=session.session_id,
            model=session.model,
            cwd=session.cwd,
            permission_mode=session.permission_mode,
        )
        await feishu.update_card(card_msg_id, full_text or "（定时任务无输出）")
        if new_session_id:
            await store.on_claude_response("__scheduler__", chat_id, new_session_id, task_prompt)

    import threading
    def _start_scheduler():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_scheduler(_scheduler_trigger))

    sched_thread = threading.Thread(target=_start_scheduler, daemon=True)
    sched_thread.start()
```

### Step 5.3 — 在 commands.py 中添加 /schedule 命令

在 `BOT_COMMANDS` 集合中添加 `"schedule"`，并在 `handle_command` 中添加处理逻辑：

```python
# BOT_COMMANDS 集合中加入：
"schedule"

# handle_command 中加入：
elif cmd == "schedule":
    from scheduler import add_schedule, remove_schedule, list_schedules
    if not args:
        # 列出当前 chat 的所有定时任务
        tasks = list_schedules(chat_id)
        if not tasks:
            return "⏰ 当前没有定时任务。\n\n用法：`/schedule [cron表达式] [任务描述]`\n例如：`/schedule 08:00 生成今日工作摘要`"
        lines = ["⏰ **定时任务列表**\n"]
        for t in tasks:
            status = "✅" if t.get("enabled") else "❌"
            lines.append(f"{status} `{t['id']}` | {t['cron']} | {t['task'][:30]}")
        lines.append("\n删除：`/schedule del 任务ID`")
        return "\n".join(lines)

    parts = args.split(None, 1)
    if parts[0].lower() in ("del", "delete", "rm", "remove") and len(parts) > 1:
        success = remove_schedule(parts[1].strip())
        return "✅ 已删除" if success else "❌ 未找到该任务 ID"

    if len(parts) < 2:
        return "⚠️ 用法：`/schedule [时间/cron] [任务内容]`\n例：`/schedule 08:00 给我生成今日计划`"

    cron_expr = parts[0]
    task_prompt = parts[1]
    task_id = add_schedule(chat_id, cron_expr, task_prompt)
    return f"✅ 定时任务已创建\nID：`{task_id}`\n时间：`{cron_expr}`\n任务：{task_prompt}"
```

---

## Phase 6：自主学习与进化

**目标**：Bot 能从每次对话中学习，自动积累经验，越用越聪明。

### Step 6.1 — 在全局 CLAUDE.md 末尾追加学习规则

在 `~/.claude/CLAUDE.md` 末尾追加：

```markdown

---

## 自主学习规则（Claude 必须遵守）

### 何时更新记忆
以下情况发生时，必须主动更新 ~/.claude/CLAUDE.md 或对应项目的 CLAUDE.md：
1. 用户纠正了我的某个行为 → 记录"避免 XXX，原因：XXX"
2. 用户确认某个方案很好 → 记录"XXX 方案有效，适用场景：XXX"
3. 发现某个工具/API 的限制 → 记录限制和绕过方法
4. 完成了一个新项目/任务 → 在项目 CLAUDE.md 里记录关键决策

### 学习格式（追加到本文件末尾的"经验积累"章节）
```
日期 YYYY-MM-DD
场景：XXX
学到了：XXX
下次应该：XXX
```

### 不能做的事（踩过的坑）
- 不要使用 OpenClaw（已放弃，原因记录在 Obsidian 笔记中）
- 不要在未确认的情况下删除文件或重置 Git

---

## 经验积累

<!-- Claude 在此处追加学习记录 -->
```

### Step 6.2 — 创建项目级 CLAUDE.md

创建 `/Users/wuxianbaoshi/feishu-claude-code/CLAUDE.md`：

```markdown
# 飞书 Bot 项目说明

## 架构
- main.py：飞书 WebSocket 接收 → 调用 claude CLI subprocess → 回复飞书卡片
- claude_runner.py：调用 `claude --print --output-format stream-json`
- session_store.py：SQLite 持久化会话（存在 ~/.feishu-claude/）
- scheduler.py：定时任务调度器
- commands.py：/xxx 斜杠命令处理

## 维护命令
```bash
# 查看运行状态
launchctl list | grep feishu-claude

# 重启 Bot
launchctl unload ~/Library/LaunchAgents/com.feishu-claude.bot.plist
launchctl load ~/Library/LaunchAgents/com.feishu-claude.bot.plist

# 查看日志
tail -f ~/feishu-claude-code/bot.log
```

## 注意事项
- Bot 使用 Claude Pro 订阅（通过 claude login），不使用 API Key
- 所有飞书凭证在 .env 文件中，不要提交到 git
- 修改 main.py 或 commands.py 后需要重启 Bot 才生效
- MCP 配置在 ~/.claude/mcp.json，feishu-lark MCP 已配置

## 已知问题
- WebSocket 连接每 4 小时会主动重启（watchdog 机制，正常）
- 群聊只响应 @机器人 的消息

## 变更记录
<!-- 在此记录重要变更 -->
```

---

## 执行顺序与依赖关系

```
Phase 1（launchd）← 必须最先做，确保 Bot 稳定运行
    ↓
Phase 2（记忆）  ← 最重要，影响 Claude 智能程度
    ↓
Phase 3（飞书 MCP）← 依赖 Node.js 环境
    ↓
Phase 4（知识库）← 依赖 Phase 2 的 CLAUDE.md
    ↓
Phase 5（定时任务）← 需要修改代码，风险最高
    ↓
Phase 6（自学习）← 配置性工作，无代码风险
```

---

## 验收标准

全部完成后，在飞书发以下消息验证：

1. `/status` — 应显示当前 session 信息
2. `/mcp` — 应显示 `feishu-lark` 和 `xhs-pro-mcp`
3. `你知道我是谁吗？` — Claude 应能描述用户身份（内容创作者、小红书等）
4. `搜索我知识库里关于小红书的内容` — Claude 应能读取 Obsidian 文件
5. `/schedule 08:00 生成今日工作摘要` — 应成功创建定时任务
6. 重启 Mac 后 Bot 应自动启动

---

## 回滚方案

如果某个 Phase 出错：

- **Phase 1 回滚**：`launchctl unload ~/Library/LaunchAgents/com.feishu-claude.bot.plist && cd ~/feishu-claude-code && source .venv/bin/activate && nohup python3 main.py > bot.log 2>&1 &`
- **Phase 3 回滚**：从 `~/.claude/mcp.json` 中删除 `feishu-lark` 条目
- **Phase 5 回滚**：`git checkout main.py commands.py`（如果有 git），或删除 scheduler.py 并还原 main.py 和 commands.py

---

*计划版本：v1.0 | 规划时间：2026-03-27 | 规划模型：Claude Opus 4.6*
