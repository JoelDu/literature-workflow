"""enrich 本地解析器单元测试（无网络依赖）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from litreview.enrich import parse_content_list, _clean_doi

BLOCKS = [
    {"type": "header", "text": "DOI:10.16606/j.cnki.issn0253-4320.2012.07.028", "page_idx": 0},
    {"type": "text", "text": "复混肥水溶性防结块剂的制备与 性能测试", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "正文段落内容……", "page_idx": 0},
    {"type": "page_footnote", "text": "收稿日期: 2012-03-15", "page_idx": 0},
    {"type": "table", "img_path": "images/aaa.jpg",
     "table_caption": ["表1 水溶性防结块剂理化性质"], "table_footnote": [], "table_body": "<table>...</table>",
     "page_idx": 1},
    {"type": "chart", "img_path": "images/bbb.jpg", "content": "",
     "chart_caption": ["图1 吸湿率变化"], "chart_footnote": [], "page_idx": 2},
    {"type": "image", "img_path": "images/ccc.jpg",
     "image_caption": ["图2 SEM照片"], "page_idx": 3},
    {"type": "ref_text", "text": "［1］ Song C． An overview...", "page_idx": 4},
    {"type": "ref_text", "text": "［2］ 张三. 肥料学报, 2020.", "page_idx": 4},
]


def test_parse_title_and_doi():
    out = parse_content_list(BLOCKS, "论文A_12345678")
    assert out["title"] == "复混肥水溶性防结块剂的制备与 性能测试"
    assert out["doi"] == "10.16606/j.cnki.issn0253-4320.2012.07.028"


def test_parse_assets():
    out = parse_content_list(BLOCKS, "论文A_12345678")
    assert len(out["assets"]) == 3
    types = {a["asset_type"] for a in out["assets"]}
    assert types == {"table", "chart", "image"}
    table = next(a for a in out["assets"] if a["asset_type"] == "table")
    assert table["caption"] == "表1 水溶性防结块剂理化性质"
    # 资源路径会写入 SQLite/Markdown，固定为 POSIX 分隔符，不能随测试宿主机变化。
    assert table["img_path"] == "论文A_12345678/images/aaa.jpg"
    assert table["page_idx"] == 1


def test_parse_refs_ordered():
    out = parse_content_list(BLOCKS, "")
    assert len(out["refs"]) == 2
    assert out["refs"][0].startswith("［1］")
    assert out["refs"][1].startswith("［2］")


def test_clean_doi():
    assert _clean_doi("DOI: 10.1016/j.powtec.2019.05.001.") == "10.1016/j.powtec.2019.05.001"
    assert _clean_doi("无DOI的文本") == ""
    assert _clean_doi("(10.1002/anie.202012345)") == "10.1002/anie.202012345"


def test_empty_blocks():
    out = parse_content_list([], "")
    assert out == {"title": "", "doi": "", "assets": [], "refs": []}


def test_reranker_degrades_gracefully():
    """rerank 接口不可达时应降级为原顺序而不是抛异常。"""
    from litreview.reranker import Reranker
    r = Reranker("dummy-model", api_key="x", base_url="http://127.0.0.1:9")  # 不可达端口
    result = r.rerank("query", ["doc1", "doc2"], top_n=2)
    assert result == [(0, 0.0), (1, 0.0)]
    assert result.degraded is True


def test_parse_openalex_and_crossref_biblio_helpers():
    from litreview.fetcher import parse_openalex_biblio, parse_crossref_biblio

    # 1. OpenAlex page range
    oa_item = {
        "biblio": {
            "volume": "12",
            "issue": "4",
            "first_page": "100",
            "last_page": "115"
        }
    }
    oa_meta = parse_openalex_biblio(oa_item)
    assert oa_meta == {"volume": "12", "issue": "4", "pages": "100-115"}

    # 2. OpenAlex single page
    oa_single = {"biblio": {"volume": "5", "issue": "1", "first_page": "42", "last_page": "42"}}
    assert parse_openalex_biblio(oa_single) == {"volume": "5", "issue": "1", "pages": "42"}

    # 3. OpenAlex empty biblio
    assert parse_openalex_biblio({}) == {"volume": "", "issue": "", "pages": ""}

    # 4. CrossRef standard
    cr_item = {"volume": "450", "issue": "2", "page": "123-134"}
    assert parse_crossref_biblio(cr_item) == {"volume": "450", "issue": "2", "pages": "123-134"}

    # 5. CrossRef article-number fallback
    cr_art = {"volume": "10", "issue": "", "article-number": "104521"}
    assert parse_crossref_biblio(cr_art) == {"volume": "10", "issue": "", "pages": "104521"}


def test_save_enrichment_volume_issue_pages_and_preservation(tmp_path):
    """验证 paper_details 的 volume、issue、pages 首次写入，以及后续部分缺字段时保留旧值。"""
    from litreview.store import VectorStore
    import sqlite3

    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE papers (id TEXT PRIMARY KEY, title TEXT, language TEXT, status TEXT, result_json TEXT)")
    conn.execute("INSERT INTO papers (id, title, status) VALUES ('doc1', 'Test Paper', 'EXPORTED')")
    conn.commit()
    conn.close()

    store = VectorStore(db_path, 4096)
    details = {
        "title": "Test Paper",
        "authors": "Author A, Author B",
        "journal": "Chemical Engineering Journal",
        "year": "2023",
        "volume": "450",
        "issue": "2",
        "pages": "100-110",
        "doi": "10.1016/j.cej.2023.100110",
        "doc_type": "paper",
        "legal_status": "有效",
    }
    store.save_enrichment("doc1", details, assets=[], refs=[])

    meta = store.get_paper_meta(["doc1"])
    assert meta["doc1"]["volume"] == "450"
    assert meta["doc1"]["issue"] == "2"
    assert meta["doc1"]["pages"] == "100-110"
    assert meta["doc1"]["journal"] == "Chemical Engineering Journal"
    assert meta["doc1"]["legal_status"] == "有效"

    # 第二次写入（如轻量重扫，不带 volume/issue/pages/legal_status）：应保留原有值
    details_partial = {
        "title": "Test Paper Renamed",
        "authors": "Author A, Author B",
        "journal": "Chemical Engineering Journal",
        "year": "2023",
        "doi": "10.1016/j.cej.2023.100110",
        "doc_type": "paper",
    }
    store.save_enrichment("doc1", details_partial, assets=[], refs=[])
    meta2 = store.get_paper_meta(["doc1"])
    assert meta2["doc1"]["volume"] == "450"
    assert meta2["doc1"]["issue"] == "2"
    assert meta2["doc1"]["pages"] == "100-110"
    assert meta2["doc1"]["legal_status"] == "有效"


def test_readonly_vectorstore_on_legacy_schema_without_volume(tmp_path):
    """验证 VectorStore(readonly=True) 在旧 schema（无 volume/issue/pages 列）上只读查询时不崩溃且不写 DDL。"""
    from litreview.store import VectorStore
    import sqlite3

    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE papers (id TEXT PRIMARY KEY, title TEXT, language TEXT, status TEXT, result_json TEXT)")
    # 模拟历史旧版 paper_details（完全没有 volume, issue, pages 等列）
    conn.execute("""CREATE TABLE paper_details (
        doc_id TEXT PRIMARY KEY,
        title TEXT,
        authors TEXT,
        journal TEXT,
        year TEXT,
        doi TEXT,
        doc_type TEXT DEFAULT 'paper'
    )""")
    conn.execute("INSERT INTO papers (id, title, status) VALUES ('old_doc', 'Legacy Paper', 'EXPORTED')")
    conn.execute("INSERT INTO paper_details (doc_id, title, journal, year) VALUES ('old_doc', 'Legacy Paper', 'Nature', '2021')")
    conn.commit()
    conn.close()

    # 以只读模式连接旧库
    ro_store = VectorStore(db_path, 4096, readonly=True)
    meta = ro_store.get_paper_meta(["old_doc"])

    assert "old_doc" in meta
    assert meta["old_doc"]["journal"] == "Nature"
    assert meta["old_doc"]["year"] == "2021"
    assert meta["old_doc"]["volume"] == ""
    assert meta["old_doc"]["issue"] == ""
    assert meta["old_doc"]["pages"] == ""

    # 验证旧库的表结构完全未被篡改（未执行 ALTER TABLE DDL）
    chk_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cols = {r[1] for r in chk_conn.execute("PRAGMA table_info(paper_details)").fetchall()}
    chk_conn.close()
    assert "volume" not in cols
    assert "issue" not in cols
    assert "pages" not in cols
