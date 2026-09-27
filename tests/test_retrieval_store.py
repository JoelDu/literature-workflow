import hashlib
import sqlite3
from types import SimpleNamespace

import numpy as np

from litreview.chunker import Chunk
from litreview.graph_schema import init_graph_schema
from litreview.pipeline import _ensure_vector_tables_in_test_db
from litreview.retrieval_store import create_retrieval_store
from litreview.store import VectorStore


def test_discovery_is_searchable_citable_isolated_and_readonly(tmp_path):
    primary_path, discovery = tmp_path/'primary.db', tmp_path/'discovery.db'
    primary = VectorStore(str(primary_path), 2)
    with sqlite3.connect(primary_path) as conn:
        conn.execute("INSERT INTO papers(id,title,status) VALUES('p','Primary','EXPORTED')")
    ids = primary.replace_doc_chunks('p', 'hash', [Chunk('p', 0, 'Body', 'primary evidence')])
    primary.save_embeddings(ids, np.array([[0,1]], dtype=np.float32), 'real')
    primary.mark_embedded('p')
    with sqlite3.connect(discovery) as conn:
        init_graph_schema(conn)
        _ensure_vector_tables_in_test_db(conn)
        for name, model, status in [('good','real','NORMAL'), ('fake','qwen3-embedding-test','NORMAL'), ('dirty','real','QUARANTINED')]:
            conn.execute("""INSERT INTO works(work_id,canonical_title,in_library,eligible_for_retrieval,
                quarantine_status,authors_json,publication_year,journal_or_venue,created_at,updated_at)
                VALUES(?,?,1,1,?,'["Author"]',2026,'Journal','now','now')""", (name,name,status))
            conn.execute("INSERT INTO work_identifiers(work_id,id_type,id_value,is_primary,created_at) VALUES(?,'doi',?,1,'now')", (name,'10.1/'+name))
            conn.execute("INSERT INTO review_index_meta VALUES(?,'hash',1,?,'now')", (name,model))
            conn.execute("INSERT INTO review_chunks(doc_id,chunk_index,content,char_len,embedding,embedding_model) VALUES(?,0,?,8,?,?)", (name,name+' evidence',np.array([1,0],dtype=np.float32).tobytes(),model))
    settings = SimpleNamespace(DB_PATH=str(primary_path), EMBEDDING_DIM=2, EMBEDDING_MODEL='real')
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in [primary_path, discovery]]
    store = create_retrieval_store(settings, readonly=True, discovery_db_path=str(discovery))
    hits = store.search(np.array([1,0], dtype=np.float32))
    assert [h.doc_id for h in hits] == ['good','p']
    assert hits[0].chunk_id == -1 and hits[1].chunk_id == 1
    assert store.get_paper_meta(['good'])['good']['doi'] == '10.1/good'
    assert store.get_paper_meta(['good'])['good']['authors'] == 'Author'
    assert set(store.doc_mean_vectors()) == {'good','p'}
    assert store.search(np.array([1,0]), doc_types=['book']) == []
    store.put_evidence_cache(-1, 'good evidence','q','v','m','s',4,'summary')
    assert store.get_evidence_cache(1,'primary evidence','q','v','m','s') is None
    assert store.get_evidence_cache(-1,'good evidence','q','v','m','s')['score'] == 4
    fingerprint = store.get_corpus_fingerprint()
    assert before == [hashlib.sha256(p.read_bytes()).hexdigest() for p in [primary_path, discovery]]
    with sqlite3.connect(discovery) as conn:
        conn.execute("UPDATE works SET quarantine_status='QUARANTINED' WHERE work_id='good'")
    assert store.get_corpus_fingerprint() != fingerprint
    assert [h.doc_id for h in store.search(np.array([1,0]))] == ['p']


def test_two_discovery_works_survive_writing_and_reference_assembly(tmp_path, monkeypatch):
    from litreview import stages
    from litreview.models import Evidence, Outline, OutlineSection
    primary_path, discovery = tmp_path/'main.db', tmp_path/'discovery.db'
    VectorStore(str(primary_path), 2)
    docs = ['work_oa_w10001', 'work_oa_w10002']
    with sqlite3.connect(discovery) as conn:
        init_graph_schema(conn)
        _ensure_vector_tables_in_test_db(conn)
        for i, doc in enumerate(docs, 1):
            conn.execute("""INSERT INTO works(work_id,canonical_title,in_library,eligible_for_retrieval,
                quarantine_status,authors_json,publication_year,journal_or_venue,created_at,updated_at)
                VALUES(?,?,1,1,'NORMAL','["Author"]',2026,'Journal','now','now')""", (doc,f'Paper {i}'))
            conn.execute("INSERT INTO work_identifiers(work_id,id_type,id_value,is_primary,created_at) VALUES(?,'doi',?,1,'now')", (doc,f'10.1234/paper{i}'))
            conn.execute("INSERT INTO review_index_meta VALUES(?,'hash',1,'real','now')", (doc,))
            conn.execute("INSERT INTO review_chunks(doc_id,chunk_index,content,char_len,embedding,embedding_model) VALUES(?,0,'Scientific finding',18,?,'real')", (doc,np.array([1,0],dtype=np.float32).tobytes()))
    settings = SimpleNamespace(DB_PATH=str(primary_path), EMBEDDING_DIM=2, EMBEDDING_MODEL='real')
    store = create_retrieval_store(settings, readonly=True, discovery_db_path=str(discovery))
    hits = store.search(np.array([1,0],dtype=np.float32))
    evidence = [Evidence(chunk_id=h.chunk_id, doc_id=h.doc_id, score=5, summary=h.content) for h in hits]
    meta = store.get_paper_meta()
    assert [meta[d]['cite_key'] for d in docs] == docs
    def chat(client, model, prompt, **kwargs):
        assert all(f'[@{doc}]' in prompt for doc in docs)
        return f'First finding [@{docs[0]}]. Second finding [@{docs[1]}]. Ambiguous [@work_oa_].'
    monkeypatch.setattr(stages, '_chat', chat)
    section = OutlineSection(heading='Findings')
    draft = stages.write_section(section, evidence, meta, 'Review', None, 'mock')
    assert draft.cited_doc_ids == docs
    assert '[@work_oa_]' not in draft.markdown
    review = stages.assemble_review(Outline(title='Review',topic='Topic',sections=[section]),
                                    [draft], 'Introduction.', 'Conclusion.', meta, len(evidence))
    assert review.reference_doc_ids == docs
    assert '10.1234/paper1' in review.references[0]
    assert '10.1234/paper2' in review.references[1]
    assert 'First finding [1]' in review.sections[0].markdown
    assert 'Second finding [2]' in review.sections[0].markdown


def test_ambiguous_aliases_never_point_to_another_paper(monkeypatch):
    from litreview import stages
    from litreview.models import Evidence, OutlineSection
    docs = ['abcdefgh111', 'abcdefgh222']
    meta = {doc: {'title': doc, 'cite_key': 'duplicate'} for doc in docs}
    monkeypatch.setattr(stages, '_chat', lambda *a, **k:
                        'Wrong [@abcdefgh] and [@duplicate]. Correct [@abcdefgh111].')
    # Even when just one colliding document is in this chapter, shared aliases are unsafe.
    draft = stages.write_section(OutlineSection(heading='One'),
        [Evidence(chunk_id=1,doc_id=docs[0],score=5,summary='Evidence')], meta, 'Review', None, 'mock')
    assert draft.cited_doc_ids == [docs[0]]
    assert '[@abcdefgh]' not in draft.markdown
    assert '[@duplicate]' not in draft.markdown
