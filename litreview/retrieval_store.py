"""Read discovery evidence alongside the main library without merging schemas."""
import hashlib
import json
import os
from pathlib import Path

import numpy as np

from .sqlite_runtime import connect_sqlite
from .store import ScoredChunk, VectorStore


class RetrievalStore:
    def __init__(self, primary, discovery_db_path, model):
        self.primary = primary
        self.discovery_db_path = discovery_db_path
        self.model = model
        self._cache = {}

    def __getattr__(self, name):
        return getattr(self.primary, name)

    def _discovery(self):
        path = self.discovery_db_path
        if not path or not os.path.isfile(path) or os.path.realpath(path) == os.path.realpath(self.db_path):
            return [], {}
        conn = connect_sqlite(path, readonly=True)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'works', 'review_chunks', 'review_index_meta', 'work_identifiers'} <= tables:
                return [], {}
            rows = conn.execute("""SELECT c.chunk_id,c.doc_id,c.section_title,c.content,c.embedding,
                       w.canonical_title,w.authors_json,w.journal_or_venue,w.publication_year,
                       (SELECT id_value FROM work_identifiers i WHERE i.work_id=w.work_id
                        AND i.id_type='doi' ORDER BY is_primary DESC LIMIT 1)
                FROM review_chunks c JOIN works w ON w.work_id=c.doc_id
                JOIN review_index_meta m ON m.doc_id=c.doc_id
                WHERE w.in_library=1 AND w.is_deleted=0
                  AND w.eligible_for_retrieval=1 AND w.quarantine_status='NORMAL'
                  AND m.embedding_model=? AND c.embedding_model=?
                  AND c.embedding IS NOT NULL
                  AND m.chunk_count=(SELECT COUNT(*) FROM review_chunks all_chunks WHERE all_chunks.doc_id=c.doc_id)
                  AND NOT EXISTS (SELECT 1 FROM review_chunks bad WHERE bad.doc_id=c.doc_id
                    AND (bad.embedding IS NULL OR bad.embedding_model IS NULL
                         OR bad.embedding_model != ?))
                ORDER BY c.doc_id,c.chunk_index""", (self.model, self.model, self.model)).fetchall()
        finally:
            conn.close()
        chunks, meta = [], {}
        # Production owns duplicate document IDs, so evidence and citation metadata agree.
        primary_ids = set(self.primary.get_paper_meta())
        invalid_docs = set()
        for cid, doc, section, content, blob, title, authors, journal, year, doi in rows:
            if doc in primary_ids:
                continue
            vec = np.frombuffer(blob, dtype=np.float32) if len(blob) % 4 == 0 else np.array([])
            if len(vec) != self.dim or not np.isfinite(vec).all() or np.linalg.norm(vec) == 0:
                invalid_docs.add(doc)
                continue
            try:
                names = json.loads(authors or '[]')
                if isinstance(names, list):
                    authors = ', '.join(str(a.get('display_name') or a.get('name') or '') if isinstance(a, dict) else str(a) for a in names)
            except (ValueError, TypeError):
                pass
            chunks.append((ScoredChunk(-int(cid), doc, section, content, 0.0), vec / np.linalg.norm(vec)))
            meta[doc] = dict(title=title or '', title_en=title or '', authors=authors or '',
                             journal=journal or '', year=str(year or ''), doi=doi or '',
                             doc_type='paper', language='en', tldr='', keywords='', cite_key=doc)
        return [(c, v) for c, v in chunks if c.doc_id not in invalid_docs], {d:m for d,m in meta.items() if d not in invalid_docs}

    def search(self, qvec, top_k=24, max_per_doc=4, doc_types=None):
        hits = self.primary.search(qvec, top_k=top_k, max_per_doc=max_per_doc, doc_types=doc_types)
        if doc_types is None or 'paper' in doc_types:
            chunks, _ = self._discovery()
            for chunk, vec in chunks:
                chunk.score = float(vec @ qvec)
                hits.append(chunk)
        results, counts = [], {}
        for chunk in sorted(hits, key=lambda c: c.score, reverse=True):
            if counts.get(chunk.doc_id, 0) >= max_per_doc:
                continue
            counts[chunk.doc_id] = counts.get(chunk.doc_id, 0) + 1
            results.append(chunk)
            if len(results) >= top_k:
                break
        return results

    def get_paper_meta(self, doc_ids=None):
        _, meta = self._discovery()
        if doc_ids:
            meta = {d:m for d,m in meta.items() if d in doc_ids}
        meta.update(self.primary.get_paper_meta(doc_ids))
        return meta

    def doc_mean_vectors(self):
        chunks, _ = self._discovery()
        groups = {}
        for chunk, vec in chunks:
            groups.setdefault(chunk.doc_id, []).append(vec)
        out = {}
        for doc, vecs in groups.items():
            vec = np.mean(vecs, axis=0)
            norm = np.linalg.norm(vec)
            out[doc] = vec / norm if norm else vec
        out.update(self.primary.doc_mean_vectors())
        return out

    def get_corpus_fingerprint(self):
        chunks, meta = self._discovery()
        digest = hashlib.sha256(str(self.primary.get_corpus_fingerprint()).encode())
        digest.update(json.dumps(meta, sort_keys=True, ensure_ascii=False).encode())
        for chunk, vec in chunks:
            digest.update(json.dumps([chunk.chunk_id, chunk.doc_id, chunk.content]).encode())
            digest.update(vec.tobytes())
        return digest.hexdigest()

    def get_evidence_cache(self, chunk_id, content, query, prompt_version, model_name, schema_version):
        if self.primary.readonly:
            key, _ = self.evidence_cache_key(content, query, prompt_version, model_name, schema_version)
            return self._cache.get(key)
        return self.primary.get_evidence_cache(chunk_id, content, query, prompt_version, model_name, schema_version)

    def put_evidence_cache(self, chunk_id, content, query, prompt_version, model_name, schema_version, score, summary):
        if self.primary.readonly:
            key, _ = self.evidence_cache_key(content, query, prompt_version, model_name, schema_version)
            self._cache[key] = {'score': score, 'summary': summary}
        else:
            self.primary.put_evidence_cache(chunk_id, content, query, prompt_version, model_name, schema_version, score, summary)


def discovery_db_path():
    return (os.getenv('TEST_CITATION_DB_PATH') or os.getenv('CITATION_NETWORK_DB_PATH')
            or str(Path(__file__).resolve().parent.parent / 'data' / 'test_citation_network.db'))


def create_retrieval_store(settings, readonly=False, discovery_db_path=None):
    path = discovery_db_path if discovery_db_path is not None else globals()['discovery_db_path']()
    return RetrievalStore(VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM, readonly=readonly),
                          path, settings.EMBEDDING_MODEL)
