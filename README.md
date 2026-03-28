# feishu-claude-code

在飞书里直接和你本机的 Claude Code 对话。

WebSocket 长连接，流式卡片输出，手机上随时 code review、debug、问问题。

> 复用 Claude Max/Pro 订阅，不需要 API Key，不需要公网 IP。

## 特性

**核心能力**

- **流式卡片输出** — Claude 边想边输出，工具调用进度实时可见，不是等半天发一坨
- **Session 跨设备** — 手机上开始的对话，回到电脑前接着聊；CLI 终端的会话也能在飞书恢复
- **图片识别** — 直接发截图给 Claude 分析
- **斜杠命令** — 切换模型、恢复会话、查看用量、管理工作目录
- **Skills 透传** — `/commit`、`/review` 等 Claude Code Skills 直接在飞书里用

**三层本地记忆系统**

- **Layer 1 每日日志** — 每次对话自动写入 `~/.feishu-claude/memory/YYYY-MM-DD.md`
- **Layer 2 每日复盘** — 每晚 23:30 自动用 Claude 生成摘要，提取晋升候选
- **Layer 3 长期记忆** — `MEMORY.md` + `SOUL.md`，每次对话注入，用 `/promote` 确认晋升
- **自学习** — 检测用户纠正信号，自动写入 `ERRORS.md`，不重复犯同类错误
- **命令**：`/memory` 查看记忆状态，`/promote 规则` 确认写入长期记忆

**群聊多 Bot 协作**

- 拉多个 AI Bot 进同一飞书群，@谁谁回复
- Claude bot 回复中含 `@BotName` 时自动向对方 Bot 发送真实 @mention
- 群聊历史注入：每次 @Claude 时自动拉取最近 50 条群消息作为上下文（含其他 Bot 的卡片回复）
- 支持消息 ID 去重，不重复注入已有历史

**共享记忆（多 Bot 共享同一知识库）**

- 本地记忆文件通过软链接共享给其他 Bot（`CLAUDE_MEMORY.md`、`CLAUDE_ERRORS.md`）
- 可选接入 mem0 云记忆，多 Bot 跨命名空间搜索同一用户的积累

**部署简单**

- **无需公网 IP** — 飞书 WebSocket 长连接，部署在家里的 Mac 上就行
- **零额外成本** — 直接调用本机 `claude` CLI，复用已有订阅
- **看门狗自愈** — 4 小时自动重启，防止 WebSocket 连接假死
- **守护进程** — macOS launchd / Linux systemd，开机自启，崩溃自动拉起

## 快速开始

### 前置条件

| 依赖 | 最低版本 | 验证命令 |
|------|---------|---------|
| Python | 3.11+ | `python3 --version` |
| Claude Code CLI | 最新 | `claude --version` |
| Claude Max/Pro 订阅 | — | `claude "hi"` 能正常回复 |

### 安装与启动

```bash
git clone https://github.com/joewongjc/feishu-claude-code.git
cd feishu-claude-code

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，填入飞书应用凭证（见下方「飞书应用配置」）

python main.py
```

预期输出：

```
🚀 飞书 Claude Bot 启动中...
   App ID      : cli_xxx...
✅ 连接飞书 WebSocket 长连接（自动重连）...
```

> 从旧版升级的用户可运行 `python migrate_sessions.py` 迁移 session 数据（会自动备份）。

## 命令速查

**会话管理**

| 命令 | 说明 |
|------|------|
| `/new` | 开始新 session |
| `/resume` | 查看历史 sessions |
| `/resume 序号` | 恢复指定 session |
| `/stop` | 停止当前正在运行的任务 |
| `/status` | 当前 session 信息 |

**模型与模式**

| 命令 | 说明 |
|------|------|
| `/model opus` | 切换模型 (opus / sonnet / haiku) |
| `/mode bypass` | 切换权限模式 |

**工作目录**

| 命令 | 说明 |
|------|------|
| `/cd ~/project` | 切换工作目录 |
| `/ls` | 查看当前工作目录内容 |
| `/ws save 名称 路径` | 保存命名工作空间 |
| `/ws use 名称` | 绑定当前群组/私聊到工作空间 |

**记忆系统**

| 命令 | 说明 |
|------|------|
| `/memory` | 查看近期日志摘要和待晋升候选 |
| `/memory brain` | 查看长期记忆 MEMORY.md |
| `/memory soul` | 查看行为原则 SOUL.md |
| `/promote 规则` | 确认将规则写入长期记忆 |

**定时任务**

| 命令 | 说明 |
|------|------|
| `/schedule 08:00 任务描述` | 每天固定时间执行 |
| `/schedule every 30m 任务` | 每 N 分钟执行 |
| `/schedule` | 查看所有定时任务 |
| `/schedule del 任务ID` | 删除定时任务 |

**信息查询**

| 命令 | 说明 |
|------|------|
| `/usage` | 查看 Claude Max 用量 (macOS) |
| `/skills` | 列出 Claude Skills |
| `/mcp` | 列出 MCP Servers |
| `/help` | 帮助 |

**Skills 透传**

`/commit` 等未注册的斜杠命令会直接转发给 Claude CLI 执行。

## 架构

```
┌──────────┐  WebSocket  ┌──────────────────────┐  subprocess  ┌────────────┐
│  飞书 App │◄───────────►│   feishu-claude       │─────────────►│ claude CLI │
│  (用户)   │  长连接      │   (main.py)           │ stream-json  │  (本机)    │
└──────────┘             └──────────────────────┘              └────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
             session_store    memory_local    memory_bridge
             (SQLite 会话)   (三层本地记忆)  (mem0+OpenClaw)
```

**记忆系统目录结构**

```
~/.feishu-claude/
  brain/
    MEMORY.md       # 长期稳定记忆（用户确认后写入）
    SOUL.md         # 行为原则
  memory/
    YYYY-MM-DD.md              # 每日对话日志
    YYYY-MM-DD-daily-summary.md  # 每日复盘摘要（自动生成）
    YYYY-MM-DD-promotion-candidates.md  # 晋升候选
  learnings/
    LEARNINGS.md    # 经验教训
    ERRORS.md       # 用户纠正记录（自动写入）
```

## 飞书应用配置

### 1. 创建应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app)，点击「创建企业自建应用」
2. 填写应用名称（如 `Claude Code`），选择图标，点击创建

### 2. 添加机器人能力

1. 进入应用详情，左侧菜单选择「添加应用能力」
2. 添加「机器人」能力

### 3. 开启权限

进入「权限管理」页面，搜索并开启以下权限：

| 权限 scope | 说明 |
|-----------|------|
| `im:message` | 获取与发送单聊、群组消息 |
| `im:message:send_as_bot` | 以应用的身份发送消息 |
| `im:resource` | 获取消息中的资源文件（图片等） |
| `im:message:group:readonly` | 获取群组中所有消息（群聊历史注入需要） |

### 4. 启用长连接模式

1. 左侧菜单「事件与回调」→「事件配置」
2. 订阅方式选择「使用长连接接收事件」（不是 Webhook）
3. 添加事件：`im.message.receive_v1`（接收消息）

### 5. 获取凭证

1. 进入「凭证与基础信息」页面
2. 复制 App ID 和 App Secret，填入 `.env` 文件

### 6. 发布应用

1. 点击「版本管理与发布」→「创建版本」
2. 填写版本号和更新说明，提交审核
3. 管理员在飞书管理后台审核通过后即可使用

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|:---:|-------|------|
| `FEISHU_APP_ID` | 是 | — | 飞书应用 App ID |
| `FEISHU_APP_SECRET` | 是 | — | 飞书应用 App Secret |
| `DEFAULT_MODEL` | 否 | `claude-sonnet-4-6` | 默认使用的 Claude 模型 |
| `DEFAULT_CWD` | 否 | `~` | Claude CLI 的默认工作目录 |
| `PERMISSION_MODE` | 否 | `bypassPermissions` | 工具权限模式 |
| `STREAM_CHUNK_SIZE` | 否 | `20` | 流式推送的字符积累阈值 |
| `CLAUDE_CLI_PATH` | 否 | 自动查找 | Claude CLI 可执行文件路径 |
| `MEM0_API_KEY` | 否 | — | mem0 云记忆 API Key（不填则仅用本地记忆） |
| `MEM0_USER_ID` | 否 | — | mem0 用户 ID |

## 群聊多 Bot 配置

编辑 `bot_config.py`，填入群成员的 open_id（可从飞书事件日志获取）：

```python
# 群成员显示名映射（用于历史消息标注发言人）
GROUP_KNOWN_NAMES = {
    "ou_xxxx": "Claude Bot",
    "ou_yyyy": "其他 Bot",
    "ou_zzzz": "用户昵称",
}

# 其他 Bot 的触发关键词（Claude 回复中含关键词时自动 @对方）
GROUP_BOTS = {
    "@BotB": ("ou_yyyy", "BotB 显示名"),
}
```

## 持久化部署

### macOS (launchctl)

```bash
cp deploy/feishu-claude.plist ~/Library/LaunchAgents/com.feishu-claude.bot.plist
# 修改 plist 中的 /path/to/ 为实际路径

launchctl load ~/Library/LaunchAgents/com.feishu-claude.bot.plist
launchctl list | grep feishu-claude
tail -f bot.log
```

### Linux (systemd)

```bash
sudo cp deploy/feishu-claude.service /etc/systemd/system/
# 修改 service 中的路径和 User

sudo systemctl daemon-reload
sudo systemctl enable feishu-claude
sudo systemctl start feishu-claude
journalctl -u feishu-claude -f
```

---

## English

**feishu-claude-code** bridges your local Claude Code CLI with Feishu/Lark messenger via WebSocket, with a built-in multi-tier memory system and multi-bot group collaboration.

**Key features:**
- No public IP needed (Feishu WebSocket long connection)
- Streaming card output (real-time typing effect with tool call progress)
- Reuses Claude Max/Pro subscription (no API key required)
- Full session management across devices
- **3-tier local memory system** — daily logs → daily review → long-term MEMORY.md, with self-learning from user corrections
- **Multi-bot group chat** — multiple AI bots in one Feishu group, @mention routing, shared group history injection (50 msgs), cross-bot message deduplication
- **Shared memory** — symlinked brain files for cross-bot knowledge sharing; optional mem0 cloud memory
- Image recognition, slash commands, Claude Skills passthrough, scheduled tasks

Quick start: clone, `pip install -r requirements.txt`, configure `.env`, run `python main.py`.

See the Chinese sections above for detailed setup instructions.

## License

[MIT](LICENSE)
