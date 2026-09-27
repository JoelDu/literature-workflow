"""Offline regression coverage; all persistence stays in pytest temporary paths."""
import asyncio
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def web(monkeypatch, tmp_path):
    monkeypatch.setenv('WEB_REVIEW_JOBS_DB_PATH', str(tmp_path / 'jobs.db'))
    import web_server
    monkeypatch.setattr(web_server, '_jobs', {})
    monkeypatch.setattr(web_server, '_review_jobs_loaded_path', None, raising=False)
    monkeypatch.setattr(web_server, 'get_app_settings', lambda: SimpleNamespace(
        DB_PATH=str(tmp_path / 'jobs.db'), REVIEW_MODEL='review'))
    return web_server


def test_restart_retains_checkpoint_and_marks_interrupted(web):
    from litreview.review_jobs import ReviewJobStore
    store = ReviewJobStore(web.get_app_settings().DB_PATH)
    store.save({'job_id': 'j', 'status': 'running', 'topic': 't',
                'checkpoint': [{'heading': 'done', 'markdown': 'text'}],
                'outline': {'title': 't', 'sections': [{'heading': 'done'}]}})
    result = asyncio.run(web.api_review_jobs(None))
    job = json.loads(result.body)['data'][0]
    assert job['status'] == 'interrupted'
    assert job['checkpoint'][0]['heading'] == 'done'
    assert ReviewJobStore(web.get_app_settings().DB_PATH).load_all()[0]['status'] == 'interrupted'


def test_resume_uses_saved_parameters_and_rejects_duplicate(web, monkeypatch):
    from litreview.review_jobs import ReviewJobStore
    ReviewJobStore(web.get_app_settings().DB_PATH).save({
        'job_id': 'j', 'status': 'failed', 'topic': 't', 'focus': 'f', 'words': 2000,
        'sections': 2, 'model': 'custom', 'outline': {'title': 't', 'sections': []},
        'checkpoint': [], 'log': [], 'error': 'old', 'baseline_verdict': 'FAIL'})
    calls = []
    monkeypatch.setattr(web, '_start_review_thread', lambda job: calls.append(dict(job)))
    request = SimpleNamespace(path_params={'job_id': 'j'})
    assert asyncio.run(web.api_review_resume(request)).status_code == 200
    assert calls[0]['model'] == 'custom'
    assert calls[0]['words'] == 2000
    assert calls[0]['error'] == ''
    assert calls[0]['baseline_verdict'] is None
    assert asyncio.run(web.api_review_resume(request)).status_code == 409


def test_partial_stream_terminates(web):
    web._jobs['j'] = {'job_id': 'j', 'status': 'partial', 'log': []}
    web._review_jobs_loaded_path = web.get_app_settings().DB_PATH
    async def collect():
        response = await web.api_review_job_stream(SimpleNamespace(path_params={'job_id': 'j'}))
        return [item async for item in response.body_iterator]
    assert len(asyncio.run(asyncio.wait_for(collect(), timeout=0.2))) == 1


def test_worker_resumes_shared_orchestrator_preserves_usage_and_partial(web, monkeypatch, tmp_path):
    from litreview import stages, figures, manifest
    from litreview.models import SectionDraft, Evidence
    from litreview.review_jobs import ReviewJobStore
    settings = SimpleNamespace(DB_PATH=str(tmp_path / 'jobs.db'), REVIEW_MODEL='original',
        ANALYSIS_MODEL='analysis', RERANK_MODEL='rerank', REVIEW_INSERT_FIGURES=False,
        REVIEW_OUTPUT_DIR=str(tmp_path), REVIEW_BASELINE_REGISTRY_PATH=str(tmp_path / 'baseline.json'))
    monkeypatch.setattr(web, 'get_app_settings', lambda: settings)
    monkeypatch.setattr(web, '_get_client', lambda: object())
    monkeypatch.setattr(web, '_get_embedder', lambda: object())
    monkeypatch.setattr(web, '_get_reranker', lambda: None)
    monkeypatch.setattr(web, '_get_store', lambda: SimpleNamespace(
        get_paper_meta=lambda: {}, get_corpus_fingerprint=lambda: {}))
    monkeypatch.setattr(web, 'log_run_event', lambda **kw: None)
    seen = []
    def gather(section, *args, **kwargs):
        seen.append((section.heading, dict(kwargs['doc_usage_history']), args[3]))
        return [Evidence(chunk_id=1, doc_id='doc', score=90, summary='evidence')]
    monkeypatch.setattr(stages, 'gather_evidence', gather)
    monkeypatch.setattr(stages, 'gather_broad_bibliography', lambda *a, **kw: [])
    monkeypatch.setattr(stages, 'comparison_table_required', lambda *a: False)
    monkeypatch.setattr(figures, 'select_section_figures', lambda *a: 0)
    def write(section, *a, **kw):
        assert a[4] == 'custom'
        if section.heading == 'third':
            raise RuntimeError('temporary model failure')
        return SectionDraft(heading=section.heading, markdown='text', cited_doc_ids=['doc'])
    monkeypatch.setattr(stages, 'write_section', write)
    monkeypatch.setattr(stages, 'write_intro_conclusion', lambda *a, **kw: ('intro', 'conclusion'))
    monkeypatch.setattr(stages, 'render_review_note', lambda *a: str(tmp_path / 'review.md'))
    monkeypatch.setattr(stages, 'export_docx', lambda *a: '')
    captured = []
    monkeypatch.setattr(manifest, 'write_review_manifest', lambda data: captured.append(data) or '')
    job = dict(job_id='j', topic='t', focus='f', words=3000, sections=3,
               status='running', model='custom', log=[], checkpoint=[
                   SectionDraft(heading='first', markdown='saved', cited_doc_ids=['doc'], source_evidence_count=4).model_dump()])
    web._jobs['j'] = job
    outline = {'title': 't', 'sections': [{'heading': h} for h in ('first', 'second', 'third')]}
    web._review_generator_worker('j', 't', 'f', 3000, 3, outline, 'custom')
    assert job['status'] == 'partial', job.get('error')
    assert seen == [('second', {'doc': 1}, 'custom'), ('third', {'doc': 2}, 'custom')]
    assert len(job['checkpoint']) == 2
    assert job['evidence_count'] == 5
    assert settings.REVIEW_MODEL == 'original'
    assert captured[0].evidence_by_heading['first'] is None
    assert captured[0].evidence_by_heading['second'][0].doc_id == 'doc'
    assert captured[0].evidence_by_heading['third'] == []
    assert ReviewJobStore(str(tmp_path / 'jobs.db')).load_all()[0]['status'] == 'partial'

    # Simulate a fresh Web process, then finish only the previously failed chapter.
    web._jobs.clear()
    web._review_jobs_loaded_path = None
    web._load_review_jobs()
    restored = web._jobs['j']
    seen.clear()
    monkeypatch.setattr(stages, 'write_section', lambda section, *a, **kw:
        SectionDraft(heading=section.heading, markdown='recovered', cited_doc_ids=['doc']))
    web._review_generator_worker('j', 't', 'f', 3000, 3, restored['outline'], 'custom')
    assert restored['status'] == 'done', restored.get('error')
    assert seen == [('third', {'doc': 2}, 'custom')]
    assert restored['evidence_count'] == 6
    assert [draft['heading'] for draft in restored['checkpoint']] == ['first', 'second', 'third']
    assert ReviewJobStore(str(tmp_path / 'jobs.db')).load_all()[0]['status'] == 'done'


def test_mcp_jobs_listed_read_only_and_not_resumable_from_web(monkeypatch, tmp_path):
    """MCP 的任务在 Web 列表可见（带 source=mcp），但续跑必须回 MCP，不能被 Web 抢走。"""
    import sqlite3
    from starlette.requests import Request
    import web_server
    out_dir = tmp_path / 'reviews'
    out_dir.mkdir()
    (out_dir / 'mcp_review.md').write_text('# r', encoding='utf-8')
    prod_db = tmp_path / 'batch_tracking.db'
    conn = sqlite3.connect(prod_db)
    conn.execute('CREATE TABLE review_jobs (job_id TEXT PRIMARY KEY, topic TEXT, status TEXT, '
                 'progress_log TEXT, output_path TEXT, docx_path TEXT, manifest_path TEXT, '
                 'error TEXT, queued_at TEXT, resume_after TEXT)')
    conn.execute("INSERT INTO review_jobs VALUES ('m1', 'MCP主题', 'completed', '[\"ok\"]', ?, "
                 "'/elsewhere/x.docx', '', '', '2026-09-27 10:00:00', '')",
                 (str(out_dir / 'mcp_review.md'),))
    conn.execute("INSERT INTO review_jobs VALUES ('m2', 'MCP暂停', 'paused_quota', '[]', '', '', '', "
                 "'', '2026-09-27 11:00:00', '2026-09-28T00:00:00+00:00')")
    conn.commit()
    conn.close()
    monkeypatch.setenv('WEB_REVIEW_JOBS_DB_PATH', str(tmp_path / 'web_jobs.db'))
    monkeypatch.setattr(web_server, '_jobs', {})
    monkeypatch.setattr(web_server, '_review_jobs_loaded_path', None, raising=False)
    monkeypatch.setattr(web_server, 'get_app_settings', lambda: SimpleNamespace(
        DB_PATH=str(prod_db), REVIEW_MODEL='review', REVIEW_OUTPUT_DIR=str(out_dir)))

    jobs = json.loads(asyncio.run(web_server.api_review_jobs(None)).body)['data']
    by_id = {job['job_id']: job for job in jobs}
    assert [job['job_id'] for job in jobs] == ['m2', 'm1']
    assert by_id['m1']['source'] == 'mcp' and by_id['m1']['status'] == 'done'
    assert by_id['m1']['filename_md'] == 'mcp_review.md'
    assert by_id['m1']['filename_docx'] == ''  # 不在综述目录内的路径不给下载名
    assert by_id['m2']['resume_after'] == '2026-09-28T00:00:00+00:00'

    scope = {'type': 'http', 'method': 'POST', 'path': '/api/review/jobs/m2/resume',
             'headers': [], 'path_params': {'job_id': 'm2'}}
    resp = asyncio.run(web_server.api_review_resume(Request(scope)))
    assert resp.status_code == 409
    assert 'MCP' in json.loads(resp.body)['error']
