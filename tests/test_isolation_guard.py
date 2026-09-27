"""证明 tests/conftest.py 的隔离真的生效：路径全在临时目录、生产库写不进、外网连不上。"""
import os
import socket
import sqlite3
import sys

import pytest

import conftest as lit_conftest


def test_settings_resolve_under_session_tmp(capsys):
    import utils

    tmp = str(lit_conftest._SESSION_TMP)
    settings = utils.get_settings()
    report = {}
    for name in type(settings).model_fields:
        if name.endswith(("_DIR", "_PATH")) and getattr(settings, name):
            report[name] = os.path.realpath(os.path.abspath(getattr(settings, name)))
    report["_data_dir"] = os.path.realpath(utils._data_dir())
    with capsys.disabled():
        print(f"\n[isolation] session tmp = {tmp}")
        for k, v in sorted(report.items()):
            print(f"[isolation]   {k} = {v}")
    assert report, "no path settings found"
    for k, v in report.items():
        assert v.startswith(tmp + os.sep), f"{k}={v} escaped the session tmp dir"
        assert not v.startswith("/mnt/ripe"), f"{k}={v}"


def test_dotenv_and_tee_disabled():
    assert os.environ.get("PYTHON_DOTENV_DISABLED") == "1"
    assert type(sys.stdout).__name__ != "TeeLogger"
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert k not in os.environ


def test_sqlite_guard_blocks_production_paths():
    with pytest.raises(sqlite3.OperationalError, match="isolation guard"):
        sqlite3.connect("/mnt/ripe/literature_analyzer_data/batch_tracking.db")
    with pytest.raises(sqlite3.OperationalError, match="isolation guard"):
        sqlite3.connect("file:/mnt/ripe/literature_analyzer_data/batch_tracking.db?mode=rw", uri=True)
    if os.environ.get("LIT_ALLOW_PROD_READONLY") != "1":
        with pytest.raises(sqlite3.OperationalError, match="isolation guard"):
            sqlite3.connect("file:/mnt/ripe/literature_analyzer_data/batch_tracking.db?mode=ro", uri=True)
    lit_conftest._VIOLATIONS.clear()  # 上面是故意触发的，不计入会话汇总
    sqlite3.connect(":memory:").close()


def test_socket_guard_blocks_external_hosts():
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("api.openalex.org", 443)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ConnectionRefusedError):
            s.connect(("93.184.216.34", 443))
    finally:
        s.close()
    lit_conftest._NETWORK_ATTEMPTS.pop(
        "tests/test_isolation_guard.py::test_socket_guard_blocks_external_hosts", None)


def test_default_databases_are_seeded():
    import lit_seed
    import utils

    conn = sqlite3.connect(f"file:{utils.get_settings().DB_PATH}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == len(lit_seed.PAPERS)
    finally:
        conn.close()
