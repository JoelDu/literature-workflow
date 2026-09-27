"""Web API 行为测试：每个用例一套独立的种子三库（tests/lit_seed.py），数量全部已知。

以前这些用例打默认库、依赖 bba4a59d / cand_emerg_run_50cac3bc045d_... 这类生产 ID，
审批 POST 还会真的写进生产缓存库；现在全部落在 tmp_path 里。
"""
import sqlite3

import pytest
from starlette.testclient import TestClient

import lit_seed as seed


@pytest.fixture
def client(seeded_dbs):
    from web_server import app

    return TestClient(app)


def _lifecycle(cache_db, candidate_id):
    conn = sqlite3.connect(cache_db)
    try:
        return conn.execute("SELECT lifecycle_status FROM discovered_candidates WHERE candidate_id = ?",
                            (candidate_id,)).fetchone()[0]
    finally:
        conn.close()


def test_api_dashboard_stats(client):
    resp = client.get("/api/dashboard/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    lib, graph, cands = data["data"]["library"], data["data"]["graph"], data["data"]["candidates"]
    assert lib["total_docs"] == len(seed.PAPERS)
    assert lib["total_chunks"] == 3
    assert lib["vector_indexed_docs"] == 1
    # 图谱：4 篇馆藏（1 篇隔离）+ 1 篇已入库 OA + 1 个孤儿 + 2 个外部未解析占位
    assert graph["production_baseline_total"] == 4
    assert graph["production_baseline_quarantined"] == 1
    assert graph["verified_ingested_works"] == 1
    assert graph["external_unresolved_placeholders"] == 2
    assert graph["works_total"] == 8
    assert cands["total_candidates"] == 3
    assert cands["DISCOVERED"] == 2 and cands["VECTOR_INDEXED"] == 1
    assert [r["query_run_id"] for r in data["data"]["recent_runs"]] == [seed.RUN_ID]


def test_api_library_documents(client):
    resp = client.get("/api/library/documents?page=1&page_size=10")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == len(seed.PAPERS)
    by_id = {it["id"]: it for it in data["items"]}
    assert set(by_id) == {p[0] for p in seed.PAPERS}
    # A 被 B、C 与已入库 OA 文献引用；B 引出 2 条馆内确认边
    assert by_id[seed.DOC_A]["incoming_citations"] == 3
    assert by_id[seed.DOC_B]["outgoing_citations"] == 2
    assert by_id[seed.DOC_A]["n_chunks"] == 2


def test_api_library_documents_pagination(client):
    data = client.get("/api/library/documents?page=2&page_size=3").json()["data"]
    assert data["total"] == 4 and data["total_pages"] == 2
    assert len(data["items"]) == 1


def test_api_library_document_detail_prefix_match(client):
    resp = client.get(f"/api/library/documents/{seed.DOC_B[:8]}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["doc_id"] == seed.DOC_B
    assert [m["ref_index"] for m in data["reference_mentions"]] == [1, 2, 3]
    stats = data["citation_network"]["network_statistics"]
    assert stats["total_references_in_seed"] == 3
    assert stats["confirmed_in_library_outgoing_count"] == 2
    assert stats["unresolved_external_references_count"] == 1


def test_api_citations_graph(client):
    resp = client.get(f"/api/citations/{seed.DOC_B[:8]}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["seed_id"] == seed.DOC_B
    ids = {n["id"] for n in data["nodes"]}
    assert {seed.DOC_A, seed.DOC_B, seed.DOC_C} <= ids
    assert len(data["links"]) == 3  # A、C 两条确认边 + 1 条外部未解析
    assert {link["value"] for link in data["links"]} == {"CONFIRMED", "UNRESOLVED_EXTERNAL"}
    assert len(data["categories"]) == 3


def test_api_citations_graph_unknown_doc_is_404(client):
    assert client.get("/api/citations/ffffffff").status_code == 404


def test_api_citations_graph_full_excludes_orphans_and_unresolved(client):
    data = client.get("/api/citations/graph/full").json()["data"]
    ids = {n["id"] for n in data["nodes"]}
    assert ids == {seed.DOC_A, seed.DOC_B, seed.DOC_C, seed.OA_WORK}
    assert seed.ORPHAN_WORK not in ids and seed.DOC_Q not in ids
    assert data["is_backbone"] is True
    assert data["total_links_count"] == 4

    full = client.get("/api/citations/graph/full?include_unresolved=true").json()["data"]
    full_ids = {n["id"] for n in full["nodes"]}
    assert len(full_ids - ids) == 2 and all(i.startswith("ext_work_") for i in full_ids - ids)
    assert seed.ORPHAN_WORK not in full_ids


def test_api_discovery_runs(client):
    data = client.get("/api/discovery/runs").json()
    assert data["success"] is True
    assert [r["query_run_id"] for r in data["data"]] == [seed.RUN_ID]


def test_api_discovery_candidates_filters(client):
    data = client.get("/api/discovery/candidates?page=1&page_size=10").json()["data"]
    assert data["total"] == 3
    only_found = client.get("/api/discovery/candidates?track=FOUNDATIONAL").json()["data"]
    assert [c["candidate_id"] for c in only_found["items"]] == [seed.CAND_DISCOVERED_2]


def test_api_discovery_candidate_decision_state_machine(client, seeded_dbs):
    resp = client.post("/api/discovery/candidates/decision", json={
        "candidate_ids": [seed.CAND_DISCOVERED, seed.CAND_INDEXED, "cand_does_not_exist"],
        "action": "APPROVE",
        "reason": "pytest",
    })
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["updated_count"] == 1
    assert data["rejected_count"] == 1  # VECTOR_INDEXED 不得倒退回 APPROVED
    assert data["not_found_count"] == 1
    assert _lifecycle(seeded_dbs.cache_db, seed.CAND_DISCOVERED) == "APPROVED"
    assert _lifecycle(seeded_dbs.cache_db, seed.CAND_INDEXED) == "VECTOR_INDEXED"

    # 已审批的再次审批同样被拦截
    again = client.post("/api/discovery/candidates/decision", json={
        "candidate_ids": [seed.CAND_DISCOVERED], "action": "APPROVE"}).json()["data"]
    assert again["updated_count"] == 0 and again["rejected_count"] == 1


def test_api_discovery_candidate_decision_requires_ids(client):
    assert client.post("/api/discovery/candidates/decision", json={"candidate_ids": []}).status_code == 400


def test_api_audit_events_assets(client):
    data = client.get("/api/audit/events").json()["data"]
    asset_ids = {a["asset_id"] for a in data["assets"]}
    assert "asset_seed_oa" in asset_ids
    assert {f"asset_{d[:8]}" for d in (seed.DOC_A, seed.DOC_B, seed.DOC_C)} <= asset_ids


def test_api_audit_events_lists_decisions(client):
    client.post("/api/discovery/candidates/decision",
                json={"candidate_ids": [seed.CAND_DISCOVERED], "action": "APPROVE"})
    decisions = client.get("/api/audit/events").json()["data"]["decisions"]
    assert [d["candidate_id"] for d in decisions] == [seed.CAND_DISCOVERED]
    assert decisions[0]["title"]
