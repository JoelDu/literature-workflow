"""测试隔离：默认安全，不碰真实数据、不读 .env、不出网。

这个文件必须在任何项目模块被 import 之前生效，所以顶层代码的顺序很重要：
  1. 记下原始环境（给 network / prod_readonly 这两类显式 opt-in 的用例用）；
  2. 把每一个数据路径环境变量指向本次会话的临时目录；
  3. 关掉 .env 加载（python-dotenv>=1.2 认 PYTHON_DOTENV_DISABLED）、关掉 utils 的
     stdout/stderr 落盘（LIT_DISABLE_STDIO_TEE）、清掉代理变量；
  4. 装 sqlite3.connect 与 socket 两道运行期护栏；
  5. pytest_configure 里加载真实的 Settings 做一次硬校验，任何路径落在临时目录之外、
     或落在 /mnt/ripe / 真实 DATA_ROOT 下，直接 pytest.exit，一个用例都不跑。

以前这里只 setdefault 了几个假 Key：没配 DB_PATH 的机器上测试写仓库 data/，
配了 .env 的机器上 load_dotenv() 把 DB_PATH 指回 /mnt/ripe 生产库，
tests/test_web_api.py 的审批 POST 就真的落进了生产缓存库。

标记（见 pytest.ini，默认都不选）：
  network        需要真实外网（OpenAlex 等）。`pytest -m network` 显式运行，
                 这时才放开 socket 护栏并恢复原始代理变量。
  prod_readonly  需要作者机器上的真实生产库。还必须 LIT_ALLOW_PROD_READONLY=1，
                 且即便如此，护栏也只放行 `mode=ro` 的只读连接。
"""
import errno
import ipaddress
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent
_ORIG_ENV = dict(os.environ)

# ── 1. 会话临时目录与路径环境变量 ────────────────────────────────────────────

_SESSION_TMP = Path(tempfile.mkdtemp(prefix="lit-pytest-")).resolve()
_DATA = _SESSION_TMP / "data"
_DATA.mkdir()

_PATH_ENV = {
    "DATA_ROOT": _DATA,
    "DB_PATH": _DATA / "batch_tracking.db",
    "EXCEL_OUTPUT_PATH": _DATA / "knowledge_base.xlsx",
    "STAGING_PDF_DIR": _DATA / "staging_pdf",
    "EXTERNAL_DISCOVERY_CACHE_DB_PATH": _DATA / "external_discovery_cache.db",
    "TEST_CITATION_DB_PATH": _DATA / "test_citation_network.db",
    "CITATION_NETWORK_DB_PATH": _DATA / "test_citation_network.db",
    "WEB_REVIEW_JOBS_DB_PATH": _DATA / "web_review_jobs.db",
    "OBSIDIAN_VAULT_DIR": _SESSION_TMP / "obsidian_vault",
    "REVIEW_OUTPUT_DIR": _SESSION_TMP / "obsidian_vault" / "reviews",
    "REVIEW_BASELINE_REGISTRY_PATH": _SESSION_TMP / "obsidian_vault" / "reviews" / "baselines.registry.json",
    "INPUT_PDF_DIR": _SESSION_TMP / "input_pdfs",
    "MINERU_OUTPUT_DIR": _SESSION_TMP / "mineru_output",
    "PROCESSED_PDF_DIR": _SESSION_TMP / "processed_pdfs",
    "FAILED_PDF_DIR": _SESSION_TMP / "failed_pdfs",
    "BOOK_INPUT_DIR": _SESSION_TMP / "input_books",
    "BOOK_OUTPUT_DIR": _SESSION_TMP / "book_output",
    "BOOK_PROCESSED_DIR": _SESSION_TMP / "processed_books",
    "BOOK_FAILED_DIR": _SESSION_TMP / "failed_books",
    "TEST_OUTPUT_DIR": _SESSION_TMP / "test_output",
}
for _k, _v in _PATH_ENV.items():
    os.environ[_k] = str(_v)

# ── 2. 配置来源：不读 .env，不继承真实密钥 / 代理 ─────────────────────────────

os.environ["PYTHON_DOTENV_DISABLED"] = "1"      # load_dotenv() 在 pipeline/cli/daemon/review 等 import 期调用
os.environ["LIT_DISABLE_STDIO_TEE"] = "1"       # utils 不再把 stdout/stderr 复制进 DB_PATH 目录
os.environ.pop("MCP_PROJECT_ENV_PRIORITY", None)  # 否则 mcp_server 会用 dotenv_values() 绕过上面的开关
os.environ.pop("LOCAL_EMBEDDING_MODEL_PATH", None)
os.environ.pop("SHARED_ENV_FILE", None)

# 强制覆盖而不是 setdefault：shell 里 export 的真 Key 不能流进测试
for _k, _v in {
    "MINERU_API_KEY": "mock_mineru_api_key_12345",
    "OPENAI_API_KEY": "mock_openai_api_key_12345",
    "SILICONFLOW_API_KEY": "mock_siliconflow_api_key_12345",
    "SILICONFLOW_API_BASE": "https://api.siliconflow.cn/v1",
    "DEEPSEEK_API_KEY": "",
    "GEMINI_API_KEY": "",
    "OPENALEX_API_KEY": "",
    "REMOTE_AUTH_TOKEN": "",
    "MCP_AUTH_TOKEN": "",
    "WEBHOOK_URL": "",
}.items():
    os.environ[_k] = _v

# 代理必须清掉：本机代理在 127.0.0.1 上，走代理的请求在 socket 层看起来是回环连接，
# 会绕过下面的出网护栏；SOCKS 代理还会让 httpx 因缺 socksio 直接 ImportError。
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
               "LIT_HTTP_PROXY", "LIT_HTTPS_PROXY")
for _k in _PROXY_VARS:
    os.environ.pop(_k, None)


# ── 3. 禁区：生产数据所在位置 ─────────────────────────────────────────────────

def _real(p) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(p))))


def _forbidden_roots() -> list:
    roots = {"/mnt/ripe", _real(_REPO_ROOT / "data")}
    for key in ("DATA_ROOT",):
        if _ORIG_ENV.get(key):
            roots.add(_real(_ORIG_ENV[key]))
    for key in ("DB_PATH", "EXCEL_OUTPUT_PATH", "EXTERNAL_DISCOVERY_CACHE_DB_PATH",
                "TEST_CITATION_DB_PATH", "CITATION_NETWORK_DB_PATH"):
        if _ORIG_ENV.get(key):
            roots.add(os.path.dirname(_real(_ORIG_ENV[key])))
    env_file = _REPO_ROOT / ".env"
    if env_file.exists():
        try:
            from dotenv import dotenv_values  # 只读路径类键，不碰密钥
            vals = dotenv_values(env_file)
            if vals.get("DATA_ROOT"):
                roots.add(_real(vals["DATA_ROOT"]))
            if vals.get("DB_PATH"):
                roots.add(os.path.dirname(_real(vals["DB_PATH"])))
        except Exception:
            pass
    # 临时目录本身永远不算禁区（防止有人把 DATA_ROOT 配成 /tmp 之类的父目录）
    tmp = str(_SESSION_TMP)
    return sorted(r for r in roots if r and r != "/" and not tmp.startswith(r + os.sep) and tmp != r)


_FORBIDDEN = _forbidden_roots()


def _under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([_real(path), _real(root)]) == _real(root)
    except ValueError:
        return False


def _is_forbidden(path: str) -> bool:
    return any(_under(path, r) for r in _FORBIDDEN)


# ── 4a. sqlite3.connect 护栏 ──────────────────────────────────────────────────

_VIOLATIONS: list = []
_real_sqlite_connect = sqlite3.connect


def _sqlite_target(database, uri: bool):
    """返回 (文件路径或 None, 是否只读)。None 表示内存库等无需检查的目标。"""
    if isinstance(database, bytes):
        database = database.decode("utf-8", "replace")
    db = os.fspath(database)
    if db in ("", ":memory:"):
        return None, False
    if uri and db.startswith("file:"):
        rest = db[len("file:"):]
        path, _, query = rest.partition("?")
        params = parse_qs(query)
        mode = (params.get("mode") or [""])[0]
        if mode == "memory" or path == ":memory:":
            return None, False
        return unquote(path), mode == "ro"
    return db, False


def _guarded_sqlite_connect(database, *args, **kwargs):
    uri = kwargs.get("uri", args[6] if len(args) > 6 else False)
    path, readonly = _sqlite_target(database, uri)
    if path is not None and _is_forbidden(path):
        allow_ro = os.environ.get("LIT_ALLOW_PROD_READONLY") == "1"
        if not (allow_ro and readonly):
            _VIOLATIONS.append(f"{_NET['current'] or '<setup>'}: sqlite {'ro' if readonly else 'rw'} {path}")
            # 保留 "unable to open database file" 字样：已有用例据此 skip 而不是报错
            raise sqlite3.OperationalError(
                f"unable to open database file: blocked by tests/conftest.py isolation guard "
                f"({path} is production data; set LIT_ALLOW_PROD_READONLY=1 and open with mode=ro "
                f"inside a prod_readonly test)")
    return _real_sqlite_connect(database, *args, **kwargs)


sqlite3.connect = _guarded_sqlite_connect


# ── 4b. 出网护栏 ──────────────────────────────────────────────────────────────

_NET = {"allowed": False, "current": None}
_NETWORK_ATTEMPTS: dict = {}
_LOCAL_NAMES = {"localhost", "testserver", "testclient", "ip6-localhost"}


def _host_is_local(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]").split("%")[0]
    if host in _LOCAL_NAMES or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def _record_network(host):
    _NETWORK_ATTEMPTS.setdefault(_NET["current"] or "<collection>", set()).add(str(host))


_real_sock_connect = socket.socket.connect
_real_sock_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _inet(sock) -> bool:
    return sock.family in (socket.AF_INET, socket.AF_INET6)


def _guarded_sock_connect(self, address):
    if not _NET["allowed"] and _inet(self) and not _host_is_local(address[0]):
        _record_network(address[0])
        raise ConnectionRefusedError(errno.ECONNREFUSED,
                                     f"network disabled in tests (tried {address[0]}); mark the test @pytest.mark.network")
    return _real_sock_connect(self, address)


def _guarded_sock_connect_ex(self, address):
    if not _NET["allowed"] and _inet(self) and not _host_is_local(address[0]):
        _record_network(address[0])
        return errno.ECONNREFUSED
    return _real_sock_connect_ex(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _NET["allowed"] and not _host_is_local(host):
        _record_network(host)
        raise socket.gaierror(socket.EAI_NONAME, f"network disabled in tests (DNS lookup of {host!r})")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guarded_sock_connect
socket.socket.connect_ex = _guarded_sock_connect_ex
socket.getaddrinfo = _guarded_getaddrinfo


# ── 5. 已知需要外网 / 真实生产库、但文件不归本 conftest 所有的用例 ─────────────
# 这些用例所在文件没法在这里改，按 nodeid 补标记；它们的文件以后自己加上 marker 后可删掉对应行。

_EXTRA_MARKERS = {
    "tests/test_academic_resolver.py::test_enrich_document_metadata_db_update": "network",
    # 读 CWD 相对的 data/test_citation_network.db（即仓库根的真实图谱库）；
    # 同样的孤儿隔离断言已由 tests/test_web_api.py 用种子库覆盖。
    "tests/test_isolation_regression.py::test_orphan_work_quarantine_isolation": "prod_readonly",
}

# 依赖未纳入版本库的 tests/data/ 夹具（.gitignore 目前仍忽略 tests/data/）
_FIXTURE_DEPENDENT = {
    "tests/test_ref_parser.py::test_dynamic_apa_parsing_corrections": "citation_dev_silver_v1.3.3.json",
}


# ── pytest 钩子 ───────────────────────────────────────────────────────────────

_PATH_SETTING_SUFFIXES = ("_DIR", "_PATH")


def _check_isolation():
    """加载真实 Settings，逐项确认每个路径都在会话临时目录下。任何一项不满足即中止。"""
    import utils

    utils._settings = None
    settings = utils.get_settings()
    resolved = {}
    for name in type(settings).model_fields:
        if name.endswith(_PATH_SETTING_SUFFIXES):
            value = getattr(settings, name)
            if value:
                resolved[f"Settings.{name}"] = value
    for name in _PATH_ENV:
        resolved[f"env {name}"] = os.environ[name]
    resolved["utils._data_dir()"] = utils._data_dir()
    from litreview.retrieval_store import discovery_db_path
    resolved["retrieval_store.discovery_db_path()"] = discovery_db_path()

    bad = {k: v for k, v in resolved.items()
           if not _under(v, str(_SESSION_TMP)) or _is_forbidden(v)}
    if bad:
        lines = "\n".join(f"  {k} = {v}" for k, v in sorted(bad.items()))
        pytest.exit(f"测试隔离失败，以下路径没有落在会话临时目录 {_SESSION_TMP}：\n{lines}", returncode=3)
    if not isinstance(sys.stdout, type(sys.__stdout__)) and type(sys.stdout).__name__ == "TeeLogger":
        pytest.exit("utils.TeeLogger 仍接管了 sys.stdout（LIT_DISABLE_STDIO_TEE 未生效）", returncode=3)
    return resolved


def _seed_default_databases():
    """给默认路径（即会话临时目录）铺一套最小种子数据，让依赖“默认库”的用例有确定内容可测。"""
    sys.path.insert(0, str(_TESTS_DIR))
    import lit_seed

    lit_seed.seed_all(str(_DATA))


def pytest_configure(config):
    config.addinivalue_line("markers", "network: needs real internet access (deselected by default)")
    config.addinivalue_line(
        "markers", "prod_readonly: reads the real production DB; requires LIT_ALLOW_PROD_READONLY=1")
    config._lit_isolation = _check_isolation()
    _seed_default_databases()


def pytest_report_header(config):
    return [f"lit-analyzer isolation: tmp={_SESSION_TMP} forbidden={_FORBIDDEN} "
            f"dotenv=disabled network=blocked"]


def pytest_collection_modifyitems(config, items):
    for item in items:
        mark = _EXTRA_MARKERS.get(item.nodeid)
        if mark:
            item.add_marker(getattr(pytest.mark, mark))
        fixture_name = _FIXTURE_DEPENDENT.get(item.nodeid)
        if fixture_name and not (_TESTS_DIR / "data" / fixture_name).exists():
            item.add_marker(pytest.mark.skip(
                reason=f"tests/data/{fixture_name} not present (tests/data is not versioned yet)"))


@pytest.fixture(autouse=True)
def _lit_isolation_policy(request, monkeypatch):
    node = request.node
    _NET["current"] = node.nodeid
    if node.get_closest_marker("prod_readonly") and os.environ.get("LIT_ALLOW_PROD_READONLY") != "1":
        pytest.skip("prod_readonly: set LIT_ALLOW_PROD_READONLY=1 to read the real production DB (mode=ro only)")
    if node.get_closest_marker("network"):
        _NET["allowed"] = True
        for k in _PROXY_VARS:
            if k in _ORIG_ENV:
                monkeypatch.setenv(k, _ORIG_ENV[k])
    try:
        yield
    finally:
        _NET["allowed"] = False
        _NET["current"] = None


@pytest.fixture(scope="session")
def lit_session_tmp() -> Path:
    return _SESSION_TMP


@pytest.fixture
def seeded_dbs(tmp_path, monkeypatch):
    """每个用例独立的一套种子三库，并把环境变量 / Settings 单例都指向它。"""
    import lit_seed
    import utils

    paths = lit_seed.seed_all(str(tmp_path / "data"))
    monkeypatch.setenv("DB_PATH", paths.prod_db)
    monkeypatch.setenv("TEST_CITATION_DB_PATH", paths.citation_db)
    monkeypatch.setenv("CITATION_NETWORK_DB_PATH", paths.citation_db)
    monkeypatch.setenv("EXTERNAL_DISCOVERY_CACHE_DB_PATH", paths.cache_db)
    monkeypatch.setenv("STAGING_PDF_DIR", str(tmp_path / "data" / "staging_pdf"))
    monkeypatch.setattr(utils, "_settings", None)
    yield paths
    utils._settings = None


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if _NETWORK_ATTEMPTS:
        terminalreporter.section("blocked network access (mark these @pytest.mark.network or mock them)")
        for nodeid, hosts in sorted(_NETWORK_ATTEMPTS.items()):
            terminalreporter.write_line(f"{nodeid}: {', '.join(sorted(hosts))}")
    if _VIOLATIONS:
        terminalreporter.section("blocked production-data access")
        for v in sorted(set(_VIOLATIONS)):
            terminalreporter.write_line(v)


def pytest_unconfigure(config):
    sqlite3.connect = _real_sqlite_connect
    if os.environ.get("LIT_KEEP_TEST_TMP") != "1":
        shutil.rmtree(_SESSION_TMP, ignore_errors=True)
