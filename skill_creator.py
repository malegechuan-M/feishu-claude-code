"""技能创建器 — 从 git 历史和项目文件自动提取可复用技能。"""
import os
import json
import subprocess
import shutil
import re
import logging
from datetime import datetime
from pathlib import Path
from collections import Counter

logger = logging.getLogger(__name__)

SKILLS_DIR = Path.home() / ".feishu-claude" / "skills"


def create_skill_from_git(repo_path: str = None, focus: str = None) -> str:
    """
    分析 git 仓库的提交历史，提取重复模式生成技能文件。

    Args:
        repo_path: 仓库路径，默认为飞书 Bot 项目
        focus: 聚焦领域（可选）

    Returns:
        生成结果文本
    """
    repo = repo_path or os.path.expanduser("~/feishu-claude-code")

    # 1. 收集 git 信息
    try:
        log_result = subprocess.run(
            ["git", "log", "--oneline", "--stat", "-50"],
            capture_output=True, text=True, timeout=30, cwd=repo,
        )
        git_log = log_result.stdout[:5000] if log_result.returncode == 0 else ""

        freq_result = subprocess.run(
            ["git", "log", "--pretty=format:", "--name-only", "-100"],
            capture_output=True, text=True, timeout=30, cwd=repo,
        )
        freq_text = ""
        if freq_result.returncode == 0:
            files = [f for f in freq_result.stdout.split("\n") if f.strip()]
            freq_files = Counter(files).most_common(10)
            freq_text = "\n".join(f"  {f}: {c} 次修改" for f, c in freq_files)

        tree_result = subprocess.run(
            ["find", ".", "-maxdepth", "2", "-type", "f",
             "(", "-name", "*.py", "-o", "-name", "*.md", "-o", "-name", "*.json", ")"],
            capture_output=True, text=True, timeout=10, cwd=repo,
        )
        tree = tree_result.stdout[:2000] if tree_result.returncode == 0 else ""

    except Exception as e:
        return f"❌ git 信息收集失败: {e}"

    if not git_log:
        return "❌ 无法获取 git 历史（确认目录是 git 仓库）"

    # 2. 用 Claude Haiku 分析
    claude_cli = os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude"
    focus_hint = f"\n聚焦领域：{focus}" if focus else ""

    prompt = f"""分析以下 git 仓库信息，提取 3-5 个重复出现的工作模式，生成可复用的技能文件。

Git 提交历史（最近 50 次）：
{git_log[:3000]}

最常修改的文件：
{freq_text}

项目文件结构：
{tree[:1000]}
{focus_hint}

要求：每个模式用以下格式输出，多个模式之间用 === 分隔：

---
name: 技能名（英文kebab-case）
domain: 领域
---

## 触发条件
什么情况下应该使用

## 执行步骤
1. ...

## 注意事项
- ...

===

请输出 3-5 个技能："""

    try:
        result = subprocess.run(
            [claude_cli, "--print", "--model", "claude-haiku-4-5-20251001", "-p", prompt],
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout.strip()
        if not output:
            return "❌ 分析未产生输出"

        # 3. 保存技能文件
        os.makedirs(SKILLS_DIR, exist_ok=True)

        skills = re.split(r'\n===\n', output)
        saved = 0
        for i, skill_text in enumerate(skills):
            skill_text = skill_text.strip()
            if not skill_text or len(skill_text) < 50:
                continue

            name_match = re.search(r'name:\s*(.+)', skill_text)
            name = name_match.group(1).strip().replace(" ", "-").lower() if name_match else f"auto-skill-{i+1}"

            skill_path = SKILLS_DIR / f"{name}.md"
            skill_path.write_text(skill_text, encoding="utf-8")
            saved += 1

        return f"✅ 从 git 历史提取了 {saved} 个技能 → ~/.feishu-claude/skills/"

    except Exception as e:
        return f"❌ 分析失败: {e}"


def list_skills() -> str:
    """列出所有已生成的技能"""
    if not SKILLS_DIR.exists():
        return "暂无技能文件"

    skills = sorted(SKILLS_DIR.glob("*.md"))
    if not skills:
        return "暂无技能文件"

    lines = ["**📚 技能库**\n"]
    for s in skills:
        size = s.stat().st_size
        lines.append(f"  `{s.stem}` ({size} bytes)")

    lines.append(f"\n共 {len(skills)} 个技能")
    return "\n".join(lines)
