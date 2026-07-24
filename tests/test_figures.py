"""综述插图选图逻辑单元测试（无网络/无真实文件依赖）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from litreview.figures import select_section_figures, _resolve_asset_path, _clean_caption
from litreview.models import OutlineSection, SectionDraft


class FakeStore:
    def __init__(self, assets):
        self._assets = assets

    def get_assets_for_docs(self, doc_ids, captioned_only=True):
        return [a for a in self._assets if a["doc_id"] in doc_ids]


class FakeReranker:
    def __init__(self, scores):
        self._scores = scores  # list aligned with input documents order

    def rerank(self, query, documents, top_n=None):
        ranked = sorted(enumerate(self._scores), key=lambda x: -x[1])
        return ranked[:top_n] if top_n else ranked


class FakeConsole:
    def __init__(self):
        self.lines = []

    def print(self, *a, **k):
        self.lines.append(" ".join(str(x) for x in a))


class FakeSettings:
    def __init__(self, tmp_dir, **overrides):
        self.REVIEW_OUTPUT_DIR = tmp_dir
        self.DB_PATH = os.path.join(tmp_dir, "batch_tracking.db")
        self.MINERU_OUTPUT_DIR = "./mineru_output"
        self.BOOK_OUTPUT_DIR = "./book_output"
        self.REVIEW_INSERT_FIGURES = True
        self.REVIEW_FIGURES_PER_SECTION = 2
        self.REVIEW_FIGURE_MIN_SCORE = 0.2
        for k, v in overrides.items():
            setattr(self, k, v)


def _make_section_and_draft(doc_ids=("doc1abcd",)):
    section = OutlineSection(heading="结块机理", questions=["水分迁移如何影响结块"])
    draft = SectionDraft(heading="结块机理", markdown="正文内容 [@doc1abcd]", cited_doc_ids=list(doc_ids))
    return section, draft


ASSETS = [
    {"doc_id": "doc1abcd", "asset_type": "image", "img_path": "paperA/images/fig1.jpg",
     "caption": "Figure 1. 结块过程示意图", "page_idx": 1},
    {"doc_id": "doc1abcd", "asset_type": "table", "img_path": "paperA/images/tab1.jpg",
     "caption": "Table 1. 水分含量对比", "page_idx": 2},
]


def _write_fake_image(tmp_path, rel_path):
    full = os.path.join(tmp_path, "mineru_output", rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0fake-jpeg")
    return full


def test_resolve_asset_path_found(tmp_path):
    tmp = str(tmp_path)
    _write_fake_image(tmp, "paperA/images/fig1.jpg")
    settings = FakeSettings(tmp)
    resolved = _resolve_asset_path("paperA/images/fig1.jpg", settings)
    assert resolved.endswith("fig1.jpg")
    assert os.path.isfile(resolved)


def test_resolve_asset_path_missing(tmp_path):
    settings = FakeSettings(str(tmp_path))
    assert _resolve_asset_path("nope/images/x.jpg", settings) == ""


def test_resolve_asset_path_found_under_book_output_dir(tmp_path):
    """教材图表存在 BOOK_OUTPUT_DIR（与 MINERU_OUTPUT_DIR 平行）下，也要能解析到。"""
    tmp = str(tmp_path)
    full = os.path.join(tmp, "book_output", "化工原理_deadbeef", "images", "fig1.jpg")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0fake-jpeg")
    settings = FakeSettings(tmp)
    resolved = _resolve_asset_path("化工原理_deadbeef/images/fig1.jpg", settings)
    assert resolved.endswith("fig1.jpg")
    assert os.path.isfile(resolved)


def test_clean_caption_collapses_whitespace():
    assert _clean_caption("Figure  1.\n结块  示意图 ") == "Figure 1. 结块 示意图"
    assert _clean_caption(None) == ""


def test_insert_figures_happy_path(tmp_path):
    tmp = str(tmp_path)
    for a in ASSETS:
        _write_fake_image(tmp, a["img_path"])
    store = FakeStore(ASSETS)
    reranker = FakeReranker([0.9, 0.5])
    settings = FakeSettings(tmp)
    console = FakeConsole()
    section, draft = _make_section_and_draft()
    used = set()

    n = select_section_figures(store, reranker, section, draft, settings, console, used)

    assert n == 2
    assert "assets/doc1abcd_fig1.jpg" in draft.markdown
    assert "assets/doc1abcd_tab1.jpg" in draft.markdown
    assert "结块过程示意图" in draft.markdown
    assert "[@doc1abcd]" in draft.markdown
    assert os.path.isfile(os.path.join(tmp, "assets", "doc1abcd_fig1.jpg"))
    assert len(used) == 2


def test_respects_per_section_limit(tmp_path):
    tmp = str(tmp_path)
    for a in ASSETS:
        _write_fake_image(tmp, a["img_path"])
    store = FakeStore(ASSETS)
    reranker = FakeReranker([0.9, 0.8])
    settings = FakeSettings(tmp, REVIEW_FIGURES_PER_SECTION=1)
    console = FakeConsole()
    section, draft = _make_section_and_draft()

    n = select_section_figures(store, reranker, section, draft, settings, console, set())

    assert n == 1


def test_below_threshold_inserts_nothing(tmp_path):
    tmp = str(tmp_path)
    for a in ASSETS:
        _write_fake_image(tmp, a["img_path"])
    store = FakeStore(ASSETS)
    reranker = FakeReranker([0.1, 0.05])
    settings = FakeSettings(tmp)
    console = FakeConsole()
    section, draft = _make_section_and_draft()
    original_md = draft.markdown

    n = select_section_figures(store, reranker, section, draft, settings, console, set())

    assert n == 0
    assert draft.markdown == original_md


def test_no_reranker_skips_insertion(tmp_path):
    tmp = str(tmp_path)
    store = FakeStore(ASSETS)
    settings = FakeSettings(tmp)
    console = FakeConsole()
    section, draft = _make_section_and_draft()

    n = select_section_figures(store, None, section, draft, settings, console, set())

    assert n == 0


def test_disabled_setting_skips_insertion(tmp_path):
    tmp = str(tmp_path)
    store = FakeStore(ASSETS)
    reranker = FakeReranker([0.9, 0.9])
    settings = FakeSettings(tmp, REVIEW_INSERT_FIGURES=False)
    console = FakeConsole()
    section, draft = _make_section_and_draft()

    n = select_section_figures(store, reranker, section, draft, settings, console, set())

    assert n == 0


def test_used_paths_dedup_across_sections(tmp_path):
    tmp = str(tmp_path)
    for a in ASSETS:
        _write_fake_image(tmp, a["img_path"])
    store = FakeStore(ASSETS)
    settings = FakeSettings(tmp)
    console = FakeConsole()
    used = set()

    section1, draft1 = _make_section_and_draft()
    n1 = select_section_figures(store, FakeReranker([0.9, 0.8]), section1, draft1, settings, console, used)

    section2, draft2 = _make_section_and_draft()
    n2 = select_section_figures(store, FakeReranker([0.9, 0.8]), section2, draft2, settings, console, used)

    assert n1 == 2
    assert n2 == 0
    assert "assets/" not in draft2.markdown


def test_no_cited_doc_ids_skips_insertion(tmp_path):
    tmp = str(tmp_path)
    store = FakeStore(ASSETS)
    reranker = FakeReranker([0.9, 0.9])
    settings = FakeSettings(tmp)
    console = FakeConsole()
    section = OutlineSection(heading="结块机理", questions=[])
    draft = SectionDraft(heading="结块机理", markdown="正文", cited_doc_ids=[])

    n = select_section_figures(store, reranker, section, draft, settings, console, set())

    assert n == 0


def test_missing_file_on_disk_is_skipped_gracefully(tmp_path):
    tmp = str(tmp_path)
    # 只落地第一张图，第二张图的文件缺失（模拟 mineru_output 目录不完整）
    _write_fake_image(tmp, ASSETS[0]["img_path"])
    store = FakeStore(ASSETS)
    reranker = FakeReranker([0.9, 0.8])
    settings = FakeSettings(tmp)
    console = FakeConsole()
    section, draft = _make_section_and_draft()

    n = select_section_figures(store, reranker, section, draft, settings, console, set())

    assert n == 1
    assert "fig1.jpg" in draft.markdown
    assert "tab1.jpg" not in draft.markdown


def test_rerank_failure_is_caught(tmp_path):
    tmp = str(tmp_path)
    for a in ASSETS:
        _write_fake_image(tmp, a["img_path"])

    class BrokenReranker:
        def rerank(self, *a, **k):
            raise RuntimeError("网络不可达")

    store = FakeStore(ASSETS)
    settings = FakeSettings(tmp)
    console = FakeConsole()
    section, draft = _make_section_and_draft()
    original_md = draft.markdown

    n = select_section_figures(store, BrokenReranker(), section, draft, settings, console, set())

    assert n == 0
    assert draft.markdown == original_md
