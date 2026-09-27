import asyncio
import importlib
import sys

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from litreview.guard import RemoteAccessGuard, RemoteAccessGuardMiddleware


def request_from(app, peer, path='/protected', headers=None):
    async def request():
        transport = httpx.ASGITransport(app=app, client=(peer, 1234))
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            return await client.get(path, headers=headers)
    return asyncio.run(request())


def guarded_app(**kwargs):
    async def endpoint(request):
        return JSONResponse({'ok': True})
    return Starlette(routes=[Route('/protected', endpoint)], middleware=[
        Middleware(RemoteAccessGuardMiddleware,
                   guard=RemoteAccessGuard(auth_token='test-secret', **kwargs))])


@pytest.mark.parametrize('header', ['X-Forwarded-For', 'X-Real-IP'])
def test_public_peer_cannot_forge_local_origin(header):
    app = guarded_app()
    assert request_from(app, '203.0.113.9', headers={header: '127.0.0.1'}).status_code == 403
    assert request_from(app, '203.0.113.9', headers={
        header: '127.0.0.1', 'Authorization': 'Bearer test-secret'}).status_code == 200


def test_explicit_proxy_uses_rightmost_untrusted_hop():
    app = guarded_app(trusted_proxy_cidrs=['10.0.0.2/32'])
    headers = {'X-Forwarded-For': '127.0.0.1, 203.0.113.9'}
    assert request_from(app, '10.0.0.2', headers=headers).status_code == 403
    assert request_from(app, '10.0.0.2', headers={
        'X-Forwarded-For': '100.64.0.8'}).status_code == 200
    assert request_from(app, '10.0.0.2', headers={
        'X-Forwarded-For': 'invalid'}).status_code == 403
    assert request_from(app, '10.0.0.2').status_code == 403


def test_mcp_actual_http_host_installs_guard(monkeypatch, tmp_path):
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '1')
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'test.db'))
    monkeypatch.setenv('REMOTE_AUTH_TOKEN', 'test-secret')
    import utils
    monkeypatch.setattr(utils, '_settings', None)
    server = importlib.import_module('mcp_server')
    import uvicorn
    captured = {}
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kw: captured.update(app=app, kwargs=kw))
    monkeypatch.setattr(sys, 'argv', ['mcp_server.py', '--transport', 'streamable-http'])
    server.McpServerHost.run_from_cli()
    for path in ('/mcp', '/sse', '/messages/'):
        assert request_from(captured['app'], '203.0.113.9', path,
                            {'X-Forwarded-For': '127.0.0.1'}).status_code == 403
    assert captured['kwargs'].get('proxy_headers') is False
    async def initialize():
        app = captured['app']
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=('203.0.113.9', 1234))
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                response = await client.post('/mcp', headers={
                    'Authorization': 'Bearer test-secret',
                    'Accept': 'application/json, text/event-stream',
                }, json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                    'protocolVersion': '2024-11-05', 'capabilities': {},
                    'clientInfo': {'name': 'isolated-test', 'version': '1'},
                }})
                assert response.status_code == 200
                assert 'serverInfo' in response.text
    asyncio.run(initialize())
