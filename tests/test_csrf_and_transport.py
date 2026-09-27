"""CSRF Origin 校验、DNS rebinding Host 白名单、MCP 传输层安全设置、
/api/pipeline/run batch_fetch 分支与入库跨进程锁的回归测试。全部 I/O 限定在 tmp_path。"""
import asyncio
import importlib
import sqlite3
import sys
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from litreview import guard
from litreview.guard import (RemoteAccessGuardMiddleware, check_csrf, is_allowed_host,
                             mcp_allowed_hosts_and_origins, build_mcp_transport_security)
from litreview.intake_lock import intake_lock, IntakeBusyError, intake_lock_path

PDF = b"%PDF-1.4\n" + b"0" * 200


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("WEB_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("REMOTE_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("REMOTE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)


def call(app, method, path, base_url="http://127.0.0.1:8001", client=("127.0.0.1", 1234), **kwargs):
    async def go():
        transport = httpx.ASGITransport(app=app, client=client)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as c:
            return await c.request(method, path, **kwargs)
    return asyncio.run(go())


def _mini_app():
    async def ok(request: Request):
        return JSONResponse({"ok": True})
    return Starlette(routes=[Route("/api/x", ok, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])],
                     middleware=[Middleware(RemoteAccessGuardMiddleware)])


# ── check_csrf 单元 ────────────────────────────────────────────────────────

H = {"host": "100.83.137.43:8001"}


@pytest.mark.parametrize("headers,expected", [
    ({}, True),                                                          # curl / MCP 客户端
    ({"origin": "http://100.83.137.43:8001"}, True),                    # 同源前端
    ({"referer": "http://100.83.137.43:8001/index.html"}, True),        # 只有 Referer 的同源
    ({"origin": "https://evil.example"}, False),
    ({"origin": "http://100.83.137.43:9999"}, False),                   # 同主机不同端口 = 跨源
    ({"origin": "null"}, False),
    ({"origin": "Null"}, False),
    ({"referer": "https://evil.example/a"}, False),
    ({"sec-fetch-site": "cross-site"}, False),                          # 无 Origin 但浏览器声明跨站
    ({"sec-fetch-site": "same-site"}, False),
    ({"sec-fetch-site": "none"}, True),
    ({"origin": "https://evil.example", "sec-fetch-site": "cross-site"}, False),
    ({"origin": "http://proxy.example", "sec-fetch-site": "same-origin"}, True),  # 反代改写 Host
    ({"origin": "not a url"}, False),
])
def test_check_csrf_matrix(headers, expected):
    ok, reason = check_csrf("POST", {**H, **headers}, allowed_origins=[])
    assert ok is expected, reason


def test_check_csrf_default_ports_and_safe_methods():
    assert check_csrf("POST", {"host": "example.lan", "origin": "http://example.lan"}, allowed_origins=[])[0]
    assert check_csrf("POST", {"host": "example.lan", "origin": "https://example.lan"}, "https",
                      allowed_origins=[])[0]
    assert not check_csrf("POST", {"host": "example.lan", "origin": "https://example.lan"}, "http",
                          allowed_origins=[])[0]
    for m in ("GET", "HEAD", "OPTIONS"):
        assert check_csrf(m, {**H, "origin": "https://evil.example"}, allowed_origins=[])[0]
    for m in ("PUT", "PATCH", "DELETE", "post"):
        assert not check_csrf(m, {**H, "origin": "https://evil.example"}, allowed_origins=[])[0]


def test_check_csrf_allowlist_from_env(monkeypatch):
    monkeypatch.setenv("WEB_CORS_ORIGINS", "https://ui.example/, *")
    assert guard.cors_origins() == ["https://ui.example"]
    assert check_csrf("POST", {**H, "origin": "https://ui.example"})[0]
    assert check_csrf("POST", {**H, "origin": "https://ui.example", "sec-fetch-site": "cross-site"})[0]
    assert not check_csrf("POST", {**H, "origin": "https://ui.example:444"})[0]
    assert not check_csrf("POST", {**H, "origin": "https://evil.example"})[0]


# ── Host 白名单 (DNS rebinding) ──────────────────────────────────────────────

def test_is_allowed_host():
    for h in ("127.0.0.1:8001", "100.83.137.43:8002", "10.39.183.207", "[::1]:8002", "[fd7a::1]",
              "localhost:8001", "app.localhost", "dudu-sv:8001", "testserver", None, ""):
        assert is_allowed_host(h, extra=[]), h
    for h in ("evil.example", "evil.example:8002", "127.0.0.1.nip.io:8001"):
        assert not is_allowed_host(h, extra=[]), h
    assert is_allowed_host("dudu-sv.tail123.ts.net:8002", extra=[".tail123.ts.net"])
    assert is_allowed_host("lit.example.com", extra=["lit.example.com:8001"])
    assert not is_allowed_host("lit.example.com.evil.io", extra=["lit.example.com"])
    assert is_allowed_host("anything.example", extra=["*"])


# ── 中间件集成 ────────────────────────────────────────────────────────────

def test_middleware_blocks_cross_origin_post_but_not_clients():
    app = _mini_app()
    assert call(app, "POST", "/api/x").status_code == 200                          # 无 Origin
    assert call(app, "POST", "/api/x", headers={"Origin": "http://127.0.0.1:8001"}).status_code == 200
    r = call(app, "POST", "/api/x", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and "cross-origin" in r.json()["error"]
    assert call(app, "DELETE", "/api/x", headers={"Origin": "null"}).status_code == 403
    assert call(app, "PUT", "/api/x", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert call(app, "GET", "/api/x", headers={"Origin": "https://evil.example"}).status_code == 200


def test_middleware_rejects_rebinding_host():
    app = _mini_app()
    r = call(app, "GET", "/api/x", base_url="http://evil.example:8001")
    assert r.status_code == 421
    # 攻击者页面在重绑定后 Origin 与 Host 同源，也必须被 Host 校验拦住
    r = call(app, "POST", "/api/x", base_url="http://evil.example:8001",
             headers={"Origin": "http://evil.example:8001"})
    assert r.status_code == 421
    assert call(app, "GET", "/api/x", base_url="http://100.83.137.43:8001").status_code == 200


@pytest.fixture
def web(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("WEB_REVIEW_JOBS_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("STAGING_PDF_DIR", str(tmp_path / "staging"))
    monkeypatch.setenv("EXTERNAL_DISCOVERY_CACHE_DB_PATH", str(tmp_path / "cache.db"))
    import web_server
    root = tmp_path / "root"
    monkeypatch.setattr(web_server, "get_app_settings", lambda: SimpleNamespace(
        DB_PATH=str(tmp_path / "batch_tracking.db"),
        INPUT_PDF_DIR=str(root / "input_pdfs"),
        BOOK_INPUT_DIR=str(root / "input_books"),
        REVIEW_OUTPUT_DIR=str(root / "reviews"),
        REVIEW_AUTO_INDEX=False))
    sqlite3.connect(tmp_path / "cache.db").close()
    return web_server


def _upload(app, **kw):
    return call(app, "POST", "/api/upload", data={"target_type": "pdf"},
                files=[("files", ("a.pdf", PDF, "application/pdf"))], **kw)


def test_web_upload_csrf(web, tmp_path):
    r = _upload(web.app, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert not (tmp_path / "root" / "input_pdfs" / "a.pdf").exists()
    # web_static 同源 fetch：Origin 与 Host 一致
    r = _upload(web.app, headers={"Origin": "http://127.0.0.1:8001", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200
    assert (tmp_path / "root" / "input_pdfs" / "a.pdf").exists()


def test_web_upload_allowlisted_origin(web, monkeypatch):
    monkeypatch.setenv("WEB_CORS_ORIGINS", "https://ui.example")
    app = Starlette(routes=web.routes, middleware=web.build_middleware())
    r = _upload(app, headers={"Origin": "https://ui.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "https://ui.example"


# ── MCP 传输层 ────────────────────────────────────────────────────────────

def test_mcp_allowed_hosts_and_origins():
    enable, hosts, origins = mcp_allowed_hosts_and_origins(
        "0.0.0.0", 8002, interface_ips=["100.83.137.43", "10.39.183.207", "fe80::1"],
        extra_hosts=["lit.example.com", ".ignored.suffix"], extra_origins=["https://ui.example"])
    assert enable
    for h in ("127.0.0.1:*", "localhost:*", "[::1]:*", "100.83.137.43:*", "100.83.137.43",
              "10.39.183.207:*", "[fe80::1]:*", "lit.example.com:*"):
        assert h in hosts
    assert "0.0.0.0:*" not in hosts and not any("ignored" in h for h in hosts)
    assert "http://100.83.137.43:*" in origins and "https://ui.example" in origins
    assert mcp_allowed_hosts_and_origins("", interface_ips=[], extra_hosts=["*"]) == (False, ["*"], ["*"])


def _validate(settings, headers, is_post=False):
    from mcp.server.transport_security import TransportSecurityMiddleware
    scope = {"type": "http", "method": "POST" if is_post else "GET", "path": "/",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    resp = asyncio.run(TransportSecurityMiddleware(settings).validate_request(Request(scope), is_post=is_post))
    return None if resp is None else resp.status_code


def test_mcp_native_validation_accepts_remote_ips_rejects_rebinding():
    ts = build_mcp_transport_security("0.0.0.0", 8002, interface_ips=["100.83.137.43"],
                                      extra_hosts=[], extra_origins=[])
    assert ts.enable_dns_rebinding_protection
    assert _validate(ts, {"host": "100.83.137.43:8002"}) is None
    assert _validate(ts, {"host": "127.0.0.1:8002"}) is None
    assert _validate(ts, {"host": "100.83.137.43:8002", "content-type": "application/json"}, True) is None
    assert _validate(ts, {"host": "evil.example:8002"}) == 421
    assert _validate(ts, {"host": "100.83.137.43:8002", "origin": "https://evil.example"}) == 403
    # 与 Web 守卫同一策略：未枚举到的 IP（如 DHCP 换址）、单标签主机名仍可用
    assert _validate(ts, {"host": "192.168.9.9:8002"}) is None
    assert _validate(ts, {"host": "dudu-sv:8002", "origin": "http://dudu-sv:8002"}) is None
    ts2 = build_mcp_transport_security("0.0.0.0", 8002, interface_ips=[],
                                       extra_hosts=[".tail1.ts.net"], extra_origins=["https://ui.example"])
    assert _validate(ts2, {"host": "box.tail1.ts.net:8002"}) is None
    assert _validate(ts2, {"host": "127.0.0.1:8002", "origin": "https://ui.example"}) is None
    assert _validate(ts2, {"host": "box.tail2.ts.net:8002"}) == 421


def test_mcp_run_from_cli_enables_protection(monkeypatch, tmp_path):
    monkeypatch.setenv("MINERU_API_KEY", "test-mineru")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-siliconflow")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    import utils
    utils._settings = None
    sys.modules.pop("mcp_server", None)
    mcp_server = importlib.import_module("mcp_server")
    import uvicorn
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app, **kw))
    monkeypatch.setattr(guard, "local_interface_ips", lambda: ["100.83.137.43"])
    monkeypatch.setattr(sys, "argv", ["mcp_server.py", "--transport", "sse", "--host", "0.0.0.0", "--port", "8002"])
    old = mcp_server.mcp.settings.transport_security
    try:
        mcp_server.McpServerHost.run_from_cli()
        ts = mcp_server.mcp.settings.transport_security
        assert ts.enable_dns_rebinding_protection and "100.83.137.43:*" in ts.allowed_hosts
        app = captured["app"]
        # 守卫中间件：重绑定 Host 与跨站 POST 在到达 MCP 处理器前即被拒
        assert call(app, "GET", "/sse", base_url="http://evil.example:8002").status_code == 421
        r = call(app, "POST", "/messages/?session_id=x", base_url="http://100.83.137.43:8002",
                 client=("100.83.137.43", 5555), headers={"Origin": "https://evil.example"}, json={})
        assert r.status_code == 403
        # 无 Origin 的 MCP 客户端 POST 通过守卫，进入 SSE 传输（未知会话 → 400/404，而非 403/421）
        r = call(app, "POST", "/messages/?session_id=0123456789abcdef0123456789abcdef",
                 base_url="http://100.83.137.43:8002", client=("100.83.137.43", 5555), json={})
        assert r.status_code not in (403, 421)
    finally:
        mcp_server.mcp.settings.transport_security = old
        sys.modules.pop("mcp_server", None)
        utils._settings = None


# ── /api/pipeline/run batch_fetch 与入库锁 ────────────────────────────────────

def _run_worker(web, mode):
    task_id = f"t_{mode}"
    web._active_pipelines[task_id] = {"id": task_id, "mode": mode, "status": "running", "logs": []}
    web._run_intake_worker(task_id, mode)
    return web._active_pipelines.pop(task_id)


def test_pipeline_batch_fetch_calls_fetch_batch(web, monkeypatch):
    import batch_pipeline
    calls = []

    def fake_fetch():
        calls.append(1)
        return {"jobs_checked": 2, "completed": 5, "failed": 0, "exported": 4}
    monkeypatch.setattr(batch_pipeline, "fetch_batch", fake_fetch)
    monkeypatch.setattr(batch_pipeline, "prepare_batch", lambda: pytest.fail("prepare_batch called"))
    task = _run_worker(web, "batch_fetch")
    assert calls == [1]
    assert task["status"] == "done", task
    assert any("检查 2 个批次" in l and "导出 4 篇" in l for l in task["logs"])


def test_pipeline_skips_when_intake_lock_held(web, monkeypatch, tmp_path):
    import batch_pipeline
    monkeypatch.setattr(batch_pipeline, "fetch_batch", lambda: pytest.fail("ran while locked"))
    with intake_lock(str(tmp_path / "batch_tracking.db")):
        task = _run_worker(web, "batch_fetch")
    assert task["status"] == "busy"
    # 锁释放后可再次运行
    monkeypatch.setattr(batch_pipeline, "fetch_batch", lambda: {"jobs_checked": 0, "exported": 0})
    assert _run_worker(web, "batch_fetch")["status"] == "done"


def test_intake_lock_is_exclusive_and_reentrant_after_release(tmp_path):
    db = str(tmp_path / "d" / "b.db")
    assert intake_lock_path(db) == str(tmp_path / "d" / ".intake.lock")
    with intake_lock(db):
        with pytest.raises(IntakeBusyError):
            with intake_lock(db):
                pass
    with intake_lock(db):
        pass
