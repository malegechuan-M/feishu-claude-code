import os
import shutil
from dotenv import load_dotenv

load_dotenv()

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]

CLAUDE_CLI = os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude"

DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "claude-sonnet-4-6")
DEFAULT_CWD = os.path.expanduser(os.getenv("DEFAULT_CWD", "~"))
PERMISSION_MODE = os.getenv("PERMISSION_MODE", "bypassPermissions")

SESSIONS_DIR = os.path.expanduser("~/.feishu-claude")

# 流式卡片更新：每积累多少字符推送一次
STREAM_CHUNK_SIZE = int(os.getenv("STREAM_CHUNK_SIZE", "20"))

# 群聊中已知发言者的 open_id → 显示名映射（用于群聊历史注入）
# 填入群成员的 open_id（可在飞书开放平台事件日志中获取）
GROUP_KNOWN_NAMES: dict[str, str] = {
    # "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx": "Bot A (Claude)",
    # "ou_yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy": "Bot B (Other)",
    # "ou_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz": "用户昵称",
}

# 群聊中其他 Bot 的信息，用于实现真正的 @mention
# 格式: {触发关键词: (open_id, 显示名)}
# Claude 回复中包含触发关键词时，自动向对应 Bot 发送 @mention 消息
GROUP_BOTS = {
    # 示例：
    # "@BotB": ("ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "BotB"),
}
