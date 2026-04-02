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
    "@麦克斯": ("ou_5a39f813fbd735f6095173bf12d56527", "麦克斯"),
    "@M": ("ou_5a39f813fbd735f6095173bf12d56527", "麦克斯"),
    "@openclaw": ("ou_5a39f813fbd735f6095173bf12d56527", "麦克斯"),
}

# OpenClaw 本地协同：触发关键词 → (agent_id, 飞书群 chat_id 映射)
# CC bot 通过 openclaw CLI 直接调用 agent，结果由 OpenClaw 自己投递到飞书群
OPENCLAW_AGENTS = {
    "@麦克斯": "agent-a-coo",
    "@M": "agent-a-coo",
    "@openclaw": "agent-a-coo",
}

# 群聊 chat_id → OpenClaw reply-to target 的映射
OPENCLAW_GROUP_TARGETS = {
    "oc_ff3d17ce731af0fd8c015e16f321760d": "group:oc_ff3d17ce731af0fd8c015e16f321760d",
    "oc_8743c4c9b397a56575d57ccf9ab45eed": "group:oc_8743c4c9b397a56575d57ccf9ab45eed",
}

OPENCLAW_CLI = os.getenv("OPENCLAW_CLI", "openclaw")
