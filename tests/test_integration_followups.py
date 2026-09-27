"""四路修复合并后的收尾回归：daemon 入库锁、enrich 端点、CLI 大纲阶段额度暂停、候选全文不走浏览器。

全部在 tmp_path / 种子库里跑，不联网、不调用 LLM。
"""
import importlib
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient

import lit_seed as seed


def test_daemon_jobs_skip_round_when_web_holds_intake_lock(tmp_path, monkeypatch):
    from litreview.intake_lock import intake_lock

    daemon = importlib.import_module("daemon")
    db_path = str(tmp_path / "batch_tracking.db")
    monkeypatch.setattr(daemon, "settings", SimpleNamespace(DB_PATH=db_path))
    notify = Mock()
    monkeypatch.setattr(daemon, "send_notification", notify)
    calls = Mock(return_value={"jobs_checked": 0, "completed": 0, "failed": 0, "exported": 0,
                               "submitted": 0, "pdf_parsed_failed": 0, "submit_failed": 0})
    monkeypatch.setattr(daemon.pipeline, "main", calls)
    monkeypatch.setattr(daemon.batch_pipeline, "prepare_batch", calls)
    monkeypatch.setattr(daemon.batch_pipeline, "fetch_batch", calls)

    with intake_lock(db_path):
        daemon.realtime_job()
        daemon.batch_prepare_job()
        daemon.batch_fetch_job()
    calls.assert_not_called()
    notify.assert_not_called()  # 锁忙是正常让路，不是异常，不发告警

    daemon.batch_fetch_job()
    calls.assert_called_once()


def test_document_enrich_endpoint_is_wired(seeded_dbs, monkeypatch):
    import web_server

    seen = {}

    def fake_enrich(doc_id, *paths):
        seen["doc_id"] = doc_id
        return {"success": True, "doc_id": doc_id}

    monkeypatch.setattr(web_server, "enrich_document_metadata", fake_enrich)
    resp = TestClient(web_server.app).post(f"/api/library/documents/{seed.DOC_A}/enrich")
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert seen["doc_id"] == seed.DOC_A


def test_cli_outline_quota_exits_with_pause_code(monkeypatch):
    review = importlib.import_module("review")
    from litreview import retrieval_store, stages
    from litreview.quota import QuotaExceededError

    monkeypatch.setattr(review, "_make_client", lambda: (object(), "m"))
    monkeypatch.setattr(review, "_require_siliconflow", lambda: None)
    monkeypatch.setattr(review, "_make_embedder", lambda _c: object())
    monkeypatch.setattr(retrieval_store, "create_retrieval_store", lambda *_a, **_k: object())

    def no_quota(*_a, **_k):
        raise QuotaExceededError("额度不足", utc_date="2026-09-27", model="gpt-5.4")

    monkeypatch.setattr(stages, "generate_outline", no_quota)
    args = SimpleNamespace(topic="测试", focus="", sections=0, words=0, lang="zh", output=None,
                           gap="", thesis="", logic="")
    with pytest.raises(SystemExit) as exc:
        review.cmd_outline(args)
    assert exc.value.code == review.EXIT_PAUSED_QUOTA


def test_fulltext_acquisition_has_no_browser_fallback(tmp_path, monkeypatch):
    """直链拿不到 PDF 就停在不可得，交给手动上传；不再有无头浏览器绕反爬的分支。"""
    from litreview import pipeline as cand_pipeline

    assert not hasattr(cand_pipeline, "_download_via_browser")

    cache_db = str(tmp_path / "cache.db")
    cand_pipeline.init_cache_db(cache_db)
    conn = sqlite3.connect(cache_db)
    conn.execute("""INSERT INTO discovered_candidates
        (candidate_id, query_run_id, track, direction, source_seeds_json, co_citation_count,
         recommendation_reason, in_library, data_source, response_hash, discovered_at,
         title, lifecycle_status, is_open_access, oa_url)
        VALUES ('c1', 'r1', 'EMERGING', 'forward', '[]', 0, 'r', 0, 'openalex', 'h', 'now',
                'T', 'APPROVED', 1, 'https://oa.invalid/paper.pdf')""")
    conn.commit()
    conn.close()

    requested = []

    def fake_urlopen(req, timeout=0):
        requested.append((req.full_url, req.get_header("User-agent")))
        raise OSError("403 Forbidden")

    monkeypatch.setattr(cand_pipeline.urllib.request, "urlopen", fake_urlopen)
    res = cand_pipeline.acquire_fulltext(cache_db, staging_dir=str(tmp_path / "staging"))

    assert [url for url, _ua in requested] == ["https://oa.invalid/paper.pdf"]
    assert requested[0][1].startswith("LiteratureAnalyzer/")
    assert res["results"][0]["status"] != "FULLTEXT_ACQUIRED"
