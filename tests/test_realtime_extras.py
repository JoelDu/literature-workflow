"""实时管道补齐（Word 导出、CAJ、锁冲突不判死）+ 索引客户端配置 + 守护进程心跳。

全部使用 tmp_path 与假对象，不发起网络/LLM 请求。
"""
import importlib
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from litreview.analysis_record import PaperAnalysisRecord
from utils import Settings


@pytest.fixture
def intake(tmp_path, monkeypatch):
    import dotenv
    monkeypatch.setattr(dotenv, 'load_dotenv', lambda *a, **kw: False)
    import utils
    settings = Settings(**{name: str(tmp_path / 'root' / name) for name in (
        'INPUT_PDF_DIR', 'MINERU_OUTPUT_DIR', 'OBSIDIAN_VAULT_DIR',
        'PROCESSED_PDF_DIR', 'FAILED_PDF_DIR')},
        DB_PATH=str(tmp_path / 'root' / 'data' / 'papers.db'),
        EXCEL_OUTPUT_PATH=str(tmp_path / 'root' / 'data' / 'notes.xlsx'))
    monkeypatch.setattr(utils, 'get_settings', lambda: settings)
    module = importlib.import_module('pipeline')
    monkeypatch.setattr(module, 'settings', settings)
    for field in ('load_processed_hashes', 'save_processed_hash', 'log_run_event', 'export_to_excel'):
        monkeypatch.setattr(module, field, Mock(return_value=set() if field == 'load_processed_hashes' else None))
    md = '# Test paper\n' + 'Verified scientific content. ' * 40

    def parse(pdf, folder):
        Path(folder).mkdir(parents=True, exist_ok=True)
        (Path(folder) / 'full.md').write_text(md)
        docx = Path(folder) / 'full.docx'
        docx.write_bytes(b'PK docx')
        return {'markdown': md, 'images_dir': folder, 'docx_path': str(docx)}

    miner = Mock()
    miner.process_pdf.side_effect = parse
    monkeypatch.setattr(module, 'MinerUClient', Mock(return_value=miner))
    router = Mock()
    router.analyze_paper.return_value = PaperAnalysisRecord(tldr='result')
    monkeypatch.setattr(module, 'LLMRouter', Mock(return_value=router))
    monkeypatch.setattr(module, 'generate_obsidian_note', Mock(return_value=str(tmp_path / 'note.md')))
    from litreview import indexer
    monkeypatch.setattr(indexer, 'build_index', Mock())
    Path(settings.INPUT_PDF_DIR).mkdir(parents=True)
    return module, settings, router, miner


def test_realtime_copies_mineru_docx_to_word_exports(intake):
    module, settings, _, _ = intake
    (Path(settings.INPUT_PDF_DIR) / 'paper.pdf').write_bytes(b'%PDF test')
    assert module.main() == 1
    word = Path(settings.INPUT_PDF_DIR).parent / 'word_exports' / 'paper.docx'
    assert word.read_bytes() == b'PK docx'


def test_realtime_converts_caj_before_scanning(intake, monkeypatch):
    module, settings, _, miner = intake
    (Path(settings.INPUT_PDF_DIR) / 'thesis.caj').write_bytes(b'CAJ')
    import caj_converter
    calls = []

    def fake_convert(input_dir=None, backup_dir=None):
        calls.append((input_dir, backup_dir))
        src = Path(input_dir) / 'thesis.caj'
        (Path(input_dir) / 'thesis.pdf').write_bytes(b'%PDF converted')
        Path(backup_dir).mkdir(parents=True, exist_ok=True)
        src.rename(Path(backup_dir) / 'thesis.caj')
        return 1, 0

    monkeypatch.setattr(caj_converter, 'convert_caj_files', fake_convert)
    assert module.main() == 1
    assert calls == [(settings.INPUT_PDF_DIR,
                      str(Path(settings.DB_PATH).parent / 'caj_originals'))]
    assert miner.process_pdf.call_count == 1
    assert (Path(settings.PROCESSED_PDF_DIR) / 'thesis.pdf').exists()


def test_realtime_database_locked_is_transient(intake):
    module, settings, router, _ = intake
    pdf = Path(settings.INPUT_PDF_DIR) / 'paper.pdf'
    pdf.write_bytes(b'%PDF test')
    router.analyze_paper.side_effect = sqlite3.OperationalError('database is locked')
    assert module.main() == 0
    assert pdf.exists()
    assert not Path(settings.FAILED_PDF_DIR).exists() or not list(Path(settings.FAILED_PDF_DIR).iterdir())


def test_realtime_other_operational_error_still_permanent(intake):
    module, settings, router, _ = intake
    pdf = Path(settings.INPUT_PDF_DIR) / 'paper.pdf'
    pdf.write_bytes(b'%PDF test')
    router.analyze_paper.side_effect = ValueError('schema broken')
    assert module.main() == 0
    assert not pdf.exists()
    assert (Path(settings.FAILED_PDF_DIR) / 'paper.pdf').exists()


def test_caj_converter_honours_explicit_dirs(tmp_path, monkeypatch):
    import caj_converter
    inp, backup = tmp_path / 'in', tmp_path / 'bk'
    inp.mkdir()
    (inp / 'a.caj').write_bytes(b'CAJ')

    def fake_run(cmd, **_kw):
        Path(cmd[-1]).write_bytes(b'%PDF')
        return Mock(returncode=0, stderr='')

    monkeypatch.setattr(caj_converter.subprocess, 'run', fake_run)
    assert caj_converter.convert_caj_files(str(inp), str(backup)) == (1, 0)
    assert (inp / 'a.pdf').exists() and (backup / 'a.caj').exists()


# ── 索引客户端配置 ─────────────────────────────────────────────────────────

def test_indexer_uses_siliconflow_api_base_with_timeout(monkeypatch):
    from litreview import indexer
    monkeypatch.setenv('SILICONFLOW_API_KEY', 'k')
    monkeypatch.setenv('SILICONFLOW_API_BASE', 'https://new.example/v1')
    monkeypatch.setenv('SILICONFLOW_BASE_URL', 'https://old.example/v1')
    import openai
    fake = Mock()
    monkeypatch.setattr(openai, 'OpenAI', fake)  # 不真正构造 httpx 客户端（环境代理无关）
    indexer._get_default_embed_client()
    fake.assert_called_once_with(api_key='k', base_url='https://new.example/v1',
                                 timeout=indexer._EMBED_TIMEOUT_SECONDS)


def test_indexer_falls_back_to_legacy_base_url_name(monkeypatch):
    from litreview import indexer
    monkeypatch.delenv('SILICONFLOW_API_BASE', raising=False)
    monkeypatch.setenv('SILICONFLOW_BASE_URL', 'https://old.example/v1')
    assert indexer._siliconflow_base(Settings()) == 'https://old.example/v1'
    monkeypatch.delenv('SILICONFLOW_BASE_URL')
    assert indexer._siliconflow_base(Mock()) == indexer._DEFAULT_SF_BASE


# ── 守护进程心跳 ───────────────────────────────────────────────────────────

def test_daemon_heartbeat_lives_next_to_db(tmp_path, monkeypatch):
    daemon = importlib.import_module('daemon')
    settings = Settings(DB_PATH=str(tmp_path / 'data' / 'batch_tracking.db'))
    monkeypatch.setattr(daemon, 'settings', settings)
    monkeypatch.delenv('DAEMON_HEARTBEAT_PATH', raising=False)
    path = daemon.write_heartbeat()
    assert path == str(tmp_path / 'data' / 'heartbeat')
    content = Path(path).read_text()
    assert 'pid=' in content
    monkeypatch.setenv('DAEMON_HEARTBEAT_PATH', str(tmp_path / 'hb' / 'alive'))
    assert daemon.heartbeat_path() == str(tmp_path / 'hb' / 'alive')
