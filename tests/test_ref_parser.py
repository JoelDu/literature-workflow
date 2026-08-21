"""Tests for Reference Parser and Candidate Resolution Engine."""
import json
from pathlib import Path
import pytest
from litreview.ref_parser import (
    parse_reference_mention,
    generate_resolution_candidates,
    make_resolution_decision,
    normalize_title,
    ParsedReference
)


def test_normalize_title():
    assert normalize_title("《磷酸萃取净化工艺研究》.") == "磷酸萃取净化工艺研究"
    assert normalize_title("  “Extraction of H3PO4: A Review”  ") == "extraction of h3po4: a review"
    assert normalize_title("") == ""


def test_parse_reference_chinese_journal():
    raw = "[6] 李正军. 磷酸酯合成工艺的改进及单双酯含量的检测 [J]. 西部皮革, 2007, 29(2): 31-34."
    p = parse_reference_mention(raw)
    assert p.quality_flag == "NORMAL"
    assert p.parse_status == "PARSED_COMPLETE"
    assert p.parsed_authors == "李正军"
    assert p.parsed_title == "磷酸酯合成工艺的改进及单双酯含量的检测"
    assert p.parsed_year == 2007
    assert p.parsed_venue == "西部皮革"


def test_parse_reference_english_doi():
    raw = "7. Chen, M.; Wu, S.; Xu, S. Caking of crystals: Characterization, mechanisms and prevention. Powder Technol. 2017. doi:10.1016/j.powtec.2017.07.086"
    p = parse_reference_mention(raw)
    assert p.quality_flag == "NORMAL"
    assert p.parse_status == "PARSED_COMPLETE"
    assert p.parsed_doi == "10.1016/j.powtec.2017.07.086"
    assert p.parsed_year == 2017
    assert "Caking of crystals" in p.clean_text


def test_parse_reference_broken_fragment():
    raw = "36(5):122-129"
    p = parse_reference_mention(raw)
    assert p.quality_flag == "OCR_CORRUPTED"
    assert p.parse_status == "PARSE_FAILED"

    decision = make_resolution_decision(p, [])
    assert decision.decision_type == "REJECT"
    assert decision.selected_work_id is None


def test_resolution_exact_doi_match():
    p = ParsedReference(
        raw_text="Test ref",
        raw_length=8,
        quality_flag="NORMAL",
        parsed_doi="10.1002/aocs.12360",
        parsed_title="Bio-based amino oil",
        parsed_year=2020
    )
    known = [{
        "work_id": "w_target_01",
        "dois": ["10.1002/aocs.12360"],
        "titles": ["bio-based amino oil"],
        "authors": "Zhang",
        "year": "2020"
    }]
    cands = generate_resolution_candidates(p, known)
    assert len(cands) == 1
    assert cands[0].confidence_score == 1.0
    assert cands[0].match_method == "exact_doi"

    decision = make_resolution_decision(p, cands)
    assert decision.decision_type == "LINK"
    assert decision.selected_work_id == "w_target_01"


def test_resolution_fuzzy_match_routes_to_defer():
    p = ParsedReference(
        raw_text="Test ref",
        raw_length=8,
        quality_flag="NORMAL",
        parsed_doi=None,
        parsed_title="Development of a carbon based slow release fertilizer with enhanced nutrients",
        parsed_year=2021
    )
    known = [{
        "work_id": "w_target_02",
        "dois": [],
        "titles": ["carbon based slow release fertilizer with polymer coating"],
        "authors": "Ye",
        "year": "2021"
    }]
    cands = generate_resolution_candidates(p, known)
    if cands:
        assert cands[0].confidence_score < 0.95
        decision = make_resolution_decision(p, cands)
        assert decision.decision_type == "DEFER"
        assert decision.selected_work_id is None


def test_resolution_out_of_library_no_match():
    p = ParsedReference(
        raw_text="Test ref",
        raw_length=8,
        quality_flag="NORMAL",
        parsed_doi=None,
        parsed_title="Quantum entanglement in photosynthetic light harvesting complexes",
        parsed_year=2015
    )
    known = [{
        "work_id": "w_target_03",
        "dois": ["10.1002/aocs.12360"],
        "titles": ["bio-based amino oil"],
        "authors": "Zhang",
        "year": "2020"
    }]
    cands = generate_resolution_candidates(p, known)
    assert len(cands) == 0
    decision = make_resolution_decision(p, cands)
    assert decision.decision_type in ("NO_MATCH", "NO_MATCH_IN_DEV_CANDIDATE_POOL")
    assert decision.selected_work_id is None
    assert 0.70 <= decision.confidence_score <= 0.85


def test_resolution_english_author_year_title_match():
    """动态测试 #19：验证中英文题名与第一作者+年份的跨语言多键精准召回与候选快照"""
    raw = "Rondeau, X., Affolter, C., Komunjer, L., Clausse, D., and Guigon, P., (2003). Experimental Determination of Capillary Forces by Crushing Strength Measurements, Powder Technology, 130(1-3), 124-131."
    p = parse_reference_mention(raw)
    assert p.parsed_year == 2003
    assert "experimental determination of capillary forces" in p.parsed_title.lower()
    assert "Rondeau" in p.parsed_authors

    known = [{
        "work_id": "1255f7df8b256aba5e7d4a41284d8673b2d75e671cef8f288e99655140a3c9a4",
        "dois": [],
        "titles": ["通过压碎强度测量实验确定毛细力", "Experimental determination of capillary forces by crushing strength measurements"],
        "authors": "X. Rondeau, C. Affolter, L. Komunjer, D. Clausse, P. Guigon",
        "year": "2003"
    }]
    cands = generate_resolution_candidates(p, known)
    assert len(cands) >= 1
    assert cands[0].candidate_work_id.startswith("1255f7df")
    assert cands[0].confidence_score >= 0.95
    assert "evidence" in cands[0].match_evidence or "title" in cands[0].match_evidence or "author" in cands[0].match_evidence

    decision = make_resolution_decision(p, cands)
    assert decision.decision_type == "LINK"
    assert decision.selected_work_id.startswith("1255f7df")
    assert len(decision.candidates) >= 1  # 严禁 LINK 发生时 candidates 为空


def test_dynamic_apa_parsing_corrections():
    """动态测试从 v1.3.3 读取真实生产原文进行的解析纠偏：覆盖 #14, #17, #22, #24, #26, #28"""
    v133_path = Path(__file__).parent / "data" / "citation_dev_silver_v1.3.3.json"
    with open(v133_path, encoding="utf-8") as f:
        data = json.load(f)

    doc_map = {doc["doc_id"][:8]: {r["ref_index"]: r for r in doc["references"]} for doc in data["documents"]}

    # 1. 真实 #14: doc 5324d9dd ref #1 (FAO 2019 / 排除 2020 访问年)
    raw_14 = doc_map["5324d9dd"][1]["raw_text"]
    p_14 = parse_reference_mention(raw_14)
    assert p_14.parsed_year == 2019
    assert "world fertilizer trends and outlook to 2022" in p_14.parsed_title.lower()

    # 2. 真实 #17: doc 9948f9ee ref #28 (多作者分号 / 提取 Cationic Surfactants 题名)
    raw_17 = doc_map["9948f9ee"][28]["raw_text"]
    p_17 = parse_reference_mention(raw_17)
    assert p_17.parsed_year == 1991
    assert "cationic surfactants" in p_17.parsed_title.lower()
    assert "Rubingh" in p_17.parsed_authors

    # 3. 真实 #22: doc 1470d81b ref #83 (完整 DOI: 10.1016/j.tca.2007.07.001)
    raw_22 = doc_map["1470d81b"][83]["raw_text"]
    p_22 = parse_reference_mention(raw_22)
    assert p_22.parsed_year == 2007
    assert p_22.parsed_doi == "10.1016/j.tca.2007.07.001"
    assert "dehydration of iron" in p_22.parsed_title.lower()

    # 4. 真实 #24: doc fdd5482f ref #12 (法文带括号与法语字符完整 96 字符题名断言)
    raw_24 = doc_map["fdd5482f"][12]["raw_text"]
    p_24 = parse_reference_mention(raw_24)
    assert p_24.parsed_year == 2017
    target_full_title_24 = "valorisation du divinylglycol (dvg), dérivé du glycérol, pour la synthèse de polymères originaux"
    assert p_24.parsed_title == target_full_title_24
    assert len(p_24.parsed_title) == 96
    assert "Bonnot" in p_24.parsed_authors

    # 5. 真实 #26: doc 1470d81b ref #68 (排除单字符，提取完整书章题名与 DOI)
    raw_26 = doc_map["1470d81b"][68]["raw_text"]
    p_26 = parse_reference_mention(raw_26)
    assert p_26.parsed_year == 2020
    assert "spray drying as an advantageous strategy" in p_26.parsed_title.lower()
    assert p_26.parsed_doi == "10.1007/s13346-019-00648-9"

    # 6. 真实 #28: doc 9948f9ee ref #193 (多作者分号与外部题名/DOI审校赋能)
    r_28 = doc_map["9948f9ee"][193]
    raw_28 = r_28["raw_text"]
    p_28 = parse_reference_mention(raw_28)
    assert p_28.parsed_year == 1999
    assert "Pauly" in p_28.parsed_authors
    assert "Billinge" in p_28.parsed_authors
    assert r_28["reviewed_title"] == "Textural Mesoporosity and the Catalytic Activity of Mesoporous Molecular Sieves with Wormhole Framework Structures"
    assert r_28["reviewed_doi"] == "10.1021/ja991400t"
    assert r_28["review_reason_code"] == "ENRICH_EXTERNAL_TITLE_AND_DOI"


def test_quality_interception_rules():
    """质量拦截测试：单字符题名拦截、截断 DOI 拦截、OCR DOI 自动修复、作者片段误入题名拦截"""
    # 1. 截断 DOI 拦截 (SUSPECT_DOI_TRUNCATED)
    raw_trunc_doi = "Author, A. (2018). Some Title. Journal Name, 10, 1-10. https://doi.org/10.1016/j"
    p_trunc = parse_reference_mention(raw_trunc_doi)
    assert p_trunc.quality_flag == "SUSPECT_DOI_TRUNCATED"
    assert p_trunc.parsed_doi is None

    # 2. OCR DOI 修复 (10.1016/i. -> 10.1016/j.)
    raw_ocr_doi = "Alfaro-Galarza, O. (2020). Title. LWT, 117. https://doi.org 10.1016/i.lwt.2019.108686"
    p_ocr = parse_reference_mention(raw_ocr_doi)
    assert p_ocr.parsed_doi == "10.1016/j.lwt.2019.108686"
    assert p_ocr.quality_flag == "OCR_DOI_CORRECTED"

    # 3. 单字符题名拦截
    raw_short_t = "Sosnik, A. (2015). h. Polymer Chemistry, 6, 8192."
    p_short = parse_reference_mention(raw_short_t)
    assert p_short.quality_flag == "SUSPECT_TITLE_TOO_SHORT"
    assert p_short.parsed_title is None

    # 4. 作者片段误落入题名拦截 (SUSPECT_TITLE_IS_AUTHOR)
    raw_auth_t = "Smith, J. D. (2019). Zhang, X.; Wang, Y. Journal of Science, 12, 100."
    p_auth = parse_reference_mention(raw_auth_t)
    assert p_auth.quality_flag == "SUSPECT_TITLE_IS_AUTHOR"
    assert p_auth.parsed_title is None
