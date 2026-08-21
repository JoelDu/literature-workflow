"""Tests for Academic Metadata Resolver and Enrichment Engine."""
import pytest
import sqlite3
import os
from litreview.fetcher import (
    clean_doi,
    normalize_title_for_search,
    calculate_title_similarity,
    fetch_crossref,
    fetch_openalex_by_doi,
    resolve_work_by_title_and_author
)
from litreview.library_store import enrich_document_metadata, get_document_full_detail
from litreview.graph_schema import GRAPH_SCHEMA_DDL


def test_clean_doi():
    assert clean_doi("https://doi.org/10.1021/ie0005349") == "10.1021/ie0005349"
    assert clean_doi("10.1021/ie0005349,") == "10.1021/ie0005349"
    assert clean_doi("doi: 10.15255/cabeq.2020.1866.") == "10.15255/cabeq.2020.1866"
    assert clean_doi("not_a_doi") == ""
    assert clean_doi("") == ""


def test_normalize_title_for_search():
    t1 = "2001 Extraction of cadmium from phosphoric acid"
    assert normalize_title_for_search(t1) == "Extraction of cadmium from phosphoric acid"
    
    t2 = "《理解工业肥料中的结块现象》"
    assert normalize_title_for_search(t2) == "理解工业肥料中的结块现象"


def test_calculate_title_similarity():
    s1 = "Extraction of Cadmium from Phosphoric Acid"
    s2 = "extraction of cadmium from phosphoric acid using resins"
    sim = calculate_title_similarity(s1, s2)
    assert sim > 0.6
    
    s3 = "Completely Unrelated Machine Learning Paper"
    assert calculate_title_similarity(s1, s3) == 0.0


def test_enrich_document_metadata_db_update(tmp_path):
    # Setup test sqlite db
    test_db = str(tmp_path / "test_citation.db")
    conn = sqlite3.connect(test_db)
    conn.executescript(GRAPH_SCHEMA_DDL)
    
    doc_id = "test_doc_001"
    conn.execute("""
        INSERT INTO works (work_id, work_type, canonical_title, publication_year, journal_or_venue, created_at, updated_at)
        VALUES (?, 'journal_article', 'Extraction of cadmium from phosphoric acid using resins impregnated with organophosphorus extractants', 2001, 'Old Venue', '2026-08-19 12:00:00', '2026-08-19 12:00:00')
    """, (doc_id,))
    conn.commit()
    conn.close()
    
    res = enrich_document_metadata(
        doc_id=doc_id,
        prod_db_path="/nonexistent/prod.db",
        test_citation_db_path=test_db,
        cache_db_path=str(tmp_path / "cache.db")
    )
    
    assert res["success"] is True
    assert "enriched_data" in res
    assert "10.1021/ie0005349" in res["enriched_data"]["doi"]
    
    # Check that works and work_identifiers were updated
    conn = sqlite3.connect(test_db)
    w_row = conn.execute("SELECT journal_or_venue, authors_json FROM works WHERE work_id = ?", (doc_id,)).fetchone()
    assert "Industrial & Engineering Chemistry Research" in w_row[0]
    
    id_row = conn.execute("SELECT id_value FROM work_identifiers WHERE work_id = ? AND id_type = 'doi'", (doc_id,)).fetchone()
    assert id_row is not None
    assert id_row[0] == "10.1021/ie0005349"
    conn.close()
