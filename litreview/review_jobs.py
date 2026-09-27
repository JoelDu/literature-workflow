"""Durable Web review snapshots, independent of the read-only literature database.

One Web server process owns this registry, matching the in-process worker lifecycle.
"""
import json
import sqlite3
from pathlib import Path


class ReviewJobStore:
    def __init__(self, path):
        self.path = str(path)

    def _connect(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute('CREATE TABLE IF NOT EXISTS web_review_jobs '
                     '(job_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)')
        return conn

    def save(self, job):
        conn = self._connect()
        try:
            with conn:
                conn.execute('INSERT OR REPLACE INTO web_review_jobs VALUES (?, ?)',
                             (job['job_id'], json.dumps(job, ensure_ascii=False)))
        finally:
            conn.close()

    def load_all(self):
        conn = self._connect()
        try:
            return [json.loads(row[0]) for row in conn.execute('SELECT snapshot FROM web_review_jobs')]
        finally:
            conn.close()


# ── 额度暂停后的自动续跑 ──────────────────────────────────────────────────────
#
# 综述任务在 gpt-5.4 / mini 的 UTC 日额度耗尽时进入 paused_quota（而非 failed），
# 章节检查点完整保留。两个服务进程都没有通用调度循环，这里用去重的 threading.Timer
# 在 UTC 零点后（默认 +120 秒缓冲）回调入口层的 resume 函数；进程重启后由入口层
# 对持久化的 paused_quota 任务重新布置定时器。REVIEW_AUTO_RESUME_QUOTA=0 可关闭。

import os
import threading
from datetime import datetime, timezone

PAUSED_QUOTA = 'paused_quota'
RESUMABLE_STATUSES = ('interrupted', 'failed', 'partial', PAUSED_QUOTA)

_resume_timers: dict = {}
_resume_lock = threading.Lock()


def auto_resume_enabled() -> bool:
    return os.getenv('REVIEW_AUTO_RESUME_QUOTA', '1').strip().lower() not in {'0', 'false', 'no', 'off'}


def _parse_resume_at(resume_at) -> datetime:
    if isinstance(resume_at, datetime):
        value = resume_at
    else:
        value = datetime.fromisoformat(str(resume_at))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def schedule_quota_resume(job_id: str, resume_at, callback, grace_seconds: float = 120.0,
                          now: datetime | None = None) -> float | None:
    """在 resume_at（UTC 重置时刻）+ grace 后调用 callback(job_id)；同一任务只保留一个定时器。

    返回延迟秒数；自动续跑被禁用或参数无效时返回 None。
    """
    if not auto_resume_enabled() or not job_id or not resume_at:
        return None
    try:
        target = _parse_resume_at(resume_at)
    except (TypeError, ValueError):
        return None
    current = now or datetime.now(timezone.utc)
    delay = max(1.0, (target - current).total_seconds() + grace_seconds)

    def fire():
        with _resume_lock:
            if _resume_timers.get(job_id) is timer:
                _resume_timers.pop(job_id, None)
        callback(job_id)

    timer = threading.Timer(delay, fire)
    timer.daemon = True
    with _resume_lock:
        previous = _resume_timers.pop(job_id, None)
        if previous is not None:
            previous.cancel()
        _resume_timers[job_id] = timer
    timer.start()
    return delay


def cancel_quota_resume(job_id: str) -> bool:
    with _resume_lock:
        timer = _resume_timers.pop(job_id, None)
    if timer is None:
        return False
    timer.cancel()
    return True


def scheduled_quota_resumes() -> list:
    with _resume_lock:
        return sorted(_resume_timers)
