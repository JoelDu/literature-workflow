import pytest
from starlette.testclient import TestClient
from web_server import app

@pytest.fixture
def client():
    return TestClient(app)

def test_api_dashboard_stats(client):
    resp = client.get("/api/dashboard/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "library" in data["data"]
    assert "graph" in data["data"]
    assert "candidates" in data["data"]
    assert data["data"]["library"]["total_docs"] >= 0

def test_api_library_documents(client):
    resp = client.get("/api/library/documents?page=1&page_size=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "items" in data["data"]
    assert "total" in data["data"]

def test_api_library_document_detail(client):
    resp = client.get("/api/library/documents/bba4a59d")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["doc_id"].startswith("bba4a59d")

def test_api_citations_graph(client):
    resp = client.get("/api/citations/bba4a59d")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "nodes" in data["data"]
    assert "links" in data["data"]
    assert "categories" in data["data"]

def test_api_citations_graph_full(client):
    resp = client.get("/api/citations/graph/full")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "nodes" in data["data"]
    assert "links" in data["data"]

def test_api_discovery_runs(client):
    resp = client.get("/api/discovery/runs")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert isinstance(data["data"], list)

def test_api_discovery_candidates(client):
    resp = client.get("/api/discovery/candidates?page=1&page_size=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "items" in data["data"]

def test_api_discovery_candidate_decision_guard(client):
    # 对已经是 VECTOR_INDEXED 的文献尝试再次审批应该被状态机拦截
    resp = client.post("/api/discovery/candidates/decision", json={
        "candidate_ids": ["cand_emerg_run_50cac3bc045d_W4392381569"],
        "action": "APPROVE",
        "reason": "ILLEGAL_ROLLBACK_ATTEMPT"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["data"]["updated_count"] == 0
    assert data["data"]["rejected_count"] == 1

def test_api_audit_events(client):
    resp = client.get("/api/audit/events")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "decisions" in data["data"]
    assert "assets" in data["data"]
