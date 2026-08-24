"""MCP 生成/检索客户端分流与调度的回归测试。"""
import importlib
import sys
import threading
import time
from types import SimpleNamespace


def _fresh_mcp(monkeypatch, tmp_path):
    monkeypatch.setenv("MINERU_API_KEY", "test-mineru")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-siliconflow")
    monkeypatch.setenv("SILICONFLOW_API_BASE", "https://siliconflow.invalid/v1")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    import utils
    utils._settings = None
    sys.modules.pop("mcp_server", None)
    return importlib.import_module("mcp_server")


def test_mcp_embedding_client_is_siliconflow_not_openai(monkeypatch, tmp_path):
    """Qwen3 嵌入必须走硅基流动端点，不能复用 OpenAI 生成客户端。"""
    calls = []
    direct_client = object()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    import openai
    import httpx
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs:
                        direct_client if kwargs == {"timeout": 180, "trust_env": False} else None)
    server = _fresh_mcp(monkeypatch, tmp_path)

    server._embedding_client()

    assert calls == [{
        "api_key": "test-siliconflow",
        "base_url": "https://siliconflow.invalid/v1",
        "timeout": 180,
        "http_client": direct_client,
    }]


def test_openai_generation_client_uses_explicit_lit_proxy(monkeypatch, tmp_path):
    """MCP 启动脚本清空系统代理后，生成客户端仍须使用项目 LIT 代理。"""
    import httpx
    import llm_router
    import utils

    proxy_client = object()
    openai_calls = []
    monkeypatch.setenv("MINERU_API_KEY", "test-mineru")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LIT_HTTPS_PROXY", "http://proxy.invalid:1234")
    utils._settings = None
    monkeypatch.setattr(httpx, "Client", lambda **kwargs:
                        proxy_client if kwargs == {
                            "proxy": "http://proxy.invalid:1234", "timeout": 180
                        } else None)
    monkeypatch.setattr(llm_router, "OpenAI",
                        lambda **kwargs: openai_calls.append(kwargs) or object())

    llm_router.make_openai_client(quiet=True)

    assert openai_calls[0]["http_client"] is proxy_client


def test_mcp_slow_tools_are_offloaded_from_event_loop(monkeypatch, tmp_path):
    """大纲等同步重活必须在线程中执行，不能阻塞同连接上的检索请求。"""
    server = _fresh_mcp(monkeypatch, tmp_path)
    tool = server.mcp._tool_manager.get_tool("generate_outline")
    assert tool.is_async is True


def test_review_jobs_use_single_fifo_worker(monkeypatch, tmp_path):
    """多篇综述应进入单消费者队列，不能每篇各开一个无限并发线程。"""
    server = _fresh_mcp(monkeypatch, tmp_path)
    started = []
    release = threading.Event()

    def fake_run(job_id, *_args):
        started.append(job_id)
        release.wait(timeout=2)

    monkeypatch.setattr(server, "_run_review_job", fake_run)
    start = server.start_review.__wrapped__
    start("综述一")
    deadline = time.time() + 1
    while not started and time.time() < deadline:
        time.sleep(0.01)
    start("综述二")
    time.sleep(0.05)
    assert len(started) == 1
    release.set()
    deadline = time.time() + 1
    while len(started) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert len(started) == 2


def test_review_pipeline_uses_mini_for_evidence_and_review_model_for_writing(
        monkeypatch, tmp_path):
    """MCP 完整综述只有正文写作使用 GPT-5.4，证据评分固定 mini。"""
    server = _fresh_mcp(monkeypatch, tmp_path)
    from litreview.models import Evidence, SectionDraft
    import litreview.stages as stages
    import litreview.figures as figures

    models = {"evidence": [], "section": [], "intro": []}
    outline_json = '{"title":"测试综述","sections":[{"heading":"第一章","questions":["问题"]}]}'
    evidence = Evidence(chunk_id=1, doc_id="doc1", score=8, summary="证据",
                        section_title="", retrieval_score=0.9)

    monkeypatch.setattr(server, "_client", lambda: object())
    monkeypatch.setattr(server, "_store", lambda: SimpleNamespace(
        get_paper_meta=lambda: {"doc1": {"title": "文献"}}))
    monkeypatch.setattr(server, "_embedder", lambda: object())
    monkeypatch.setattr(server, "_reranker", lambda: None)
    monkeypatch.setattr(stages, "gather_evidence",
                        lambda _sec, _store, _embedder, _client, model, *_args, **_kwargs:
                        models["evidence"].append(model) or [evidence])
    monkeypatch.setattr(stages, "write_section",
                        lambda sec, _ev, _meta, _title, _client, model, **_kwargs:
                        models["section"].append(model) or SectionDraft(
                            heading=sec.heading, markdown="正文 [@doc1]", cited_doc_ids=["doc1"]))
    monkeypatch.setattr(stages, "write_intro_conclusion",
                        lambda _outline, _drafts, _client, model, **_kwargs:
                        models["intro"].append(model) or ("引言", "结论"))
    monkeypatch.setattr(stages, "assemble_review", lambda *_args, **_kwargs: SimpleNamespace(
        title="测试综述", sections=[1], doc_count=1, evidence_count=1))
    monkeypatch.setattr(stages, "render_review_note", lambda *_args: str(tmp_path / "review.md"))
    monkeypatch.setattr(stages, "export_docx", lambda _path: "")
    monkeypatch.setattr(figures, "select_section_figures", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(server, "log_run_event", lambda **_kwargs: None)
    server._jobs["job"] = {"status": "running", "topic": "测试", "log": [],
                            "output": "", "docx": "", "error": "", "summary": "",
                            "started_at": "now"}

    server._run_review_job("job", "测试", "", 0, 0, outline_json)

    assert models == {
        "evidence": [server.settings.ANALYSIS_MODEL],
        "section": [server.settings.REVIEW_MODEL],
        "intro": [server.settings.REVIEW_MODEL],
    }
    assert server._jobs["job"]["checkpoint_json"]
    assert server._jobs["job"]["heartbeat_at"]
    assert server._jobs["job"]["outline_json"]


def test_review_job_persistence_retries_locked_database(monkeypatch, tmp_path):
    """持久化不能吞掉 locked；必须让五次退避装饰器接管重试。"""
    server = _fresh_mcp(monkeypatch, tmp_path)
    attempts = []

    class FakeConnection:
        def execute(self, *_args, **_kwargs):
            import sqlite3
            attempts.append(1)
            if len(attempts) < 3:
                raise sqlite3.OperationalError("database is locked")

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(server, "_init_review_jobs_db", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_db_conn", lambda *a, **k: FakeConnection())

    server._persist_review_job({"job_id": "job-retry"})

    assert len(attempts) == 3


def test_normal_persistence_does_not_interrupt_running_job(monkeypatch, tmp_path):
    """启动恢复只能执行一次；正常心跳持久化不能把当前任务改成 interrupted。"""
    server = _fresh_mcp(monkeypatch, tmp_path)
    job = {
        "job_id": "running-job", "topic": "测试", "status": "running",
        "log": [], "heartbeat_at": "2026-08-24 12:00:00",
    }
    server._persist_review_job(job)
    server._init_review_jobs_db()

    persisted = server._get_persistent_review_job("running-job")
    assert persisted["status"] == "running"


def test_resume_review_requeues_persisted_checkpoint(monkeypatch, tmp_path):
    """中断任务应携带规范化大纲和章节检查点重新入队。"""
    import queue
    server = _fresh_mcp(monkeypatch, tmp_path)
    checkpoint = '[{"heading":"第一章","markdown":"正文","cited_doc_ids":["doc1"]}]'
    outline = '{"title":"测试综述","sections":[{"heading":"第一章","questions":["问题"]},{"heading":"第二章","questions":["问题"]}]}'
    server._persist_review_job({
        "job_id": "resume-job", "topic": "测试", "status": "interrupted",
        "outline_json": outline, "checkpoint_json": checkpoint, "log": [],
        "research_gap": "缺口", "thesis": "立论", "target_lang": "zh",
    })
    queued = queue.Queue()
    monkeypatch.setattr(server, "_review_queue", queued)
    monkeypatch.setattr(server, "_ensure_review_worker", lambda: None)

    message = server.resume_review.__wrapped__("resume-job")

    assert "重新入队" in message
    assert queued.get_nowait()[0] == "resume-job"
    assert server._get_persistent_review_job("resume-job")["status"] == "queued"


def test_review_cli_uses_review_model_for_body_writing(monkeypatch):
    """命令行 generate 与 MCP 一致，正文客户端返回固定 GPT-5.4。"""
    import llm_router
    import review

    client = object()
    monkeypatch.setattr(llm_router, "make_openai_client",
                        lambda quiet=False: (client, "gpt-5.4-mini"))

    actual_client, model = review._make_client()

    assert actual_client is client
    assert model == review.settings.REVIEW_MODEL == "gpt-5.4"


def test_mcp_list_documents_pagination_and_filter(monkeypatch, tmp_path):
    """测试 MCP list_documents 工具的分页、关键词过滤与状态筛选。"""
    import sqlite3
    db_path = tmp_path / "test.db"
    server = _fresh_mcp(monkeypatch, tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE papers (id TEXT PRIMARY KEY, title TEXT, pdf_path TEXT, language TEXT, mineru_md TEXT, images_dir TEXT, status TEXT, batch_provider TEXT, batch_job_id TEXT, result_json TEXT, error_message TEXT, processed_at TEXT)")
    conn.execute("CREATE TABLE paper_details (doc_id TEXT PRIMARY KEY, title TEXT, title_zh TEXT, title_en TEXT, doi TEXT, authors TEXT, journal TEXT, year TEXT, keywords TEXT, n_figures INTEGER, n_tables INTEGER, n_refs INTEGER, enriched_at TEXT, source TEXT, doc_type TEXT, patent_no TEXT, filing_date TEXT, legal_status TEXT, status_checked_at TEXT, publisher TEXT, doc_no TEXT, effective_date TEXT)")
    conn.execute("CREATE TABLE review_index_meta (doc_id TEXT PRIMARY KEY, md_hash TEXT, n_chunks INTEGER, embedded INTEGER, indexed_at TEXT)")
    
    # Insert 3 papers
    conn.execute("INSERT INTO papers (id, title, status, mineru_md, processed_at) VALUES ('p1', '磷酸萃取工艺', 'EXPORTED', '正文内容很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长', '2026-08-01')")
    conn.execute("INSERT INTO paper_details (doc_id, title_zh, authors, doc_type, year) VALUES ('p1', '磷酸萃取工艺', '张三', 'paper', '2023')")
    conn.execute("INSERT INTO review_index_meta (doc_id, md_hash, n_chunks, embedded) VALUES ('p1', 'h1', 5, 1)")

    conn.execute("INSERT INTO papers (id, title, status, mineru_md, processed_at) VALUES ('p2', '磷肥标准GB', 'EXPORTED', '标准正文很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长', '2026-08-02')")
    conn.execute("INSERT INTO paper_details (doc_id, title_zh, authors, doc_type, doc_no, year) VALUES ('p2', '磷肥标准GB', '国家标准局', 'standard', 'GB 1234-2024', '2024')")
    conn.execute("INSERT INTO review_index_meta (doc_id, md_hash, n_chunks, embedded) VALUES ('p2', 'h2', 2, 0)")

    conn.execute("INSERT INTO papers (id, title, status, mineru_md, processed_at) VALUES ('p3', '空正文论文', 'EXPORTED', '', '2026-08-03')")
    conn.execute("INSERT INTO paper_details (doc_id, title_zh, authors, doc_type, year) VALUES ('p3', '空正文论文', '李四', 'paper', '2022')")

    conn.commit()
    conn.close()

    list_docs = server.list_documents.__wrapped__

    # 1. 默认查询
    res = list_docs(limit=10)
    assert "共命中 3 篇文献" in res
    assert "磷酸萃取工艺" in res
    assert "GB 1234-2024" in res
    assert "✅已索引(5块)" in res
    assert "⏳待向量化(2块)" in res
    assert "❌不可索引" in res

    # 2. 过滤 doc_type='standard'
    res_std = list_docs(doc_type="standard")
    assert "共命中 1 篇文献" in res_std
    assert "GB 1234-2024" in res_std

    # 3. 过滤 vector_status='indexed'
    res_idx = list_docs(vector_status="indexed")
    assert "共命中 1 篇文献" in res_idx
    assert "磷酸萃取工艺" in res_idx

    # 4. 游标分页 limit=1
    res_page1 = list_docs(limit=1, cursor=0)
    assert "当前显示第 1 ~ 1 篇" in res_page1
    assert "cursor=1" in res_page1


def test_mcp_library_reconcile_is_strictly_readonly(monkeypatch, tmp_path):
    """测试 MCP library_reconcile 是严格只读诊断，且正确输出四层审计报告。"""
    import sqlite3
    db_path = tmp_path / "test.db"
    server = _fresh_mcp(monkeypatch, tmp_path)
    
    # 确保初始化 schema
    store = server._store()
    conn = sqlite3.connect(str(db_path))

    conn.execute("INSERT INTO papers (id, title, status, mineru_md, processed_at) VALUES ('p1', '重复标题论文', 'EXPORTED', '正文', '2026-08-01')")
    conn.execute("INSERT INTO paper_details (doc_id, title_zh, doi) VALUES ('p1', '重复标题论文', '10.1000/182')")
    conn.execute("INSERT INTO review_index_meta (doc_id, md_hash, n_chunks, embedded) VALUES ('p1', 'h1', 5, 1)")

    conn.execute("INSERT INTO papers (id, title, status, mineru_md, processed_at) VALUES ('p2', '重复标题论文', 'EXPORTED', '正文2', '2026-08-02')")
    conn.execute("INSERT INTO paper_details (doc_id, title_zh, doi) VALUES ('p2', '重复标题论文', '10.1000/182')")
    conn.execute("INSERT INTO review_index_meta (doc_id, md_hash, n_chunks, embedded) VALUES ('p2', 'h2', 3, 0)")
    conn.commit()
    conn.close()

    # 记录执行前 DB 大小
    db_size_before = db_path.stat().st_size
    report = server.library_reconcile.__wrapped__()
    db_size_after = db_path.stat().st_size

    assert "四层数据对齐审计报告" in report
    assert "已索引文献 (embedded=1): 1 篇" in report
    assert "待向量化文献 (embedded=0): 1 篇" in report
    assert "重复 DOI: 1 组" in report
    assert "重复完全同名题名: 1 组" in report
    assert db_size_before == db_size_after


def test_quarantine_records_excluded_from_search_and_list(monkeypatch, tmp_path):
    """测试脏数据/隔离文献被强制从检索和目录默认视图中排除。"""
    import numpy as np
    import sqlite3
    db_path = tmp_path / "test.db"
    server = _fresh_mcp(monkeypatch, tmp_path)
    store = server._store()
    conn = sqlite3.connect(str(db_path))

    # 插入 1 篇正常文献和 1 篇隔离脏文献
    conn.execute("INSERT INTO papers (id, title, status, mineru_md, processed_at) VALUES ('good_doc', '正常磷化工论文', 'EXPORTED', '磷酸萃取正文...', '2026-08-01')")
    conn.execute("INSERT INTO paper_details (doc_id, title_zh, quarantine_status) VALUES ('good_doc', '正常磷化工论文', 'NORMAL')")
    v1 = np.ones(store.dim, dtype=np.float32)
    v1 = v1 / np.linalg.norm(v1)
    conn.execute("INSERT INTO review_chunks (doc_id, chunk_index, section_title, content, char_len, embedding) VALUES ('good_doc', 0, '引言', '磷酸萃取', 4, ?)", (v1.tobytes(),))
    conn.execute("INSERT INTO review_index_meta (doc_id, md_hash, n_chunks, embedded) VALUES ('good_doc', 'h1', 1, 1)")

    conn.execute("INSERT INTO papers (id, title, status, mineru_md, processed_at) VALUES ('dirty_mock', '污染测试Mock论文', 'EXPORTED', '深度学习超分辨率正文...', '2026-08-01')")
    conn.execute("INSERT INTO paper_details (doc_id, title_zh, quarantine_status, quarantine_reason) VALUES ('dirty_mock', '污染测试Mock论文', 'QUARANTINED', 'MOCK_DIRTY_DATA')")
    v2 = np.ones(store.dim, dtype=np.float32)
    v2 = v2 / np.linalg.norm(v2)
    conn.execute("INSERT INTO review_chunks (doc_id, chunk_index, section_title, content, char_len, embedding) VALUES ('dirty_mock', 0, '引言', '深度学习天线', 6, ?)", (v2.tobytes(),))
    conn.execute("INSERT INTO review_index_meta (doc_id, md_hash, n_chunks, embedded) VALUES ('dirty_mock', 'h2', 1, 1)")
    conn.commit()
    conn.close()

    # 1. 向量检索中严格排除 dirty_mock
    res_search = store.search(v1, top_k=10)
    found_doc_ids = [r.doc_id for r in res_search]
    assert "good_doc" in found_doc_ids
    assert "dirty_mock" not in found_doc_ids

    # 2. list_documents 默认视图中严格排除 dirty_mock
    list_docs = server.list_documents.__wrapped__
    out_default = list_docs()
    assert "正常磷化工论文" in out_default
    assert "污染测试Mock论文" not in out_default

    # 3. include_quarantined=True 可查出隔离记录供诊断
    out_with_q = list_docs(include_quarantined=True)
    assert "正常磷化工论文" in out_with_q
    assert "污染测试Mock论文" in out_with_q


def test_get_citation_network_and_expand_mcp_tools(monkeypatch, tmp_path):
    """测试 get_citation_network 与 citation_expand 的包装调用与口径对齐。"""
    from litreview.graph_schema import init_graph_schema
    import sqlite3
    db_path = tmp_path / "test_citation_network.db"
    conn = sqlite3.connect(str(db_path))
    init_graph_schema(conn)

    # 插入测试文献
    conn.execute("INSERT INTO works (work_id, canonical_title, publication_year, in_library, quarantine_status, eligible_for_citation_graph, created_at, updated_at) VALUES ('seed1', '晶体结块机理', 2017, 1, 'NORMAL', 1, '2026-08-01', '2026-08-01')")
    conn.execute("INSERT INTO works (work_id, canonical_title, publication_year, in_library, quarantine_status, eligible_for_citation_graph, created_at, updated_at) VALUES ('citing1', '防结块涂层', 2021, 1, 'NORMAL', 1, '2026-08-01', '2026-08-01')")
    conn.execute("INSERT INTO works (work_id, canonical_title, publication_year, in_library, quarantine_status, eligible_for_citation_graph, created_at, updated_at) VALUES ('ext1', '外部文献', 2010, 0, 'UNRESOLVED', 1, '2026-08-01', '2026-08-01')")
    conn.execute("INSERT INTO citation_edges (edge_id, citing_work_id, cited_work_id, edge_status, first_observed_at, last_confirmed_at) VALUES ('e1', 'citing1', 'seed1', 'CONFIRMED', '2026-08-01', '2026-08-01')")
    conn.execute("INSERT INTO citation_edges (edge_id, citing_work_id, cited_work_id, edge_status, first_observed_at, last_confirmed_at) VALUES ('e2', 'seed1', 'ext1', 'UNRESOLVED_EXTERNAL', '2026-08-01', '2026-08-01')")
    conn.execute("INSERT INTO reference_mentions (mention_id, citing_work_id, ref_index, raw_text, raw_length, quality_flag, parse_status, created_at) VALUES ('m1', 'seed1', 1, 'Ref 1 text', 10, 'NORMAL', 'PARSED_COMPLETE', '2026-08-01')")
    conn.commit()
    conn.close()

    monkeypatch.setenv("CITATION_NETWORK_DB_PATH", str(db_path))
    server = _fresh_mcp(monkeypatch, tmp_path)

    # 1. get_citation_network with include_candidates=False
    get_net = server.get_citation_network.__wrapped__
    out_net = get_net(doc_id="seed1", include_candidates=False)
    assert "**核心文献**: 《晶体结块机理》" in out_net
    assert "网络拓扑指标统计" in out_net
    assert "馆内被引次数" in out_net

    # 2. citation_expand with mock
    mock_res = {
        "query_run_id": "run_test123",
        "seeds": [{"doc_id": "seed1", "title": "晶体结块机理", "publication_year": 2017, "openalex_id": "https://openalex.org/W1", "doi": None, "referenced_works_count": 10, "cited_by_count": 5}],
        "statistics": {
            "query_run_id": "run_test123", "seeds_count": 1, "backward_raw_mentions": 10,
            "backward_unique_ids_before_limit": 10, "backward_metadata_requested": 10, "backward_metadata_returned": 10,
            "forward_total_available": 5, "forward_records_retrieved": 5, "forward_records_truncated": False,
            "materialized_candidates_before_dedup": 15, "unique_candidates_after_dedup": 12,
            "foundational_candidates_count": 8, "emerging_candidates_count": 4,
            "in_library_matches_count": 1, "new_external_candidates_count": 11,
            "api_requests_count": 3, "api_failures_count": 0, "api_success_rate": 1.0, "metadata_coverage": 1.0
        },
        "top_foundational_literature": [{
            "title": "经典结块文献", "publication_year": 2005, "authors": ["Author X"], "journal_or_venue": "Powder Tech",
            "recommendation_reason": "被种子共同引用", "cited_by_count": 120, "doi": "10.1016/j.powtec.2005",
            "landing_page_url": "https://doi.org/10.1016/j.powtec.2005", "openalex_id": "https://openalex.org/W2",
            "in_library": True, "in_library_work_id": "seed1", "is_open_access": False
        }],
        "top_emerging_literature": [{
            "title": "2024最新施引论文", "publication_year": 2024, "authors": ["Author Y"], "journal_or_venue": "Crystals",
            "recommendation_reason": "近3年施引", "cited_by_count": 30, "doi": "10.3390/cryst2024",
            "landing_page_url": "https://doi.org/10.3390/cryst2024", "openalex_id": "https://openalex.org/W3",
            "in_library": False, "in_library_work_id": None, "is_open_access": True, "oa_url": "https://mdpi.com/oa.pdf"
        }],
        "shared_by_all_seeds": [],
        "in_library_matches": [{"title": "经典结块文献", "publication_year": 2005, "in_library_work_id": "seed1", "track": "FOUNDATIONAL"}]
    }

    with monkeypatch.context() as m:
        import litreview.snowball
        m.setattr(litreview.snowball, "expand_citations", lambda **kwargs: mock_res)
        expand = server.citation_expand.__wrapped__
        out_expand = expand(seed_ids="seed1", direction="both")
        assert "OpenAlex 外部引文网络滚雪球发现报告" in out_expand
        assert "经典结块文献" in out_expand
        assert "2024最新施引论文" in out_expand

    # 3. test list_discovery_candidates, approve, acquire, ingest
    from litreview.snowball import init_cache_db
    cache_db = tmp_path / "external_discovery_cache.db"
    monkeypatch.setenv("EXTERNAL_DISCOVERY_CACHE_DB_PATH", str(cache_db))
    init_cache_db(str(cache_db))
    c_conn = sqlite3.connect(str(cache_db))
    c_conn.execute("INSERT INTO discovery_runs (query_run_id, seeds_json, direction, depth, parameters_json, statistics_json, created_at) VALUES ('run_mcp_1', '[]', 'both', 1, '{}', '{}', '2026-08-01')")
    c_conn.execute("""
        INSERT INTO discovered_candidates (
            candidate_id, query_run_id, openalex_id, doi, title, authors_json,
            journal_or_venue, publication_year, cited_by_count, is_open_access,
            oa_url, landing_page_url, track, direction, source_seeds_json,
            co_citation_count, recommendation_reason, in_library, in_library_work_id,
            match_method, match_evidence_json, data_source, response_hash,
            lifecycle_status, discovered_at
        ) VALUES (
            'cand_mcp_1', 'run_mcp_1', 'https://openalex.org/W99', '10.1016/j.test',
            'MCP Testing Paper', '[\"Author T\"]', 'Test Venue', 2024, 10, 1,
            'https://example.com/test.pdf', 'https://doi.org/10.1016/j.test',
            'EMERGING', 'forward', '[\"seed1\"]', 1, '测试推荐', 0, NULL, NULL, NULL,
            'openalex', 'h', 'DISCOVERED', '2026-08-01'
        )
    """)
    c_conn.commit()
    c_conn.close()

    list_cands = server.list_discovery_candidates.__wrapped__
    out_list = list_cands()
    assert "外部发现候选文献池" in out_list
    assert "MCP Testing Paper" in out_list

    approve_fn = server.approve_candidates.__wrapped__
    out_app = approve_fn(candidate_ids="cand_mcp_1", action="APPROVE")
    assert "候选文献审批确认报告" in out_app
    assert "APPROVED" in out_app


def test_mcp_propose_review_angles(monkeypatch, tmp_path):
    """测试前置立论探测 propose_review_angles 工具与大纲生成对齐。"""
    import json
    server = _fresh_mcp(monkeypatch, tmp_path)
    
    def fake_propose(*args, **kwargs):
        return [
            {
                "angle_id": "A",
                "title": "生物基聚氨酯包膜尿素养分释放机理与土壤全降解研究进展",
                "thesis": "论证植物油改性交联网络可兼顾长控释期与微塑料零残留",
                "research_gap": "现有研究忽略了残膜在不同温湿度土壤中的微塑料降解分歧",
                "mainline_logic": "要素并列利弊暗转",
                "target_value": "填补环境生态与控释性能协同评估的空白",
                "suggested_sections": ["成膜交联机理", "释放动力学", "土壤降解动力学", "工业化连续包膜瓶颈"]
            }
        ]

    monkeypatch.setattr("litreview.stages.propose_review_angles", fake_propose)
    propose_fn = server.propose_review_angles.__wrapped__
    res_str = propose_fn("包膜尿素", words=8000)
    data = json.loads(res_str)
    
    assert data["topic"] == "包膜尿素"
    assert "proposed_angles" in data
    assert len(data["proposed_angles"]) == 1
    assert data["proposed_angles"][0]["angle_id"] == "A"
    assert "植物油改性" in data["proposed_angles"][0]["thesis"]
    assert "alignment_guidance" in data
