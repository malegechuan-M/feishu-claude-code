# 飞书 Bot 项目说明

## 架构
- `main.py`：飞书 WebSocket 接收 → 调用 claude CLI subprocess → 回复飞书卡片
- `claude_runner.py`：调用 `claude --print --output-format stream-json`
- `session_store.py`：SQLite 持久化会话（存在 ~/.feishu-claude/）
- `scheduler.py`：定时任务调度器（任务存储在 ~/.feishu-claude/schedules.json）
- `commands.py`：/xxx 斜杠命令处理
- `memory_bridge.py`：共享记忆桥接层（mem0 云记忆 + OpenClaw FTS5 + workspace 文件搜索）

## 运行方式
通过 launchd 守护进程管理，开机自启，崩溃自动重启。

## 维护命令
```bash
# 查看运行状态
launchctl list | grep feishu-claude

# 重启 Bot
launchctl unload ~/Library/LaunchAgents/com.feishu-claude.bot.plist
launchctl load ~/Library/LaunchAgents/com.feishu-claude.bot.plist

# 查看实时日志
tail -f ~/feishu-claude-code/bot.log
```

## 飞书 Bot 命令列表
| 命令 | 功能 |
|------|------|
| `/help` | 查看所有命令 |
| `/new` | 新建会话 |
| `/resume` | 查看/恢复历史会话 |
| `/model opus/sonnet/haiku` | 切换模型 |
| `/status` | 查看当前会话状态 |
| `/schedule HH:MM 任务` | 创建定时任务 |
| `/schedule` | 查看定时任务列表 |
| `/mcp` | 查看已配置的 MCP |
| `/search-kb 关键词` | 搜索 Obsidian 知识库 |
| `/usage` | 查看 Claude 用量 |

## 注意事项
- Bot 使用 Claude Pro 订阅（通过 claude login），不使用 API Key
- 所有飞书凭证在 .env 文件中，不要提交到 git
- 修改 main.py 或 commands.py 后需要重启 Bot 才生效
- MCP 配置：feishu-lark（飞书官方）、xhs-pro-mcp（小红书采集）

## 已知问题
- WebSocket 连接每 4 小时会主动重启（watchdog 机制，正常）
- 群聊只响应 @机器人 的消息
- 定时任务的 chat_id 需要是用户 open_id（私聊）或群 chat_id

## 共享记忆系统（与 OpenClaw 协作）
- **mem0 云记忆**：与 OpenClaw 共享同一个 mem0 账号
  - Claude 命名空间：`wuxianbaoshi:agent:claude-feishu`
  - OpenClaw 命名空间：`wuxianbaoshi:agent:agent-a-coo` 等
  - 搜索时同时查 Claude 和 OpenClaw 的命名空间
- **OpenClaw FTS5**：搜索 OpenClaw 的 SQLite 全文索引（`~/.openclaw/memory/agent-a-coo.sqlite`）
- **OpenClaw workspace**：关键词匹配 `/Users/wuxianbaoshi/openclaw/workspace/memory/*.md`
- **消息流**：用户消息 → recall_all() → 注入上下文 → Claude → capture_memory() → mem0

## 飞书群协作（Claude Bot + OpenClaw Bot）
- Claude Bot open_id：`ou_40cfcc05ae577c6914104447abc7a66d`
- OpenClaw Bot open_id：已加入群 `oc_8743c4c9b397a56575d57ccf9ab45eed` 的 allowFrom
- 协作规则：@谁谁回复，双方 requireMention=true

## 变更记录
- 2026-03-27：接入 mem0 共享记忆 + OpenClaw FTS5/workspace 知识检索 + 群协作配置
- 2026-03-27：初始部署，launchd + claude-mem + feishu-lark MCP + scheduler + 知识库接入
