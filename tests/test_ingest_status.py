import asyncio
import json
import sqlite3

import pytest


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '1')
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'primary.db'))
    monkeypatch.setenv('TEST_CITATION_DB_PATH', str(tmp_path / 'graph.db'))
    monkeypatch.setenv('EXTERNAL_DISCOVERY_CACHE_DB_PATH', str(tmp_path / 'candidates.db'))
    import utils
    monkeypatch.setattr(utils, '_settings', None)
    import web_server
    return web_server


@pytest.mark.parametrize('indexed,failed,status', [(0, 1, 'failed'), (1, 1, 'partial'), (1, 0, 'completed')])
def test_ingest_job_requires_real_vectors(server, monkeypatch, indexed, failed, status):
    from litreview import pipeline
    monkeypatch.setattr(pipeline, 'ingest_acquired_candidates', lambda **kw: dict(
        ingested_count=indexed + failed, indexed_count=indexed, failed_count=failed))
    job = server._create_bg_job('ingest', 'test')
    server._run_ingest_worker(job, ['candidate'])
    assert server._get_bg_job(job)['status'] == status


def test_ingest_returned_error_cannot_be_reported_success(server, monkeypatch):
    from litreview import pipeline
    monkeypatch.setattr(pipeline, 'ingest_acquired_candidates', lambda **kw: {'error': 'missing graph'})
    job = server._create_bg_job('ingest', 'test')
    server._run_ingest_worker(job, ['candidate'])
    assert server._get_bg_job(job)['status'] == 'failed'


def test_unfinished_embeddings_stay_in_retry_queue(server):
    with sqlite3.connect(server._get_cache_db_path()) as conn:
        conn.execute('CREATE TABLE discovered_candidates (candidate_id TEXT, lifecycle_status TEXT, discovered_at TEXT)')
        conn.executemany('INSERT INTO discovered_candidates VALUES (?, ?, ?)', [
            (state, state, 'today') for state in ('GRAPH_INDEXED', 'PARSED', 'VECTOR_INDEXED_TEST', 'VECTOR_INDEXED')])
    response = asyncio.run(server.api_pipeline_overview(None))
    data = json.loads(response.body)['data']
    assert {r['candidate_id'] for r in data['pending']} == {'GRAPH_INDEXED', 'PARSED', 'VECTOR_INDEXED_TEST'}
    assert [r['candidate_id'] for r in data['success']] == ['VECTOR_INDEXED']


def test_mcp_reports_missing_fulltext_without_work_id(server, monkeypatch):
    from litreview import pipeline
    monkeypatch.setattr(pipeline, 'ingest_acquired_candidates', lambda **kw: dict(
        ingested_count=0, indexed_count=0, failed_count=1, ingested_works=[dict(
            candidate_id='c', title='Scanned PDF', lifecycle_status='FULLTEXT_ACQUIRED',
            retryable=True, error='OCR required')]))
    import mcp_server
    result = mcp_server.ingest_acquired_candidates.__wrapped__(['c'])
    assert 'OCR required' in result
    assert '尚未入库' in result
    assert '待重试 **1**' in result


def test_mcp_sufficiency_counts_discovery_metadata(server, monkeypatch):
    from types import SimpleNamespace
    import mcp_server
    monkeypatch.setenv('SILICONFLOW_API_KEY', 'test-dummy')
    monkeypatch.setattr(mcp_server, '_store', lambda: SimpleNamespace(get_paper_meta=lambda: {
        'work_oa_w123': {'title': 'Coated fertilizer', 'doc_type': 'paper'},
    }))
    result = mcp_server._check_literature_sufficiency('fertilizer')
    assert result['matched_count'] == 1
    assert sum(result['breakdown'].values()) == 1
