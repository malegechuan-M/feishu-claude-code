"""直觉系统 — YAML 存储行为规则，置信度动态管理，审批流控制。"""
import os
import re
import time
import uuid
import logging
from pathlib import Path
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

INSTINCTS_DIR = Path.home() / ".feishu-claude" / "memory" / "instincts"

# 置信度边界
MIN_CONFIDENCE = 0.10   # 低于此值自动删除
MAX_CONFIDENCE = 0.95
BOOST_STEP = 0.05       # 使用一次 +0.05
DECAY_STEP = 0.05       # 30 天未用 -0.05
DECAY_DAYS = 30          # 衰减周期

# 来源默认置信度
DEFAULT_CONFIDENCE = {
    "user_correction": 0.60,  # 用户纠正自动创建
    "ai_extracted": 0.40,     # AI 提取的规律
    "manual": 0.70,           # 手动添加
}


def _load_instinct(path: Path) -> dict | None:
    """从文件加载一条直觉（支持 YAML 和 JSON 两种格式）"""
    try:
        content = path.read_text(encoding="utf-8")
        # 优先尝试 YAML
        if path.suffix in (".yaml", ".yml"):
            try:
                import yaml
                return yaml.safe_load(content)
            except ImportError:
                pass
        # fallback JSON（不依赖 pyyaml）
        import json
        return json.loads(content)
    except Exception as e:
        logger.debug(f"[instinct] 加载失败 {path}: {e}")
        return None


def _save_instinct(data: dict):
    """保存直觉到文件"""
    os.makedirs(INSTINCTS_DIR, exist_ok=True)
    instinct_id = data.get("id", str(uuid.uuid4())[:8])

    # 优先 YAML，无 pyyaml 时用 JSON
    try:
        import yaml
        path = INSTINCTS_DIR / f"{instinct_id}.yaml"
        path.write_text(
            yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8"
        )
    except ImportError:
        import json
        path = INSTINCTS_DIR / f"{instinct_id}.json"
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    logger.debug(f"[instinct] 已保存 {instinct_id}: {data.get('trigger', '')[:50]}")


def _list_instinct_files() -> list[Path]:
    """列出所有直觉文件"""
    if not INSTINCTS_DIR.exists():
        return []
    return sorted(
        [f for f in INSTINCTS_DIR.iterdir() if f.suffix in (".yaml", ".yml", ".json")],
        key=lambda f: f.stat().st_mtime,
        reverse=True
    )


def _load_all() -> list[dict]:
    """加载所有直觉"""
    results = []
    for path in _list_instinct_files():
        data = _load_instinct(path)
        if data:
            data["_path"] = str(path)
            results.append(data)
    return results


def create_instinct(trigger: str, action: str, domain: str = "general",
                    source: str = "manual", status: str = "pending") -> str:
    """
    创建新直觉。

    Args:
        trigger: 触发条件描述
        action: 期望行为描述
        domain: 领域标签
        source: 来源（user_correction / ai_extracted / manual）
        status: 初始状态（pending 需审批 / active 已激活）

    Returns:
        直觉 ID
    """
    instinct_id = str(uuid.uuid4())[:8]
    confidence = DEFAULT_CONFIDENCE.get(source, 0.50)

    data = {
        "id": instinct_id,
        "trigger": trigger,
        "action": action,
        "domain": domain,
        "confidence": confidence,
        "status": status,
        "source": source,
        "created_at": datetime.now().isoformat(),
        "last_used": None,
        "use_count": 0,
    }
    _save_instinct(data)
    logger.info(f"[instinct] 新建 {instinct_id}: trigger='{trigger[:50]}' status={status} conf={confidence}")
    return instinct_id


def activate(instinct_id: str) -> bool:
    """审批通过，激活直觉"""
    for path in _list_instinct_files():
        if instinct_id in path.stem:
            data = _load_instinct(path)
            if data:
                data["status"] = "active"
                _save_instinct(data)
                logger.info(f"[instinct] 已激活 {instinct_id}")
                return True
    return False


def deactivate(instinct_id: str) -> bool:
    """停用直觉"""
    for path in _list_instinct_files():
        if instinct_id in path.stem:
            data = _load_instinct(path)
            if data:
                data["status"] = "inactive"
                _save_instinct(data)
                return True
    return False


def reject(instinct_id: str) -> bool:
    """拒绝 pending 直觉（直接删除文件）"""
    for path in _list_instinct_files():
        if instinct_id in path.stem:
            try:
                path.unlink()
                logger.info(f"[instinct] 已拒绝并删除 {instinct_id}")
                return True
            except Exception:
                return False
    return False


def boost(instinct_id: str):
    """使用一次直觉后提升置信度"""
    for path in _list_instinct_files():
        if instinct_id in path.stem:
            data = _load_instinct(path)
            if data:
                data["confidence"] = min(MAX_CONFIDENCE, data.get("confidence", 0.5) + BOOST_STEP)
                data["last_used"] = datetime.now().isoformat()
                data["use_count"] = data.get("use_count", 0) + 1
                _save_instinct(data)
                return


def decay_all():
    """
    对所有 active 直觉执行衰减：
    超过 DECAY_DAYS 天未使用则 confidence -= DECAY_STEP，
    低于 MIN_CONFIDENCE 自动删除。
    """
    cutoff = datetime.now() - timedelta(days=DECAY_DAYS)
    deleted = 0
    decayed = 0

    for path in _list_instinct_files():
        data = _load_instinct(path)
        if not data or data.get("status") != "active":
            continue

        last_used = data.get("last_used")
        if last_used:
            try:
                last_dt = datetime.fromisoformat(last_used)
                if last_dt > cutoff:
                    continue  # 最近使用过，不衰减
            except Exception:
                pass

        # 衰减
        new_conf = data.get("confidence", 0.5) - DECAY_STEP
        if new_conf < MIN_CONFIDENCE:
            # 删除
            try:
                path.unlink()
                deleted += 1
            except Exception:
                pass
        else:
            data["confidence"] = round(new_conf, 2)
            _save_instinct(data)
            decayed += 1

    if deleted or decayed:
        logger.info(f"[instinct] 衰减完成: {decayed} 条降级, {deleted} 条删除")


def match_instincts(text: str, limit: int = 10) -> list[dict]:
    """
    匹配用户消息与活跃直觉的 trigger。
    返回匹配到的直觉列表，按 confidence 降序排列。
    """
    all_instincts = _load_all()
    matched = []

    text_lower = text.lower()
    for inst in all_instincts:
        if inst.get("status") != "active":
            continue
        trigger = inst.get("trigger", "").lower()
        if not trigger:
            continue

        # 关键词匹配：trigger 中的关键词出现在 text 中
        keywords = [w.strip() for w in re.split(r"[,，;；|/、]", trigger) if w.strip()]
        if any(kw in text_lower for kw in keywords):
            matched.append(inst)
            # 自动 boost 命中的直觉
            boost(inst.get("id", ""))

    # 按 confidence 降序
    matched.sort(key=lambda x: x.get("confidence", 0), reverse=True)
    return matched[:limit]


def get_instinct_context(text: str) -> str:
    """格式化匹配到的直觉为注入 prompt 的文本"""
    matched = match_instincts(text)
    if not matched:
        return ""

    lines = ["[行为直觉 — 请遵循以下已学习的规则]"]
    for inst in matched:
        conf = inst.get("confidence", 0)
        lines.append(f"- 当「{inst.get('trigger', '')}」时 → {inst.get('action', '')} (置信度: {conf:.0%})")

    return "\n".join(lines) + "\n"


def get_instinct_list() -> str:
    """格式化所有直觉列表，供 /instinct 命令使用"""
    all_instincts = _load_all()
    if not all_instincts:
        return "暂无直觉规则。使用 `/instinct create <trigger> | <action>` 创建。"

    # 按状态分组
    active = [i for i in all_instincts if i.get("status") == "active"]
    pending = [i for i in all_instincts if i.get("status") == "pending"]
    inactive = [i for i in all_instincts if i.get("status") == "inactive"]

    lines = ["**🧠 直觉系统**\n"]

    if pending:
        lines.append(f"**待审批** ({len(pending)} 条):")
        for inst in pending:
            iid = inst.get("id", "?")
            lines.append(f"  `{iid}` [{inst.get('source', '?')}] 当「{inst.get('trigger', '')}」→ {inst.get('action', '')}")
        lines.append(f"  用 `/instinct approve <id>` 或 `/instinct reject <id>` 处理\n")

    if active:
        lines.append(f"**已激活** ({len(active)} 条):")
        for inst in active:
            iid = inst.get("id", "?")
            conf = inst.get("confidence", 0)
            uses = inst.get("use_count", 0)
            lines.append(f"  `{iid}` 当「{inst.get('trigger', '')}」→ {inst.get('action', '')} ({conf:.0%}, 用{uses}次)")

    if inactive:
        lines.append(f"\n**已停用** ({len(inactive)} 条)")

    lines.append(f"\n**操作**: approve/reject/create `<trigger> | <action>`")
    return "\n".join(lines)


def evolve_instincts(min_confidence: float = 0.50, min_cluster_size: int = 3) -> str:
    """
    将相似直觉聚类为完整技能文件。

    流程：
    1. 加载所有 active 且 confidence >= min_confidence 的直觉
    2. 按 domain 分组
    3. 每个 domain 有 >= min_cluster_size 条直觉时触发聚类
    4. 用 Claude CLI (Haiku) 将该 domain 的直觉合成一个完整 SOP
    5. 写入 ~/.feishu-claude/skills/{domain}.md
    6. 标记已进化的直觉 status = "evolved"
    """
    all_instincts = _load_all()

    # 按 domain 分组
    from collections import defaultdict
    domain_groups = defaultdict(list)
    for inst in all_instincts:
        if inst.get("status") != "active":
            continue
        if inst.get("confidence", 0) < min_confidence:
            continue
        domain = inst.get("domain", "general")
        domain_groups[domain].append(inst)

    # 筛选可进化的 domain
    evolvable = {d: insts for d, insts in domain_groups.items() if len(insts) >= min_cluster_size}

    if not evolvable:
        return "暂无可进化的直觉组（需要同一 domain 至少 3 条活跃直觉）"

    skills_dir = Path.home() / ".feishu-claude" / "skills"
    os.makedirs(skills_dir, exist_ok=True)

    results = []
    for domain, instincts in evolvable.items():
        try:
            skill_content = _synthesize_skill(domain, instincts)
            if skill_content:
                skill_path = skills_dir / f"{domain}.md"
                skill_path.write_text(skill_content, encoding="utf-8")

                # 标记已进化
                for inst in instincts:
                    inst_id = inst.get("id", "")
                    if inst_id:
                        _mark_evolved(inst_id)

                results.append(f"✅ {domain}: {len(instincts)} 条直觉 → skills/{domain}.md")
            else:
                results.append(f"⚠️ {domain}: 合成失败")
        except Exception as e:
            results.append(f"❌ {domain}: {e}")

    return "\n".join(results) if results else "进化完成但无输出"


def _synthesize_skill(domain: str, instincts: list) -> str:
    """用 Claude CLI Haiku 将一组直觉合成为完整 SOP"""
    import subprocess
    import shutil

    claude_cli = os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude"

    # 格式化直觉列表
    instinct_text = "\n".join(
        f"- 当「{i.get('trigger', '')}」时 → {i.get('action', '')} (置信度: {i.get('confidence', 0):.0%})"
        for i in instincts
    )

    prompt = f"""请将以下 {len(instincts)} 条行为直觉合成为一个完整的 SOP（标准作业程序）。

领域：{domain}

直觉列表：
{instinct_text}

要求：
1. 用 Markdown 格式输出
2. 包含：## 触发条件、## 执行步骤、## 注意事项、## 禁忌
3. 将重复/相似的直觉合并为一个步骤
4. 保持简洁，每个步骤一行
5. 开头加 YAML frontmatter：name, domain, source_count, created_at

直接输出 Markdown："""

    try:
        result = subprocess.run(
            [claude_cli, "--print", "--model", "claude-haiku-4-5-20251001", "-p", prompt],
            capture_output=True, text=True, timeout=120,
        )
        return result.stdout.strip() if result.stdout.strip() else None
    except Exception as e:
        logger.error(f"[evolve] 合成失败: {e}")
        return None


def _mark_evolved(instinct_id: str):
    """标记直觉为已进化"""
    for path in _list_instinct_files():
        if instinct_id in path.stem:
            data = _load_instinct(path)
            if data:
                data["status"] = "evolved"
                _save_instinct(data)
                return
