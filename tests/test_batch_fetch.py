"""Batch 拉取失败状态机的回归测试。"""
import json
import sqlite3
from types import SimpleNamespace

import batch_pipeline
from db_schema import PAPERS_DDL, ensure_llm_quota_tables


class _FakeFiles:
    def __init__(self, payload):
        self.payload = payload

    def content(self, _file_id):
        return SimpleNamespace(content=self.payload.encode("utf-8"))


class _FakeClient:
    def __init__(self, status, files):
        self.status = status
        self.files = _FakeFiles(files)
        self.batches = self

    def retrieve(self, _job_id):
        return self.status


class _FakeRouter:
    def __init__(self, client):
        self.deepseek_client = client


def _db_with_batch(tmp_path, ids):
    conn = sqlite3.connect(tmp_path / "batch.db")
    conn.execute(PAPERS_DDL)
    conn.executemany(
        "INSERT INTO papers (id, title, status, batch_provider, batch_job_id) "
        "VALUES (?, ?, ?, 'deepseek', 'batch-1')",
        [(doc_id, doc_id, batch_pipeline.STATUS_SUBMITTED) for doc_id in ids],
    )
    conn.commit()
    return conn


def test_all_failed_completed_batch_is_requeued(tmp_path, monkeypatch):
    """云端 completed + 0 成功 + error_file 必须离开 BATCH_SUBMITTED。"""
    status = SimpleNamespace(
        status="completed",
        output_file_id=None,
        error_file_id="errors",
        request_counts=SimpleNamespace(completed=0, failed=2, total=2),
    )
    errors = "\n".join(json.dumps({"custom_id": doc_id, "error": "model unavailable"})
                        for doc_id in ("a", "b"))
    conn = _db_with_batch(tmp_path, ["a", "b"])
    monkeypatch.setattr(batch_pipeline, "log_run_event", lambda **_kwargs: None)

    completed, failed = batch_pipeline._fetch_deepseek_results(
        "batch-1", _FakeRouter(_FakeClient(status, errors)),
        conn.cursor(), conn,
    )

    assert (completed, failed) == (0, 2)
    assert {row[0] for row in conn.execute(
        "SELECT id FROM papers WHERE status=?", (batch_pipeline.STATUS_BATCH_FAILED,)
    )} == {"a", "b"}
    assert all("model unavailable" in row[0] for row in conn.execute(
        "SELECT error_message FROM papers ORDER BY id"
    ))
    conn.close()


def test_mixed_batch_marks_error_file_requests_failed(tmp_path, monkeypatch):
    """有 output_file 时，error_file 中的失败请求也必须闭环。"""
    status = SimpleNamespace(
        status="completed",
        output_file_id="outputs",
        error_file_id="errors",
        request_counts=SimpleNamespace(completed=1, failed=1, total=2),
    )
    output = json.dumps({
        "custom_id": "ok",
        "response": {"body": {"choices": [{"message": {"content": "{}"}}]}},
    })
    errors = json.dumps({"custom_id": "bad", "error": "invalid request"})
    conn = _db_with_batch(tmp_path, ["ok", "bad"])
    monkeypatch.setattr(batch_pipeline, "log_run_event", lambda **_kwargs: None)

    class _Files(_FakeFiles):
        def content(self, file_id):
            payload = output if file_id == "outputs" else errors
            return SimpleNamespace(content=payload.encode("utf-8"))

    client = _FakeClient(status, errors)
    client.files = _Files(errors)
    completed, failed = batch_pipeline._fetch_deepseek_results(
        "batch-1", _FakeRouter(client), conn.cursor(), conn,
    )

    assert (completed, failed) == (1, 1)
    assert conn.execute("SELECT status FROM papers WHERE id='ok'").fetchone()[0] == batch_pipeline.STATUS_COMPLETED
    assert conn.execute("SELECT status FROM papers WHERE id='bad'").fetchone()[0] == batch_pipeline.STATUS_BATCH_FAILED
    conn.close()


def test_batch_usage_is_added_to_shared_mini_ledger(tmp_path):
    """Batch 和同步 mini 调用必须共用模型级 UTC 账本，不能各跑满日限额。"""
    conn = sqlite3.connect(tmp_path / "batch.db")
    ensure_llm_quota_tables(conn)
    conn.execute(
        "INSERT INTO llm_batches (batch_id, model, utc_date, reserved_tokens, status, created_at, updated_at) "
        "VALUES ('batch-1', 'gpt-5.4-mini', '2026-08-12', 100, 'submitted', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO llm_usage_daily (utc_date, reserved_tokens, updated_at) VALUES ('2026-08-12', 100, 'now')"
    )
    conn.execute(
        "INSERT INTO llm_usage_model_daily (utc_date, model, reserved_tokens, updated_at) "
        "VALUES ('2026-08-12', 'gpt-5.4-mini', 100, 'now')"
    )
    batch_pipeline._record_batch_usage(
        "batch-1", {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
        conn.cursor(), conn, "completed",
    )
    assert conn.execute(
        "SELECT reserved_tokens, actual_total_tokens FROM llm_usage_model_daily "
        "WHERE utc_date='2026-08-12' AND model='gpt-5.4-mini'"
    ).fetchone() == (0, 50)
    conn.close()
