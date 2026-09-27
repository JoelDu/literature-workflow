"""Upload path-traversal, candidate_id and CORS regressions; all I/O stays under tmp_path."""
import asyncio
import os
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from litreview.safe_paths import safe_basename, resolve_within, is_valid_candidate_id

PDF = b"%PDF-1.4\n" + b"0" * 200


@pytest.fixture
def web(monkeypatch, tmp_path):
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '1')
    monkeypatch.setenv('WEB_REVIEW_JOBS_DB_PATH', str(tmp_path / 'jobs.db'))
    monkeypatch.setenv('STAGING_PDF_DIR', str(tmp_path / 'staging'))
    monkeypatch.setenv('EXTERNAL_DISCOVERY_CACHE_DB_PATH', str(tmp_path / 'cache.db'))
    monkeypatch.delenv('WEB_CORS_ORIGINS', raising=False)
    import web_server
    root = tmp_path / 'root'
    monkeypatch.setattr(web_server, 'get_app_settings', lambda: SimpleNamespace(
        DB_PATH=str(tmp_path / 'batch_tracking.db'),
        INPUT_PDF_DIR=str(root / 'input_pdfs'),
        BOOK_INPUT_DIR=str(root / 'input_books'),
        REVIEW_OUTPUT_DIR=str(root / 'reviews')))
    conn = sqlite3.connect(tmp_path / 'cache.db')
    conn.execute("CREATE TABLE discovered_candidates (candidate_id TEXT PRIMARY KEY, lifecycle_status TEXT)")
    conn.execute("INSERT INTO discovered_candidates VALUES ('cand_found_run_abc123_W42', 'APPROVED')")
    conn.commit()
    conn.close()
    return web_server


def call(app, method, path, **kwargs):
    async def go():
        transport = httpx.ASGITransport(app=app, client=('127.0.0.1', 1234))
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(go())


def all_files(path):
    return sorted(os.path.relpath(os.path.join(d, f), path)
                  for d, _, fs in os.walk(path) for f in fs)


def test_helpers():
    assert safe_basename('../../x.pdf') == 'x.pdf'
    assert safe_basename('/etc/x.pdf') == 'x.pdf'
    assert safe_basename('C:\\Users\\a\\磷肥.pdf') == '磷肥.pdf'
    for bad in ('', '..', '.', '../', 'a\x00.pdf', 'a\n.pdf', None):
        assert safe_basename(bad) is None
    assert resolve_within('/tmp/base', '../x') is None
    assert is_valid_candidate_id('cand_emerg_run_0a1b2c_W123')
    for bad in ('../x', 'a/b', 'a..b', 'a b', '', None, 'x\x00'):
        assert not is_valid_candidate_id(bad)


@pytest.mark.parametrize('name', ['../../evil.pdf', 'ABS', '..\\..\\evil.pdf', 'sub/../../evil.pdf'])
def test_upload_traversal_is_neutralised(web, tmp_path, name):
    if name == 'ABS':  # 绝对路径也限定在 tmp_path 内，避免旧代码回归时写到共享 /tmp
        name = str(tmp_path / 'outside' / 'evil.pdf')
    r = call(web.app, 'POST', '/api/upload', data={'target_type': 'pdf'},
             files=[('files', (name, PDF, 'application/pdf'))])
    assert r.status_code == 200
    body = r.json()['data']
    assert all_files(tmp_path / 'root') == ['input_pdfs/evil.pdf']
    assert body['saved'][0]['filename'] == 'evil.pdf'
    assert not (tmp_path / 'evil.pdf').exists() and not (tmp_path / 'outside').exists()


def raw_upload(app, filename: bytes):
    """httpx 会转义文件名中的控制字符，这里手写 multipart 以发送原始字节。"""
    body = (b'--B\r\nContent-Disposition: form-data; name="target_type"\r\n\r\npdf\r\n'
            b'--B\r\nContent-Disposition: form-data; name="files"; filename="' + filename + b'"\r\n'
            b'Content-Type: application/pdf\r\n\r\n' + PDF + b'\r\n--B--\r\n')
    return call(app, 'POST', '/api/upload', content=body,
                headers={'Content-Type': 'multipart/form-data; boundary=B'})


@pytest.mark.parametrize('name', [b'..', b'a\x00.pdf', b'x\x01.pdf', b'../'])
def test_upload_rejects_unsafe_names(web, tmp_path, name):
    r = raw_upload(web.app, name)
    assert r.status_code == 200
    assert r.json()['data']['total_saved'] == 0
    assert all_files(tmp_path) == ['cache.db']


def test_upload_normal_and_chinese_names(web, tmp_path):
    r = call(web.app, 'POST', '/api/upload', data={'target_type': 'pdf'},
             files=[('files', ('paper.pdf', PDF, 'application/pdf')),
                    ('files', ('磷肥 缓释机理.pdf', PDF, 'application/pdf'))])
    assert r.json()['data']['total_saved'] == 2
    assert all_files(tmp_path / 'root') == ['input_pdfs/paper.pdf', 'input_pdfs/磷肥 缓释机理.pdf']
    r = call(web.app, 'POST', '/api/upload', data={'target_type': 'book'},
             files=[('files', ('教材.epub', b'PK', 'application/epub+zip'))])
    assert r.json()['data']['total_saved'] == 1
    assert (tmp_path / 'root' / 'input_books' / '教材.epub').exists()


@pytest.mark.parametrize('cid', ['../../evil', '/tmp/evil', 'a/b', 'x..y', 'cand bad'])
def test_pipeline_upload_rejects_bad_candidate_id(web, tmp_path, cid):
    r = call(web.app, 'POST', '/api/pipeline/upload', data={'candidate_id': cid},
             files={'file': ('p.pdf', PDF, 'application/pdf')})
    assert r.status_code == 400
    assert all_files(tmp_path) == ['cache.db']


def test_pipeline_upload_requires_existing_candidate(web, tmp_path):
    r = call(web.app, 'POST', '/api/pipeline/upload', data={'candidate_id': 'cand_found_run_abc123_W999'},
             files={'file': ('p.pdf', PDF, 'application/pdf')})
    assert r.status_code == 404
    assert not (tmp_path / 'staging').exists()
    r = call(web.app, 'POST', '/api/pipeline/upload', data={'candidate_id': 'cand_found_run_abc123_W42'},
             files={'file': ('p.pdf', PDF, 'application/pdf')})
    assert r.status_code == 200
    assert all_files(tmp_path / 'staging') == ['cand_found_run_abc123_W42.pdf']
    conn = sqlite3.connect(tmp_path / 'cache.db')
    assert conn.execute("SELECT lifecycle_status FROM discovered_candidates").fetchone()[0] == 'FULLTEXT_ACQUIRED'
    conn.close()


def test_review_download_rejects_symlink_escape(web, tmp_path):
    reviews = tmp_path / 'root' / 'reviews'
    reviews.mkdir(parents=True)
    (tmp_path / 'secret.md').write_text('secret', encoding='utf-8')
    os.symlink(tmp_path / 'secret.md', reviews / 'link.md')
    (reviews / '综述.md').write_text('# ok', encoding='utf-8')
    assert call(web.app, 'GET', '/api/review/preview', params={'filename': 'link.md'}).status_code == 400
    assert call(web.app, 'GET', '/api/review/download', params={'filename': 'link.md'}).status_code == 400
    assert call(web.app, 'GET', '/api/review/preview', params={'filename': '../secret.md'}).status_code == 400
    r = call(web.app, 'GET', '/api/review/preview', params={'filename': '综述.md'})
    assert r.status_code == 200 and r.json()['data']['content'] == '# ok'


def test_cross_origin_gets_no_cors_header_by_default(web):
    headers = {'Origin': 'https://evil.example'}
    r = call(web.app, 'GET', '/api/review/files', headers=headers)
    assert 'access-control-allow-origin' not in r.headers
    r = call(web.app, 'OPTIONS', '/api/upload', headers={
        **headers, 'Access-Control-Request-Method': 'POST'})
    assert 'access-control-allow-origin' not in r.headers


def test_cors_allowlist_from_env(web, monkeypatch):
    monkeypatch.setenv('WEB_CORS_ORIGINS', 'https://ui.example, * ,')
    assert web._cors_origins() == ['https://ui.example']
    app = Starlette(routes=web.routes, middleware=web.build_middleware())
    ok = call(app, 'GET', '/api/review/files', headers={'Origin': 'https://ui.example'})
    assert ok.headers.get('access-control-allow-origin') == 'https://ui.example'
    bad = call(app, 'GET', '/api/review/files', headers={'Origin': 'https://evil.example'})
    assert 'access-control-allow-origin' not in bad.headers
