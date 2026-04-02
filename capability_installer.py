"""能力自装系统 — 自动安装 skill/pip/MCP，需审查+用户确认。"""
import os
import json
import subprocess
import shutil
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

INSTALL_HISTORY = Path.home() / ".feishu-claude" / "install_history.json"

# 安全白名单：pip 包名只允许字母、数字、连字符、下划线
import re
_SAFE_PKG_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")

# 支持的安装动作
ALLOWED_ACTIONS = {"skill_install", "pip_install", "mcp_config", "memory_update"}


def _load_history() -> list:
    if INSTALL_HISTORY.exists():
        try:
            return json.loads(INSTALL_HISTORY.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_history(history: list):
    os.makedirs(INSTALL_HISTORY.parent, exist_ok=True)
    INSTALL_HISTORY.write_text(json.dumps(history[-50:], ensure_ascii=False, indent=2), encoding="utf-8")


def propose_install(action: str, target: str, reason: str = "") -> dict:
    """
    提出安装提案（pending 状态，等待用户确认）。

    Args:
        action: 安装类型（skill_install / pip_install / mcp_config）
        target: 安装目标（包名/skill名/MCP配置）
        reason: 安装原因

    Returns:
        提案字典
    """
    if action not in ALLOWED_ACTIONS:
        return {"error": f"不支持的操作: {action}"}

    proposal = {
        "id": f"inst-{datetime.now():%Y%m%d%H%M%S}",
        "action": action,
        "target": target,
        "reason": reason,
        "status": "pending",
        "proposed_at": datetime.now().isoformat(),
        "executed_at": None,
        "result": None,
    }

    history = _load_history()
    history.append(proposal)
    _save_history(history)

    logger.info(f"[installer] 新提案: {action} {target}")
    return proposal


def execute_install(proposal_id: str) -> str:
    """
    执行已批准的安装提案。

    Returns:
        执行结果文本
    """
    history = _load_history()
    proposal = None
    for p in history:
        if p.get("id") == proposal_id:
            proposal = p
            break

    if not proposal:
        return f"❌ 未找到提案 {proposal_id}"

    if proposal.get("status") != "pending":
        return f"❌ 提案已处理: {proposal['status']}"

    action = proposal["action"]
    target = proposal["target"]

    try:
        if action == "pip_install":
            result = _pip_install(target)
        elif action == "skill_install":
            result = _skill_install(target)
        elif action == "mcp_config":
            result = _mcp_config(target)
        elif action == "memory_update":
            result = _memory_update(target)
        else:
            result = f"不支持的操作: {action}"

        proposal["status"] = "done"
        proposal["executed_at"] = datetime.now().isoformat()
        proposal["result"] = result
        _save_history(history)

        logger.info(f"[installer] 执行完成: {action} {target} → {result[:100]}")
        return f"✅ {result}"

    except Exception as e:
        proposal["status"] = "failed"
        proposal["result"] = str(e)
        _save_history(history)
        logger.error(f"[installer] 执行失败: {e}")
        return f"❌ 执行失败: {e}"


def reject_install(proposal_id: str) -> str:
    """拒绝安装提案"""
    history = _load_history()
    for p in history:
        if p.get("id") == proposal_id and p.get("status") == "pending":
            p["status"] = "rejected"
            _save_history(history)
            return f"✅ 已拒绝 {proposal_id}"
    return f"❌ 未找到待处理的提案 {proposal_id}"


def list_proposals(status: str = None) -> str:
    """列出安装提案"""
    history = _load_history()
    if status:
        history = [p for p in history if p.get("status") == status]

    if not history:
        return "暂无安装提案"

    lines = ["**🔧 能力自装**\n"]
    for p in history[-10:]:
        s = p.get("status", "?")
        icon = {"pending": "⏳", "done": "✅", "failed": "❌", "rejected": "🚫"}.get(s, "?")
        lines.append(f"{icon} `{p['id']}` [{p['action']}] {p['target'][:50]}")
        if p.get("reason"):
            lines.append(f"   原因: {p['reason'][:80]}")

    pending = [p for p in history if p.get("status") == "pending"]
    if pending:
        lines.append(f"\n用 `/install approve <id>` 批准，`/install reject <id>` 拒绝")

    return "\n".join(lines)


def _pip_install(package: str) -> str:
    """在 venv 中安装 pip 包"""
    if not _SAFE_PKG_NAME.match(package):
        raise ValueError(f"包名不安全: {package}")

    # 优先使用项目 venv
    venv_pip = Path.home() / "feishu-claude-code" / ".venv" / "bin" / "pip"
    pip_cmd = str(venv_pip) if venv_pip.exists() else "pip3"

    result = subprocess.run(
        [pip_cmd, "install", package],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode == 0:
        return f"pip install {package} 成功"
    raise RuntimeError(f"pip install 失败: {result.stderr[:200]}")


def _skill_install(skill_spec: str) -> str:
    """安装 Claude Code slash command"""
    claude_cli = os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude"
    result = subprocess.run(
        [claude_cli, "skill", "install", skill_spec],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0:
        return f"skill install {skill_spec} 成功: {result.stdout[:100]}"
    raise RuntimeError(f"skill install 失败: {result.stderr[:200]}")


def _mcp_config(config_json: str) -> str:
    """添加 MCP 配置"""
    # 解析配置
    config = json.loads(config_json)
    name = config.get("name", "unknown")
    return f"MCP {name} 配置已记录（需手动添加到 .claude/mcp.json）"


def _memory_update(content: str) -> str:
    """追加内容到长期记忆"""
    memory_file = Path.home() / ".feishu-claude" / "brain" / "MEMORY.md"
    with open(memory_file, "a", encoding="utf-8") as f:
        f.write(f"\n- {content}\n")
    return f"已追加到 MEMORY.md"
