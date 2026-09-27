"""Batch 导出完整性回归测试：解析失败不导出、单篇异常不阻塞、Excel 原子写。

全部使用 tmp_path 下的临时库/临时文件与假对象，不触碰真实数据、不发起网络请求。
"""
import json
import os
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

import batch_pipeline
import utils
from db_schema import ensure_papers_table

MD = "# Test paper\n" + "Verified scientific content. " * 40


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把 batch_pipeline 的所有落盘路径指向 tmp_path，并屏蔽副作用。"""
    s = batch_pipeline.settings
    for name in ("INPUT_PDF_DIR", "MINERU_OUTPUT_DIR", "OBSIDIAN_VAULT_DIR",
                 "PROCESSED_PDF_DIR", "FAILED_PDF_DIR"):
        monkeypatch.setattr(s, name, str(tmp_path / name))
    monkeypatch.setattr(s, "DB_PATH", str(tmp_path / "db" / "batch.db"))
    monkeypatch.setattr(s, "EXCEL_OUTPUT_PATH", str(tmp_path / "xl" / "kb.xlsx"))
    monkeypatch.setattr(s, "REVIEW_AUTO_INDEX", False)
    monkeypatch.setattr(batch_pipeline, "log_run_event", lambda **_kw: None)

    notes = []

    def fake_note(meta, analysis, images, vault, doc_id):
        notes.append(doc_id)
        return os.path.join(vault, f"note_{doc_id}.md")

    monkeypatch.setattr(batch_pipeline, "generate_obsidian_note", fake_note)
    os.makedirs(tmp_path / "db")
    conn = sqlite3.connect(s.DB_PATH)
    ensure_papers_table(conn)
    conn.commit()
    yield SimpleNamespace(conn=conn, settings=s, notes=notes, tmp=tmp_path)
    conn.close()


def _insert(conn, doc_id, result_json, status=batch_pipeline.STATUS_COMPLETED):
    conn.execute(
        """INSERT INTO papers (id, title, pdf_path, language, mineru_md, images_dir,
               status, batch_provider, batch_job_id, result_json)
           VALUES (?, ?, ?, 'en', ?, '', ?, 'deepseek', 'job-1', ?)""",
        (doc_id, f"title-{doc_id}", f"{doc_id}.pdf", MD, status, result_json),
    )
    conn.commit()


def _status(conn, doc_id):
    return conn.execute("SELECT status FROM papers WHERE id=?", (doc_id,)).fetchone()[0]


GOOD = json.dumps({"tldr": "ok", "authors": "A", "year": "2024"})


def test_parse_error_is_not_exported_and_gets_resubmitted(env, monkeypatch):
    _insert(env.conn, "bad", "this is {not json")
    _insert(env.conn, "good", GOOD)

    exported = batch_pipeline._export_completed(env.conn.cursor(), env.conn)

    assert exported == 1
    assert _status(env.conn, "bad") == batch_pipeline.STATUS_RESULT_PARSE_FAILED
    assert _status(env.conn, "good") == batch_pipeline.STATUS_EXPORTED
    assert "bad" not in env.notes
    df = pd.read_excel(env.settings.EXCEL_OUTPUT_PATH, engine="openpyxl")
    assert list(df["文献ID"]) == ["good"]

    # 下一轮 prepare_batch 必须真正把它重新提交
    submitted = []

    class _Router:
        def build_request(self, doc_id, md):
            return {"custom_id": doc_id, "body": md[:10]}

    monkeypatch.setattr(batch_pipeline, "MinerUClient", lambda *_a, **_k: object())
    monkeypatch.setattr(batch_pipeline, "LLMRouter", _Router)
    monkeypatch.setattr(batch_pipeline, "_submit_openai_batches",
                        lambda reqs, *_a: (submitted.extend(r["custom_id"] for r in reqs), (len(reqs), 0))[1])
    batch_pipeline.prepare_batch()
    assert submitted == ["bad"]


def test_exception_mid_loop_neither_strands_nor_blocks(env, monkeypatch):
    for doc_id in ("p1", "p2", "boom", "p4"):
        _insert(env.conn, doc_id, GOOD)

    real_note = batch_pipeline.generate_obsidian_note

    def flaky(meta, analysis, images, vault, doc_id):
        if doc_id == "boom":
            raise OSError("disk hiccup")
        return real_note(meta, analysis, images, vault, doc_id)

    monkeypatch.setattr(batch_pipeline, "generate_obsidian_note", flaky)

    exported = batch_pipeline._export_completed(env.conn.cursor(), env.conn)

    assert exported == 3
    df = pd.read_excel(env.settings.EXCEL_OUTPUT_PATH, engine="openpyxl")
    assert sorted(df["文献ID"]) == ["p1", "p2", "p4"]
    for doc_id in ("p1", "p2", "p4"):
        assert _status(env.conn, doc_id) == batch_pipeline.STATUS_EXPORTED
    # 环境类异常不重提交 LLM（不重花额度）：保持 COMPLETED、记错误，排到队尾
    assert _status(env.conn, "boom") == batch_pipeline.STATUS_COMPLETED
    err = env.conn.execute("SELECT error_message FROM papers WHERE id='boom'").fetchone()[0]
    assert "disk hiccup" in err

    # 新完成的文献优先于上轮出错的那篇，即使 LIMIT 只剩一个名额
    _insert(env.conn, "fresh", GOOD)
    monkeypatch.setattr(env.settings, "BATCH_SIZE_LIMIT", 1)
    assert batch_pipeline._export_completed(env.conn.cursor(), env.conn) == 1
    assert _status(env.conn, "fresh") == batch_pipeline.STATUS_EXPORTED

    # 故障排除后下一轮导出成功，错误信息清空
    monkeypatch.setattr(batch_pipeline, "generate_obsidian_note", real_note)
    assert batch_pipeline._export_completed(env.conn.cursor(), env.conn) == 1
    assert _status(env.conn, "boom") == batch_pipeline.STATUS_EXPORTED


def test_excel_failure_keeps_papers_completed_for_retry(env, monkeypatch):
    _insert(env.conn, "p1", GOOD)
    _insert(env.conn, "p2", GOOD)

    def broken(*_a, **_k):
        raise PermissionError("excel locked")

    monkeypatch.setattr(batch_pipeline, "export_to_excel", broken)
    assert batch_pipeline._export_completed(env.conn.cursor(), env.conn) == 0
    # Excel 没写成功 → 绝不能标 EXPORTED
    assert _status(env.conn, "p1") == batch_pipeline.STATUS_COMPLETED
    assert _status(env.conn, "p2") == batch_pipeline.STATUS_COMPLETED

    monkeypatch.setattr(batch_pipeline, "export_to_excel", utils.export_to_excel)
    assert batch_pipeline._export_completed(env.conn.cursor(), env.conn) == 2
    assert _status(env.conn, "p1") == batch_pipeline.STATUS_EXPORTED
    df = pd.read_excel(env.settings.EXCEL_OUTPUT_PATH, engine="openpyxl")
    assert sorted(df["文献ID"]) == ["p1", "p2"]


def test_excel_write_is_atomic(tmp_path, monkeypatch):
    path = str(tmp_path / "kb.xlsx")
    utils.export_to_excel([{"文献ID": "a", "标题": "A"}], path)
    before = open(path, "rb").read()

    real_to_excel = pd.DataFrame.to_excel

    def crash_midway(self, target, *a, **k):
        # 先写出半截内容再崩溃，模拟进程在写盘途中被打断
        with open(target, "wb") as f:
            f.write(b"PK\x03\x04 truncated")
        raise OSError("killed during write")

    monkeypatch.setattr(pd.DataFrame, "to_excel", crash_midway)
    with pytest.raises(OSError):
        utils.export_to_excel([{"文献ID": "b", "标题": "B"}], path)

    assert open(path, "rb").read() == before
    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp.xlsx")]
    assert leftovers == []

    # 恢复后合并去重语义不变
    monkeypatch.setattr(pd.DataFrame, "to_excel", real_to_excel)
    utils.export_to_excel([{"文献ID": "a", "标题": "A2"}, {"文献ID": "b", "标题": "B"}], path)
    df = pd.read_excel(path, engine="openpyxl")
    assert list(df["文献ID"]) == ["a", "b"]
    assert list(df["标题"]) == ["A2", "B"]
