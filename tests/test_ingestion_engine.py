import os
import tempfile
import pytest
from litreview.ingestion_engine import (
    verify_document_file,
    DocumentIngestionEngine,
    IngestionStrategy,
    VerificationResult,
    IngestionResult
)


def test_verify_document_file_non_existent():
    res = verify_document_file("/path/to/non_existent_file.pdf")
    assert res.is_valid is False
    assert "不存在" in res.error_reason


def test_verify_document_file_invalid_pdf():
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"NOT A REAL PDF CONTENT WITH SUFFICIENT BYTES TO PASS SIZE CHECK " * 50)
        f_path = f.name

    try:
        res = verify_document_file(f_path)
        assert res.is_valid is False
        assert "缺少 %PDF-" in res.error_reason or "结构损坏" in res.error_reason
    finally:
        if os.path.exists(f_path):
            os.remove(f_path)


def test_verify_document_file_valid_pdf():
    # Use one of our actual PDFs in tests or processed_pdfs
    pdf_path = None
    for root, _, files in os.walk("processed_pdfs"):
        for file in files:
            if file.endswith(".pdf"):
                pdf_path = os.path.join(root, file)
                break
        if pdf_path:
            break

    if pdf_path and os.path.exists(pdf_path):
        res = verify_document_file(pdf_path)
        assert res.is_valid is True
        assert len(res.sha256) == 64
        assert res.page_count >= 1
        assert res.magic_valid is True


class DummySuccessStrategy(IngestionStrategy):
    def acquire(self, source):
        return source

    def verify(self, file_path):
        return VerificationResult(
            is_valid=True, sha256="dummy_sha256", file_size=1024,
            page_count=5, magic_valid=True
        )

    def parse(self, file_path, verification):
        return {"chunks": [{"content": "sample text", "index": 0}]}

    def vectorize(self, parsed_data):
        return {"embeddings": [[0.1] * 4096]}

    def persist(self, file_path, parsed_data, vectorized_data):
        return IngestionResult(
            success=True,
            doc_id="dummy_sha256",
            stage="VECTOR_INDEXED",
            metadata={"chunks_count": 1}
        )


def test_document_ingestion_engine_lifecycle():
    strategy = DummySuccessStrategy()
    engine = DocumentIngestionEngine(strategy)
    result = engine.execute("dummy_source.pdf")

    assert result.success is True
    assert result.doc_id == "dummy_sha256"
    assert result.stage == "VECTOR_INDEXED"
    assert result.metadata["chunks_count"] == 1
