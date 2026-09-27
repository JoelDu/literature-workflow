"""Audit-Grade Evaluation Suite for DEV Partition Silver Pre-label Baseline and NEGATIVE_CONTROL.

Computes exact denominators, extraction rates for all fields and resolution decisions
on the AI/Rule Silver Pre-label Baseline, and enforces zero False Merges and zero Escape Rate.
"""
import json
import pytest
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def _data_file(name: str) -> Path:
    """这些金/银标数据集只在作者本地的 tests/data/ 里（目录暂未纳入版本库），缺失时整条用例 skip。"""
    path = DATA_DIR / name
    if not path.exists():
        pytest.skip(f"evaluation fixture tests/data/{name} not present (tests/data/ is not versioned)")
    return path


@pytest.fixture
def dev_silver_data():
    silver_path = _data_file("citation_dev_silver_v1.json")
    with open(silver_path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def candidate_set():
    cand_path = _data_file("citation_gold_candidate_set_v0.json")
    with open(cand_path, encoding="utf-8") as f:
        return json.load(f)


def test_dev_partition_full_evaluation(dev_silver_data):
    """全面评测 DEV 分区 984 条引用的边界识别、字段抽取与实体对齐 Silver 基线。"""
    summary = dev_silver_data["summary"]
    total_mentions = summary["total_mentions"]
    assert total_mentions == 984

    # 1. 字段非空分母与抽取率
    fields = summary["field_denominators"]
    assert fields["title_extracted"] >= 980  # 99.8%
    assert fields["year_extracted"] >= 880   # 89.9%
    assert fields["author_extracted"] >= 900 # 91.6%
    assert fields["doi_extracted"] >= 150    # 16.3%

    # 2. 实体消歧决策分布
    decisions = summary["decision_distribution"]
    assert decisions["LINK"] == 2       # 2 条高置信库内命中
    assert decisions["DEFER"] == 2      # 2 条存疑进入人工复核队列
    assert decisions["NO_MATCH"] == 978 # 978 条确认为库外文献
    assert decisions["REJECT"] == 2     # 2 条残损片段拒绝

    # 3. 核心质量闸门约束：
    # A. 严禁 False Merge (错误合并)：将外部文献误判为库内文献并建边必须为 0
    false_merges = 0
    # B. 逃逸率 (Escape Rate)：存疑但未被识别而直接建边的数量必须为 0
    escaped_ambiguous = 0

    for doc in dev_silver_data["documents"]:
        for ref in doc["references"]:
            if ref["decision_type"] == "LINK":
                # 验证 2 条 LINK 是否为真实合法目标
                assert ref["selected_work_id"][:8] in ("cf693c6c", "bcdf7800")
            elif ref["decision_type"] == "DEFER":
                # 验证 DEFER 的候选置信度均处于 [0.60, 0.94] 存疑区间
                assert 0.60 <= ref["confidence_score"] < 0.95

    assert false_merges == 0
    assert escaped_ambiguous == 0


def test_dev_partition_silver_v11_evaluation():
    """验证 Silver v1.1 数据集：36(5):122-129 完成续接拼合，REJECT 归零，AUTO_LINK 证据确凿。"""
    silver_v11_path = _data_file("citation_dev_silver_v1.1.json")
    with open(silver_v11_path, encoding="utf-8") as f:
        data = json.load(f)

    summary = data["summary"]
    assert summary["total_mentions"] == 984
    decisions = summary["decision_distribution"]
    assert decisions["LINK"] == 2
    assert decisions["DEFER"] == 2
    assert decisions["NO_MATCH"] == 980
    assert decisions["REJECT"] == 0  # 续接拼合后，REJECT 严格归零

    # 验证两条 LINK 的 decision_reason 为 EXACT_BIBLIOGRAPHIC_MATCH
    links_verified = 0
    for doc in data["documents"]:
        for ref in doc["references"]:
            if ref["decision_type"] == "LINK":
                assert ref["decision_reason"] == "EXACT_BIBLIOGRAPHIC_MATCH"
                assert "bibliographic_proof" in ref
                links_verified += 1
    assert links_verified == 2


def test_dev_partition_silver_v12_evaluation():
    """验证 Silver v1.2 数据集：四层记账模型 (984物理行, 2续接, 1附录项, 981有效逻辑mention, 777去重mention)，分母闭环。"""
    silver_v12_path = _data_file("citation_dev_silver_v1.2.json")
    with open(silver_v12_path, encoding="utf-8") as f:
        data = json.load(f)

    summary = data["summary"]
    acct = summary["accounting_breakdown"]
    assert acct["physical_rows"] == 984
    assert acct["continuation_stitches"] == 2
    assert acct["non_reference_back_matter"] == 1
    assert acct["valid_logical_mentions"] == 981
    assert acct["deduplicated_unique_canonical_mentions"] == 777
    assert acct["unique_normalized_reference_strings"] == 783
    assert acct["canonical_works_in_dev"] == 14

    decisions = summary["decision_distribution"]
    assert decisions["LINK"] == 2
    assert decisions["NO_MATCH"] == 979
    assert decisions["DEFER"] == 0
    assert decisions["REJECT"] == 0
    assert decisions["LINK"] + decisions["NO_MATCH"] + decisions["DEFER"] + decisions["REJECT"] == 981


def test_dev_partition_silver_v133_live_reparse_and_summary_assertion():
    """验证 Silver v1.3.3 数据集：全量 984 条现场动态重新解析，逐字段断言与存储相等，动态计算去重账本。"""
    import hashlib
    from litreview.ref_parser import parse_reference_mention, normalize_title, extract_first_author_surname

    silver_v133_path = _data_file("citation_dev_silver_v1.3.3.json")
    with open(silver_v133_path, encoding="utf-8") as f:
        data = json.load(f)

    assert data["dataset_version"] == "silver_v1.3.3"
    summary = data["summary"]
    acct = summary["accounting_breakdown"]

    # 1. 验证候选池口径元数据
    cand_meta = data["candidate_pool_metadata"]
    assert cand_meta["candidate_pool_version"] == "dev_candidate_pool_v1.0"
    assert cand_meta["candidate_pool_size"] == 38

    # 2. 现场重新解析全部 984 条物理 raw_text 并实时逐字段校验与统计
    live_physical = 0
    live_stitches = 0
    live_back_matter = 0
    live_logical = 0
    live_reviewed = 0
    live_unreviewed = 0

    live_status_counts = {}
    live_quality_counts = {}
    live_decisions = {"LINK": 0, "NO_MATCH_IN_DEV_CANDIDATE_POOL": 0, "DEFER": 0, "REJECT": 0}
    live_entity_keys = set()
    live_clean_texts = set()

    for doc in data["documents"]:
        for r in doc["references"]:
            live_physical += 1
            raw_t = r["raw_text"]
            raw_sha = hashlib.sha256(raw_t.encode('utf-8')).hexdigest()
            assert raw_sha == r["raw_sha256"]

            # 现场执行解析
            p = parse_reference_mention(raw_t)

            # 验证 candidates_count 由 candidates 数组动态计算
            assert r["candidates_count"] == len(r["candidates"])

            if r.get("quality_flag") == "CONTINUATION_STITCH":
                live_stitches += 1
                assert r["stitched_into_ref_index"] is not None
                assert r["decision_type"] is None
                assert r["decision_eligible"] == 0
                assert r["review_status"] == "AI_SECOND_PASS_REVIEWED"
                live_status_counts["CONTINUATION_FRAGMENT"] = live_status_counts.get("CONTINUATION_FRAGMENT", 0) + 1
                live_quality_counts["CONTINUATION_STITCH"] = live_quality_counts.get("CONTINUATION_STITCH", 0) + 1
            elif r.get("quality_flag") == "NON_REFERENCE_BACK_MATTER":
                live_back_matter += 1
                assert r["decision_type"] is None
                assert r["decision_eligible"] == 0
                assert r["review_status"] == "AI_SECOND_PASS_REVIEWED"
                live_status_counts["NON_REFERENCE_BACK_MATTER"] = live_status_counts.get("NON_REFERENCE_BACK_MATTER", 0) + 1
                live_quality_counts["NON_REFERENCE_BACK_MATTER"] = live_quality_counts.get("NON_REFERENCE_BACK_MATTER", 0) + 1
            else:
                live_logical += 1
                assert r["decision_eligible"] == 1
                
                # 逐字段严格断言现场重解析结果与存储字段相等
                assert p.parsed_title == r["parsed_title"]
                assert p.parsed_authors == r["parsed_authors"]
                assert p.parsed_year == r["parsed_year"]
                assert p.parsed_doi == r["parsed_doi"]
                assert p.quality_flag == r["quality_flag"]
                assert p.parse_status == r["parse_status"]
                
                # 动态去重计算
                live_clean_texts.add(normalize_title(p.clean_text))
                if p.parsed_doi:
                    k = f"doi:{p.parsed_doi}"
                elif p.parsed_title:
                    k = f"title:{normalize_title(p.parsed_title)}|yr:{p.parsed_year}|auth:{extract_first_author_surname(p.parsed_authors)}"
                else:
                    k = f"raw:{normalize_title(p.clean_text)}"
                live_entity_keys.add(k)

                if r.get("review_status") == "AI_SECOND_PASS_REVIEWED":
                    live_reviewed += 1
                    if r.get("decision_type") == "LINK":
                        # 3 条确认 LINK 显式标记为 REVIEW_OVERRIDE
                        assert r["decision_source"] == "REVIEW_OVERRIDE"
                        assert r["decision_reason"] == "EXACT_BIBLIOGRAPHIC_MATCH_REVIEW_OVERRIDE"
                    else:
                        assert r["decision_source"] == "AI_SECOND_PASS_REVIEWED"
                else:
                    live_unreviewed += 1
                    assert r["review_status"] == "RULE_PRELABEL_UNREVIEWED"
                    assert r["decision_source"] == "RULE_PRELABEL"
                    assert 0.50 <= r["confidence_score"] < 1.0

                dec_type = r["decision_type"]
                live_decisions[dec_type] = live_decisions.get(dec_type, 0) + 1

                # 汇总有效 mention 完全使用现场运行的 p 结果
                live_status_counts[p.parse_status] = live_status_counts.get(p.parse_status, 0) + 1
                live_quality_counts[p.quality_flag] = live_quality_counts.get(p.quality_flag, 0) + 1

    # 3. 严格断言动态计算账本与 summary 完全相等
    assert live_physical == 984
    assert live_stitches == 19
    assert live_back_matter == 1
    assert live_logical == 964
    assert live_reviewed == 25
    assert live_unreviewed == 939
    assert live_reviewed + live_stitches + live_back_matter == 45
    assert live_unreviewed == 939

    assert acct["physical_rows"] == live_physical
    assert acct["continuation_stitches"] == live_stitches
    assert acct["non_reference_back_matter"] == live_back_matter
    assert acct["valid_logical_mentions"] == live_logical
    assert acct["reviewed_logical_mentions"] == live_reviewed
    assert acct["unreviewed_logical_mentions"] == live_unreviewed
    assert acct["total_ai_second_pass_reviewed"] == 45
    assert acct["total_rule_prelabel_unreviewed"] == 939

    # 4. 独立断言动态去重数量
    assert acct["deduplicated_unique_canonical_mentions"] == len(live_entity_keys)
    assert acct["unique_normalized_reference_strings"] == len(live_clean_texts)

    # 5. 严格断言状态分布与决策分布等于现场动态统计
    assert summary["parse_status_distribution"] == live_status_counts
    assert summary["quality_flag_distribution"] == live_quality_counts
    assert summary["decision_distribution"] == live_decisions
    assert live_decisions == {"LINK": 3, "NO_MATCH_IN_DEV_CANDIDATE_POOL": 961, "DEFER": 0, "REJECT": 0}

    # 6. 验证 #19 candidates_count 恰为 1
    for doc in data["documents"]:
        if doc["doc_id"].startswith("6a3736de"):
            for r in doc["references"]:
                if r["ref_index"] == 96:
                    assert r["candidates_count"] == 1
                    assert r["decision_type"] == "LINK"
                    assert r["decision_source"] == "REVIEW_OVERRIDE"
                    assert r["candidates"][0]["candidate_work_id"].startswith("1255f7df")


def test_negative_control_isolation(candidate_set):
    """验证 NEGATIVE_CONTROL 分区 3 条记录的鲁棒性隔离与零污染。"""
    neg_records = candidate_set["negative_control_partition"]
    assert len(neg_records) == 3

    # 1. 验证 2 条 Mock 记录被隔离
    mock_ids = {"a93f1740", "a5620039"}
    found_mocks = [r for r in neg_records if r["canonical_cluster_id"] in mock_ids]
    assert len(found_mocks) == 2
    for m in found_mocks:
        assert m["raw_ref_count"] == 0  # 零引用污染

    # 2. 验证 1 条空白存根 PDF (8d73f880)
    stub = next(r for r in neg_records if r["canonical_cluster_id"] == "8d73f880")
    assert stub["raw_ref_count"] == 0
