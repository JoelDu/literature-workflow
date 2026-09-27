import pytest
from litreview.facade import LiteratureReviewFacade, JobRegistry, get_facade

import lit_seed as seed


def test_job_registry_lifecycle():
    registry = JobRegistry()
    job_id = registry.create_job(job_type="review", title="测试综述生成")
    assert job_id.startswith("job_revi_")
    
    job = registry.get_job(job_id)
    assert job["status"] == "RUNNING"
    assert job["progress"] == 0
    
    registry.update_job(job_id, progress=50, step="大纲提取完成", log="正在提取子章节...")
    updated = registry.get_job(job_id)
    assert updated["progress"] == 50
    assert updated["current_step"] == "大纲提取完成"
    assert len(updated["logs"]) == 1
    
    registry.update_job(job_id, progress=100, status="COMPLETED", result={"output": "done"})
    completed = registry.get_job(job_id)
    assert completed["status"] == "COMPLETED"
    assert completed["result"]["output"] == "done"


@pytest.fixture
def facade(seeded_dbs, tmp_path):
    return LiteratureReviewFacade(
        prod_db_path=seeded_dbs.prod_db,
        test_citation_db_path=seeded_dbs.citation_db,
        cache_db_path=seeded_dbs.cache_db,
        staging_dir=str(tmp_path / "staging_pdf"),
    )


def test_facade_dashboard_stats(facade):
    stats = facade.get_dashboard_stats()
    graph = stats["graph"]
    assert graph["production_baseline_total"] == (
        graph["production_baseline_normal"] + graph["production_baseline_quarantined"]) == 4
    assert graph["edges_total"] == (
        graph["confirmed_edges"] + graph["candidate_edges"] + graph["unresolved_edges"])
    assert stats["library"]["total_docs"] == 4


def test_facade_list_documents(facade):
    docs = facade.list_documents(page=1, page_size=3)
    assert docs["total"] == 4
    assert len(docs["items"]) == 3


def test_facade_citation_graph(facade):
    graph = facade.get_citation_graph(include_unresolved=False)
    node_ids = {node["id"] for node in graph["nodes"]}
    assert node_ids == {seed.DOC_A, seed.DOC_B, seed.DOC_C, seed.OA_WORK}
    assert all(link["source"] in node_ids and link["target"] in node_ids
               for link in graph["links"])
    assert graph["is_backbone"] is True


def test_get_facade_uses_isolated_env_paths(monkeypatch, seeded_dbs):
    """单例外观按环境变量取路径；在测试里它必须指向种子库而不是 /mnt/ripe。"""
    import litreview.facade as facade_mod

    monkeypatch.setattr(facade_mod, "_default_facade", None)
    f = get_facade()
    assert f.prod_db_path == seeded_dbs.prod_db
    assert f.test_citation_db_path == seeded_dbs.citation_db
    assert f.get_dashboard_stats()["library"]["total_docs"] == 4
