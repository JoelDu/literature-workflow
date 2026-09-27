"""Regression coverage uses temporary assets and no external clients."""
import importlib
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from litreview.analysis_record import PaperAnalysisRecord
from litreview.store import VectorStore
from utils import Settings


@pytest.fixture
def intake(tmp_path, monkeypatch):
    import dotenv
    monkeypatch.setattr(dotenv, 'load_dotenv', lambda *a, **kw: False)
    import utils
    settings = Settings(**{name: str(tmp_path / name) for name in (
        'INPUT_PDF_DIR', 'MINERU_OUTPUT_DIR', 'OBSIDIAN_VAULT_DIR',
        'PROCESSED_PDF_DIR', 'FAILED_PDF_DIR')},
        DB_PATH=str(tmp_path / 'papers.db'), EXCEL_OUTPUT_PATH=str(tmp_path / 'notes.xlsx'))
    monkeypatch.setattr(utils, 'get_settings', lambda: settings)
    module = importlib.import_module('pipeline')
    monkeypatch.setattr(module, 'settings', settings)
    for field in ('load_processed_hashes', 'save_processed_hash', 'log_run_event', 'export_to_excel'):
        monkeypatch.setattr(module, field, Mock(return_value=set() if field == 'load_processed_hashes' else None))
    md = '# Test paper\n' + 'Verified scientific content. ' * 40
    def parse(pdf, folder):
        Path(folder).mkdir(parents=True, exist_ok=True)
        (Path(folder) / 'full.md').write_text(md)
        (Path(folder) / 'content_list.json').write_text('[]')
        return {'markdown': md, 'images_dir': folder}
    miner = Mock()
    miner.process_pdf.side_effect = parse
    monkeypatch.setattr(module, 'MinerUClient', Mock(return_value=miner))
    router = Mock()
    router.analyze_paper.return_value = PaperAnalysisRecord(tldr='result')
    monkeypatch.setattr(module, 'LLMRouter', Mock(return_value=router))
    monkeypatch.setattr(module, 'generate_obsidian_note', Mock(return_value=str(tmp_path / 'note.md')))
    Path(settings.INPUT_PDF_DIR).mkdir()
    (Path(settings.INPUT_PDF_DIR) / 'paper.pdf').write_bytes(b'%PDF test')
    return module, settings, router, miner, md


def test_realtime_persists_retrievable_source_and_parse_assets(intake, monkeypatch):
    module, settings, _, _, md = intake
    from litreview import indexer
    seen = []
    def index(**kwargs):
        store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
        seen.extend(store.docs_needing_index(doc_ids=kwargs['doc_ids']))
    monkeypatch.setattr(indexer, 'build_index', index)
    assert module.main() == 1
    assert len(seen) == 1
    assert seen[0][1] == md
    with sqlite3.connect(settings.DB_PATH) as conn:
        row = conn.execute('SELECT images_dir, pdf_path, result_json FROM papers').fetchone()
    assert (Path(row[0]) / 'content_list.json').exists()
    assert Path(row[1]).exists()
    assert 'result' in row[2]


def test_quota_exhaustion_keeps_inputs_and_stops_scan(intake, monkeypatch):
    module, settings, router, miner, _ = intake
    from litreview import quota
    settings.LLM_DAILY_TOKEN_CAP = 0
    router.analyze_paper.side_effect = lambda _: quota.reserve(settings, quota.MINI_MODEL, 'test', 1)
    (Path(settings.INPUT_PDF_DIR) / 'second.pdf').write_bytes(b'%PDF second')
    assert module.main() == 0
    assert len(list(Path(settings.INPUT_PDF_DIR).glob('*.pdf'))) == 2
    assert not list(Path(settings.FAILED_PDF_DIR).glob('*.pdf'))
    assert miner.process_pdf.call_count == 1


def test_book_index_constructs_own_embedding_client(intake, monkeypatch):
    _, settings, _, _, _ = intake
    daemon = importlib.import_module('daemon')
    monkeypatch.setattr(daemon, 'settings', settings)
    monkeypatch.setattr(daemon, 'send_notification', Mock())
    import llm_router
    monkeypatch.setattr(llm_router, 'make_openai_client', Mock(return_value=(object(), 'model')))
    from litreview import bookintake, indexer
    monkeypatch.setattr(bookintake, 'run_scheduled_intake', Mock(return_value=dict(scanned=1, ok=1, skipped=0, failed=0, quarantined=0, deferred=0, pages_used=1)))
    build = Mock()
    monkeypatch.setattr(indexer, 'build_index', build)
    daemon.book_intake_job()
    build.assert_called_once_with(settings=settings, console=daemon.console)


def test_realtime_pdf_can_be_indexed_searched_and_enriched(intake, monkeypatch):
    import numpy as np
    module, settings, _, _, _ = intake
    from litreview import indexer
    settings.EMBEDDING_DIM = 2
    embedder = Mock()
    embedder.embed_texts.side_effect = lambda texts: np.array([[1.0, 0.0]] * len(texts), dtype=np.float32)
    monkeypatch.setattr(indexer.LiteratureIndexer, '_ensure_embedder', lambda _: embedder)
    assert module.main() == 1
    store = VectorStore(settings.DB_PATH, 2)
    found = store.search(np.array([1.0, 0.0], dtype=np.float32))
    assert found
    assert 'Verified scientific content' in found[0].content
    assert store.docs_needing_index() == []
    assert store.docs_needing_enrich()[0][0] == found[0].doc_id
    assert store.get_paper_meta([found[0].doc_id])[found[0].doc_id]['title'] == 'paper'


def test_router_does_not_retry_daily_quota(intake, monkeypatch):
    _, settings, _, _, _ = intake
    from litreview import quota
    from llm_router import LLMRouter
    from tenacity import wait_none
    router = object.__new__(LLMRouter)
    router.openai_model = quota.MINI_MODEL
    router.openai_client = Mock()
    settings.LLM_DAILY_TOKEN_CAP = 0
    reserve = Mock(wraps=quota.reserve)
    monkeypatch.setattr(quota, 'reserve', reserve)
    with pytest.raises(quota.QuotaExceededError):
        router._call_openai.retry_with(wait=wait_none())(router, 'scientific text')
    assert reserve.call_count == 1
    router.openai_client.chat.completions.create.assert_not_called()


def test_quota_deferral_survives_second_scan_and_resumes_next_utc_day(intake, monkeypatch):
    module, settings, router, miner, _ = intake
    from litreview import quota, indexer
    monkeypatch.setattr(quota, '_now', lambda: ('2026-09-25', '2026-09-25T23:58:00+00:00'))
    monkeypatch.setattr(indexer, 'build_index', Mock())
    settings.LLM_DAILY_TOKEN_CAP = 0
    router.analyze_paper.side_effect = lambda _: quota.reserve(settings, quota.MINI_MODEL, 'test', 1)
    assert module.main() == 0
    assert miner.process_pdf.call_count == 1
    assert module.main() == 0
    assert miner.process_pdf.call_count == 1
    assert module.MinerUClient.call_count == 1
    assert module.LLMRouter.call_count == 1
    # An independent process must see the persisted deferral, without cached state.
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-c',
        "from types import SimpleNamespace; from litreview import quota; "
        "import sys; quota._now=lambda: ('2026-09-25', 'unused'); "
        "print(quota.realtime_deferred_until(SimpleNamespace(DB_PATH=sys.argv[1])))",
        settings.DB_PATH], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == '2026-09-26'
    monkeypatch.setattr(quota, '_now', lambda: ('2026-09-26', '2026-09-26T00:00:00+00:00'))
    router.analyze_paper.side_effect = None
    assert module.main() == 1
    assert miner.process_pdf.call_count == 2
