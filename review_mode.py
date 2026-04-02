"""群聊审查模式 — 审查 MiniMax/GLM 等其他 Bot 的产出质量。"""
import json
import os
import logging
import subprocess
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

QUALITY_LOG = Path.home() / ".feishu-claude" / "quality_log.json"


def review_output(text: str, source: str = "unknown") -> dict:
    """
    调用 Claude 审查其他 Bot 的产出。

    Args:
        text: 待审查的文本内容
        source: 来源标识（如 "minimax", "glm"）

    Returns:
        {"score": 0-100, "issues": [...], "summary": "..."}
    """
    claude_cli = os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude"

    prompt = f"""请审查以下由 AI 助手（{source}）生成的内容，从三个维度评分和点评：

1. **事实准确性** — 是否有明显错误或编造的信息
2. **逻辑一致性** — 论述是否自洽，结论是否合理
3. **完整性** — 是否遗漏了关键要点

请用 JSON 格式返回（不要其他内容）：
{{"score": 0-100, "issues": ["问题1", "问题2"], "strengths": ["优点1"], "summary": "一句话总评"}}

待审查内容：
{text[:3000]}

JSON 结果："""

    try:
        result = subprocess.run(
            [claude_cli, "--print", "--model", "claude-haiku-4-5-20251001", "-p", prompt],
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout.strip()

        # 提取 JSON
        import re
        m = re.search(r'\{.*\}', output, re.S)
        if m:
            review = json.loads(m.group())
            # 记录到质量日志
            _log_review(source, review, text[:200])
            return review
    except Exception as e:
        logger.error(f"[review] 审查失败: {e}")

    return {"score": -1, "issues": ["审查失败"], "summary": "无法完成审查"}


def format_review(review: dict, source: str = "") -> str:
    """格式化审查结果为可读文本"""
    score = review.get("score", -1)
    if score < 0:
        return f"❌ 审查失败: {review.get('summary', '未知错误')}"

    # 评级
    if score >= 90:
        grade = "🟢 优秀"
    elif score >= 70:
        grade = "🟡 良好"
    elif score >= 50:
        grade = "🟠 一般"
    else:
        grade = "🔴 较差"

    lines = [f"**审查结果** ({source}) — {grade} ({score}/100)"]

    if review.get("summary"):
        lines.append(f"\n{review['summary']}")

    issues = review.get("issues", [])
    if issues:
        lines.append("\n**问题**:")
        for issue in issues[:5]:
            lines.append(f"  - {issue}")

    strengths = review.get("strengths", [])
    if strengths:
        lines.append("\n**优点**:")
        for s in strengths[:3]:
            lines.append(f"  - {s}")

    return "\n".join(lines)


def _log_review(source: str, review: dict, content_preview: str):
    """记录审查结果到质量日志"""
    os.makedirs(QUALITY_LOG.parent, exist_ok=True)

    existing = []
    if QUALITY_LOG.exists():
        try:
            existing = json.loads(QUALITY_LOG.read_text(encoding="utf-8"))
        except Exception:
            pass

    existing.append({
        "timestamp": datetime.now().isoformat(),
        "source": source,
        "score": review.get("score", -1),
        "issues_count": len(review.get("issues", [])),
        "summary": review.get("summary", ""),
        "content_preview": content_preview,
    })

    # 最多保留 100 条
    existing = existing[-100:]
    QUALITY_LOG.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
