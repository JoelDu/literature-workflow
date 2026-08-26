import json
import os
import re
import sqlite3
import uuid
import pytest
from unittest.mock import patch, MagicMock

from litreview.manifest import (
    write_review_manifest,
    ManifestInput,
    RunContext,
    PromptSnapshot,
    RetrievalSnapshot,
    ManifestError,
    ALLOWLIST_PAPER_META_KEYS,
)
from litreview.models import Outline, OutlineSection, ReviewDoc, SectionDraft, Evidence
from litreview.review_quality import audit_and_clean_review, EvidenceContext
from litreview.store import VectorStore


def test_corpus_fingerprint_deterministic_and_missing_fallback(tmp_path):
    """验证 VectorStore.get_corpus_fingerprint 的确定性与表缺失安全降级。"""
    db_path = str(tmp_path / "test_corpus.db")

    # 1. 模拟旧库（仅 papers 表，无 review_index_meta），只读连接时返回 None 且不跑 DDL
    conn_raw = sqlite3.connect(db_path)
    conn_raw.execute("CREATE TABLE papers (id TEXT PRIMARY KEY, title TEXT)")
    conn_raw.commit()
    conn_raw.close()

    ro_store = VectorStore(db_path, 4096, readonly=True)
    assert ro_store.get_corpus_fingerprint() is None

    # 2. 插入两行 review_index_meta
    store = VectorStore(db_path, 4096)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO review_index_meta (doc_id, md_hash, n_chunks, embedded) VALUES ('doc_b', 'hash_b', 1, 1)")
    conn.execute("INSERT INTO review_index_meta (doc_id, md_hash, n_chunks, embedded) VALUES ('doc_a', 'hash_a', 1, 1)")
    conn.commit()
    conn.close()

    fp1 = store.get_corpus_fingerprint()
    assert fp1 is not None
    assert len(fp1) == 64

    # 乱序插入应得出相同指纹（因为 ORDER BY doc_id ASC）
    db_path2 = str(tmp_path / "test_corpus2.db")
    store2 = VectorStore(db_path2, 4096)
    conn2 = sqlite3.connect(db_path2)
    conn2.execute("INSERT INTO review_index_meta (doc_id, md_hash, n_chunks, embedded) VALUES ('doc_a', 'hash_a', 1, 1)")
    conn2.execute("INSERT INTO review_index_meta (doc_id, md_hash, n_chunks, embedded) VALUES ('doc_b', 'hash_b', 1, 1)")
    conn2.commit()
    conn2.close()

    fp2 = store2.get_corpus_fingerprint()
    assert fp1 == fp2


def test_quality_gate_populates_reference_doc_ids():
    """验证 ReviewQualityGate 在清洗后按参考文献最终编号顺序填充 reference_doc_ids。"""
    raw_review = ReviewDoc(
        title="测试综述",
        topic="磷酸化机理",
        sections=[
            SectionDraft(heading="第一节", markdown="正文引用 [2] 与 [1]"),
        ],
    )
    allowed = EvidenceContext(number_to_doc_id={1: "doc_alpha", 2: "doc_beta"})
    paper_meta = {
        "doc_alpha": {"title": "Alpha 论文", "authors": "张三", "year": "2021"},
        "doc_beta": {"title": "Beta 论文", "authors": "李四", "year": "2022"},
    }

    result = audit_and_clean_review(raw_review, allowed, paper_meta)
    cleaned = result.review

    assert len(cleaned.references) == 2
    assert len(cleaned.reference_doc_ids) == 2
    # 第一次出现的 [2] 会被重编号为 [1] (doc_beta)，[1] 会被重编号为 [2] (doc_alpha)
    assert cleaned.reference_doc_ids == ["doc_beta", "doc_alpha"]


def test_reference_mapping_status_paths(tmp_path):
    """验证 reference_mapping_status 的三条路径：complete、unavailable_legacy_output、empty。"""
    md_file = tmp_path / "test_ref.md"
    md_file.write_text("正文", encoding="utf-8")

    run_ctx = RunContext(
        topic="测试", run_id="r1", trigger_source="cli", model_name="m1",
        analysis_model="m2", reranker_model="m3", target_words=1000, target_lang="zh"
    )

    # 路径 A: 正常输出，reference_doc_ids 存在 -> complete
    rev_a = ReviewDoc(
        title="T", topic="Top",
        references=["[1] 张三. 2021."],
        reference_doc_ids=["doc_1"],
        sections=[SectionDraft(heading="H", markdown="[1]", cited_doc_ids=["doc_1"])],
    )
    path_a = write_review_manifest(ManifestInput(
        output_md_path=str(md_file), review=rev_a, outline=Outline(title="T", topic="Top"),
        evidence_by_heading={"H": []}, paper_meta={"doc_1": {"title": "Doc1"}}, run_context=run_ctx
    ))
    data_a = json.load(open(path_a, "r", encoding="utf-8"))
    assert data_a["reference_mapping_status"] == "complete"
    assert len(data_a["cited_references"]) == 1
    assert data_a["cited_references"][0]["doc_id"] == "doc_1"

    # 路径 B: 历史旧输出（有 references 但无 reference_doc_ids） -> unavailable_legacy_output，禁止伪造映射
    rev_b = ReviewDoc(
        title="T", topic="Top",
        references=["[1] 张三. 2021."],
        reference_doc_ids=[],
        sections=[SectionDraft(heading="H", markdown="[1]", cited_doc_ids=["doc_1"])],
    )
    path_b = write_review_manifest(ManifestInput(
        output_md_path=str(md_file), review=rev_b, outline=Outline(title="T", topic="Top"),
        evidence_by_heading={"H": []}, paper_meta={"doc_1": {"title": "Doc1"}}, run_context=run_ctx
    ))
    data_b = json.load(open(path_b, "r", encoding="utf-8"))
    assert data_b["reference_mapping_status"] == "unavailable_legacy_output"
    assert data_b["cited_references"] == []

    # 路径 C: 无 references 且无 reference_doc_ids -> empty
    rev_c = ReviewDoc(
        title="T", topic="Top",
        references=[],
        reference_doc_ids=[],
        sections=[SectionDraft(heading="H", markdown="正文", cited_doc_ids=[])],
    )
    path_c = write_review_manifest(ManifestInput(
        output_md_path=str(md_file), review=rev_c, outline=Outline(title="T", topic="Top"),
        evidence_by_heading={"H": []}, paper_meta={}, run_context=run_ctx
    ))
    data_c = json.load(open(path_c, "r", encoding="utf-8"))
    assert data_c["reference_mapping_status"] == "empty"
    assert data_c["cited_references"] == []

    # 路径 D: 数量不匹配（2 篇格式化引用 vs 1 个 ID） -> unavailable_legacy_output，禁止部分映射
    rev_d = ReviewDoc(
        title="T", topic="Top",
        references=["[1] 张三. 2021.", "[2] 李四. 2022."],
        reference_doc_ids=["doc_1"],
        sections=[SectionDraft(heading="H", markdown="[1] [2]", cited_doc_ids=["doc_1", "doc_2"])],
    )
    path_d = write_review_manifest(ManifestInput(
        output_md_path=str(md_file), review=rev_d, outline=Outline(title="T", topic="Top"),
        evidence_by_heading={"H": []}, paper_meta={"doc_1": {"title": "Doc1"}}, run_context=run_ctx
    ))
    data_d = json.load(open(path_d, "r", encoding="utf-8"))
    assert data_d["reference_mapping_status"] == "unavailable_legacy_output"
    assert data_d["cited_references"] == []

    # 路径 E: 反向不匹配（0 篇格式化引用 vs 1 个 ID） -> unavailable_legacy_output，严禁标记 complete
    rev_e = ReviewDoc(
        title="T", topic="Top",
        references=[],
        reference_doc_ids=["doc_1"],
        sections=[SectionDraft(heading="H", markdown="正文", cited_doc_ids=["doc_1"])],
    )
    path_e = write_review_manifest(ManifestInput(
        output_md_path=str(md_file), review=rev_e, outline=Outline(title="T", topic="Top"),
        evidence_by_heading={"H": []}, paper_meta={"doc_1": {"title": "Doc1"}}, run_context=run_ctx
    ))
    data_e = json.load(open(path_e, "r", encoding="utf-8"))
    assert data_e["reference_mapping_status"] == "unavailable_legacy_output"
    assert data_e["cited_references"] == []


def test_evidence_status_all_variants(tmp_path):
    """验证 evidence_status 四种精细状态：complete、unavailable_legacy_checkpoint、unavailable_missing_mapping、empty。"""
    md_file = tmp_path / "test_ev.md"
    md_file.write_text("正文", encoding="utf-8")

    ev1 = Evidence(chunk_id=1, doc_id="d1", score=90, summary="提取到证据A", section_title="1.1")
    review = ReviewDoc(
        title="T", topic="Top",
        sections=[
            SectionDraft(heading="H1_新生成有证据", markdown="[1]", cited_doc_ids=["d1"]),
            SectionDraft(heading="H2_旧断点恢复", markdown="[2]", cited_doc_ids=["d2"]),
            SectionDraft(heading="H3_缺失映射", markdown="[3]", cited_doc_ids=["d3"]),
            SectionDraft(heading="H4_无引用空章", markdown="纯描述", cited_doc_ids=[]),
        ]
    )
    evidence_by_heading = {
        "H1_新生成有证据": [ev1],
        "H2_旧断点恢复": None,
        # "H3_缺失映射" 故意不放进 key
        "H4_无引用空章": [],
    }

    run_ctx = RunContext(
        topic="Top", run_id="r1", trigger_source="cli", model_name="m1",
        analysis_model="m2", reranker_model="m3", target_words=1000, target_lang="zh"
    )

    path = write_review_manifest(ManifestInput(
        output_md_path=str(md_file), review=review, outline=Outline(title="T", topic="Top"),
        evidence_by_heading=evidence_by_heading, paper_meta={}, run_context=run_ctx
    ))
    data = json.load(open(path, "r", encoding="utf-8"))
    tree = {item["heading"]: item for item in data["section_evidence"]}

    assert tree["H1_新生成有证据"]["evidence_status"] == "complete"
    assert len(tree["H1_新生成有证据"]["chunks"]) == 1
    assert tree["H2_旧断点恢复"]["evidence_status"] == "unavailable_legacy_checkpoint"
    assert tree["H2_旧断点恢复"]["chunks"] == []
    assert tree["H3_缺失映射"]["evidence_status"] == "unavailable_missing_mapping"
    assert tree["H3_缺失映射"]["chunks"] == []
    assert tree["H4_无引用空章"]["evidence_status"] == "empty"
    assert tree["H4_无引用空章"]["chunks"] == []


def test_privacy_strict_allowlist_and_no_leak(tmp_path):
    """验证 Manifest 严格遵守白名单，禁止密钥、未授权字段及摘要原文泄漏。"""
    md_file = tmp_path / "test_sec.md"
    md_file.write_text("content", encoding="utf-8")

    review = ReviewDoc(
        title="T",
        topic="Top",
        references=["[1] Auth. Doc1."],
        reference_doc_ids=["d1"],
        sections=[SectionDraft(heading="H", markdown="正文内容", cited_doc_ids=["d1"])],
    )
    ev = Evidence(chunk_id=1, doc_id="d1", score=80, summary="敏感摘要内容包含 sk-1234567890abcdef 与私有实验细节")
    paper_meta = {"d1": {"title": "Doc1", "authors": "Auth", "api_key": "sk-secret999", "custom_leak": "leaked_data"}}
    run_ctx = RunContext(
        topic="Top", run_id="r1", trigger_source="mcp", model_name="m1",
        analysis_model="m2", reranker_model="m3", target_words=1000, target_lang="zh"
    )

    manifest_path = write_review_manifest(ManifestInput(
        output_md_path=str(md_file),
        review=review,
        outline=Outline(title="T", topic="Top", sections=[OutlineSection(heading="H")]),
        evidence_by_heading={"H": [ev]},
        paper_meta=paper_meta,
        run_context=run_ctx,
    ))

    raw_manifest_text = open(manifest_path, "r", encoding="utf-8").read()

    # 1. 严格禁止出现敏感字符串
    assert "sk-secret999" not in raw_manifest_text
    assert "sk-1234567890abcdef" not in raw_manifest_text
    assert "敏感摘要内容包含" not in raw_manifest_text
    assert "custom_leak" not in raw_manifest_text
    assert "api_key" not in raw_manifest_text

    # 2. 检查 cited_references 白名单
    data = json.loads(raw_manifest_text)
    cited_meta = data["cited_references"][0]
    for k in cited_meta.keys():
        assert k == "doc_id" or k == "meta_hash" or k in ALLOWLIST_PAPER_META_KEYS


def test_git_failure_graceful_degradation(tmp_path, monkeypatch):
    """验证 git 无法读取时，git_commit='unknown' 且 git_dirty=None (JSON 序列化为 null)。"""
    md_file = tmp_path / "test_git.md"
    md_file.write_text("content", encoding="utf-8")

    def mock_subprocess_run(*args, **kwargs):
        raise OSError("git command not found")

    monkeypatch.setattr("subprocess.run", mock_subprocess_run)

    review = ReviewDoc(title="T", topic="Top")
    run_ctx = RunContext(
        topic="Top", run_id="r1", trigger_source="cli", model_name="m1",
        analysis_model="m2", reranker_model="m3", target_words=1000, target_lang="zh"
    )

    manifest_path = write_review_manifest(ManifestInput(
        output_md_path=str(md_file),
        review=review,
        outline=Outline(title="T", topic="Top"),
        evidence_by_heading={},
        paper_meta={},
        run_context=run_ctx,
    ))

    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["env_fingerprint"]["git_commit"] == "unknown"
    assert data["env_fingerprint"]["git_dirty"] is None


def test_atomic_write_and_cleanup_on_error(tmp_path, monkeypatch):
    """验证写入失败时清理本次临时文件，绝不覆盖已有旧清单文件。"""
    md_file = tmp_path / "test_doc.md"
    md_file.write_text("md content", encoding="utf-8")

    manifest_file = tmp_path / "test_doc.manifest.json"
    manifest_file.write_text('{"existing": "true"}', encoding="utf-8")

    # 模拟 os.replace 抛出异常
    def mock_replace(src, dst):
        raise IOError("Disk full simulation")

    monkeypatch.setattr("os.replace", mock_replace)

    review = ReviewDoc(title="T", topic="Top")
    run_ctx = RunContext(
        topic="Top", run_id="r1", trigger_source="cli", model_name="m1",
        analysis_model="m2", reranker_model="m3", target_words=1000, target_lang="zh"
    )

    with pytest.raises(ManifestError) as exc_info:
        write_review_manifest(ManifestInput(
            output_md_path=str(md_file),
            review=review,
            outline=Outline(title="T", topic="Top"),
            evidence_by_heading={},
            paper_meta={},
            run_context=run_ctx,
        ))

    assert "Manifest 原子落盘失败" in str(exc_info.value)
    # 验证旧清单未被覆盖
    with open(manifest_file, "r", encoding="utf-8") as f:
        old_data = json.load(f)
    assert old_data == {"existing": "true"}

    # 验证同目录下无残留的 .tmp 文件
    tmp_files = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert len(tmp_files) == 0


def test_manifest_sha256_exact_match(tmp_path):
    """验证 Manifest 内记录的 markdown_sha256 和 docx_sha256 与物理文件完全一致。"""
    import hashlib

    md_file = tmp_path / "review.md"
    md_content = "# Title\n\nSome review body text."
    md_file.write_text(md_content, encoding="utf-8")
    expected_md_sha256 = hashlib.sha256(md_content.encode("utf-8")).hexdigest()

    docx_file = tmp_path / "review.docx"
    docx_bytes = b"PK\x03\x04fake_docx_binary_data"
    docx_file.write_bytes(docx_bytes)
    expected_docx_sha256 = hashlib.sha256(docx_bytes).hexdigest()

    review = ReviewDoc(title="T", topic="Top")
    run_ctx = RunContext(
        topic="Top", run_id="r1", trigger_source="cli", model_name="m1",
        analysis_model="m2", reranker_model="m3", target_words=1000, target_lang="zh"
    )

    manifest_path = write_review_manifest(ManifestInput(
        output_md_path=str(md_file),
        review=review,
        outline=Outline(title="T", topic="Top"),
        evidence_by_heading={},
        paper_meta={},
        run_context=run_ctx,
        docx_path=str(docx_file),
    ))

    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["artifacts"]["markdown_sha256"] == expected_md_sha256
    assert data["artifacts"]["docx_sha256"] == expected_docx_sha256


def _prepare_mcp_server(monkeypatch, tmp_path, test_db=None, out_root=None, reg_path=None):
    """隔离 mcp_server 的环境与 settings 缓存，确保首次或重载导入时日志与数据库均落在 tmp_path。"""
    if test_db is None:
        test_db = str(tmp_path / "mcp_test.db")
    if out_root is None:
        out_root = str(tmp_path / "reviews")
    if reg_path is None:
        reg_path = str(tmp_path / "reviews" / "baselines.registry.json")

    os.makedirs(os.path.dirname(os.path.abspath(test_db)), exist_ok=True)
    os.makedirs(out_root if isinstance(out_root, str) else str(out_root), exist_ok=True)

    monkeypatch.setenv("DB_PATH", str(test_db))
    monkeypatch.setenv("REVIEW_OUTPUT_DIR", str(out_root))
    monkeypatch.setenv("REVIEW_BASELINE_REGISTRY_PATH", str(reg_path))
    monkeypatch.setattr("utils._settings", None, raising=False)

    import mcp_server
    if hasattr(mcp_server, "settings"):
        monkeypatch.setattr(mcp_server.settings, "DB_PATH", str(test_db), raising=False)
        monkeypatch.setattr(mcp_server.settings, "REVIEW_OUTPUT_DIR", str(out_root), raising=False)
        monkeypatch.setattr(mcp_server.settings, "REVIEW_BASELINE_REGISTRY_PATH", str(reg_path), raising=False)

    mcp_server._review_jobs_db_initialized.clear()
    mcp_server._review_jobs_db_recovered.clear()
    return mcp_server


@pytest.mark.anyio
async def test_mcp_server_review_jobs_persistence_and_recovery(tmp_path, monkeypatch):
    """测试 MCP review_jobs 数据库表的向后兼容迁移、持久化与跨重启回读（完全隔离在 tmp_path）。"""
    test_db = str(tmp_path / "mcp_isolated.db")

    # 1. 模拟旧版本数据库（无 manifest_path 和 manifest_error 列）
    conn = sqlite3.connect(test_db)
    conn.execute("""
        CREATE TABLE review_jobs (
            job_id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            focus TEXT,
            words INTEGER DEFAULT 0,
            sections INTEGER DEFAULT 0,
            outline_json TEXT,
            status TEXT NOT NULL,
            progress_log TEXT,
            output_path TEXT,
            docx_path TEXT,
            summary TEXT,
            error TEXT,
            queued_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            heartbeat_at TEXT,
            checkpoint_json TEXT,
            research_gap TEXT,
            thesis TEXT,
            mainline_logic TEXT,
            target_lang TEXT DEFAULT 'zh'
        )
    """)
    conn.execute("INSERT INTO review_jobs (job_id, topic, status) VALUES ('old_job_1', '旧综述', 'done')")
    conn.commit()
    conn.close()

    # 2. 导入并初始化 mcp_server（隔离 settings，触发安全增量 DDL 迁移）
    mcp_server = _prepare_mcp_server(monkeypatch, tmp_path, test_db=test_db)
    mcp_server._init_review_jobs_db()

    # 验证旧任务回读（manifest_path 为空字符串，不报错）
    old_job = mcp_server._get_persistent_review_job("old_job_1")
    assert old_job is not None
    assert old_job["job_id"] == "old_job_1"
    assert old_job["manifest"] == ""
    assert old_job["manifest_error"] == ""

    # 3. 真实调用 mcp_server._run_review_job 执行完整综述生成流程
    mock_outline = Outline(title="测试大纲", topic="新综述", sections=[OutlineSection(heading="第一节")])
    mock_draft = SectionDraft(heading="第一节", markdown="正文 [1]", cited_doc_ids=["doc_1"])
    mock_sec_res = MagicMock()
    mock_sec_res.drafts = [mock_draft]
    mock_sec_res.evidence_count = 1
    mock_sec_res.failed_sections = []
    mock_sec_res.evidence_by_heading = {"第一节": []}

    mock_review_doc = ReviewDoc(
        title="测试大纲", topic="新综述",
        sections=[mock_draft],
        references=["[1] 张三. 2021."],
        reference_doc_ids=["doc_1"],
        doc_count=1, evidence_count=1,
    )

    out_md = tmp_path / "综述_新综述.md"
    out_md.write_text("# 综述\n正文", encoding="utf-8")

    monkeypatch.setattr("litreview.stages.generate_outline", lambda *args, **kwargs: mock_outline)
    monkeypatch.setattr("litreview.review_orchestrator.generate_review_sections", lambda *args, **kwargs: mock_sec_res)
    monkeypatch.setattr("litreview.stages.write_intro_conclusion", lambda *args, **kwargs: ("引言", "结论"))
    monkeypatch.setattr("litreview.stages.assemble_review", lambda *args, **kwargs: mock_review_doc)
    monkeypatch.setattr("litreview.stages.render_review_note", lambda *args, **kwargs: str(out_md))
    monkeypatch.setattr("litreview.stages.export_docx", lambda *args, **kwargs: str(tmp_path / "综述_新综述.docx"))
    monkeypatch.setattr(mcp_server, "_client", lambda: MagicMock())
    monkeypatch.setattr(mcp_server, "_store", lambda: MagicMock(get_paper_meta=lambda: {"doc_1": {"title": "Doc1"}}, get_corpus_fingerprint=lambda: "a"*64))
    monkeypatch.setattr(mcp_server, "_embedder", lambda: MagicMock())

    mcp_server._run_review_job(
        job_id="mcp_real_run_1", topic="新综述", focus="", words=1000,
        n_sections=1, outline_json=mock_outline.model_dump_json()
    )

    # 4. 模拟进程重启（清空内存中的 _jobs），验证 _persist_review_job 在生产代码中已真正执行落盘
    mcp_server._jobs.clear()
    loaded_job = mcp_server._get_persistent_review_job("mcp_real_run_1")
    assert loaded_job is not None
    assert loaded_job["job_id"] == "mcp_real_run_1"
    assert loaded_job["status"] == "done"
    assert "综述_新综述.manifest.json" in loaded_job["manifest"]
    assert loaded_job["manifest_error"] == ""

    # 5. 验证 review_status 工具展示（脱敏绝对路径，只返回规范 URI）
    status_text = await mcp_server.review_status("mcp_real_run_1")
    assert "Manifest URI: vault://reviews/综述_新综述.manifest.json" in status_text or "综述_新综述.manifest.json" in status_text
    assert f"(文件: {out_md.parent / '综述_新综述.manifest.json'})" not in status_text


def test_web_server_real_worker_manifest_failure_flow(tmp_path, monkeypatch):
    """验证 Web 服务真实工作线程在 Manifest 写入失败时，current_stage 设为 PARTIAL_COMPLETED 且不输出庆祝日志。"""
    import web_server
    test_db = str(tmp_path / "web_isolated.db")
    monkeypatch.setenv("DB_PATH", test_db)

    mock_outline = Outline(title="测试大纲", topic="Web综述", sections=[OutlineSection(heading="第一节")])
    mock_draft = SectionDraft(heading="第一节", markdown="正文 [1]", cited_doc_ids=["doc_1"])
    mock_review_doc = ReviewDoc(
        title="测试大纲", topic="Web综述",
        sections=[mock_draft],
        references=["[1] 张三. 2021."],
        reference_doc_ids=["doc_1"],
        doc_count=1, evidence_count=1,
    )
    out_md = tmp_path / "综述_Web.md"
    out_md.write_text("# 综述", encoding="utf-8")

    monkeypatch.setattr("litreview.stages.generate_outline", lambda *args, **kwargs: mock_outline)
    monkeypatch.setattr("litreview.stages.gather_evidence", lambda *args, **kwargs: [Evidence(chunk_id=1, doc_id="doc_1", score=90, summary="证据")])
    monkeypatch.setattr("litreview.stages.write_section", lambda *args, **kwargs: mock_draft)
    monkeypatch.setattr("litreview.stages.write_intro_conclusion", lambda *args, **kwargs: ("引言", "结论"))
    monkeypatch.setattr("litreview.stages.assemble_review", lambda *args, **kwargs: mock_review_doc)
    monkeypatch.setattr("litreview.stages.render_review_note", lambda *args, **kwargs: str(out_md))
    monkeypatch.setattr("litreview.stages.export_docx", lambda *args, **kwargs: str(tmp_path / "综述_Web.docx"))
    monkeypatch.setattr(web_server, "_get_client", lambda: MagicMock())
    monkeypatch.setattr(web_server, "_get_store", lambda: MagicMock(get_paper_meta=lambda: {"doc_1": {"title": "Doc1"}}, get_corpus_fingerprint=lambda: "a"*64))
    monkeypatch.setattr(web_server, "_get_embedder", lambda: MagicMock())
    monkeypatch.setattr(web_server, "_get_reranker", lambda: None)

    # 模拟 write_review_manifest 抛出异常
    from litreview import manifest
    def mock_write_fail(*args, **kwargs):
        raise manifest.ManifestError("Disk write permission denied")
    monkeypatch.setattr(manifest, "write_review_manifest", mock_write_fail)

    web_server._jobs["job_web_fail_1"] = {
        "job_id": "job_web_fail_1", "status": "running", "current_stage": "PENDING", "log": []
    }

    web_server._review_generator_worker(
        job_id="job_web_fail_1", topic="Web综述", focus="", words=1000,
        n_sections=1, outline_json=mock_outline.model_dump_json(), model_override=None
    )

    job = web_server._jobs["job_web_fail_1"]
    assert job["status"] == "partial"
    assert job["current_stage"] == "PARTIAL_COMPLETED"
    assert job["manifest_error"] == "Disk write permission denied"
    assert not any("🎉" in l for l in job["log"])
    assert any("⚠️ 综述正文与 Word 已生成，但运行清单生成失败" in l for l in job["log"])


def test_rerank_model_settings_consistency():
    """验证配置对象与三入口统一使用 settings.RERANK_MODEL 真实字段，无硬编码错名。"""
    from utils import get_settings
    settings = get_settings()
    assert hasattr(settings, "RERANK_MODEL")
    assert isinstance(settings.RERANK_MODEL, str)
    assert len(settings.RERANK_MODEL) > 0
    # 确保 settings 中不存在拼写错误的 RERANKER_MODEL 字段
    assert not hasattr(settings, "RERANKER_MODEL")


@pytest.mark.anyio
async def test_mcp_server_resume_chain_clears_stale_manifest_error(tmp_path, monkeypatch):
    """测试 MCP partial 任务带历史 manifest_error 时，resume 重跑成功后能彻底清除旧错误，并在跨重启回读后为 done 与新清单。"""
    test_db = str(tmp_path / "mcp_resume_test.db")
    mcp_server = _prepare_mcp_server(monkeypatch, tmp_path, test_db=test_db)
    mcp_server._init_review_jobs_db()

    mock_outline = Outline(title="续跑大纲", topic="续跑综述", sections=[OutlineSection(heading="第一节")])
    mock_draft = SectionDraft(heading="第一节", markdown="正文 [1]", cited_doc_ids=["doc_1"])
    mock_review_doc = ReviewDoc(
        title="续跑大纲", topic="续跑综述",
        sections=[mock_draft],
        references=["[1] 张三. 2021."],
        reference_doc_ids=["doc_1"],
        doc_count=1, evidence_count=1,
    )
    out_md = tmp_path / "综述_续跑.md"
    out_md.write_text("# 续跑正文", encoding="utf-8")

    # 1. 模拟历史 partial 任务，且残留旧的 manifest_error
    stale_job = {
        "job_id": "job_resume_chain_1",
        "topic": "续跑综述",
        "focus": "",
        "words": 1000,
        "sections": 1,
        "outline_json": mock_outline.model_dump_json(),
        "status": "partial",
        "output": str(tmp_path / "old.md"),
        "docx": str(tmp_path / "old.docx"),
        "manifest": str(tmp_path / "old.manifest.json"),
        "manifest_error": "Legacy disk full error from past run",
        "summary": "旧任务概要",
        "checkpoint_json": json.dumps([mock_draft.model_dump()]),
    }
    mcp_server._persist_review_job(stale_job)

    # 2. 调用 resume_review 重新入队
    res_msg = await mcp_server.resume_review("job_resume_chain_1")
    assert "已从检查点重新入队" in res_msg
    queued_job = mcp_server._get_persistent_review_job("job_resume_chain_1")
    assert queued_job["status"] in ("queued", "running")
    assert queued_job["manifest"] == ""
    assert queued_job["manifest_error"] == ""

    # 3. 设置 mock 执行新一轮 _run_review_job 且完全成功
    mock_sec_res = MagicMock()
    mock_sec_res.drafts = [mock_draft]
    mock_sec_res.evidence_count = 1
    mock_sec_res.failed_sections = []
    mock_sec_res.evidence_by_heading = {"第一节": []}

    monkeypatch.setattr("litreview.stages.generate_outline", lambda *args, **kwargs: mock_outline)
    monkeypatch.setattr("litreview.review_orchestrator.generate_review_sections", lambda *args, **kwargs: mock_sec_res)
    monkeypatch.setattr("litreview.stages.write_intro_conclusion", lambda *args, **kwargs: ("引言", "结论"))
    monkeypatch.setattr("litreview.stages.assemble_review", lambda *args, **kwargs: mock_review_doc)
    monkeypatch.setattr("litreview.stages.render_review_note", lambda *args, **kwargs: str(out_md))
    monkeypatch.setattr("litreview.stages.export_docx", lambda *args, **kwargs: str(tmp_path / "综述_续跑.docx"))
    monkeypatch.setattr(mcp_server, "_client", lambda: MagicMock())
    monkeypatch.setattr(mcp_server, "_store", lambda: MagicMock(get_paper_meta=lambda: {"doc_1": {"title": "Doc1"}}, get_corpus_fingerprint=lambda: "b"*64))
    monkeypatch.setattr(mcp_server, "_embedder", lambda: MagicMock())
    monkeypatch.setattr(mcp_server, "_reranker", lambda: None)

    mcp_server._run_review_job(
        job_id="job_resume_chain_1", topic="续跑综述", focus="", words=1000,
        n_sections=1, outline_json=mock_outline.model_dump_json()
    )

    # 4. 模拟进程重启（清空内存缓存），从 SQLite 回读校验
    mcp_server._jobs.clear()
    loaded_job = mcp_server._get_persistent_review_job("job_resume_chain_1")
    assert loaded_job is not None
    assert loaded_job["job_id"] == "job_resume_chain_1"
    assert loaded_job["status"] == "done"
    assert loaded_job["manifest_error"] == ""
    assert "综述_续跑.manifest.json" in loaded_job["manifest"]

    # 5. 校验 review_status 结果，确认无历史警告残留
    status_text = await mcp_server.review_status("job_resume_chain_1")
    assert "综述_续跑.manifest.json" in status_text
    assert "Manifest 警告" not in status_text
    assert "Legacy disk full error" not in status_text


@pytest.mark.anyio
async def test_mcp_server_review_status_sanitizes_absolute_path_in_partial_error(tmp_path, monkeypatch):
    """测试 MCP 任务在 manifest 生成失败时，review_status 输出全文脱敏，绝不泄露 /home/... 物理绝对路径。"""
    test_db = str(tmp_path / "mcp_sanitize_test.db")
    mcp_server = _prepare_mcp_server(monkeypatch, tmp_path, test_db=test_db)
    mcp_server._init_review_jobs_db()

    mock_outline = Outline(title="脱敏大纲", topic="脱敏测试", sections=[OutlineSection(heading="第一节")])
    mock_draft = SectionDraft(heading="第一节", markdown="正文 [1]", cited_doc_ids=["doc_1"])
    mock_sec_res = MagicMock()
    mock_sec_res.drafts = [mock_draft]
    mock_sec_res.evidence_count = 1
    mock_sec_res.failed_sections = []
    mock_sec_res.evidence_by_heading = {"第一节": []}

    mock_review_doc = ReviewDoc(
        title="脱敏大纲", topic="脱敏测试",
        sections=[mock_draft],
        references=["[1] 张三. 2021."],
        reference_doc_ids=["doc_1"],
        doc_count=1, evidence_count=1,
    )
    out_md = tmp_path / "综述_脱敏.md"
    out_md.write_text("# 脱敏正文", encoding="utf-8")

    monkeypatch.setattr("litreview.stages.generate_outline", lambda *args, **kwargs: mock_outline)
    monkeypatch.setattr("litreview.review_orchestrator.generate_review_sections", lambda *args, **kwargs: mock_sec_res)
    monkeypatch.setattr("litreview.stages.write_intro_conclusion", lambda *args, **kwargs: ("引言", "结论"))
    monkeypatch.setattr("litreview.stages.assemble_review", lambda *args, **kwargs: mock_review_doc)
    monkeypatch.setattr("litreview.stages.render_review_note", lambda *args, **kwargs: str(out_md))
    monkeypatch.setattr("litreview.stages.export_docx", lambda *args, **kwargs: str(tmp_path / "综述_脱敏.docx"))
    monkeypatch.setattr(mcp_server, "_client", lambda: MagicMock())
    monkeypatch.setattr(mcp_server, "_store", lambda: MagicMock(get_paper_meta=lambda: {"doc_1": {"title": "Doc1"}}, get_corpus_fingerprint=lambda: "c"*64))
    monkeypatch.setattr(mcp_server, "_embedder", lambda: MagicMock())
    monkeypatch.setattr(mcp_server, "_reranker", lambda: None)

    # 模拟 write_review_manifest 抛出包含服务器敏感绝对路径的错误
    leak_path = "/home/dudu/private_data/secret_vault/reviews/fail_target.manifest.json"
    from litreview import manifest
    def mock_write_leak_fail(*args, **kwargs):
        raise manifest.ManifestError(f"Permission denied accessing server path: {leak_path}")
    monkeypatch.setattr(manifest, "write_review_manifest", mock_write_leak_fail)

    mcp_server._run_review_job(
        job_id="job_leak_test_1", topic="脱敏测试", focus="", words=1000,
        n_sections=1, outline_json=mock_outline.model_dump_json()
    )

    # 模拟重启后回读
    mcp_server._jobs.clear()
    loaded_job = mcp_server._get_persistent_review_job("job_leak_test_1")
    assert loaded_job["status"] == "partial"
    assert "Permission denied" in loaded_job["manifest_error"]
    # summary 只写固定文案，绝不拼入原始 manifest_err
    assert leak_path not in loaded_job["summary"]

    # 验证 review_status 输出全文严格不包含敏感物理绝对路径
    status_text = await mcp_server.review_status("job_leak_test_1")
    assert leak_path not in status_text
    assert "/home/dudu/private_data" not in status_text
    assert "Manifest 警告" in status_text


@pytest.mark.anyio
async def test_web_review_worker_five_baseline_outcomes(tmp_path, monkeypatch):
    """测试 Web worker 针对基线检查 5 种结果（PASS、WARN、FAIL、NO_BASELINE、check_error）的状态隔离与诚实汇报。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    import web_server
    web_settings = web_server.get_app_settings()
    monkeypatch.setattr(web_settings, "REVIEW_OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(web_settings, "REVIEW_BASELINE_REGISTRY_PATH", reg_path)

    mock_outline = Outline(title="Web基线大纲", topic="Web基线综述", sections=[OutlineSection(heading="第一节")])
    mock_draft = SectionDraft(heading="第一节", markdown="正文 [1]", cited_doc_ids=["doc_1"])
    mock_review_doc = ReviewDoc(
        title="Web基线大纲", topic="Web基线综述",
        sections=[mock_draft],
        references=["[1] 张三. 2021."],
        reference_doc_ids=["doc_1"],
        doc_count=1, evidence_count=1,
    )

    def mock_render(review, meta, settings, template, model):
        p = out_root / f"综述_Web_{uuid.uuid4().hex[:6]}.md"
        p.write_text(f"# {review.title}\n\n正文 [1]\n", encoding="utf-8")
        return str(p)

    monkeypatch.setattr("litreview.stages.generate_outline", lambda *args, **kwargs: mock_outline)
    monkeypatch.setattr("litreview.stages.gather_evidence", lambda *args, **kwargs: [Evidence(chunk_id=1, doc_id="doc_1", score=90, summary="证据")])
    monkeypatch.setattr("litreview.stages.write_section", lambda *args, **kwargs: mock_draft)
    monkeypatch.setattr("litreview.stages.write_intro_conclusion", lambda *args, **kwargs: ("引言", "结论"))
    monkeypatch.setattr("litreview.stages.assemble_review", lambda *args, **kwargs: mock_review_doc)
    monkeypatch.setattr("litreview.stages.render_review_note", mock_render)
    monkeypatch.setattr("litreview.stages.export_docx", lambda p: str(p).replace(".md", ".docx"))
    monkeypatch.setattr(web_server, "_get_client", lambda: MagicMock())
    monkeypatch.setattr(web_server, "_get_store", lambda: MagicMock(get_paper_meta=lambda: {"doc_1": {"title": "Doc1"}}, get_corpus_fingerprint=lambda: "a"*64))
    monkeypatch.setattr(web_server, "_get_embedder", lambda: MagicMock())
    monkeypatch.setattr(web_server, "_get_reranker", lambda: None)

    # 1. 首次生成 -> NO_BASELINE
    web_server._jobs["job_w1"] = {"job_id": "job_w1", "status": "running", "current_stage": "PENDING", "log": []}
    web_server._review_generator_worker("job_w1", "Web基线综述", "", 1000, 1, mock_outline.model_dump_json(), None)
    j1 = web_server._jobs["job_w1"]
    assert j1["status"] == "done"
    assert j1["baseline_status"] == "no_baseline"
    assert j1["baseline_verdict"] == "WARN"
    assert any("🎉" in l for l in j1["log"])
    assert any("无已注册基线" in l for l in j1["log"])

    # 提升该运行为官方基线
    from litreview.run_baseline import promote_review_baseline
    rep_pm = promote_review_baseline(j1["output_manifest"], reg_path, str(out_root), allow_warn=True)
    assert rep_pm.verdict in ("PASS", "WARN")

    # 2. 第二次生成相同内容 -> compared (PASS)
    web_server._jobs["job_w2"] = {"job_id": "job_w2", "status": "running", "current_stage": "PENDING", "log": []}
    web_server._review_generator_worker("job_w2", "Web基线综述", "", 1000, 1, mock_outline.model_dump_json(), None)
    j2 = web_server._jobs["job_w2"]
    assert j2["status"] == "done"
    assert j2["baseline_status"] == "compared"
    assert j2["baseline_verdict"] in ("PASS", "WARN")
    assert any("🎉" in l for l in j2["log"])

    # 3. 产生演进差异 -> compared (WARN)
    mock_warn_draft = SectionDraft(heading="第一节", markdown="正文 [2]", cited_doc_ids=["doc_2"])
    mock_review_warn = ReviewDoc(
        title="Web基线大纲", topic="Web基线综述",
        sections=[mock_warn_draft],
        references=["[2] 李四. 2022."],
        reference_doc_ids=["doc_2"],
        doc_count=1, evidence_count=1,
    )
    monkeypatch.setattr("litreview.stages.assemble_review", lambda *args, **kwargs: mock_review_warn)
    web_server._jobs["job_w3"] = {"job_id": "job_w3", "status": "running", "current_stage": "PENDING", "log": []}
    web_server._review_generator_worker("job_w3", "Web基线综述", "", 1000, 1, mock_outline.model_dump_json(), None)
    j3 = web_server._jobs["job_w3"]
    assert j3["status"] == "done"
    assert j3["baseline_status"] == "compared"
    assert j3["baseline_verdict"] == "WARN"

    # 4. 模拟质量退化 (FAIL) -> status 仍为 done，无盲目庆祝 emoji
    from litreview.run_baseline import BaselineCheckReport
    from litreview.run_validation import ValidationIssue
    def mock_fail_check(*args, **kwargs):
        return BaselineCheckReport(
            verdict="FAIL", status="compared", topic_key="k", topic="Web基线综述",
            candidate_manifest_locator="cand.manifest.json", baseline_manifest_locator="base.manifest.json",
            candidate_validation=None,
            comparison_report=None,
            issues=[ValidationIssue(code="EVIDENCE_PROVENANCE_REGRESSED", severity="FAIL", message="退化", location="sec")]
        )
    monkeypatch.setattr("litreview.run_baseline.check_review_candidate", mock_fail_check)
    web_server._jobs["job_w4"] = {"job_id": "job_w4", "status": "running", "current_stage": "PENDING", "log": []}
    web_server._review_generator_worker("job_w4", "Web基线综述", "", 1000, 1, mock_outline.model_dump_json(), None)
    j4 = web_server._jobs["job_w4"]
    assert j4["status"] == "done"
    assert j4["baseline_status"] == "compared"
    assert j4["baseline_verdict"] == "FAIL"
    assert not any("🎉" in l for l in j4["log"])
    assert any("⚠️ 综述生成完成，但基线检查发现质量退化或基线损坏" in l for l in j4["log"])

    # 5. 模拟基线检查代码抛出异常 -> check_error
    def mock_check_raise(*args, **kwargs):
        raise RuntimeError("Fake check error at /home/dudu/private/secret.py")
    monkeypatch.setattr("litreview.run_baseline.check_review_candidate", mock_check_raise)
    web_server._jobs["job_w5"] = {"job_id": "job_w5", "status": "running", "current_stage": "PENDING", "log": []}
    web_server._review_generator_worker("job_w5", "Web基线综述", "", 1000, 1, mock_outline.model_dump_json(), None)
    j5 = web_server._jobs["job_w5"]
    assert j5["status"] == "done"
    assert j5["baseline_status"] == "check_error"
    assert j5["baseline_verdict"] is None
    assert "/home/dudu" not in str(j5["baseline_error"])
    assert not any("🎉" in l for l in j5["log"])
    assert any("⚠️ 综述生成完成，但自动基线检查异常" in l for l in j5["log"])

    # 6. Manifest 失败门禁：write_review_manifest 失败时 check_review_candidate 绝对不调用
    checker_mock = MagicMock()
    monkeypatch.setattr("litreview.run_baseline.check_review_candidate", checker_mock)
    from litreview import manifest
    def mock_write_fail(*args, **kwargs):
        raise manifest.ManifestError("Permission denied")
    monkeypatch.setattr(manifest, "write_review_manifest", mock_write_fail)
    web_server._jobs["job_w6"] = {"job_id": "job_w6", "status": "running", "current_stage": "PENDING", "log": []}
    web_server._review_generator_worker("job_w6", "Web基线综述", "", 1000, 1, mock_outline.model_dump_json(), None)
    j6 = web_server._jobs["job_w6"]
    assert j6["status"] == "partial"
    assert j6["manifest_error"] == "Permission denied"
    checker_mock.assert_not_called()

    # 7. 真实 API 详情断言四个 baseline 字段
    from starlette.requests import Request
    scope = {"type": "http", "method": "GET", "path": "/api/reviews/detail/job_w4", "path_params": {"job_id": "job_w4"}}
    req = Request(scope)
    res = await web_server.api_review_job_detail(req)
    import json
    data = json.loads(res.body.decode("utf-8"))
    assert data["data"]["baseline_verdict"] == "FAIL"
    assert data["data"]["baseline_status"] == "compared"
    assert data["data"]["baseline_summary"] == "检测到基线质量退化"


@pytest.mark.anyio
async def test_mcp_review_job_five_baseline_outcomes_and_db_migration(tmp_path, monkeypatch):
    """测试 MCP 综述任务 5 种基线检查结果、老数据库向后兼容迁移、跨重启持久化与 resume 清零。"""
    test_db = str(tmp_path / "mcp_migration_test.db")
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    monkeypatch.setenv("DB_PATH", test_db)
    monkeypatch.setenv("REVIEW_OUTPUT_DIR", str(out_root))
    monkeypatch.setenv("REVIEW_BASELINE_REGISTRY_PATH", reg_path)

    import sqlite3
    # 1. 模拟老版本 SQLite 数据库结构（无 baseline 相关字段）
    conn = sqlite3.connect(test_db)
    conn.execute("""
        CREATE TABLE review_jobs (
            job_id TEXT PRIMARY KEY,
            topic TEXT,
            focus TEXT,
            words INTEGER,
            sections INTEGER,
            outline_json TEXT,
            status TEXT,
            progress_log TEXT,
            output_path TEXT,
            docx_path TEXT,
            summary TEXT,
            error TEXT,
            queued_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            heartbeat_at TEXT,
            checkpoint_json TEXT,
            research_gap TEXT,
            thesis TEXT,
            mainline_logic TEXT,
            target_lang TEXT DEFAULT 'zh',
            manifest_path TEXT,
            manifest_error TEXT
        )
    """)
    conn.commit()
    conn.close()
    # 2. 隔离环境并导入初始化 mcp_server（触发安全增量 DDL 迁移）
    mcp_server = _prepare_mcp_server(monkeypatch, tmp_path, test_db=test_db, out_root=out_root, reg_path=reg_path)
    mcp_server._init_review_jobs_db()

    mock_outline = Outline(title="MCP基线大纲", topic="MCP基线测试", sections=[OutlineSection(heading="第一节")])
    mock_draft = SectionDraft(heading="第一节", markdown="正文 [1]", cited_doc_ids=["doc_1"])
    mock_sec_res = MagicMock()
    mock_sec_res.drafts = [mock_draft]
    mock_sec_res.evidence_count = 1
    mock_sec_res.failed_sections = []
    mock_sec_res.evidence_by_heading = {"第一节": []}

    mock_review_doc = ReviewDoc(
        title="MCP基线大纲", topic="MCP基线测试",
        sections=[mock_draft],
        references=["[1] 张三. 2021."],
        reference_doc_ids=["doc_1"],
        doc_count=1, evidence_count=1,
    )

    def mock_mcp_render(review, meta, settings, template, model):
        p = out_root / f"综述_MCP_{uuid.uuid4().hex[:6]}.md"
        p.write_text(f"# {review.title}\n\n正文 [1]\n", encoding="utf-8")
        return str(p)

    monkeypatch.setattr("litreview.stages.generate_outline", lambda *args, **kwargs: mock_outline)
    monkeypatch.setattr("litreview.review_orchestrator.generate_review_sections", lambda *args, **kwargs: mock_sec_res)
    monkeypatch.setattr("litreview.stages.write_intro_conclusion", lambda *args, **kwargs: ("引言", "结论"))
    monkeypatch.setattr("litreview.stages.assemble_review", lambda *args, **kwargs: mock_review_doc)
    monkeypatch.setattr("litreview.stages.render_review_note", mock_mcp_render)
    monkeypatch.setattr("litreview.stages.export_docx", lambda p: str(p).replace(".md", ".docx"))
    monkeypatch.setattr(mcp_server, "_client", lambda: MagicMock())
    monkeypatch.setattr(mcp_server, "_store", lambda: MagicMock(get_paper_meta=lambda: {"doc_1": {"title": "Doc1"}}, get_corpus_fingerprint=lambda: "d"*64))
    monkeypatch.setattr(mcp_server, "_embedder", lambda: MagicMock())
    monkeypatch.setattr(mcp_server, "_reranker", lambda: None)

    # 1. 首次生成 -> NO_BASELINE
    mcp_server._jobs["job_m1"] = {
        "job_id": "job_m1", "topic": "MCP基线测试", "status": "running", "log": []
    }
    mcp_server._run_review_job("job_m1", "MCP基线测试", "", 1000, 1, mock_outline.model_dump_json())

    mcp_server._jobs.clear()
    j1 = mcp_server._get_persistent_review_job("job_m1")
    assert j1["status"] == "done"
    assert j1["baseline_status"] == "no_baseline"
    assert j1["baseline_verdict"] == "WARN"

    status_str = await mcp_server.review_status("job_m1")
    assert "基线检查: WARN" in status_str

    # 提升为基线
    from litreview.run_baseline import promote_review_baseline
    rep_pm = promote_review_baseline(j1["manifest"], reg_path, str(out_root), allow_warn=True)
    assert rep_pm.verdict in ("PASS", "WARN")

    # 2. 再次生成相同内容 -> PASS
    mcp_server._jobs["job_m2"] = {
        "job_id": "job_m2", "topic": "MCP基线测试", "status": "running", "log": []
    }
    mcp_server._run_review_job("job_m2", "MCP基线测试", "", 1000, 1, mock_outline.model_dump_json())
    mcp_server._jobs.clear()
    j2 = mcp_server._get_persistent_review_job("job_m2")
    assert j2["status"] == "done"
    assert j2["baseline_status"] == "compared"
    assert j2["baseline_verdict"] in ("PASS", "WARN")

    status_str2 = await mcp_server.review_status("job_m2")
    assert ("基线检查: PASS" in status_str2 or "基线检查: WARN" in status_str2)

    # 3. 产生演进差异 -> WARN
    mock_warn_draft = SectionDraft(heading="第一节", markdown="正文 [2]", cited_doc_ids=["doc_2"])
    mock_review_warn = ReviewDoc(
        title="MCP基线大纲", topic="MCP基线测试",
        sections=[mock_warn_draft],
        references=["[2] 李四. 2022."],
        reference_doc_ids=["doc_2"],
        doc_count=1, evidence_count=1,
    )
    mock_sec_res_warn = MagicMock()
    mock_sec_res_warn.drafts = [mock_warn_draft]
    mock_sec_res_warn.evidence_count = 1
    mock_sec_res_warn.failed_sections = []
    mock_sec_res_warn.evidence_by_heading = {"第一节": []}
    monkeypatch.setattr("litreview.review_orchestrator.generate_review_sections", lambda *args, **kwargs: mock_sec_res_warn)
    monkeypatch.setattr("litreview.stages.assemble_review", lambda *args, **kwargs: mock_review_warn)

    mcp_server._jobs["job_m3"] = {
        "job_id": "job_m3", "topic": "MCP基线测试", "status": "running", "log": []
    }
    mcp_server._run_review_job("job_m3", "MCP基线测试", "", 1000, 1, mock_outline.model_dump_json())
    mcp_server._jobs.clear()
    j3 = mcp_server._get_persistent_review_job("job_m3")
    assert j3["status"] == "done"
    assert j3["baseline_status"] == "compared"
    assert j3["baseline_verdict"] == "WARN"

    status_str3 = await mcp_server.review_status("job_m3")
    assert "基线检查: WARN" in status_str3

    # 4. 模拟质量退化 (FAIL) -> status 仍为 done，review_status 输出 FAIL
    from litreview.run_baseline import BaselineCheckReport
    from litreview.run_validation import ValidationIssue
    def mock_fail_check_mcp(*args, **kwargs):
        return BaselineCheckReport(
            verdict="FAIL", status="compared", topic_key="k", topic="MCP基线测试",
            candidate_manifest_locator="cand.manifest.json", baseline_manifest_locator="base.manifest.json",
            candidate_validation=None,
            comparison_report=None,
            issues=[ValidationIssue(code="EVIDENCE_PROVENANCE_REGRESSED", severity="FAIL", message="退化", location="sec")]
        )
    monkeypatch.setattr("litreview.run_baseline.check_review_candidate", mock_fail_check_mcp)
    mcp_server._jobs["job_m4"] = {
        "job_id": "job_m4", "topic": "MCP基线测试", "status": "running", "log": []
    }
    mcp_server._run_review_job("job_m4", "MCP基线测试", "", 1000, 1, mock_outline.model_dump_json())
    mcp_server._jobs.clear()
    j4 = mcp_server._get_persistent_review_job("job_m4")
    assert j4["status"] == "done"
    assert j4["baseline_status"] == "compared"
    assert j4["baseline_verdict"] == "FAIL"

    status_str4 = await mcp_server.review_status("job_m4")
    assert "基线检查: FAIL" in status_str4

    # 5. 模拟基线检查代码抛出异常 (check_error) -> review_status 输出不可用且脱敏物理路径
    def mock_raise_check_mcp(*args, **kwargs):
        raise RuntimeError("Checker crashed at /home/dudu/private/secret.py")
    monkeypatch.setattr("litreview.run_baseline.check_review_candidate", mock_raise_check_mcp)
    mcp_server._jobs["job_m5"] = {
        "job_id": "job_m5", "topic": "MCP基线测试", "status": "running", "log": []
    }
    mcp_server._run_review_job("job_m5", "MCP基线测试", "", 1000, 1, mock_outline.model_dump_json())
    mcp_server._jobs.clear()
    j5 = mcp_server._get_persistent_review_job("job_m5")
    assert j5["status"] == "done"
    assert j5["baseline_status"] == "check_error"
    assert j5["baseline_verdict"] is None
    assert "/home/dudu" not in str(j5["baseline_error"])

    status_str5 = await mcp_server.review_status("job_m5")
    assert "基线检查: 不可用" in status_str5
    assert "/home/dudu" not in status_str5

    # 6. resume 重跑清空旧 baseline 状态
    mcp_server._persist_review_job({
        "job_id": "job_m_fail",
        "topic": "MCP基线测试",
        "status": "failed",
        "outline_json": mock_outline.model_dump_json(),
        "baseline_verdict": "FAIL",
        "baseline_status": "compared",
        "baseline_summary": "历史质量退化",
        "baseline_error": "旧错误",
    })
    res_msg = await mcp_server.resume_review("job_m_fail")
    assert "重新入队" in res_msg
    queued_job = mcp_server._get_persistent_review_job("job_m_fail")
    assert queued_job["baseline_verdict"] is None
    assert queued_job["baseline_status"] is None
    assert queued_job["baseline_summary"] is None
    assert queued_job["baseline_error"] is None

    # 7. Manifest 失败门禁：write_review_manifest 失败时 check_review_candidate 绝对不调用
    checker_mock = MagicMock()
    monkeypatch.setattr("litreview.run_baseline.check_review_candidate", checker_mock)
    from litreview import manifest
    def mock_write_mcp_fail(*args, **kwargs):
        raise manifest.ManifestError("Permission denied")
    monkeypatch.setattr(manifest, "write_review_manifest", mock_write_mcp_fail)
    mcp_server._jobs["job_m6"] = {
        "job_id": "job_m6", "topic": "MCP基线测试", "status": "running", "log": []
    }
    mcp_server._run_review_job("job_m6", "MCP基线测试", "", 1000, 1, mock_outline.model_dump_json())
    j6 = mcp_server._get_persistent_review_job("job_m6")
    assert j6["status"] == "partial"
    assert j6["manifest_error"] == "Permission denied"
    checker_mock.assert_not_called()


def test_manifest_chinese_number_prefix_evidence_mapping_end_to_end(tmp_path):
    """验证包含 一、二、 等中文/阿拉伯章节前缀时，质量清洗后的标题在 Manifest 中仍能精确映射证据。"""
    md_file = tmp_path / "test_chinese_prefix.md"
    md_file.write_text("正文", encoding="utf-8")

    ev1 = Evidence(chunk_id=101, doc_id="doc_a", score=9, summary="改性机理总结A", section_title="1.1")
    ev2 = Evidence(chunk_id=102, doc_id="doc_b", score=8, summary="改性机理总结B", section_title="1.2")
    ev3 = Evidence(chunk_id=103, doc_id="doc_c", score=9, summary="性能指标横向对比", section_title="2.1")

    # 1. 模拟生成阶段：evidence_by_heading 键带有大纲中的完整中文编号前缀
    evidence_by_heading = {
        "一、国内外研究现状与产业进展": [ev1, ev2],
        "二、不同包膜材料性能横向对比": [ev3],
        "三、断点恢复章节（历史遗留）": None,
        # "四、缺失映射章节" 故意不放进 evidence_by_heading
        "五、无引用描述章节": [],
    }

    # 2. 模拟 ReviewQuality 清洗后 ReviewDoc 中的章节标题（编号前缀已去除）
    review = ReviewDoc(
        title="包膜肥料综述",
        topic="包膜肥料",
        intro="引言完整内容",
        conclusion="结论完整内容",
        sections=[
            SectionDraft(heading="国内外研究现状与产业进展", markdown="正文段落 [1][2]", cited_doc_ids=["doc_a", "doc_b"]),
            SectionDraft(heading="不同包膜材料性能横向对比", markdown="正文对比 [3]", cited_doc_ids=["doc_c"]),
            SectionDraft(heading="断点恢复章节（历史遗留）", markdown="正文恢复 [4]", cited_doc_ids=["doc_d"]),
            SectionDraft(heading="缺失映射章节", markdown="正文未映射 [5]", cited_doc_ids=["doc_e"]),
            SectionDraft(heading="无引用描述章节", markdown="纯文字阐述", cited_doc_ids=[]),
        ],
        references=["[1] A", "[2] B", "[3] C", "[4] D", "[5] E"],
        reference_doc_ids=["doc_a", "doc_b", "doc_c", "doc_d", "doc_e"],
    )

    run_ctx = RunContext(
        topic="包膜肥料", run_id="run_chinese_001", trigger_source="cli",
        model_name="gpt-5.4", analysis_model="gpt-5.4", reranker_model="bge-reranker",
        target_words=5000, target_lang="zh"
    )

    manifest_path = write_review_manifest(ManifestInput(
        output_md_path=str(md_file),
        review=review,
        outline=Outline(title="包膜肥料综述", topic="包膜肥料"),
        evidence_by_heading=evidence_by_heading,
        paper_meta={
            "doc_a": {"title": "Doc A"},
            "doc_b": {"title": "Doc B"},
            "doc_c": {"title": "Doc C"},
            "doc_d": {"title": "Doc D"},
            "doc_e": {"title": "Doc E"},
        },
        run_context=run_ctx,
    ))

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest_data = json.load(f)

    tree = manifest_data["section_evidence"]
    assert len(tree) == 5

    # 第一章：清洗后标题成功映射到带编号的原始证据，状态为 complete，包含 2 个 chunk
    assert tree[0]["heading"] == "国内外研究现状与产业进展"
    assert tree[0]["evidence_status"] == "complete"
    assert len(tree[0]["chunks"]) == 2
    assert tree[0]["chunks"][0]["doc_id"] == "doc_a"
    assert tree[0]["chunks"][1]["doc_id"] == "doc_b"

    # 第二章：清洗后标题成功映射，状态为 complete，包含 1 个 chunk
    assert tree[1]["heading"] == "不同包膜材料性能横向对比"
    assert tree[1]["evidence_status"] == "complete"
    assert len(tree[1]["chunks"]) == 1
    assert tree[1]["chunks"][0]["doc_id"] == "doc_c"

    # 第三章：None 映射保留为 unavailable_legacy_checkpoint
    assert tree[2]["heading"] == "断点恢复章节（历史遗留）"
    assert tree[2]["evidence_status"] == "unavailable_legacy_checkpoint"
    assert tree[2]["chunks"] == []

    # 第四章：真缺失映射保留为 unavailable_missing_mapping
    assert tree[3]["heading"] == "缺失映射章节"
    assert tree[3]["evidence_status"] == "unavailable_missing_mapping"
    assert tree[3]["chunks"] == []

    # 第五章：空列表且无引用保留为 empty
    assert tree[4]["heading"] == "无引用描述章节"
    assert tree[4]["evidence_status"] == "empty"
    assert tree[4]["chunks"] == []


def test_manifest_normalized_heading_collision_fails_closed(tmp_path):
    """对抗测试：当多个原始键规范化后发生碰撞（如 '一、同名章节' 与 '二、同名章节'），
    清洗后的同名章节必须 fail closed，标为 unavailable_missing_mapping 且 chunks 为空；
    而原始精确键依然能精确命中。
    """
    md_file = tmp_path / "test_collision.md"
    md_file.write_text("正文内容", encoding="utf-8")

    ev_a = Evidence(chunk_id=201, doc_id="doc_a", score=9, summary="章节A证据")
    ev_b = Evidence(chunk_id=202, doc_id="doc_b", score=8, summary="章节B证据")

    # 1. 构造规范化后碰撞的 evidence_by_heading
    evidence_by_heading = {
        "一、同名章节": [ev_a],
        "二、同名章节": [ev_b],
    }

    # 2. 构造两个章节：
    #    章节1 使用清理后无前缀的标题 "同名章节"（遭遇碰撞）
    #    章节2 使用精确带前缀的标题 "一、同名章节"（精确命中）
    review = ReviewDoc(
        title="碰撞测试综述",
        topic="碰撞测试",
        intro="引言内容",
        conclusion="结论内容",
        sections=[
            SectionDraft(heading="同名章节", markdown="段落1 [@doc_a]", cited_doc_ids=["doc_a"]),
            SectionDraft(heading="一、同名章节", markdown="段落2 [@doc_a]", cited_doc_ids=["doc_a"]),
        ],
        references=["[1] 文献A"],
        reference_doc_ids=["doc_a"],
    )

    run_ctx = RunContext(
        topic="碰撞测试", run_id="run_collision_001", trigger_source="cli",
        model_name="gpt-5.4", analysis_model="gpt-5.4", reranker_model="bge-reranker",
        target_words=3000, target_lang="zh"
    )

    manifest_path = write_review_manifest(ManifestInput(
        output_md_path=str(md_file),
        review=review,
        outline=Outline(title="碰撞测试综述", topic="碰撞测试"),
        evidence_by_heading=evidence_by_heading,
        paper_meta={"doc_a": {"title": "Doc A"}, "doc_b": {"title": "Doc B"}},
        run_context=run_ctx,
    ))

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest_data = json.load(f)

    tree = manifest_data["section_evidence"]
    assert len(tree) == 2

    # 章节1（清理后标题 "同名章节"）：规范化键存在碰撞，必须 fail closed，不得绑定 ev_a 或 ev_b
    assert tree[0]["heading"] == "同名章节"
    assert tree[0]["evidence_status"] == "unavailable_missing_mapping"
    assert tree[0]["chunks"] == []

    # 章节2（原始精确标题 "一、同名章节"）：原始精确键不受规范化碰撞影响，正常匹配 ev_a
    assert tree[1]["heading"] == "一、同名章节"
    assert tree[1]["evidence_status"] == "complete"
    assert len(tree[1]["chunks"]) == 1
    assert tree[1]["chunks"][0]["doc_id"] == "doc_a"
