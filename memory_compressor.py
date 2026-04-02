"""Memory file compression and archival."""
import asyncio
import os
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# 各记忆文件的大小阈值（字节），超过则触发压缩
THRESHOLDS = {
    "MEMORY.md": 8 * 1024,      # 8KB
    "LEARNINGS.md": 6 * 1024,   # 6KB
    "ERRORS.md": 5 * 1024,      # 5KB
    "DECISIONS.md": 5 * 1024,   # 5KB
    "PATTERNS.md": 4 * 1024,    # 4KB
}

# 与 memory_local.py 保持一致的路径常量
ARCHIVE_DIR = Path.home() / ".feishu-claude" / "archive"
BRAIN_DIR = Path.home() / ".feishu-claude" / "brain"
LEARNINGS_DIR = Path.home() / ".feishu-claude" / "learnings"

KEEP_DAYS = 14  # 保留最近14天的条目，不压缩

# 各文件对应的存储目录映射
_FILE_DIRS = {
    "MEMORY.md": BRAIN_DIR,
    "LEARNINGS.md": LEARNINGS_DIR,
    "ERRORS.md": LEARNINGS_DIR,
    "DECISIONS.md": BRAIN_DIR,
    "PATTERNS.md": BRAIN_DIR,
}


def check_and_compress():
    """
    检查所有记忆文件大小，超过阈值时执行压缩归档。
    该函数由 daily_review.py 在每日复盘结束后调用。
    异常不会传播，避免影响主流程。
    """
    # 确保归档目录存在
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    for filename, threshold in THRESHOLDS.items():
        file_dir = _FILE_DIRS.get(filename)
        if not file_dir:
            continue
        file_path = file_dir / filename

        try:
            if not file_path.exists():
                continue

            size = file_path.stat().st_size
            if size <= threshold:
                logger.info(
                    f"[memory_compressor] {filename} 大小 {size}B，未超阈值 {threshold}B，跳过"
                )
                continue

            logger.info(
                f"[memory_compressor] {filename} 大小 {size}B 超过 {threshold}B，开始压缩..."
            )

            content = file_path.read_text(encoding="utf-8")

            # 按日期拆分：保留最近 KEEP_DAYS 天，旧内容压缩
            old_content, new_content = _split_by_date(content, KEEP_DAYS)

            if not old_content.strip():
                logger.info(f"[memory_compressor] {filename} 无旧内容可压缩，跳过")
                continue

            # 归档原始旧内容
            _archive(filename, old_content)

            # 调用 Haiku 压缩旧内容为精华摘要
            compressed = _compress_with_haiku(old_content, filename)

            if not compressed:
                # 压缩失败时保守处理：只归档，不截断文件
                logger.warning(
                    f"[memory_compressor] {filename} Haiku 压缩失败，仅归档不截断"
                )
                continue

            # 将压缩摘要 + 新内容写回原文件
            separator = f"\n\n---\n<!-- 以上为 {datetime.now():%Y-%m-%d} 前历史压缩摘要 -->\n---\n\n"
            combined = compressed + separator + new_content
            file_path.write_text(combined, encoding="utf-8")

            new_size = file_path.stat().st_size
            logger.info(
                f"[memory_compressor] {filename} 压缩完成: {size}B → {new_size}B"
            )

        except Exception as e:
            # 单个文件失败不影响其他文件处理
            logger.error(f"[memory_compressor] 处理 {filename} 时出错: {e}", exc_info=True)


def _split_by_date(content: str, keep_days: int) -> tuple[str, str]:
    """
    按日期标记拆分内容为旧/新两部分。

    支持的日期格式：
    - ## YYYY-MM-DD
    - ## ERR-YYYYMMDD-xxx / ## LRN-YYYYMMDD-xxx
    - [YYYY-MM-DD ...]
    - YYYY-MM-DD

    Args:
        content: 文件完整内容
        keep_days: 保留最近 N 天

    Returns:
        (old_content, new_content) — 旧部分（待压缩）和新部分（保留）
    """
    cutoff = datetime.now() - timedelta(days=keep_days)
    lines = content.split("\n")

    # 找到第一条"足够新"的条目起始行
    new_start_line = None

    # 匹配各种日期格式
    import re
    date_patterns = [
        re.compile(r"##\s+(\d{4}-\d{2}-\d{2})"),                       # ## 2026-03-01
        re.compile(r"##\s+(?:ERR|LRN)-(\d{4})(\d{2})(\d{2})-\d+"),    # ## ERR-20260301-001
        re.compile(r"\[(\d{4}-\d{2}-\d{2})\s"),                        # [2026-03-01 ...]
        re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s"),                      # 2026-03-01 ...
    ]

    for i, line in enumerate(lines):
        entry_date = None
        for pat in date_patterns:
            m = pat.search(line)
            if m:
                try:
                    if len(m.groups()) == 3:
                        # ERR-YYYYMMDD 格式
                        y, mo, d = m.group(1), m.group(2), m.group(3)
                        entry_date = datetime(int(y), int(mo), int(d))
                    else:
                        date_str = m.group(1)
                        entry_date = datetime.strptime(date_str, "%Y-%m-%d")
                except (ValueError, IndexError):
                    continue
                break

        if entry_date and entry_date >= cutoff:
            new_start_line = i
            break

    if new_start_line is None:
        # 没有找到足够新的条目，全部为旧内容
        return content, ""
    elif new_start_line == 0:
        # 第一行就是新内容，无旧内容需要压缩
        return "", content

    old_content = "\n".join(lines[:new_start_line])
    new_content = "\n".join(lines[new_start_line:])
    return old_content, new_content


def _compress_with_haiku(old_content: str, filename: str = "") -> str:
    """
    调用 Haiku（通过 claude CLI subprocess）将旧条目压缩为精华摘要。
    使用 subprocess 而非 chat_haiku API，与项目中 daily_review.py 风格一致。

    Args:
        old_content: 待压缩的旧内容
        filename: 文件名（用于定制 prompt）

    Returns:
        压缩后的摘要文本，失败时返回空字符串
    """
    import subprocess
    import shutil

    claude_cli = os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude"

    file_hint = f"（文件：{filename}）" if filename else ""
    prompt = f"""请将以下记忆文件的历史条目{file_hint}压缩为精华摘要。

要求：
1. 保留所有关键决策、经验教训、重要规律
2. 去除重复内容和无关细节
3. 用简洁的 Markdown 列表格式输出
4. 保留原始条目 ID（如 ERR-xxx、LRN-xxx）供追溯
5. 摘要长度不超过原内容的 30%

【待压缩内容】
{old_content[:6000]}

请直接输出压缩后的摘要，不要加额外说明："""

    try:
        result = subprocess.run(
            [claude_cli, "--print", "--model", "claude-haiku-4-5-20251001", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
        summary = result.stdout.strip()
        if summary:
            header = f"## 历史压缩摘要（截至 {datetime.now():%Y-%m-%d}）\n\n"
            return header + summary
        return ""
    except Exception as e:
        logger.error(f"[memory_compressor] Haiku 压缩调用失败: {e}")
        return ""


def _archive(filename: str, content: str):
    """
    将旧内容归档到 archive 目录，文件名带年月前缀以便按时间查阅。

    Args:
        filename: 原始文件名（如 MEMORY.md）
        content: 待归档的内容
    """
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    archive_name = f"{datetime.now():%Y-%m}-{filename}"
    archive_path = ARCHIVE_DIR / archive_name

    # 如果同月已有归档，追加而非覆盖
    mode = "a" if archive_path.exists() else "w"
    try:
        with open(archive_path, mode, encoding="utf-8") as f:
            if mode == "a":
                f.write(f"\n\n<!-- 追加于 {datetime.now():%Y-%m-%d %H:%M} -->\n\n")
            f.write(content)
        logger.info(f"[memory_compressor] 已归档 {len(content)} 字节 → {archive_path}")
    except Exception as e:
        logger.error(f"[memory_compressor] 归档失败 {archive_path}: {e}")
