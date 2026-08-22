"""tests/test_remote_access.py — 远程访问深模块 (Locator, Guard, Host) 单元与集成测试。"""
import os
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from litreview.locator import DocumentLocator, get_locator, ResourceDescriptor
from litreview.guard import RemoteAccessGuard, RemoteAccessGuardMiddleware, NetworkClassification


def test_document_locator_descriptors(tmp_path):
    """测试 DocumentLocator 生成便携式 URI 与安全过滤。"""
    locator = DocumentLocator(base_web_url="http://100.83.137.43:8001")
    
    # 1. 测试 PDF 描述符
    desc = locator.get_pdf_descriptor("doc_test123", title="磷肥释放机理研究")
    assert desc.uri == "asset://pdf/doc_test123"
    assert desc.asset_type == "pdf"
    assert desc.remote_path == "http://100.83.137.43:8001/api/documents/doc_test123/pdf"
    
    # 序列化测试：默认不暴露 local_path
    d = desc.to_dict(include_local_path=False)
    assert "local_path" not in d
    assert d["uri"] == "asset://pdf/doc_test123"
    assert d["remote_url"] == "http://100.83.137.43:8001/api/documents/doc_test123/pdf"

    # 2. 测试综述产物描述符
    r_desc = locator.get_review_descriptor("job_review_456", topic="新型缓释材料", extension="docx")
    assert r_desc.uri == "vault://reviews/job_review_456.docx"
    assert r_desc.remote_path == "http://100.83.137.43:8001/api/reviews/job_review_456/download?format=docx"

    # 3. 测试路径净化
    assert locator.sanitize_path_for_remote("/mnt/ripe/literature_analyzer_data/obsidian_磷化工文献/reviews/综述.md") == "vault://reviews/综述.md"
    assert locator.sanitize_path_for_remote("/mnt/ripe/literature_analyzer_data/processed_pdfs/a1b2c3d4.pdf") == "asset://pdf/a1b2c3d4.pdf"


def test_remote_access_guard_ip_classification():
    """测试 RemoteAccessGuard 对不同网络环境的 IP 精准分类。"""
    guard = RemoteAccessGuard()

    # Localhost
    assert guard.classify_ip("127.0.0.1") == NetworkClassification.LOCALHOST
    assert guard.classify_ip("::1") == NetworkClassification.LOCALHOST
    assert guard.classify_ip("localhost") == NetworkClassification.LOCALHOST

    # Tailscale IP (100.64.0.0/10)
    assert guard.classify_ip("100.83.137.43") == NetworkClassification.TAILSCALE
    assert guard.classify_ip("100.64.0.1") == NetworkClassification.TAILSCALE
    assert guard.classify_ip("100.127.255.254") == NetworkClassification.TAILSCALE

    # Private LAN (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
    assert guard.classify_ip("10.39.183.207") == NetworkClassification.LAN
    assert guard.classify_ip("192.168.1.100") == NetworkClassification.LAN
    assert guard.classify_ip("172.20.0.5") == NetworkClassification.LAN

    # Public WAN
    assert guard.classify_ip("8.8.8.8") == NetworkClassification.WAN
    assert guard.classify_ip("114.114.114.114") == NetworkClassification.WAN


def test_remote_access_guard_access_verification():
    """测试受信任网络自动放行与公网 Token 校验策略。"""
    guard = RemoteAccessGuard(auth_token="secret_token_123", allow_public_wan=False)

    # 1. 本地与 Tailscale 专网自动信任放行
    ok, reason = guard.verify_request_access("127.0.0.1", {})
    assert ok is True
    assert "localhost" in reason

    ok, reason = guard.verify_request_access("100.83.137.43", {"tailscale-user-login": "dudu@tailscale"})
    assert ok is True
    assert "tailscale" in reason

    # 2. 公网未提供 Token 拦截
    ok, reason = guard.verify_request_access("203.0.113.1", {})
    assert ok is False
    assert "Unauthorized" in reason

    # 3. 公网提供合法 Bearer Token 放行
    ok, reason = guard.verify_request_access("203.0.113.1", {"authorization": "Bearer secret_token_123"})
    assert ok is True
    assert "Authenticated" in reason

    # 4. 公网提供错误 Token 拦截
    ok, reason = guard.verify_request_access("203.0.113.1", {"authorization": "Bearer wrong_token"})
    assert ok is False


def test_remote_access_guard_middleware_integration():
    """测试 Starlette 中间件集成下的访问拦截与放行。"""
    guard = RemoteAccessGuard(auth_token="my_token_abc")

    async def sample_endpoint(request):
        return JSONResponse({"status": "ok", "message": "hello world"})

    routes = [Route("/api/test_data", endpoint=sample_endpoint, methods=["GET"])]
    middleware = [Middleware(RemoteAccessGuardMiddleware, guard=guard)]
    app = Starlette(routes=routes, middleware=middleware)

    client = TestClient(app)

    # 1. 模拟本地 Localhost 请求 -> 放行
    resp = client.get("/api/test_data", headers={"X-Forwarded-For": "127.0.0.1"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # 2. 模拟 Tailscale 专网请求 -> 放行
    resp = client.get("/api/test_data", headers={"X-Forwarded-For": "100.83.137.43", "Tailscale-User-Login": "dudu"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # 3. 模拟公网非法请求 -> 403 拦截
    resp = client.get("/api/test_data", headers={"X-Forwarded-For": "198.51.100.5"})
    assert resp.status_code == 403
    assert "Forbidden" in resp.json()["error"]

    # 4. 模拟公网带合法 Token 请求 -> 200 放行
    resp = client.get("/api/test_data", headers={"X-Forwarded-For": "198.51.100.5", "Authorization": "Bearer my_token_abc"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
