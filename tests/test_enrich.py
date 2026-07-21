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
    assert table["img_path"] == os.path.join("论文A_12345678", "images/aaa.jpg")
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
