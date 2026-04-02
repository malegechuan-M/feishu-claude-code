"""七步每日进化 — 从简单复盘升级为全面进化流程。"""
import json
import os
import re
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

MEMORY_DIR = Path.home() / ".feishu-claude" / "memory"
BRAIN_DIR = Path.home() / ".feishu-claude" / "brain"
LEARNINGS_DIR = Path.home() / ".feishu-claude" / "learnings"
PENDING_TASKS_FILE = MEMORY_DIR / "pending-tasks.json"
CAPABILITY_GAPS_FILE = MEMORY_DIR / "capability_gaps.json"

# 每日日志路径（与 memory_local.py 一致）
def _today_log() -> Path:
    return MEMORY_DIR / f"{datetime.now():%Y-%m-%d}.md"


def run_evolution():
    """
    执行七步每日进化。每步独立 try/except，失败不阻塞后续。
    返回执行报告文本。
    """
    report = []
    report.append(f"# 每日进化报告 — {datetime.now():%Y-%m-%d %H:%M}\n")

    steps = [
        ("1. 联系人画像更新", step_update_contacts),
        ("2. 知识提取", step_extract_knowledge),
        ("3. 待办提取", step_extract_todos),
        ("4. 模式检测", step_detect_patterns),
        ("5. 能力缺口扫描", step_scan_gaps),
        ("6. 直觉整理", step_manage_instincts),
        ("7. 指标追踪", step_generate_metrics),
        ("8. 审查 OpenClaw 产出", step_review_openclaw),
    ]

    for name, func in steps:
        try:
            result = func()
            report.append(f"✅ {name}: {result}")
            logger.info(f"[evolution] {name} 完成: {result}")
        except Exception as e:
            report.append(f"❌ {name}: {e}")
            logger.error(f"[evolution] {name} 失败: {e}", exc_info=True)

    # 保存报告
    report_text = "\n".join(report)
    report_path = MEMORY_DIR / f"{datetime.now():%Y-%m-%d}-evolution-report.md"
    try:
        os.makedirs(MEMORY_DIR, exist_ok=True)
        report_path.write_text(report_text, encoding="utf-8")
    except Exception as e:
        logger.error(f"[evolution] 保存报告失败: {e}")

    return report_text


def step_update_contacts() -> str:
    """分析当日日志，提取每个 user_id 的交互特征并更新联系人档案"""
    log_path = _today_log()
    if not log_path.exists():
        return "无今日日志"

    content = log_path.read_text(encoding="utf-8")
    if len(content) < 50:
        return "日志内容过短"

    # 提取日志中出现的 open_id（ou_ 开头）
    user_ids = set(re.findall(r'(ou_[a-f0-9]{20,})', content))
    if not user_ids:
        return "未发现用户交互"

    updated = 0
    for uid in user_ids:
        try:
            from contact_memory import update_contact
            # 简单更新：记录今天有交互（详细画像更新留给每日进化 AI 分析）
            update_contact(uid, last_seen=datetime.now().isoformat())
            updated += 1
        except Exception:
            continue

    return f"更新了 {updated} 个联系人"


def step_extract_knowledge() -> str:
    """分析当日日志，提取有价值的知识写入 LEARNINGS.md"""
    log_path = _today_log()
    if not log_path.exists():
        return "无今日日志"

    content = log_path.read_text(encoding="utf-8")
    if len(content) < 100:
        return "日志内容过短"

    # 使用 claude CLI 提取知识（与 daily_review.py 风格一致）
    import subprocess
    import shutil

    claude_cli = os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude"

    prompt = f"""分析以下今日对话日志，提取有价值的经验教训和规律性认知。

要求：
1. 只提取确实有价值的发现（忽略日常寒暄和简单问答）
2. 每条用一行简洁描述
3. 如果没有值得记录的内容，回复"无"
4. 最多提取 5 条

日志内容：
{content[:4000]}

请直接输出提取结果："""

    try:
        result = subprocess.run(
            [claude_cli, "--print", "--model", "claude-haiku-4-5-20251001", "-p", prompt],
            capture_output=True, text=True, timeout=120,
        )
        extracted = result.stdout.strip()

        if extracted and extracted != "无" and len(extracted) > 5:
            # 追加到 LEARNINGS.md
            learnings_file = LEARNINGS_DIR / "LEARNINGS.md"
            os.makedirs(LEARNINGS_DIR, exist_ok=True)
            with open(learnings_file, "a", encoding="utf-8") as f:
                f.write(f"\n\n## 自动提取 {datetime.now():%Y-%m-%d}\n{extracted}\n")
            return f"提取了知识写入 LEARNINGS.md"
        return "无值得记录的知识"
    except Exception as e:
        return f"Claude 调用失败: {e}"


def step_extract_todos() -> str:
    """从今日日志中检测未完成的待办事项"""
    log_path = _today_log()
    if not log_path.exists():
        return "无今日日志"

    content = log_path.read_text(encoding="utf-8")

    # 关键词匹配未完成任务
    todo_patterns = [
        re.compile(r"(待办|TODO|todo|明天|下次|回头|稍后|之后)(再|要|需要|得)(.{5,50})"),
        re.compile(r"(还没|尚未|暂时没)(完成|做好|搞定)(.{5,50})"),
        re.compile(r"(记得|别忘了|提醒我)(.{5,50})"),
    ]

    todos = []
    for pat in todo_patterns:
        for m in pat.finditer(content):
            todos.append(m.group(0)[:100])

    if not todos:
        return "无待办事项"

    # 去重
    todos = list(set(todos))[:10]

    # 写入 pending-tasks.json
    os.makedirs(MEMORY_DIR, exist_ok=True)
    existing = []
    if PENDING_TASKS_FILE.exists():
        try:
            existing = json.loads(PENDING_TASKS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    for todo in todos:
        existing.append({
            "text": todo,
            "extracted_at": datetime.now().isoformat(),
            "status": "pending",
        })

    # 最多保留 30 条
    existing = existing[-30:]
    PENDING_TASKS_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    return f"提取了 {len(todos)} 条待办"


def step_detect_patterns() -> str:
    """规则检测重复行为，3 次阈值时创建 pending 直觉"""
    log_path = _today_log()
    if not log_path.exists():
        return "无今日日志"

    content = log_path.read_text(encoding="utf-8").lower()

    # 预定义模式：(模式描述, 匹配关键词, 直觉action)
    pattern_defs = [
        ("excel_auto_analyze", ["excel", "xlsx", "分析表格", "电子表格"], "收到 Excel 文件后自动进行数据分析"),
        ("image_describe", ["图片", "截图", "看一下", "这张图"], "收到图片后自动描述内容"),
        ("code_review", ["review", "代码审查", "看看代码"], "收到代码时自动进行代码审查"),
        ("summary_request", ["总结一下", "摘要", "概括"], "用户倾向于要求摘要，优先提供精简版"),
    ]

    # 统计文件（持久化计数）
    counter_file = MEMORY_DIR / "pattern_counters.json"
    counters = {}
    if counter_file.exists():
        try:
            counters = json.loads(counter_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    created = 0
    for name, keywords, action in pattern_defs:
        hits = sum(1 for kw in keywords if kw in content)
        if hits > 0:
            counters[name] = counters.get(name, 0) + hits

        # 3 次阈值：创建 pending 直觉
        if counters.get(name, 0) >= 3 and not counters.get(f"{name}_created"):
            try:
                from instinct_manager import create_instinct
                create_instinct(
                    trigger=", ".join(keywords[:3]),
                    action=action,
                    domain="auto_pattern",
                    source="ai_extracted",
                    status="pending",
                )
                counters[f"{name}_created"] = True
                created += 1
            except Exception:
                pass

    counter_file.write_text(json.dumps(counters, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"检测到 {created} 个新模式" if created else "无新模式"


def step_scan_gaps() -> str:
    """检测 Bot 回复中的能力缺口"""
    log_path = _today_log()
    if not log_path.exists():
        return "无今日日志"

    content = log_path.read_text(encoding="utf-8")

    gap_patterns = [
        re.compile(r"(不支持|做不到|暂不支持|无法完成|超出.*能力)(.{5,80})"),
        re.compile(r"(sorry|cannot|unable to|don't have|not able)(.{5,80})", re.I),
    ]

    gaps = []
    for pat in gap_patterns:
        for m in pat.finditer(content):
            gaps.append(m.group(0)[:120])

    if not gaps:
        return "无能力缺口"

    gaps = list(set(gaps))[:10]

    # 写入 capability_gaps.json
    os.makedirs(MEMORY_DIR, exist_ok=True)
    existing = []
    if CAPABILITY_GAPS_FILE.exists():
        try:
            existing = json.loads(CAPABILITY_GAPS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    for gap in gaps:
        existing.append({
            "description": gap,
            "detected_at": datetime.now().isoformat(),
        })

    existing = existing[-20:]
    CAPABILITY_GAPS_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    return f"发现 {len(gaps)} 个能力缺口"


def step_manage_instincts() -> str:
    """对所有直觉执行衰减，清理过期规则"""
    try:
        from instinct_manager import decay_all
        decay_all()
        return "直觉衰减完成"
    except Exception as e:
        return f"衰减失败: {e}"


def step_generate_metrics() -> str:
    """统计今日各项指标"""
    metrics = {}

    # 今日日志大小
    log_path = _today_log()
    if log_path.exists():
        metrics["日志大小"] = f"{log_path.stat().st_size} 字节"

    # 配额使用
    try:
        from quota_tracker import tracker
        metrics["配额状态"] = "已记录"
    except Exception:
        pass

    # 联系人总数
    contacts_dir = MEMORY_DIR / "contacts"
    if contacts_dir.exists():
        metrics["联系人数"] = len(list(contacts_dir.glob("*.json")))

    # 直觉数量
    instincts_dir = MEMORY_DIR / "instincts"
    if instincts_dir.exists():
        files = list(instincts_dir.glob("*.yaml")) + list(instincts_dir.glob("*.json"))
        metrics["直觉规则"] = len(files)

    # 群聊记忆
    groups_dir = MEMORY_DIR / "groups"
    if groups_dir.exists():
        metrics["群聊记忆"] = len(list(groups_dir.glob("*.json")))

    summary = "; ".join(f"{k}: {v}" for k, v in metrics.items())
    return summary or "无指标数据"


def step_review_openclaw() -> str:
    """审查 OpenClaw 今日产出质量（用 Haiku 快速评分）"""
    oc_memory_dir = Path.home() / "openclaw" / "workspace" / "memory"
    today = datetime.now().strftime("%Y-%m-%d")

    # 收集 OpenClaw 今日的所有输出文件
    today_files = sorted(oc_memory_dir.glob(f"{today}*.md"))
    if not today_files:
        return "OpenClaw 今日无产出"

    # 读取所有产出（限制总量）
    content_parts = []
    total_chars = 0
    for f in today_files:
        try:
            text = f.read_text(encoding="utf-8")
            content_parts.append(f"--- {f.name} ---\n{text[:1500]}")
            total_chars += len(text)
        except Exception:
            continue

    if not content_parts:
        return "OpenClaw 今日产出为空"

    combined = "\n\n".join(content_parts)[:5000]

    # 用 Haiku 快速审查
    import subprocess
    import shutil
    claude_cli = os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude"

    prompt = f"""快速审查以下 AI 助手（OpenClaw/麦克斯）今日的工作产出，评分并指出问题。

评分维度（每项 1-5 分）：
1. 完整性：任务是否做完了
2. 准确性：数据和结论是否可靠
3. 结构性：输出是否有清晰结构
4. 主动性：是否主动发现和汇报问题

格式：一行总评 + 每项评分 + 最多 3 条改进建议。不超过 200 字。

产出内容：
{combined}

审查结果："""

    try:
        result = subprocess.run(
            [claude_cli, "--print", "--model", "claude-haiku-4-5-20251001", "-p", prompt],
            capture_output=True, text=True, timeout=120,
        )
        review = result.stdout.strip()
        if review:
            # 写入审查报告
            review_path = MEMORY_DIR / f"{today}-openclaw-review.md"
            review_path.write_text(
                f"# OpenClaw 产出审查 — {today}\n\n"
                f"审查文件：{len(today_files)} 个，总计 {total_chars} 字符\n\n"
                f"{review}\n",
                encoding="utf-8"
            )
            return f"审查了 {len(today_files)} 个文件，报告已保存"
        return "审查未产生结果"
    except Exception as e:
        return f"审查失败: {e}"


# 如果直接运行（测试用）
if __name__ == "__main__":
    print(run_evolution())
