"""
每日复盘脚本（由 launchd 定时触发，每天 23:30）
读取当日日志 → 调用 Claude 生成摘要 → 写入 daily-summary.md → 检测晋升候选
"""

import asyncio
import subprocess
import sys
import os
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memory_local import (
    MEMORY_DIR, LEARNINGS_FILE, ERRORS_FILE,
    _today, write_promotion_candidate,
)

CLAUDE_CLI = os.getenv("CLAUDE_CLI_PATH") or "claude"


def _daily_log_path() -> Path:
    return MEMORY_DIR / f"{_today()}.md"


def _summary_path() -> Path:
    return MEMORY_DIR / f"{_today()}-daily-summary.md"


def run_claude_summary(prompt: str) -> str:
    """调用 claude CLI 生成摘要"""
    try:
        result = subprocess.run(
            [CLAUDE_CLI, "--print", "--model", "claude-haiku-4-5-20251001", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.stdout.strip()
    except Exception as e:
        print(f"[daily_review] claude 调用失败: {e}", flush=True)
        return ""


def run_daily_review():
    # 七步进化（新增）
    try:
        from daily_evolution import run_evolution
        evolution_report = run_evolution()
        print(f"[daily] 进化完成:\n{evolution_report[:500]}", flush=True)
    except Exception as e:
        print(f"[daily] 进化失败（继续执行原有复盘）: {e}", flush=True)

    log_path = _daily_log_path()
    summary_path = _summary_path()

    if summary_path.exists():
        print(f"[daily_review] 今日摘要已存在，跳过", flush=True)
        return

    if not log_path.exists():
        print(f"[daily_review] 今日无日志，跳过", flush=True)
        return

    log_content = log_path.read_text(encoding="utf-8")
    if len(log_content.strip()) < 50:
        print(f"[daily_review] 今日日志内容太少，跳过", flush=True)
        return

    print(f"[daily_review] 开始生成 {_today()} 每日摘要...", flush=True)

    # 同时读取 ERRORS.md 今日新增内容
    errors_today = ""
    try:
        errors_content = ERRORS_FILE.read_text(encoding="utf-8")
        today = _today().replace("-", "")
        if today in errors_content:
            lines = errors_content.split("\n")
            capturing = False
            today_errors = []
            for line in lines:
                if today in line and line.startswith("## ERR"):
                    capturing = True
                if capturing:
                    today_errors.append(line)
            errors_today = "\n".join(today_errors)
    except Exception:
        pass

    prompt = f"""请对以下今日对话日志进行复盘，生成结构化摘要。

【今日日志】
{log_content[:3000]}

{"【今日错误记录】" + chr(10) + errors_today if errors_today else ""}

请按以下格式输出（中文，简洁）：

## {_today()} 每日复盘

### 1. 关键事件（最多5条）
-

### 2. 有效方法（最多3条）
-

### 3. 错误与纠正（今日发生的）
-

### 4. 晋升候选（出现≥2次的规律性经验，值得长期记住的）
-

### 5. 明日注意事项
-
"""

    summary = run_claude_summary(prompt)
    if not summary:
        print(f"[daily_review] 摘要生成失败", flush=True)
        return

    summary_path.write_text(summary, encoding="utf-8")
    print(f"[daily_review] 摘要已写入: {summary_path}", flush=True)

    # 从摘要中提取晋升候选，写入候选文件
    if "晋升候选" in summary:
        lines = summary.split("\n")
        in_section = False
        candidates = []
        for line in lines:
            if "晋升候选" in line:
                in_section = True
                continue
            if in_section:
                if line.startswith("###") or line.startswith("## "):
                    break
                if line.strip().startswith("-") and len(line.strip()) > 5:
                    candidates.append(line.strip().lstrip("- "))
        for c in candidates:
            if c:
                write_promotion_candidate(c, source=f"每日复盘 {_today()}")

    print(f"[daily_review] 完成", flush=True)

    # ── 记忆文件压缩检查 ─────────────────────────────────────
    try:
        from memory_compressor import check_and_compress
        check_and_compress()
    except Exception as _mc_err:
        print(f"[daily_review] memory_compressor 调用失败（忽略）: {_mc_err}", flush=True)


if __name__ == "__main__":
    run_daily_review()
