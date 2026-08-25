import pytest
from litreview.facade import LiteratureReviewFacade, JobRegistry, get_facade


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


def test_facade_dashboard_stats():
    facade = get_facade()
    stats = facade.get_dashboard_stats()
    assert "library" in stats
    assert "graph" in stats
    assert "candidates" in stats
    graph = stats["graph"]
    assert graph["production_baseline_total"] == (
        graph["production_baseline_normal"] + graph["production_baseline_quarantined"])
    assert graph["edges_total"] == (
        graph["confirmed_edges"] + graph["candidate_edges"] + graph["unresolved_edges"])


def test_facade_list_documents():
    facade = get_facade()
    try:
        docs = facade.list_documents(page=1, page_size=5)
    except Exception as exc:
        if "unable to open database file" in str(exc):
            pytest.skip("production database is not readable in this environment")
        raise
    assert "items" in docs
    assert docs["total"] >= len(docs["items"])
    assert len(docs["items"]) <= 5


def test_facade_citation_graph():
    facade = get_facade()
    graph = facade.get_citation_graph(include_unresolved=False)
    assert "nodes" in graph
    assert "links" in graph
    node_ids = {node["id"] for node in graph["nodes"]}
    assert all(link["source"] in node_ids and link["target"] in node_ids
               for link in graph["links"])
    assert graph["is_backbone"] is True
