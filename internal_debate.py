"""内部自辩系统 — 输出方案前先正反方讨论，提升方案质量。

流程：
1. 用户提出任务/问题
2. Claude 生成初步方案
3. 内部 Critic（反方）审视方案，找问题
4. 综合正反方意见，输出最终方案

通过 system prompt 模拟正反方角色，一次 Haiku 调用完成审视。
"""
import os
import logging
import subprocess
import shutil

logger = logging.getLogger(__name__)

# 触发条件：只有复杂任务/方案类输出才走内部自辩
DEBATE_TRIGGERS = [
    "方案", "计划", "策略", "建议", "分析", "设计",
    "report", "plan", "strategy", "proposal", "analysis",
]

# Critic prompt
CRITIC_PROMPT = """你是一位严格的方案审查官。你的任务是审视以下方案，找出潜在问题。

审查维度：
1. 逻辑漏洞：论证是否自洽？有没有跳步？
2. 遗漏风险：有没有忽略的关键因素？
3. 可行性：执行层面是否现实？
4. 更优替代：是否有更好的方案？

规则：
- 只指出真正的问题，不挑无关紧要的毛病
- 如果方案已经很好，直接说"方案完备，无需修改"
- 每个问题用一行描述，最多列 5 个
- 最后给出综合评价：通过 / 需修改 / 需重做

用户原始需求：
{user_request}

待审查方案：
{draft_response}

请直接输出审查结果："""


def should_debate(text: str, response: str) -> bool:
    """判断是否需要内部自辩（只对方案类输出触发）"""
    # 响应太短不需要
    if len(response) < 300:
        return False

    # 检查用户请求或响应是否包含方案类关键词
    combined = (text + response).lower()
    return any(trigger in combined for trigger in DEBATE_TRIGGERS)


def run_debate(user_request: str, draft_response: str) -> dict:
    """
    执行内部自辩：用 Haiku 模拟 Critic 角色审视方案。

    Args:
        user_request: 用户原始请求
        draft_response: Claude 的初步回复

    Returns:
        {
            "verdict": "pass" | "revise" | "redo",
            "issues": ["问题1", "问题2"],
            "critique": "完整审查文本",
            "enhanced_response": "修改后的回复（如果需要）"
        }
    """
    claude_cli = os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude"

    prompt = CRITIC_PROMPT.format(
        user_request=user_request[:500],
        draft_response=draft_response[:3000],
    )

    try:
        result = subprocess.run(
            [claude_cli, "--print", "--model", "claude-haiku-4-5-20251001", "-p", prompt],
            capture_output=True, text=True, timeout=60,
        )
        critique = result.stdout.strip()

        if not critique:
            return {"verdict": "pass", "issues": [], "critique": "", "enhanced_response": ""}

        # 解析结论
        critique_lower = critique.lower()
        if "需重做" in critique or "需要重做" in critique or "redo" in critique_lower:
            verdict = "redo"
        elif "需修改" in critique or "需要修改" in critique or "revise" in critique_lower:
            verdict = "revise"
        else:
            verdict = "pass"

        # 提取问题列表
        issues = []
        for line in critique.split("\n"):
            line = line.strip()
            if line and (line.startswith("-") or line.startswith("•") or line[0:1].isdigit()):
                issues.append(line.lstrip("-•0123456789. "))

        logger.info(f"[debate] 审查结果: {verdict}, {len(issues)} 个问题")

        return {
            "verdict": verdict,
            "issues": issues[:5],
            "critique": critique,
            "enhanced_response": "",
        }

    except Exception as e:
        logger.error(f"[debate] 内部自辩失败: {e}")
        return {"verdict": "pass", "issues": [], "critique": f"审查失败: {e}", "enhanced_response": ""}


def enhance_with_critique(original_response: str, critique: str) -> str:
    """
    将审查意见附加到原始回复末尾（作为补充说明）。
    只在 verdict 为 revise 时调用。
    """
    if not critique:
        return original_response

    supplement = "\n\n---\n**⚠️ 内部审查补充**\n"
    supplement += critique
    supplement += "\n---"

    return original_response + supplement


def format_debate_log(debate_result: dict) -> str:
    """格式化自辩日志，用于写入 daily log"""
    verdict = debate_result.get("verdict", "?")
    issues = debate_result.get("issues", [])
    return f"[自辩] 结论={verdict} 问题数={len(issues)}"
