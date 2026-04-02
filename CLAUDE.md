# 飞书 Bot 项目说明

## 架构

### 核心模块
- `main.py`：飞书 WebSocket 接收 → 上下文构建 → claude CLI subprocess → 回复飞书卡片
- `claude_runner.py`：调用 `claude --print --output-format stream-json`
- `session_store.py`：SQLite 持久化会话（存在 ~/.feishu-claude/）
- `commands.py`：/xxx 斜杠命令处理（23 个命令）
- `context_builder.py`：统一上下文构建，按优先级拼接所有记忆模块
- `feishu_client.py`：飞书消息收发（post 富文本 + 流式卡片更新）

### 记忆系统（6 层）
- `memory_local.py`：本地四层记忆（日志 → 晋升候选 → 长期 → 自学习）
- `memory_bridge.py`：共享记忆桥接（mem0 云 + OpenClaw FTS5 + workspace + 向量库）
- `contact_memory.py`：联系人记忆（每用户 JSON，自动获取飞书真名，TTL 缓存）
- `group_memory.py`：群聊观察记忆（Observer 模式，每 8 轮 Haiku 提取笔记）
- `router_context.py`：三层加载策略（Index/Summary/Full，意图驱动选择）
- `context_dag.py`：SQLite 无损对话压缩
- `vector_store.py` + `indexer.py`：ChromaDB + bge-small-zh 向量知识库

### 自我进化
- `instinct_manager.py`：直觉系统（YAML/JSON，置信度 0.1-0.95，衰减 + 审批流）
- `reflect_detector.py`：纠正检测增强（11 种模式，分级置信度，高置信度立即注入）
- `daily_evolution.py`：七步每日进化（联系人更新 → 知识提取 → 待办 → 模式 → 缺口 → 直觉 → 指标）
- `daily_review.py`：每日复盘入口（23:30 launchd 触发）
- `memory_compressor.py`：记忆压缩归档（超阈值自动 Haiku 压缩）

### 安全与资源
- `prompt_guard.py`：Prompt 注入防护（14 种模式，群聊强制过滤）
- `quota_tracker.py`：配额自学习 + 自动降级链（Opus → Sonnet → Haiku → external）
- `review_mode.py`：审查其他 Bot（MiniMax/GLM）产出质量

### 其他
- `intent_router.py`：两级意图分类（规则 + Haiku LLM）
- `long_task.py`：长任务检查点持久化（SQLite）
- `scheduler.py`：定时任务调度器
- `llm_client.py`：Haiku API 轻量封装
- `run_control.py`：活跃进程管理（/stop 命令）
- `bot_config.py`：配置常量

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
| `/quota` | 查看配额状态和降级情况 |
| `/contact [open_id]` | 查看联系人记忆档案 |
| `/group-memory` | 查看当前群的观察记忆 |
| `/instinct [list/approve/reject/create]` | 管理行为直觉 |
| `/review <文本>` | 审查内容质量 |

## 上下文注入优先级
由 `context_builder.py` 统一管理，注入顺序：
1. 环境提示（新 session 首条）
2. 任务检查点 + 用户消息
3. 群聊上下文（Observer 观察 > deque 增量 fallback）
4. 联系人记忆
5. Brain 长期记忆（三层加载：trivial→跳过, chat→标题, question→摘要, task→全量）
6. 最近纠正（高置信度立即注入）
7. 行为直觉（匹配活跃规则）
8. 共享记忆（mem0 + RAG 召回）

## 数据目录
```
~/.feishu-claude/
├── feishu_sessions.db          # 会话管理
├── context_dag.db              # 对话压缩
├── tasks.db                    # 长任务检查点
├── quota.db                    # 配额追踪
├── vector_db/                  # ChromaDB 向量库
├── memory/
│   ├── YYYY-MM-DD.md           # 每日日志
│   ├── contacts/{open_id}.json # 联系人档案
│   ├── groups/{chat_id}.json   # 群聊观察记忆
│   ├── instincts/{id}.yaml     # 行为直觉
│   ├── corrections_queue.json  # 高置信度纠正队列
│   └── learnings_queue.json    # 低置信度纠正候选
├── brain/
│   ├── MEMORY.md               # 长期记忆
│   └── SOUL.md                 # 行为原则
├── learnings/
│   ├── LEARNINGS.md            # 经验教训
│   └── ERRORS.md               # 错误记录
└── archive/                    # 压缩归档
```

## 注意事项
- Bot 使用 Claude Max 订阅（通过 claude login），不使用 API Key
- 所有飞书凭证在 .env 文件中，不要提交到 git
- 修改 main.py 或 commands.py 后需要重启 Bot 才生效
- MCP 配置：feishu-lark（飞书官方）、xhs-pro-mcp（小红书采集）

## 已知问题
- WebSocket 连接每 24 小时会主动重启（watchdog 机制，正常）
- 群聊只响应 @机器人 的消息
- 定时任务的 chat_id 需要是用户 open_id（私聊）或群 chat_id

## 共享记忆系统（与 OpenClaw 协作）
- **mem0 云记忆**：与 OpenClaw 共享同一个 mem0 账号
  - Claude 命名空间：`wuxianbaoshi:agent:claude-feishu`
  - OpenClaw 命名空间：`wuxianbaoshi:agent:agent-a-coo` 等
  - 搜索时同时查 Claude 和 OpenClaw 的命名空间
- **OpenClaw FTS5**：搜索 OpenClaw 的 SQLite 全文索引
- **OpenClaw workspace**：关键词匹配 `~/openclaw/workspace/memory/*.md`
- **消息流**：用户消息 → recall_all() → 注入上下文 → Claude → capture_memory() → mem0

## 飞书群协作（Claude Bot + OpenClaw Bot）
- Claude Bot open_id：`ou_40cfcc05ae577c6914104447abc7a66d`
- 协作规则：@谁谁回复，双方 requireMention=true
- Claude 可通过 `/review` 审查 OpenClaw Bot 的产出

## 变更记录
- 2026-04-01：全面改造 — 新增 11 个模块（6 层记忆 + 直觉系统 + 每日进化 + 注入防护 + 配额追踪 + 审查模式 + 统一上下文构建）
- 2026-03-27：接入 mem0 共享记忆 + OpenClaw FTS5/workspace 知识检索 + 群协作配置
- 2026-03-27：初始部署，launchd + claude-mem + feishu-lark MCP + scheduler + 知识库接入
