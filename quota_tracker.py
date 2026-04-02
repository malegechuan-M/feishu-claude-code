"""Self-learning quota tracker with auto-degradation."""
import sqlite3
import time
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = Path.home() / ".feishu-claude" / "quota.db"

# 模型降级链：超限后按此顺序降级
FALLBACK_CHAIN = {
    "claude-opus-4-6": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-haiku-4-5-20251001",
    "claude-haiku-4-5-20251001": "external",  # 转发给 OpenClaw 或降级为最低
}

# 冷却时间（秒）
COOLDOWN_TEMP = 15 * 60        # 临时限流 15 分钟
COOLDOWN_CONFIRMED = 2 * 3600  # 确认限流 2 小时

# 最小可靠调用次数（低于此次数不学习阈值，避免误判）
MIN_RELIABLE_CALLS = {
    "claude-opus-4-6": 5,
    "claude-sonnet-4-6": 10,
    "claude-haiku-4-5-20251001": 20,
}

# 预警阈值比例：当前用量达到已知阈值的 80% 时触发降级
WARN_RATIO = 0.80


class QuotaTracker:
    def __init__(self):
        """初始化 SQLite 数据库，创建配额记录表和阈值学习表。"""
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        # WAL 模式：与 context_dag.py / long_task.py 保持一致
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_tables()

    def _init_tables(self):
        """创建所有必要的数据表（幂等）。"""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS quota_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                model       TEXT NOT NULL,
                timestamp   REAL NOT NULL,      -- Unix 时间戳
                hour_bucket TEXT NOT NULL        -- YYYY-MM-DD-HH
            );

            CREATE INDEX IF NOT EXISTS idx_quota_log_model_hour
                ON quota_log(model, hour_bucket);

            CREATE TABLE IF NOT EXISTS quota_thresholds (
                model       TEXT PRIMARY KEY,
                hourly_limit INTEGER NOT NULL,   -- 每小时最大调用次数（学习所得）
                learned_at  TEXT NOT NULL        -- 学习时间 ISO8601
            );

            CREATE TABLE IF NOT EXISTS cooldowns (
                model       TEXT PRIMARY KEY,
                until_ts    REAL NOT NULL,       -- 冷却结束时间戳
                level       TEXT NOT NULL        -- 'temp' | 'confirmed'
            );
        """)
        self._conn.commit()

    def _hour_bucket(self) -> str:
        """返回当前小时桶标识，格式 YYYY-MM-DD-HH。"""
        return datetime.now().strftime("%Y-%m-%d-%H")

    def record_call(self, model: str):
        """
        记录一次模型调用。
        写入 quota_log 表，用于后续阈值学习和用量统计。
        """
        try:
            self._conn.execute(
                "INSERT INTO quota_log (model, timestamp, hour_bucket) VALUES (?, ?, ?)",
                (model, time.time(), self._hour_bucket()),
            )
            self._conn.commit()
            logger.debug(f"[quota] 记录调用: model={model}")
        except Exception as e:
            logger.error(f"[quota] record_call 失败: {e}")

    def _calls_this_hour(self, model: str) -> int:
        """查询当前小时内指定模型的调用次数。"""
        try:
            bucket = self._hour_bucket()
            row = self._conn.execute(
                "SELECT COUNT(*) FROM quota_log WHERE model=? AND hour_bucket=?",
                (model, bucket),
            ).fetchone()
            return row[0] if row else 0
        except Exception as e:
            logger.error(f"[quota] _calls_this_hour 失败: {e}")
            return 0

    def _known_limit(self, model: str) -> int | None:
        """从 quota_thresholds 表读取该模型的已学习阈值，未知则返回 None。"""
        try:
            row = self._conn.execute(
                "SELECT hourly_limit FROM quota_thresholds WHERE model=?",
                (model,),
            ).fetchone()
            return row[0] if row else None
        except Exception as e:
            logger.error(f"[quota] _known_limit 失败: {e}")
            return None

    def check_quota(self, model: str) -> bool:
        """
        检查模型是否应该降级。

        Returns:
            True  — 可以正常使用该模型
            False — 应该降级到下一个模型
        """
        # 1. 检查冷却状态
        if self.is_cooling_down(model):
            logger.info(f"[quota] {model} 冷却中，触发降级")
            return False

        # 2. 检查已知阈值
        known_limit = self._known_limit(model)
        if known_limit is None:
            # 未学习到阈值，不降级
            return True

        current = self._calls_this_hour(model)
        warn_threshold = int(known_limit * WARN_RATIO)

        if current >= warn_threshold:
            logger.warning(
                f"[quota] {model} 本小时已调用 {current} 次，"
                f"达到预警阈值 {warn_threshold}（已知限制 {known_limit}），触发降级"
            )
            return False

        return True

    def on_rate_limit(self, model: str):
        """
        被限流时调用：学习阈值并设置冷却时间。

        学习规则：仅当当前小时调用次数超过该模型的最小可靠次数时，
        才记录为可信阈值，避免单次异常导致误判。
        """
        try:
            current_calls = self._calls_this_hour(model)
            min_reliable = MIN_RELIABLE_CALLS.get(model, 5)

            if current_calls >= min_reliable:
                # 学习：将当前调用次数记录为该模型的每小时限制
                self._conn.execute(
                    """INSERT INTO quota_thresholds (model, hourly_limit, learned_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(model) DO UPDATE SET
                           hourly_limit = MIN(hourly_limit, excluded.hourly_limit),
                           learned_at = excluded.learned_at""",
                    (model, current_calls, datetime.now().isoformat()),
                )
                logger.info(
                    f"[quota] 学习到 {model} 阈值: {current_calls} 次/小时"
                )
            else:
                logger.info(
                    f"[quota] {model} 调用次数 {current_calls} 低于最小可靠次数 {min_reliable}，"
                    "不学习阈值（可能是临时限流）"
                )

            # 设置冷却：首次是临时冷却，再次触发升级为确认冷却
            existing = self._conn.execute(
                "SELECT level FROM cooldowns WHERE model=?", (model,)
            ).fetchone()

            if existing and existing[0] == "temp":
                # 已有临时冷却还被限流了，升级为确认冷却
                until_ts = time.time() + COOLDOWN_CONFIRMED
                level = "confirmed"
            else:
                until_ts = time.time() + COOLDOWN_TEMP
                level = "temp"

            self._conn.execute(
                """INSERT INTO cooldowns (model, until_ts, level)
                   VALUES (?, ?, ?)
                   ON CONFLICT(model) DO UPDATE SET
                       until_ts = excluded.until_ts,
                       level = excluded.level""",
                (model, until_ts, level),
            )
            self._conn.commit()

            logger.warning(
                f"[quota] {model} 进入 {level} 冷却，"
                f"解除时间: {datetime.fromtimestamp(until_ts):%H:%M:%S}"
            )

        except Exception as e:
            logger.error(f"[quota] on_rate_limit 处理失败: {e}")

    def get_fallback_model(self, model: str) -> str:
        """
        返回降级链中下一个模型名。
        如果当前模型不在降级链中，返回 haiku 作为兜底。
        """
        return FALLBACK_CHAIN.get(model, "claude-haiku-4-5-20251001")

    def is_cooling_down(self, model: str) -> bool:
        """检查模型是否仍在冷却期内。冷却期已过时自动清除记录。"""
        try:
            row = self._conn.execute(
                "SELECT until_ts FROM cooldowns WHERE model=?", (model,)
            ).fetchone()
            if not row:
                return False
            if time.time() < row[0]:
                return True
            # 冷却已过期，清除记录
            self._conn.execute("DELETE FROM cooldowns WHERE model=?", (model,))
            self._conn.commit()
            logger.info(f"[quota] {model} 冷却期已结束")
            return False
        except Exception as e:
            logger.error(f"[quota] is_cooling_down 失败: {e}")
            return False

    def get_status(self) -> str:
        """
        格式化当前配额状态，供 /quota 命令展示。
        包含：本小时各模型调用量、已学习阈值、冷却状态。
        """
        try:
            lines = ["**📊 配额状态**\n"]
            bucket = self._hour_bucket()

            all_models = list(FALLBACK_CHAIN.keys()) + ["claude-opus-4-6"]
            # 去重保序
            seen = set()
            models = []
            for m in all_models:
                if m not in seen:
                    seen.add(m)
                    models.append(m)

            for model in models:
                # 本小时调用次数
                calls = self._calls_this_hour(model)
                # 已知阈值
                known = self._known_limit(model)
                # 冷却状态
                cooling = self.is_cooling_down(model)

                model_short = model.replace("claude-", "").replace("-4-6", "").replace("-4-5-20251001", "")

                if cooling:
                    row = self._conn.execute(
                        "SELECT until_ts, level FROM cooldowns WHERE model=?", (model,)
                    ).fetchone()
                    if row:
                        remaining = max(0, int(row[0] - time.time()))
                        mins = remaining // 60
                        status = f"❄️ 冷却中 ({row[1]}, 剩余 {mins}分钟)"
                    else:
                        status = "❄️ 冷却中"
                elif known:
                    warn = int(known * WARN_RATIO)
                    pct = int(calls / known * 100)
                    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                    status = f"{bar} {calls}/{known} ({pct}%，预警>{warn})"
                else:
                    status = f"{calls} 次（阈值未学习）"

                lines.append(f"**{model_short}**: {status}")

            # 显示降级链
            lines.append("\n**降级链**: opus → sonnet → haiku → external")

            # 统计今日总调用
            try:
                today = datetime.now().strftime("%Y-%m-%d")
                total = self._conn.execute(
                    "SELECT COUNT(*) FROM quota_log WHERE hour_bucket LIKE ?",
                    (f"{today}%",),
                ).fetchone()[0]
                lines.append(f"\n今日总调用: {total} 次")
            except Exception:
                pass

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"[quota] get_status 失败: {e}")
            return f"❌ 获取配额状态失败: {e}"

    def close(self):
        """关闭数据库连接。"""
        try:
            self._conn.close()
        except Exception:
            pass


# 全局单例，模块加载时初始化
try:
    tracker = QuotaTracker()
except Exception as _e:
    logger.error(f"[quota] QuotaTracker 初始化失败: {_e}")
    # 提供一个降级的 no-op 对象，确保导入不会崩溃主流程

    class _NoopTracker:
        def record_call(self, model): pass
        def check_quota(self, model): return True
        def on_rate_limit(self, model): pass
        def get_fallback_model(self, model): return FALLBACK_CHAIN.get(model, model)
        def get_status(self): return "❌ QuotaTracker 初始化失败"
        def is_cooling_down(self, model): return False

    tracker = _NoopTracker()
