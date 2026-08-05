"""TeeLogger 按天分文件 + 过期清理的单元测试。"""
import io
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils
from utils import TeeLogger, _log_retention_days


class _FakeDatetime:
    """只替 datetime.now()，让测试能自由拨表。"""
    _now = datetime(2026, 8, 1, 23, 59, 30)

    @classmethod
    def now(cls):
        return cls._now


def _freeze(monkeypatch, when):
    _FakeDatetime._now = when
    monkeypatch.setattr(utils, "datetime", _FakeDatetime)


def test_tee_logger_rolls_over_at_midnight(tmp_path, monkeypatch):
    """常驻进程跨天必须换文件。

    日期一旦在 __init__ 里定死，daemon 连跑一周的日志会全堆进它启动那天的文件，
    分天等于白分——这里锁的就是"写入时现算日期"这个前提。
    """
    _freeze(monkeypatch, datetime(2026, 8, 1, 23, 59, 30))
    tee = TeeLogger(str(tmp_path), io.StringIO(), retention_days=0)
    tee.write("昨天的事\n")

    _freeze(monkeypatch, datetime(2026, 8, 2, 0, 0, 30))     # 同一个进程，跨了天
    tee.write("今天的事\n")
    tee.flush()

    assert (tmp_path / "app-2026-08-01.log").read_text(encoding="utf-8") == "昨天的事\n"
    assert (tmp_path / "app-2026-08-02.log").read_text(encoding="utf-8") == "今天的事\n"


def test_tee_logger_appends_without_truncating(tmp_path, monkeypatch):
    """同一天里第二个进程接着写，不能把前一个进程的日志截掉。"""
    _freeze(monkeypatch, datetime(2026, 8, 1, 10, 0, 0))
    first = TeeLogger(str(tmp_path), io.StringIO(), retention_days=0)
    first.write("daemon 写的\n")
    first.flush()

    second = TeeLogger(str(tmp_path), io.StringIO(), retention_days=0)
    second.write("CLI 写的\n")
    second.flush()

    assert (tmp_path / "app-2026-08-01.log").read_text(encoding="utf-8") == "daemon 写的\nCLI 写的\n"


def test_tee_logger_prunes_expired_logs(tmp_path, monkeypatch):
    """跨天换文件时顺手删过期日志，保留期内的和别人的文件都不许动。"""
    old = tmp_path / "app-2026-06-01.log"
    old.write_text("很久以前", encoding="utf-8")
    os.utime(old, (time.time() - 40 * 86400,) * 2)

    fresh = tmp_path / "app-2026-07-30.log"
    fresh.write_text("前天", encoding="utf-8")

    other = tmp_path / "pipeline_history.jsonl"      # 不是本 prefix 的，别误删
    other.write_text("{}", encoding="utf-8")
    os.utime(other, (time.time() - 400 * 86400,) * 2)

    _freeze(monkeypatch, datetime(2026, 8, 1, 3, 0, 0))
    TeeLogger(str(tmp_path), io.StringIO(), retention_days=30).write("今天\n")

    assert not old.exists()
    assert fresh.exists()
    assert other.exists()


def test_tee_logger_retention_zero_keeps_everything(tmp_path, monkeypatch):
    """配 0 = 永久保留，别把人家的历史悄悄清了。"""
    old = tmp_path / "app-2020-01-01.log"
    old.write_text("上古", encoding="utf-8")
    os.utime(old, (time.time() - 2000 * 86400,) * 2)

    _freeze(monkeypatch, datetime(2026, 8, 1, 3, 0, 0))
    TeeLogger(str(tmp_path), io.StringIO(), retention_days=0).write("今天\n")

    assert old.exists()


def test_tee_logger_survives_unwritable_dir(monkeypatch):
    """日志目录写不了时只能吞掉：这段在 import 期装到 sys.stdout 上，
    往终端打印时抛异常会把整条管道带崩。"""
    tee = TeeLogger("/proc/nonexistent_dir", io.StringIO(), retention_days=0)
    tee.write("照样得回到终端\n")        # 不抛异常即为通过
    assert tee.terminal.getvalue() == "照样得回到终端\n"


def test_tee_logger_survives_non_utf8_terminal(tmp_path):
    """GBK 等终端显示不了 emoji 时不能拖垮 CLI，UTF-8 日志仍应保留原文。"""
    raw = io.BytesIO()
    terminal = io.TextIOWrapper(raw, encoding="gbk")
    tee = TeeLogger(str(tmp_path), terminal, retention_days=0)

    tee.write("状态📚正常\n")
    tee.flush()

    terminal.seek(0)
    assert "状态" in terminal.read()
    with open(tee.current_path(), encoding="utf-8") as log_file:
        assert log_file.read() == "状态📚正常\n"


def test_log_retention_days_falls_back_on_garbage(monkeypatch):
    """这个函数在 import 期跑，配错值抛异常会让整个项目起不来。"""
    monkeypatch.setenv("LOG_RETENTION_DAYS", "三十天")
    assert _log_retention_days() == 30

    monkeypatch.setenv("LOG_RETENTION_DAYS", "7")
    assert _log_retention_days() == 7

    monkeypatch.delenv("LOG_RETENTION_DAYS")
    assert _log_retention_days() == 30
