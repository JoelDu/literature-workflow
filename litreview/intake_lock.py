"""litreview/intake_lock.py — 入库流水线的跨进程互斥锁。

Web 手动触发 (/api/pipeline/run) 与 daemon 定时任务各自扫描 INPUT_PDF_DIR，
没有互斥时同一 PDF 可能被两个进程同时解析、重复计费。这里用 DB_PATH 同目录的
``.intake.lock`` 文件加 fcntl.flock 独占锁：进程崩溃时内核自动释放，不会留死锁。
Windows 无 fcntl，退化为不加锁（no-op）。

用法::

    from litreview.intake_lock import intake_lock, IntakeBusyError
    try:
        with intake_lock():          # 默认非阻塞，已被占用立即抛 IntakeBusyError
            pipeline.main()
    except IntakeBusyError:
        ...  # 另一进程正在入库，本轮跳过
"""
import os
from contextlib import contextmanager
from typing import Iterator, Optional

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

LOCK_FILENAME = ".intake.lock"


class IntakeBusyError(RuntimeError):
    """另一进程/线程已持有入库锁。"""


def intake_lock_path(db_path: Optional[str] = None) -> str:
    db_path = db_path or os.getenv("DB_PATH", "./data/batch_tracking.db")
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), LOCK_FILENAME)


@contextmanager
def intake_lock(db_path: Optional[str] = None, blocking: bool = False) -> Iterator[str]:
    """持有入库独占锁期间执行 with 块；锁已被占用且 blocking=False 时抛 IntakeBusyError。

    flock 按打开的文件描述归属，所以同一进程里的两个线程各自 open 也会互斥。
    """
    path = intake_lock_path(db_path)
    if fcntl is None:
        yield path
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+")
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fh.fileno(), flags)
        except BlockingIOError as e:
            raise IntakeBusyError(f"入库流水线正被其他进程占用（锁文件 {path}）") from e
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
            yield path
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()
