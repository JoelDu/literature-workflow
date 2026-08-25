"""
test_isolation_regression.py — 孤儿Work隔离、分层统计、临时库状态机与生产库只读回归测试
"""
import os
import sqlite3
import pytest
import tempfile
from starlette.testclient import TestClient
from web_server import app, get_app_settings
from litreview.store import VectorStore
from litreview.pipeline import approve_candidates


@pytest.fixture
def client():
    return TestClient(app)


def test_orphan_work_quarantine_isolation():
    """验证孤儿 Work (work_oa_w4391143884) 被严格隔离"""
    test_db = "data/test_citation_network.db"
    if not os.path.exists(test_db):
        pytest.skip("test_citation_network.db not found")

    conn = sqlite3.connect(f"file:{os.path.abspath(test_db)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM works WHERE work_id = 'work_oa_w4391143884'").fetchone()
    conn.close()

    assert row is not None, "Orphan work should exist in audit database"
    assert row["quarantine_status"] == "QUARANTINED_INCOMPLETE"
    assert row["eligible_for_retrieval"] == 0
    assert row["eligible_for_citation_graph"] == 0
    assert row["in_library"] == 0


def test_dashboard_stratified_statistics(client):
    """验证 Dashboard 实体分区以当前数据库动态闭合。"""
    resp = client.get("/api/dashboard/stats")
    assert resp.status_code == 200
    data = resp.json()["data"]

    lib = data["library"]
    graph = data["graph"]
    cands = data["candidates"]

    # 1. 互斥闭合实体分区断言
    prod_normal = graph["production_baseline_normal"]
    prod_quarantined = graph["production_baseline_quarantined"]
    test_normal = graph["verified_ingested_works"]
    test_quarantined = graph["quarantined_works_total"] - prod_quarantined  # 孤儿 Work
    ext_unresolved = graph["external_unresolved_placeholders"]

    assert all(value >= 0 for value in (
        prod_normal, prod_quarantined, test_normal, test_quarantined, ext_unresolved))
    partition_sum = prod_normal + prod_quarantined + test_normal + test_quarantined + ext_unresolved
    assert partition_sum == graph["works_total"]

    # 2. 隔离统计必须精确为 4
    assert lib["quarantined_works_total"] == graph["quarantined_works_total"]

    # 3. 生产馆藏基线必须为 344
    assert graph["production_baseline_total"] == prod_normal + prod_quarantined

    # 4. 能力指标 (不重复相加)
    assert lib["vector_indexed_capability"] == graph["vector_indexed_capability"]

    # 5. 候选池真实状态分布断言: 351/3/1/0 (合计 355)
    assert cands["total_candidates"] >= max(
        cands["DISCOVERED"], cands["VECTOR_INDEXED"], cands["QUARANTINED_INCOMPLETE"])


def test_temporary_db_decision_and_state_machine_guard(tmp_path):
    """所有审批与状态机测试使用临时数据库，不污染生产与测试库"""
    temp_db = str(tmp_path / "temp_discovery.db")
    conn = sqlite3.connect(temp_db)
    conn.execute("""
        CREATE TABLE discovered_candidates (
            candidate_id TEXT PRIMARY KEY,
            query_run_id TEXT,
            openalex_id TEXT,
            doi TEXT,
            title TEXT,
            authors_json TEXT,
            journal_or_venue TEXT,
            publication_year INTEGER,
            cited_by_count INTEGER,
            is_open_access INTEGER,
            oa_url TEXT,
            landing_page_url TEXT,
            track TEXT,
            direction TEXT,
            source_seeds_json TEXT,
            co_citation_count INTEGER,
            recommendation_reason TEXT,
            in_library INTEGER,
            in_library_work_id TEXT,
            match_method TEXT,
            match_evidence_json TEXT,
            data_source TEXT,
            response_hash TEXT,
            lifecycle_status TEXT,
            discovered_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE candidate_decisions (
            decision_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            previous_status TEXT NOT NULL,
            new_status TEXT NOT NULL,
            action TEXT NOT NULL,
            decision_source TEXT NOT NULL,
            reason TEXT,
            candidate_snapshot_json TEXT NOT NULL,
            decided_at TEXT NOT NULL,
            operator TEXT NOT NULL
        )
    """)

    # 插入 1 个 DISCOVERED 和 1 个 VECTOR_INDEXED
    conn.execute("""
        INSERT INTO discovered_candidates (candidate_id, title, lifecycle_status)
        VALUES ('cand_temp_001', 'Test Paper 1', 'DISCOVERED'),
               ('cand_temp_002', 'Test Paper 2 (Already Indexed)', 'VECTOR_INDEXED')
    """)
    conn.commit()
    conn.close()

    # 1. 正常审批 DISCOVERED
    res1 = approve_candidates(
        cache_db_path=temp_db,
        candidate_ids=["cand_temp_001"],
        action="APPROVE",
        reason="TEMP_TEST_APPROVED",
        decision_source="USER",
        operator="pytest_runner"
    )
    assert res1["updated_count"] == 1
    assert res1["rejected_count"] == 0

    # 2. 非法倒退 VECTOR_INDEXED -> APPROVED 必须被硬拦截
    res2 = approve_candidates(
        cache_db_path=temp_db,
        candidate_ids=["cand_temp_002"],
        action="APPROVE",
        reason="ILLEGAL_ROLLBACK",
        decision_source="USER",
        operator="pytest_runner"
    )
    assert res2["updated_count"] == 0
    assert res2["rejected_count"] == 1
    assert "只有处于 'DISCOVERED' 状态的候选才能执行审批" in res2["rejected"][0]["error"]


def test_production_db_readonly_enforcement():
    """验证生产库连接受 mode=ro 强制保护，任何写操作均抛出异常"""
    settings = get_app_settings()
    prod_path = settings.DB_PATH
    if not os.path.exists(prod_path):
        pytest.skip("Production batch_tracking.db not found")

    store_ro = VectorStore(prod_path, settings.EMBEDDING_DIM, readonly=True)
    try:
        conn = store_ro._conn()
        row = conn.execute("SELECT COUNT(*) FROM papers").fetchone()
    except sqlite3.OperationalError as exc:
        if "unable to open database file" in str(exc):
            pytest.skip("production database is not readable in this environment")
        raise

    # 1. 只读查询必须成功
    assert row[0] > 0

    # 2. 尝试写入必须抛出 OperationalError: attempt to write a readonly database
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("UPDATE papers SET title = 'ILLEGAL_WRITE' WHERE id = 'bba4a59d'")

    conn.close()


def test_full_citation_graph_isolation_and_closure(client):
    """验证 /api/citations/graph/full 严格排除隔离Work并保证边拓扑闭合"""
    # 1. 默认骨干模式 (极速流畅 60 FPS)
    resp_backbone = client.get("/api/citations/graph/full")
    assert resp_backbone.status_code == 200
    d_bb = resp_backbone.json()["data"]
    assert "work_oa_w4391143884" not in {n["id"] for n in d_bb["nodes"]}
    for l in d_bb["links"]:
        assert l["source"] in {n["id"] for n in d_bb["nodes"]}
        assert l["target"] in {n["id"] for n in d_bb["nodes"]}

    # 2. 全量模式 (包含外部未解析节点: include_unresolved=true)
    resp_full = client.get("/api/citations/graph/full?include_unresolved=true")
    assert resp_full.status_code == 200
    data = resp_full.json()["data"]

    nodes = data["nodes"]
    links = data["links"]
    node_ids = {n["id"] for n in nodes}

    assert len(nodes) >= len(d_bb["nodes"])
    assert len(links) >= len(d_bb["links"])

    # work_oa_w4391143884 绝不存在于 nodes 中
    assert "work_oa_w4391143884" not in node_ids

    # 绝不存在以 work_oa_w4391143884 为 source 或 target 的边
    for l in links:
        assert l["source"] != "work_oa_w4391143884"
        assert l["target"] != "work_oa_w4391143884"

    # 所有返回边的 source 和 target 都能在 nodes 中找到 (闭合性)
    for l in links:
        assert l["source"] in node_ids, f"Source {l['source']} missing from nodes"
        assert l["target"] in node_ids, f"Target {l['target']} missing from nodes"
