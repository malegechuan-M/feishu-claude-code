"""
简单的持久化定时任务调度器。
任务存储在 ~/.feishu-claude/schedules.json
支持 cron 表达式和每天固定时间。
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime
from typing import Callable, Optional

SCHEDULES_FILE = os.path.expanduser("~/.feishu-claude/schedules.json")


def _load_schedules() -> list[dict]:
    if not os.path.exists(SCHEDULES_FILE):
        return []
    try:
        with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_schedules(schedules: list[dict]):
    os.makedirs(os.path.dirname(SCHEDULES_FILE), exist_ok=True)
    with open(SCHEDULES_FILE, "w", encoding="utf-8") as f:
        json.dump(schedules, f, ensure_ascii=False, indent=2)


def add_schedule(chat_id: str, cron_expr: str, task: str) -> str:
    """添加一个定时任务，返回任务 ID。"""
    schedules = _load_schedules()
    task_id = f"task_{int(time.time())}"
    schedules.append({
        "id": task_id,
        "chat_id": chat_id,
        "cron": cron_expr,
        "task": task,
        "created_at": datetime.now().isoformat(),
        "enabled": True,
    })
    _save_schedules(schedules)
    return task_id


def remove_schedule(task_id: str) -> bool:
    """删除定时任务，返回是否成功。"""
    schedules = _load_schedules()
    new = [s for s in schedules if s["id"] != task_id]
    if len(new) == len(schedules):
        return False
    _save_schedules(new)
    return True


def list_schedules(chat_id: Optional[str] = None) -> list[dict]:
    """列出定时任务（可按 chat_id 过滤）。"""
    schedules = _load_schedules()
    if chat_id:
        return [s for s in schedules if s["chat_id"] == chat_id]
    return schedules


def _cron_matches(cron_expr: str, now: datetime) -> bool:
    """
    支持：
    - "every Xm"   — 每 X 分钟
    - "HH:MM"      — 每天固定时间
    - "*/5 * * * *" 等标准 5 字段 cron（分 时 日 月 周）
    """
    cron_expr = cron_expr.strip()

    # 每 X 分钟
    m = re.match(r"every\s+(\d+)m", cron_expr)
    if m:
        interval = int(m.group(1))
        return now.minute % interval == 0

    # 每天固定时间 HH:MM
    m = re.match(r"^(\d{1,2}):(\d{2})$", cron_expr)
    if m:
        return now.hour == int(m.group(1)) and now.minute == int(m.group(2))

    # 标准 5 字段 cron
    parts = cron_expr.split()
    if len(parts) != 5:
        return False

    def _match_field(field: str, value: int) -> bool:
        if field == "*":
            return True
        if field.startswith("*/"):
            step = int(field[2:])
            return value % step == 0
        if "-" in field:
            a, b = field.split("-")
            return int(a) <= value <= int(b)
        return int(field) == value

    minute, hour, day, month, weekday = parts
    return (
        _match_field(minute, now.minute)
        and _match_field(hour, now.hour)
        and _match_field(day, now.day)
        and _match_field(month, now.month)
        and _match_field(weekday, now.weekday())
    )


async def run_scheduler(on_trigger: Callable):
    """后台循环，每分钟整点检查触发。"""
    print("[scheduler] 定时任务调度器已启动", flush=True)
    while True:
        now = datetime.now()
        seconds_to_next = 60 - now.second
        await asyncio.sleep(seconds_to_next)

        now = datetime.now()
        schedules = _load_schedules()
        for s in schedules:
            if not s.get("enabled", True):
                continue
            try:
                if _cron_matches(s["cron"], now):
                    print(f"[scheduler] 触发任务 {s['id']}: {s['task'][:40]}", flush=True)
                    if asyncio.iscoroutinefunction(on_trigger):
                        await on_trigger(s["chat_id"], s["task"])
                    else:
                        on_trigger(s["chat_id"], s["task"])
            except Exception as e:
                print(f"[scheduler] 任务 {s['id']} 触发失败: {e}", flush=True)
