"""Read-only health audit for Literature Analyzer's three SQLite databases.

The public seam is :func:`inspect_databases`.  Every connection is opened with
``mode=ro``; this module never creates schemas, repairs rows, or changes PRAGMAs.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from litreview.graph_domain import CitationEdgeStatus
from litreview.sqlite_runtime import connect_sqlite


@dataclass(frozen=True)
class HealthIssue:
    code: str
    severity: str
    database: str
    message: str
    count: int = 0


@dataclass(frozen=True)
class DatabaseSummary:
    name: str
    path: str
    tables: Mapping[str, int]
    quick_check: str
    foreign_key_violations: int
    user_version: int


@dataclass(frozen=True)
class DatabaseHealthReport:
    databases: tuple[DatabaseSummary, ...]
    issues: tuple[HealthIssue, ...]
    metrics: Mapping[str, int]
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def _readonly(path: str) -> sqlite3.Connection:
    conn = connect_sqlite(path, readonly=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def _open_summary(name: str, path: str) -> tuple[sqlite3.Connection | None, DatabaseSummary, list[HealthIssue]]:
    issues: list[HealthIssue] = []
    if not os.path.isfile(path):
        issues.append(HealthIssue("database_missing", "blocking", name, f"数据库不存在：{path}"))
        return None, DatabaseSummary(name, path, {}, "missing", 0, 0), issues
    try:
        conn = _readonly(path)
        names = _tables(conn)
        counts = {table: _scalar(conn, f'SELECT COUNT(*) FROM "{table}"') for table in sorted(names)}
        quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        fk_count = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if quick.lower() != "ok":
            issues.append(HealthIssue("quick_check_failed", "blocking", name, quick))
        if fk_count:
            issues.append(HealthIssue("foreign_key_violations", "blocking", name,
                                      "存在外键完整性违规", fk_count))
        return conn, DatabaseSummary(name, path, counts, quick, fk_count, version), issues
    except (OSError, sqlite3.Error) as exc:
        issues.append(HealthIssue("database_unreadable", "blocking", name, str(exc)))
        return None, DatabaseSummary(name, path, {}, "unreadable", 0, 0), issues


def inspect_databases(prod_db_path: str, graph_db_path: str,
                      cache_db_path: str) -> DatabaseHealthReport:
    """Audit all databases independently and return a JSON-serializable report."""
    opened: dict[str, sqlite3.Connection] = {}
    summaries: list[DatabaseSummary] = []
    issues: list[HealthIssue] = []
    metrics: dict[str, int] = {}
    try:
        for name, path in (("production", prod_db_path), ("graph", graph_db_path),
                           ("candidate_cache", cache_db_path)):
            conn, summary, found = _open_summary(name, path)
            summaries.append(summary)
            issues.extend(found)
            if conn is not None:
                opened[name] = conn

        prod = opened.get("production")
        if prod is not None:
            tables = _tables(prod)
            if {"review_chunks", "review_index_meta"} <= tables:
                actual = _scalar(prod, "SELECT COUNT(DISTINCT doc_id) FROM review_chunks WHERE embedding IS NOT NULL")
                reported = _scalar(prod, "SELECT COUNT(*) FROM review_index_meta WHERE embedded=1")
                false_complete = _scalar(prod, """
                    SELECT COUNT(*) FROM review_index_meta m
                    WHERE m.embedded=1 AND NOT EXISTS (
                        SELECT 1 FROM review_chunks c
                        WHERE c.doc_id=m.doc_id AND c.embedding IS NOT NULL)
                """)
                missing_vectors = _scalar(prod, """
                    SELECT COUNT(DISTINCT c.doc_id) FROM review_chunks c
                    WHERE c.embedding IS NULL AND NOT EXISTS (
                        SELECT 1 FROM review_chunks e
                        WHERE e.doc_id=c.doc_id AND e.embedding IS NOT NULL)
                """)
                metrics.update(production_vectorized_docs=actual,
                               production_reported_embedded_docs=reported,
                               production_false_complete_docs=false_complete,
                               production_docs_missing_embeddings=missing_vectors)
                if false_complete:
                    issues.append(HealthIssue("false_complete_index", "blocking", "production",
                                              "索引元数据标为完成，但文献没有实际向量", false_complete))
                if missing_vectors:
                    issues.append(HealthIssue("chunks_without_embeddings", "blocking", "production",
                                              "文献已有分块但没有任何向量", missing_vectors))
                nul_chunks = _scalar(prod, "SELECT COUNT(*) FROM review_chunks WHERE instr(content, char(0)) > 0")
                metrics["production_nul_chunks"] = nul_chunks
                if nul_chunks:
                    issues.append(HealthIssue("nul_in_chunks", "blocking", "production",
                                              "分块正文含 NUL 控制字符，需重分块并重嵌入", nul_chunks))
            if "papers" in tables and "mineru_md" in _columns(prod, "papers"):
                nul_docs = _scalar(prod, "SELECT COUNT(*) FROM papers WHERE instr(mineru_md, char(0)) > 0")
                metrics["production_nul_documents"] = nul_docs
                if nul_docs:
                    issues.append(HealthIssue("nul_in_source_text", "blocking", "production",
                                              "MinerU 正文含 NUL 控制字符", nul_docs))
            if "paper_details" in tables and "doi" in _columns(prod, "paper_details"):
                duplicate_doi = _scalar(prod, """
                    SELECT COUNT(*) FROM (
                        SELECT lower(trim(doi)) FROM paper_details
                        WHERE doi IS NOT NULL AND trim(doi) != ''
                        GROUP BY lower(trim(doi)) HAVING COUNT(*) > 1)
                """)
                metrics["production_duplicate_doi_groups"] = duplicate_doi
                if duplicate_doi:
                    issues.append(HealthIssue("duplicate_doi_groups", "warning", "production",
                                              "存在规范化 DOI 重复组，需人工区分真重复与截断误提取", duplicate_doi))

        graph = opened.get("graph")
        if graph is not None:
            tables = _tables(graph)
            if "citation_edges" in tables:
                unresolved = _scalar(graph, "SELECT COUNT(*) FROM citation_edges WHERE edge_status=?",
                                     (CitationEdgeStatus.UNRESOLVED_EXTERNAL.value,))
                metrics["graph_unresolved_external_edges"] = unresolved
            if {"citation_assertions", "citation_edges"} <= tables:
                orphan = _scalar(graph, """
                    SELECT COUNT(*) FROM citation_assertions a
                    LEFT JOIN citation_edges e ON e.edge_id=a.edge_id
                    WHERE e.edge_id IS NULL
                """)
                metrics["graph_orphan_assertions"] = orphan
                if orphan:
                    issues.append(HealthIssue("orphan_citation_assertions", "blocking", "graph",
                                              "引文断言指向不存在的边，启用外键前必须隔离", orphan))

        if prod is not None and graph is not None and "papers" in _tables(prod) and "works" in _tables(graph):
            prod_ids = {str(row[0]) for row in prod.execute("SELECT id FROM papers")}
            graph_ids = {str(row[0]) for row in graph.execute("SELECT work_id FROM works WHERE in_library=1")}
            missing = len(prod_ids - graph_ids)
            metrics["production_documents_missing_from_graph"] = missing
            if missing:
                issues.append(HealthIssue("production_missing_from_graph", "warning", "graph",
                                          "生产馆藏文献尚未同步到引文图谱", missing))
            if "paper_details" in _tables(prod) and "quarantine_status" in _columns(prod, "paper_details"):
                prod_q = {str(r[0]): str(r[1] or "NORMAL") for r in prod.execute(
                    "SELECT doc_id, quarantine_status FROM paper_details")}
                graph_q = {str(r[0]): str(r[1] or "NORMAL") for r in graph.execute(
                    "SELECT work_id, quarantine_status FROM works WHERE in_library=1")}
                drift = sum(prod_q[k] != graph_q[k] for k in prod_q.keys() & graph_q.keys())
                metrics["quarantine_status_drift"] = drift
                if drift:
                    issues.append(HealthIssue("quarantine_status_drift", "warning", "graph",
                                              "生产解析状态与图谱拓扑资格存在漂移，需按文件 SHA-256 与正文长度复核", drift))

        cache = opened.get("candidate_cache")
        if cache is not None and "discovered_candidates" in _tables(cache):
            cols = _columns(cache, "discovered_candidates")
            if "openalex_id" in cols:
                duplicate = _scalar(cache, """
                    SELECT COUNT(*) FROM (
                        SELECT lower(trim(openalex_id)) FROM discovered_candidates
                        WHERE openalex_id IS NOT NULL AND trim(openalex_id) != ''
                        GROUP BY lower(trim(openalex_id)) HAVING COUNT(*) > 1)
                """)
                metrics["candidate_duplicate_openalex_groups"] = duplicate
                if duplicate:
                    issues.append(HealthIssue("duplicate_candidate_entities", "warning", "candidate_cache",
                                              "同一 OpenAlex Work 跨运行重复出现", duplicate))
            if {"openalex_id", "lifecycle_status"} <= cols:
                conflicts = _scalar(cache, """
                    SELECT COUNT(*) FROM (
                        SELECT lower(trim(openalex_id)) FROM discovered_candidates
                        WHERE openalex_id IS NOT NULL AND trim(openalex_id) != ''
                        GROUP BY lower(trim(openalex_id))
                        HAVING COUNT(DISTINCT lifecycle_status) > 1)
                """)
                metrics["candidate_status_conflict_groups"] = conflicts
                if conflicts:
                    issues.append(HealthIssue("candidate_status_conflicts", "warning", "candidate_cache",
                                              "同一候选实体存在生命周期状态冲突", conflicts))
    finally:
        for conn in opened.values():
            conn.close()

    return DatabaseHealthReport(
        databases=tuple(summaries), issues=tuple(issues), metrics=metrics,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
