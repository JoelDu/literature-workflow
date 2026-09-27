"""Batch 提交/拉取韧性回归：重试上限、孤儿批次、额度短事务、expired 部分结果、Gemini 遗留。

全部使用 tmp_path 下的临时库与假客户端，不发起任何网络/LLM 请求。
"""
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

import batch_pipeline
from db_schema import ensure_papers_table, ensure_llm_quota_tables

MD = "# Test paper\n" + "Verified scientific content. " * 40
MODEL = "gpt-5.4-mini"


@pytest.fixture
def env(tmp_path, monkeypatch):
    s = batch_pipeline.settings
    for name in ("INPUT_PDF_DIR", "MINERU_OUTPUT_DIR", "OBSIDIAN_VAULT_DIR",
                 "PROCESSED_PDF_DIR", "FAILED_PDF_DIR"):
        monkeypatch.setattr(s, name, str(tmp_path / name))
    monkeypatch.setattr(s, "DB_PATH", str(tmp_path / "db" / "batch.db"))
    monkeypatch.setattr(s, "EXCEL_OUTPUT_PATH", str(tmp_path / "xl" / "kb.xlsx"))
    monkeypatch.setattr(s, "REVIEW_AUTO_INDEX", False)
    monkeypatch.setattr(batch_pipeline, "log_run_event", lambda **_kw: None)
    monkeypatch.setattr(batch_pipeline, "_sleep", lambda *_a: None)
    monkeypatch.delenv("BATCH_MAX_ANALYSIS_ATTEMPTS", raising=False)
    os.makedirs(tmp_path / "db")
    conn = sqlite3.connect(s.DB_PATH)
    ensure_papers_table(conn)
    ensure_llm_quota_tables(conn)
    conn.commit()
    yield SimpleNamespace(conn=conn, settings=s, tmp=tmp_path)
    conn.close()


def _insert(conn, doc_id, status, attempts=0, job="job-1", provider="openai"):
    conn.execute(
        "INSERT INTO papers (id, title, pdf_path, language, mineru_md, images_dir, status, "
        "batch_provider, batch_job_id, analysis_attempts, error_message) "
        "VALUES (?, ?, ?, 'en', ?, '', ?, ?, ?, ?, 'last error')",
        (doc_id, f"title-{doc_id}", f"{doc_id}.pdf", MD, status, provider, job, attempts),
    )
    conn.commit()


def _row(conn, doc_id):
    return conn.execute("SELECT status, analysis_attempts, batch_job_id, error_message FROM papers WHERE id=?",
                        (doc_id,)).fetchone()


def _request(doc_id):
    return {"custom_id": doc_id, "method": "POST", "url": "/v1/chat/completions",
            "body": {"model": MODEL, "messages": [{"role": "user", "content": "x" * 400}],
                     "max_completion_tokens": 100}}


def _ledger(conn):
    row = conn.execute("SELECT reserved_tokens FROM llm_usage_model_daily WHERE model=?", (MODEL,)).fetchone()
    return row[0] if row else 0


class FakeBatches:
    def __init__(self, create_errors=0, listed=None, status=None):
        self.create_errors = create_errors
        self.create_calls = 0
        self.listed = listed or []
        self.status = status
        self.on_create = None

    def create(self, **kwargs):
        self.create_calls += 1
        if self.on_create:
            self.on_create()
        if self.create_calls <= self.create_errors:
            raise TimeoutError("client timeout")
        return SimpleNamespace(id=f"batch-{self.create_calls}", input_file_id=kwargs["input_file_id"])

    def list(self, limit=20):
        return SimpleNamespace(data=self.listed)

    def retrieve(self, _job_id):
        return self.status


class FakeFiles:
    def __init__(self, contents=None):
        self.contents = contents or {}
        self.on_upload = None
        self.uploads = 0

    def create(self, file, purpose):
        self.uploads += 1
        if self.on_upload:
            self.on_upload()
        return SimpleNamespace(id="file-1")

    def retrieve(self, file_id):
        return SimpleNamespace(status="processed")

    def content(self, file_id):
        return SimpleNamespace(content=self.contents[file_id].encode("utf-8"))


class FakeRouter:
    openai_model = MODEL

    def __init__(self, client):
        self.openai_client = client


def _client(**kw):
    return SimpleNamespace(files=FakeFiles(kw.pop("contents", None)), batches=FakeBatches(**kw))


# ── 1. 重试上限 ────────────────────────────────────────────────────────────

def _run_prepare(monkeypatch):
    submitted = []

    class _Router:
        def build_request(self, doc_id, md):
            return {"custom_id": doc_id, "body": md[:10]}

    monkeypatch.setattr(batch_pipeline, "MinerUClient", lambda *_a, **_k: object())
    monkeypatch.setattr(batch_pipeline, "LLMRouter", _Router)
    monkeypatch.setattr(batch_pipeline, "_submit_openai_batches",
                        lambda reqs, *_a: (submitted.extend(r["custom_id"] for r in reqs), (len(reqs), 0))[1])
    batch_pipeline.prepare_batch()
    return submitted


def test_exhausted_paper_becomes_terminal_and_is_not_resubmitted(env, monkeypatch):
    _insert(env.conn, "worn", batch_pipeline.STATUS_RESULT_PARSE_FAILED, attempts=3)
    _insert(env.conn, "fresh", batch_pipeline.STATUS_RESULT_PARSE_FAILED, attempts=2)

    assert _run_prepare(monkeypatch) == ["fresh"]
    status, attempts, _, err = _row(env.conn, "worn")
    assert status == batch_pipeline.STATUS_ANALYSIS_FAILED
    assert "3" in err and "last error" in err
    assert batch_pipeline.STATUS_ANALYSIS_FAILED not in batch_pipeline.RESUBMITTABLE_STATUSES

    # 再跑一轮仍不会被捡回
    assert _run_prepare(monkeypatch) == ["fresh"]
    assert _row(env.conn, "worn")[0] == batch_pipeline.STATUS_ANALYSIS_FAILED


def test_attempt_limit_is_configurable(env, monkeypatch):
    monkeypatch.setenv("BATCH_MAX_ANALYSIS_ATTEMPTS", "1")
    _insert(env.conn, "once", batch_pipeline.STATUS_BATCH_FAILED, attempts=1)
    assert _run_prepare(monkeypatch) == []
    assert _row(env.conn, "once")[0] == batch_pipeline.STATUS_ANALYSIS_FAILED


def test_hot_upgrade_adds_attempts_column_idempotently(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE papers (id TEXT PRIMARY KEY, title TEXT, pdf_path TEXT, language TEXT, "
                 "mineru_md TEXT, images_dir TEXT, status TEXT, batch_provider TEXT, batch_job_id TEXT, "
                 "result_json TEXT)")
    conn.execute("INSERT INTO papers (id, status) VALUES ('old', 'PARSED')")
    assert "analysis_attempts" in ensure_papers_table(conn)
    assert ensure_papers_table(conn) == []
    assert conn.execute("SELECT analysis_attempts FROM papers WHERE id='old'").fetchone()[0] == 0
    conn.close()


# ── 3/4. 提交：短事务预留、立即落库、防重复建单 ─────────────────────────────

def test_successful_submit_persists_batch_and_counts_attempt(env):
    _insert(env.conn, "p1", batch_pipeline.STATUS_PARSED)
    client = _client()
    sub, fail = batch_pipeline._submit_openai_batches([_request("p1")], FakeRouter(client),
                                                      env.conn.cursor(), env.conn)
    assert (sub, fail) == (1, 0)
    status, attempts, job, _ = _row(env.conn, "p1")
    assert (status, attempts, job) == (batch_pipeline.STATUS_SUBMITTED, 1, "batch-1")
    reserved = env.conn.execute("SELECT reserved_tokens FROM llm_batches WHERE batch_id='batch-1'").fetchone()[0]
    assert reserved > 0 and _ledger(env.conn) == reserved
    assert env.conn.execute("SELECT submitted_batches FROM llm_usage_daily").fetchone()[0] == 1
    assert not env.conn.in_transaction
    # 临时 JSONL 不残留
    assert not [f for f in os.listdir(os.path.dirname(env.settings.DB_PATH)) if f.endswith(".jsonl")]


def test_create_timeout_reuses_existing_batch_instead_of_duplicating(env):
    _insert(env.conn, "p1", batch_pipeline.STATUS_PARSED)
    # 第一次 create 客户端超时，但云端其实已经建好了
    existing = SimpleNamespace(id="batch-cloud", input_file_id="file-1", status="validating")
    other = SimpleNamespace(id="batch-other", input_file_id="file-9", status="in_progress")
    client = _client(create_errors=1, listed=[other, existing])
    sub, _ = batch_pipeline._submit_openai_batches([_request("p1")], FakeRouter(client),
                                                   env.conn.cursor(), env.conn)
    assert sub == 1
    assert client.batches.create_calls == 1
    assert _row(env.conn, "p1")[2] == "batch-cloud"


def test_upload_is_retried_without_recreating_batches(env, monkeypatch):
    _insert(env.conn, "p1", batch_pipeline.STATUS_PARSED)
    client = _client()
    fails = {"n": 1}

    def flaky():
        if fails["n"]:
            fails["n"] -= 1
            raise ConnectionError("upload reset")

    client.files.on_upload = flaky
    monkeypatch.setattr(batch_pipeline._upload_batch_file.retry, "wait", lambda *_a, **_k: 0)
    sub, _ = batch_pipeline._submit_openai_batches([_request("p1")], FakeRouter(client),
                                                   env.conn.cursor(), env.conn)
    assert sub == 1
    assert client.files.uploads == 2
    assert client.batches.create_calls == 1


def test_failed_create_releases_reservation(env):
    _insert(env.conn, "p1", batch_pipeline.STATUS_PARSED)
    client = _client(create_errors=99)
    sub, fail = batch_pipeline._submit_openai_batches([_request("p1")], FakeRouter(client),
                                                      env.conn.cursor(), env.conn)
    assert (sub, fail) == (0, 1)
    assert client.batches.create_calls == 3
    status, attempts, _, err = _row(env.conn, "p1")
    assert status == batch_pipeline.STATUS_SUBMIT_FAILED and attempts == 0
    assert "client timeout" in err
    assert _ledger(env.conn) == 0
    assert env.conn.execute("SELECT COUNT(*) FROM llm_batches").fetchone()[0] == 0


def test_no_write_lock_held_during_network_calls(env):
    """上传/建单期间其它进程必须能立即拿到写锁（此前隐式事务一直持锁到最后 commit）。"""
    _insert(env.conn, "p1", batch_pipeline.STATUS_PARSED)
    client = _client()
    grabbed = []

    def other_process_writes():
        other = sqlite3.connect(env.settings.DB_PATH, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("UPDATE llm_usage_model_daily SET updated_at='x'")
            other.commit()
            grabbed.append(True)
        finally:
            other.close()

    client.files.on_upload = other_process_writes
    client.batches.on_create = other_process_writes
    sub, _ = batch_pipeline._submit_openai_batches([_request("p1")], FakeRouter(client),
                                                   env.conn.cursor(), env.conn)
    assert sub == 1 and grabbed == [True, True]


def test_quota_exhausted_reserves_nothing(env, monkeypatch):
    monkeypatch.setattr(env.settings, "LLM_DAILY_TOKEN_CAP", 10)
    _insert(env.conn, "p1", batch_pipeline.STATUS_PARSED)
    client = _client()
    assert batch_pipeline._submit_openai_batches([_request("p1")], FakeRouter(client),
                                                 env.conn.cursor(), env.conn) == (0, 0)
    assert client.files.uploads == 0 and _ledger(env.conn) == 0
    assert not env.conn.in_transaction


# ── 2. expired 批次保留部分结果 ─────────────────────────────────────────────

def _output_line(doc_id, content='{"tldr": "ok"}', total=50):
    return json.dumps({"custom_id": doc_id, "response": {"body": {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 30, "completion_tokens": total - 30, "total_tokens": total}}}})


def _submitted_batch(env, ids, job="batch-x", reserved=1000):
    for doc_id in ids:
        _insert(env.conn, doc_id, batch_pipeline.STATUS_SUBMITTED, attempts=1, job=job)
    env.conn.execute("INSERT INTO llm_batches (batch_id, model, utc_date, reserved_tokens, status, created_at, updated_at) "
                     "VALUES (?, ?, '2026-09-27', ?, 'submitted', 'now', 'now')", (job, MODEL, reserved))
    env.conn.execute("INSERT INTO llm_usage_daily (utc_date, reserved_tokens, updated_at) VALUES ('2026-09-27', ?, 'now')",
                     (reserved,))
    env.conn.execute("INSERT INTO llm_usage_model_daily (utc_date, model, reserved_tokens, updated_at) "
                     "VALUES ('2026-09-27', ?, ?, 'now')", (MODEL, reserved))
    env.conn.commit()


def test_expired_batch_keeps_completed_items(env):
    _submitted_batch(env, ["done", "errored", "missing"])
    status = SimpleNamespace(status="expired", output_file_id="out", error_file_id="err",
                             request_counts=SimpleNamespace(completed=1, failed=1, total=3))
    client = _client(status=status, contents={
        "out": _output_line("done"),
        "err": json.dumps({"custom_id": "errored", "error": "boom"}),
    })
    completed, failed = batch_pipeline._fetch_openai_results("batch-x", FakeRouter(client),
                                                            env.conn.cursor(), env.conn)
    assert (completed, failed) == (1, 2)
    assert _row(env.conn, "done")[0] == batch_pipeline.STATUS_COMPLETED
    assert _row(env.conn, "errored")[0] == batch_pipeline.STATUS_BATCH_FAILED
    assert "boom" in _row(env.conn, "errored")[3]
    assert _row(env.conn, "missing")[0] == batch_pipeline.STATUS_BATCH_FAILED
    assert "expired" in _row(env.conn, "missing")[3]
    # 真实 usage 入账、预留释放
    assert env.conn.execute("SELECT reserved_tokens, actual_total_tokens FROM llm_usage_model_daily").fetchone() == (0, 50)
    assert env.conn.execute("SELECT status, usage_recorded FROM llm_batches").fetchone() == ("expired", 1)
    assert env.conn.execute("SELECT failed_batches FROM llm_usage_daily").fetchone()[0] == 1


def test_expired_batch_without_output_fails_everything_and_keeps_reservation(env):
    _submitted_batch(env, ["a", "b"])
    status = SimpleNamespace(status="expired", output_file_id=None, error_file_id=None)
    client = _client(status=status)
    assert batch_pipeline._fetch_openai_results("batch-x", FakeRouter(client),
                                                env.conn.cursor(), env.conn) == (0, 2)
    assert {r[0] for r in env.conn.execute("SELECT status FROM papers")} == {batch_pipeline.STATUS_BATCH_FAILED}
    assert _ledger(env.conn) == 1000


# ── 6. Gemini 遗留批次 ──────────────────────────────────────────────────────

def test_legacy_gemini_batch_is_skipped_without_crash(env, monkeypatch):
    _insert(env.conn, "g1", batch_pipeline.STATUS_SUBMITTED, job="batches/old", provider="gemini")
    monkeypatch.setattr(batch_pipeline, "LLMRouter", lambda: SimpleNamespace())
    printed = []
    monkeypatch.setattr(batch_pipeline.console, "print", lambda *a, **_k: printed.append(" ".join(map(str, a))))
    report = batch_pipeline.fetch_batch()
    assert report["jobs_checked"] == 1 and report["failed"] == 0
    assert any("Gemini" in p and "跳过" in p for p in printed)
    assert not any("未知崩溃" in p for p in printed)
    assert _row(env.conn, "g1")[0] == batch_pipeline.STATUS_SUBMITTED
    assert not hasattr(batch_pipeline, "_submit_gemini_batch")
    assert not hasattr(batch_pipeline, "_fetch_gemini_results")
