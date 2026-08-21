import os
import sqlite3
import unittest
from unittest.mock import MagicMock
import tempfile
import pandas as pd

from litreview.store import VectorStore
from utils import remove_from_excel, sync_excel_with_db


class TestLiteratureRemoval(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test.db")
        self.excel_path = os.path.join(self.tmp_dir.name, "test.xlsx")
        self.vault_dir = os.path.join(self.tmp_dir.name, "vault")
        self.mineru_dir = os.path.join(self.tmp_dir.name, "mineru_output")
        os.makedirs(self.vault_dir, exist_ok=True)
        os.makedirs(self.mineru_dir, exist_ok=True)
        
        self.store = VectorStore(self.db_path, dim=4)
        
        # Create mock settings object
        self.settings = MagicMock()
        self.settings.DB_PATH = self.db_path
        self.settings.EXCEL_OUTPUT_PATH = self.excel_path
        self.settings.OBSIDIAN_VAULT_DIR = self.vault_dir
        self.settings.MINERU_OUTPUT_DIR = self.mineru_dir
        self.settings.BOOK_OUTPUT_DIR = os.path.join(self.tmp_dir.name, "book_output")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_delete_doc_cascading(self):
        doc_id = "a1b2c3d4e5f67890"
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # Insert test data across tables
        c.execute("INSERT INTO papers (id, title, status) VALUES (?, ?, ?)",
                  (doc_id, "Test Deletion Paper", "EXPORTED"))
        c.execute("INSERT INTO paper_details (doc_id, title, authors, year) VALUES (?, ?, ?, ?)",
                  (doc_id, "Test Deletion Paper", "Author A", "2024"))
        c.execute("INSERT INTO paper_assets (doc_id, asset_type, img_path) VALUES (?, ?, ?)",
                  (doc_id, "image", "attachments/fig1.png"))
        c.execute("INSERT INTO paper_references (doc_id, ref_index, raw_text) VALUES (?, ?, ?)",
                  (doc_id, 1, "Ref A"))
        c.execute("INSERT INTO review_chunks (chunk_id, doc_id, chunk_index, content, char_len) VALUES (?, ?, ?, ?, ?)",
                  (101, doc_id, 0, "Chunk content", 13))
        c.execute("INSERT INTO review_index_meta (doc_id, md_hash, n_chunks, embedded) VALUES (?, ?, ?, ?)",
                  (doc_id, "hash123", 1, 1))
        c.execute("INSERT INTO review_evidence_cache (cache_key, chunk_id, query, score, summary) VALUES (?, ?, ?, ?, ?)",
                  ("k1", 101, "query", 8.5, "summary"))
        conn.commit()
        conn.close()

        # Create files
        note_file = os.path.join(self.vault_dir, f"Test_Paper_{doc_id[:8]}.md")
        with open(note_file, "w", encoding="utf-8") as f:
            f.write("# Test Note")
        
        att_dir = os.path.join(self.vault_dir, "attachments", doc_id)
        os.makedirs(att_dir, exist_ok=True)
        with open(os.path.join(att_dir, "fig1.png"), "w") as f:
            f.write("image bytes")

        mineru_doc_dir = os.path.join(self.mineru_dir, f"Test_Paper_{doc_id[:8]}")
        os.makedirs(mineru_doc_dir, exist_ok=True)

        # Excel
        df = pd.DataFrame([{"文献ID": doc_id, "标题": "Test Deletion Paper"}])
        df.to_excel(self.excel_path, index=False, engine="openpyxl")

        # 1. Dry run
        report_dry = self.store.delete_doc(doc_id, settings=self.settings, dry_run=True)
        self.assertEqual(report_dry["chunks_deleted"], 1)
        self.assertEqual(report_dry["evidence_cache_deleted"], 1)
        self.assertEqual(report_dry["papers_deleted"], 1)
        self.assertTrue(os.path.exists(note_file))

        # 2. Real deletion
        report_real = self.store.delete_doc(doc_id, settings=self.settings, dry_run=False)
        self.assertEqual(report_real["papers_deleted"], 1)
        self.assertEqual(report_real["excel_rows_removed"], 1)

        # Check DB
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM papers WHERE id=?", (doc_id,)).fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_details WHERE doc_id=?", (doc_id,)).fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_chunks WHERE doc_id=?", (doc_id,)).fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_evidence_cache WHERE chunk_id=101").fetchone()[0], 0)
        conn.close()

        # Check Files
        self.assertFalse(os.path.exists(note_file))
        self.assertFalse(os.path.exists(att_dir))
        self.assertFalse(os.path.exists(mineru_doc_dir))


if __name__ == "__main__":
    unittest.main()
