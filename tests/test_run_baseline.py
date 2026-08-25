"""测试 Phase 1C-A: 综述可信基线注册表与 CLI 闭环 (tests/test_run_baseline.py)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from litreview.run_baseline import (
    BaselineCheckReport,
    BaselineListReport,
    BaselinePromoteReport,
    check_review_candidate,
    compute_topic_key,
    list_review_baselines,
    promote_review_baseline,
)
from litreview.run_validation import _calc_file_sha256, canonicalize_topic, compare_review_runs
from tests.test_run_validation import _assert_no_path_leak_recursive, _create_synthetic_run


def test_compute_topic_key_normalization_and_whitespace_folding():
    """测试主题 Key 计算：NFKC 标准化、大小写折叠、多余空白合并与空值防护。"""
    k1 = compute_topic_key("微塑料 降解   机理研究")
    k2 = compute_topic_key("  微塑料  降解 机理研究  \n\t")
    assert k1 and k1 == k2
    assert len(k1) == 64

    # 英文大小写与 NFKC 全角字符折叠
    k_en1 = compute_topic_key("Microplastic Degradation Mechanisms")
    k_en2 = compute_topic_key("microplastic   degradation   mechanisms")
    k_en_full = compute_topic_key("ｍｉｃｒｏｐｌａｓｔｉｃ degradation mechanisms")
    assert k_en1 == k_en2 == k_en_full

    # 空白或非法输入
    assert compute_topic_key("") == ""
    assert compute_topic_key("   \n\t  ") == ""
    assert compute_topic_key(None) == ""


def test_promote_first_time_no_existing_registry(tmp_path):
    """测试首次提升基线：注册表文件尚不存在时安全创建，且结构符合 schema_version=1。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    run_dir = out_root / "2026-08-25_topic"
    manifest_path, _, _ = _create_synthetic_run(run_dir, run_id="r1", topic="微塑料生物降解")

    reg_path = str(out_root / "baselines.registry.json")
    report = promote_review_baseline(manifest_path, reg_path, str(out_root))

    assert report.verdict == "PASS"
    assert report.topic == "微塑料生物降解"
    assert report.topic_key == compute_topic_key("微塑料生物降解")
    assert report.manifest_locator == "2026-08-25_topic/综述_r1.manifest.json"
    assert report.manifest_sha256 == _calc_file_sha256(manifest_path)
    assert report.run_id == "r1"
    assert report.validation_verdict == "PASS"
    assert os.path.exists(reg_path)

    with open(reg_path, "r", encoding="utf-8") as f:
        reg_data = json.load(f)
    assert reg_data["schema_version"] == 1
    assert report.topic_key in reg_data["baselines"]
    entry = reg_data["baselines"][report.topic_key]
    assert entry["manifest_locator"] == "2026-08-25_topic/综述_r1.manifest.json"
    assert entry["validation_verdict"] == "PASS"


def test_promote_warn_default_rejected_and_allow_warn_succeeds(tmp_path):
    """测试 WARN 清单默认拒绝提升；指定 allow_warn=True 时允许提升并记录 WARN。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    run_dir = out_root / "warn_run"
    manifest_path, _, _ = _create_synthetic_run(run_dir, run_id="w1", topic="警告测试", git_dirty=True)

    reg_path = str(out_root / "baselines.registry.json")

    # 1. 默认拒绝
    rep_rej = promote_review_baseline(manifest_path, reg_path, str(out_root), allow_warn=False)
    assert rep_rej.verdict == "FAIL"
    assert any(iss.code == "PROMOTION_REJECTED_WARN" for iss in rep_rej.issues)
    assert not os.path.exists(reg_path)

    # 2. 显式允许 allow_warn
    rep_ok = promote_review_baseline(manifest_path, reg_path, str(out_root), allow_warn=True)
    assert rep_ok.verdict == "WARN"
    assert rep_ok.validation_verdict == "WARN"
    assert os.path.exists(reg_path)

    with open(reg_path, "r", encoding="utf-8") as f:
        reg_data = json.load(f)
    assert reg_data["baselines"][rep_ok.topic_key]["validation_verdict"] == "WARN"


def test_promote_validation_fail_strictly_rejected_and_registry_intact(tmp_path):
    """测试 FAIL 清单坚决拒绝提升，且绝不篡改已有注册表内容。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    # 先注册一个正常基线
    good_dir = out_root / "good_run"
    good_m, _, _ = _create_synthetic_run(good_dir, run_id="good_1", topic="已有主题")
    promote_review_baseline(good_m, reg_path, str(out_root))

    with open(reg_path, "r", encoding="utf-8") as f:
        original_reg_content = f.read()

    # 构造一个损坏的 FAIL 清单
    fail_dir = out_root / "fail_run"
    fail_m, md_path, _ = _create_synthetic_run(fail_dir, run_id="fail_1", topic="失败主题")
    with open(md_path, "a", encoding="utf-8") as f:
        f.write("\ntampered\n")

    rep_fail = promote_review_baseline(fail_m, reg_path, str(out_root), allow_warn=True)
    assert rep_fail.verdict == "FAIL"
    assert any(iss.code == "PROMOTION_REJECTED_VALIDATION_FAILED" for iss in rep_fail.issues)

    # 确认旧注册表毫发无损
    with open(reg_path, "r", encoding="utf-8") as f:
        current_reg_content = f.read()
    assert current_reg_content == original_reg_content


def test_promote_empty_topic_rejected(tmp_path):
    """测试主题为空或纯空格时拒绝提升为基线。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    run_dir = out_root / "empty_topic_run"
    manifest_path, _, _ = _create_synthetic_run(run_dir, run_id="e1", topic="   ")
    reg_path = str(out_root / "baselines.registry.json")

    rep = promote_review_baseline(manifest_path, reg_path, str(out_root))
    assert rep.verdict == "FAIL"
    assert any(iss.code == "EMPTY_TOPIC" for iss in rep.issues)


def test_promote_same_topic_atomic_replacement(tmp_path):
    """测试相同 topic 多次提升时原子替换旧基线，注册表中该 topic 保持单一条目。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    run_dir_1 = out_root / "run_v1"
    m1, _, _ = _create_synthetic_run(run_dir_1, run_id="r1", topic="微塑料")
    promote_review_baseline(m1, reg_path, str(out_root))

    run_dir_2 = out_root / "run_v2"
    m2, _, _ = _create_synthetic_run(run_dir_2, run_id="r2", topic="微塑料")
    promote_review_baseline(m2, reg_path, str(out_root))

    with open(reg_path, "r", encoding="utf-8") as f:
        reg_data = json.load(f)

    assert len(reg_data["baselines"]) == 1
    tkey = compute_topic_key("微塑料")
    assert reg_data["baselines"][tkey]["run_id"] == "r2"
    assert reg_data["baselines"][tkey]["manifest_locator"] == "run_v2/综述_r2.manifest.json"


def test_promote_different_topics_coexist_and_deterministic_list(tmp_path):
    """测试不同 topic 并存，list_review_baselines 确定性按 topic 名称排序输出。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    m_beta, _, _ = _create_synthetic_run(out_root / "beta", run_id="b1", topic="Beta 主题")
    m_alpha, _, _ = _create_synthetic_run(out_root / "alpha", run_id="a1", topic="Alpha 主题")

    promote_review_baseline(m_beta, reg_path, str(out_root))
    promote_review_baseline(m_alpha, reg_path, str(out_root))

    list_rep = list_review_baselines(reg_path, str(out_root))
    assert list_rep.verdict == "PASS"
    assert list_rep.total_baselines == 2
    assert [b.topic for b in list_rep.baselines] == ["Alpha 主题", "Beta 主题"]


def test_list_when_registry_not_found_returns_empty_pass(tmp_path):
    """测试注册表文件不存在时 list_review_baselines 诚实返回空列表及 PASS。"""
    out_root = str(tmp_path / "reviews")
    reg_path = str(tmp_path / "reviews" / "non_existent.registry.json")
    rep = list_review_baselines(reg_path, out_root)
    assert rep.verdict == "PASS"
    assert rep.total_baselines == 0
    assert rep.baselines == []


def test_check_when_no_registry_or_no_topic_baseline_returns_warn_no_baseline(tmp_path):
    """测试无注册表或未注册该主题时 check_review_candidate 诚实返回 WARN (status=no_baseline)。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    cand_dir = out_root / "cand_run"
    cand_m, _, _ = _create_synthetic_run(cand_dir, run_id="c1", topic="未注册主题")

    # 1. 注册表文件尚不存在
    chk1 = check_review_candidate(cand_m, reg_path, str(out_root))
    assert chk1.verdict == "WARN"
    assert chk1.status == "no_baseline"
    assert any(iss.code == "NO_BASELINE" for iss in chk1.issues)

    # 2. 注册表存在但注册了其他主题
    other_m, _, _ = _create_synthetic_run(out_root / "other_run", run_id="o1", topic="其他主题")
    promote_review_baseline(other_m, reg_path, str(out_root))

    chk2 = check_review_candidate(cand_m, reg_path, str(out_root))
    assert chk2.verdict == "WARN"
    assert chk2.status == "no_baseline"
    assert any(iss.code == "NO_BASELINE" for iss in chk2.issues)


def test_check_automatic_comparison_pass(tmp_path):
    """测试候选与基线完全一致时自动比对返回 PASS。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    base_dir = out_root / "base_run"
    base_m, _, _ = _create_synthetic_run(base_dir, run_id="b1", topic="相同主题")
    promote_review_baseline(base_m, reg_path, str(out_root))

    cand_dir = out_root / "cand_run"
    cand_m, _, _ = _create_synthetic_run(cand_dir, run_id="c1", topic="相同主题")

    chk = check_review_candidate(cand_m, reg_path, str(out_root))
    assert chk.verdict == "PASS"
    assert chk.status == "compared"
    assert chk.comparison_report is not None
    assert chk.comparison_report.verdict == "PASS"


def test_check_reference_replacement_warns_and_creates_delta(tmp_path):
    """测试候选引用文献集合等量替换时 check 得到 WARN 并包含 references delta。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    base_dir = out_root / "base_run"
    base_m, _, _ = _create_synthetic_run(base_dir, run_id="b1", topic="引用演进", doc_ids=["doc_1", "doc_2"])
    promote_review_baseline(base_m, reg_path, str(out_root))

    cand_dir = out_root / "cand_run"
    cand_m, _, _ = _create_synthetic_run(cand_dir, run_id="c1", topic="引用演进", doc_ids=["doc_3", "doc_4"])

    chk = check_review_candidate(cand_m, reg_path, str(out_root))
    assert chk.verdict == "WARN"
    assert chk.status == "compared"
    assert any(d.category == "references" for d in chk.comparison_report.deltas)


def test_check_objective_regression_fails(tmp_path):
    """测试客观质量退化（如阻塞缺陷新增）导致 check 判定为 FAIL。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    base_dir = out_root / "base_run"
    base_m, _, _ = _create_synthetic_run(base_dir, run_id="b1", topic="退化测试")
    promote_review_baseline(base_m, reg_path, str(out_root))

    cand_dir = out_root / "cand_run"
    cand_m, _, _ = _create_synthetic_run(cand_dir, run_id="c1", topic="退化测试")
    with open(cand_m, "r", encoding="utf-8") as f:
        cdata = json.load(f)
    cdata["section_evidence"][0]["evidence_status"] = "unavailable_legacy_checkpoint"
    cdata["section_evidence"][0]["chunks"] = []
    with open(cand_m, "w", encoding="utf-8") as f:
        json.dump(cdata, f)

    chk = check_review_candidate(cand_m, reg_path, str(out_root))
    assert chk.verdict == "FAIL"
    assert chk.status == "compared"
    assert any(iss.code == "REGRESSION_EVIDENCE_PROVENANCE" for iss in chk.issues)


def test_check_candidate_invalid_fails_early(tmp_path):
    """测试候选清单校验失败时直接早退 FAIL，不尝试比对。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    bad_cand_m = str(out_root / "bad_cand.manifest.json")
    with open(bad_cand_m, "w", encoding="utf-8") as f:
        f.write("[]")

    chk = check_review_candidate(bad_cand_m, reg_path, str(out_root))
    assert chk.verdict == "FAIL"
    assert chk.status == "candidate_invalid"
    assert chk.comparison_report is None


def test_check_baseline_file_missing_or_hash_tampered_fails(tmp_path):
    """测试基线文件丢失或哈希被篡改时 check 判定为 FAIL (status=baseline_invalid)。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    base_dir = out_root / "base_run"
    base_m, _, _ = _create_synthetic_run(base_dir, run_id="b1", topic="完整性检查")
    promote_review_baseline(base_m, reg_path, str(out_root))

    cand_dir = out_root / "cand_run"
    cand_m, _, _ = _create_synthetic_run(cand_dir, run_id="c1", topic="完整性检查")

    # 1. 篡改基线 Manifest 内容
    with open(base_m, "a", encoding="utf-8") as f:
        f.write(" ")
    chk_tamper = check_review_candidate(cand_m, reg_path, str(out_root))
    assert chk_tamper.verdict == "FAIL"
    assert chk_tamper.status == "baseline_invalid"
    assert any(iss.code == "BASELINE_HASH_MISMATCH" for iss in chk_tamper.issues)

    # 2. 删除基线 Manifest
    os.remove(base_m)
    chk_miss = check_review_candidate(cand_m, reg_path, str(out_root))
    assert chk_miss.verdict == "FAIL"
    assert chk_miss.status == "baseline_invalid"
    assert any(iss.code == "BASELINE_FILE_MISSING" for iss in chk_miss.issues)


def test_unknown_registry_schema_or_corrupted_json(tmp_path):
    """测试注册表包含非法 JSON、未知 schema_version 或未知敏感字段时安全返回 FAIL。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    cand_dir = out_root / "cand_run"
    cand_m, _, _ = _create_synthetic_run(cand_dir, run_id="c1", topic="注册表损坏测试")

    # 1. 非法 JSON
    with open(reg_path, "w", encoding="utf-8") as f:
        f.write("{corrupted json}")
    assert list_review_baselines(reg_path, str(out_root)).verdict == "FAIL"
    assert promote_review_baseline(cand_m, reg_path, str(out_root)).verdict == "FAIL"
    assert check_review_candidate(cand_m, reg_path, str(out_root)).verdict == "FAIL"

    # 2. 未知 schema_version 与未知键
    bad_reg = {
        "schema_version": 2,
        "updated_at": "2026-08-25T00:00:00Z",
        "baselines": {},
        "secret_token": "leak",
    }
    with open(reg_path, "w", encoding="utf-8") as f:
        json.dump(bad_reg, f)
    assert list_review_baselines(reg_path, str(out_root)).verdict == "FAIL"
    assert promote_review_baseline(cand_m, reg_path, str(out_root)).verdict == "FAIL"
    assert check_review_candidate(cand_m, reg_path, str(out_root)).verdict == "FAIL"


def test_containment_and_dangerous_locator_directory_traversal_and_symlink_escape(tmp_path):
    """测试目录穿越、output_root 外路径以及 symlink 逃逸攻击被严格防御。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    # 1. 位于 output_root 外部的清单
    out_m, _, _ = _create_synthetic_run(outside_dir, run_id="out1", topic="外部逃逸测试")
    rep_out = promote_review_baseline(out_m, reg_path, str(out_root))
    assert rep_out.verdict == "FAIL"
    assert any(iss.code == "DANGEROUS_PATH" for iss in rep_out.issues)

    # 2. symlink 逃逸到外部
    symlink_m = str(out_root / "escaped_symlink.manifest.json")
    os.symlink(out_m, symlink_m)
    rep_sym = promote_review_baseline(symlink_m, reg_path, str(out_root))
    assert rep_sym.verdict == "FAIL"
    assert any(iss.code == "DANGEROUS_PATH" for iss in rep_sym.issues)

    # 3. 注册表中被注入危险 locator (../..)
    good_dir = out_root / "good"
    good_m, _, _ = _create_synthetic_run(good_dir, run_id="g1", topic="Locator注入")
    promote_review_baseline(good_m, reg_path, str(out_root))

    with open(reg_path, "r", encoding="utf-8") as f:
        rdata = json.load(f)
    tkey = compute_topic_key("Locator注入")
    rdata["baselines"][tkey]["manifest_locator"] = "../../etc/passwd"
    with open(reg_path, "w", encoding="utf-8") as f:
        json.dump(rdata, f)

    chk_inj = check_review_candidate(good_m, reg_path, str(out_root))
    assert chk_inj.verdict == "FAIL"
    assert any(iss.code == "DANGEROUS_LOCATOR" for iss in chk_inj.issues)


def test_atomic_write_exception_preserves_old_registry_and_cleans_tmp(tmp_path):
    """测试原子写入过程中发生异常时，旧注册表完整保留且无残留临时文件。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    m1, _, _ = _create_synthetic_run(out_root / "r1", run_id="r1", topic="持久化保护")
    promote_review_baseline(m1, reg_path, str(out_root))

    with open(reg_path, "r", encoding="utf-8") as f:
        before_content = f.read()

    m2, _, _ = _create_synthetic_run(out_root / "r2", run_id="r2", topic="新主题")
    with patch("os.replace", side_effect=OSError("Disk write simulated failure")):
        rep = promote_review_baseline(m2, reg_path, str(out_root))
        assert rep.verdict == "FAIL"
        assert any(iss.code == "REGISTRY_WRITE_ERROR" for iss in rep.issues)

    with open(reg_path, "r", encoding="utf-8") as f:
        after_content = f.read()
    assert before_content == after_content

    # 确认没有残留 .tmp 临时文件
    tmp_files = list(out_root.glob("*.tmp"))
    assert tmp_files == []


def test_cli_baseline_commands_json_pure_and_exit_codes(tmp_path):
    """测试真实 CLI 命令 baseline-promote / baseline-check / baseline-list 的退出码与纯净 JSON 输出。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    py_bin = sys.executable
    env = os.environ.copy()
    env["REVIEW_OUTPUT_DIR"] = str(out_root)
    env["REVIEW_BASELINE_REGISTRY_PATH"] = reg_path
    env["DB_PATH"] = str(tmp_path / "test_fake.db")

    m_pass, _, _ = _create_synthetic_run(out_root / "pass_run", run_id="p1", topic="CLI主题")
    m_warn, _, _ = _create_synthetic_run(out_root / "warn_run", run_id="w1", topic="CLI主题", git_dirty=True)

    # 1. baseline-list 初始空注册表 -> 退出码 0
    proc_ls_empty = subprocess.run([py_bin, "review.py", "baseline-list", "--json"], capture_output=True, text=True, env=env)
    assert proc_ls_empty.returncode == 0
    data_ls_empty = json.loads(proc_ls_empty.stdout)
    assert data_ls_empty["verdict"] == "PASS"
    assert data_ls_empty["total_baselines"] == 0

    # 2. baseline-promote 提升 PASS 清单 -> 退出码 0
    proc_pm_pass = subprocess.run([py_bin, "review.py", "baseline-promote", m_pass, "--json"], capture_output=True, text=True, env=env)
    assert proc_pm_pass.returncode == 0
    data_pm_pass = json.loads(proc_pm_pass.stdout)
    assert data_pm_pass["verdict"] == "PASS"

    # 3. baseline-list 查询 -> 退出码 0, 包含 1 项
    proc_ls_1 = subprocess.run([py_bin, "review.py", "baseline-list", "--json"], capture_output=True, text=True, env=env)
    assert proc_ls_1.returncode == 0
    data_ls_1 = json.loads(proc_ls_1.stdout)
    assert data_ls_1["total_baselines"] == 1
    assert data_ls_1["baselines"][0]["topic"] == "CLI主题"

    # 4. baseline-check 相同主题比对 -> 退出码 0
    proc_chk_pass = subprocess.run([py_bin, "review.py", "baseline-check", m_pass, "--json"], capture_output=True, text=True, env=env)
    assert proc_chk_pass.returncode == 0
    data_chk_pass = json.loads(proc_chk_pass.stdout)
    assert data_chk_pass["verdict"] == "PASS"

    # 5. baseline-check 带有 WARN 的候选清单 -> 退出码 1
    proc_chk_warn = subprocess.run([py_bin, "review.py", "baseline-check", m_warn, "--json"], capture_output=True, text=True, env=env)
    assert proc_chk_warn.returncode == 1
    data_chk_warn = json.loads(proc_chk_warn.stdout)
    assert data_chk_warn["verdict"] == "WARN"

    # 6. baseline-promote 拒绝 WARN 清单（未带 --allow-warn） -> 退出码 2
    proc_pm_warn_rej = subprocess.run([py_bin, "review.py", "baseline-promote", m_warn, "--json"], capture_output=True, text=True, env=env)
    assert proc_pm_warn_rej.returncode == 2
    data_pm_warn_rej = json.loads(proc_pm_warn_rej.stdout)
    assert data_pm_warn_rej["verdict"] == "FAIL"

    # 7. baseline-promote 允许 WARN 清单（带 --allow-warn） -> 退出码 1
    proc_pm_warn_ok = subprocess.run([py_bin, "review.py", "baseline-promote", m_warn, "--allow-warn", "--json"], capture_output=True, text=True, env=env)
    assert proc_pm_warn_ok.returncode == 1
    data_pm_warn_ok = json.loads(proc_pm_warn_ok.stdout)
    assert data_pm_warn_ok["verdict"] == "WARN"


def test_recursive_scan_entire_to_dict_no_path_leak(tmp_path):
    """递归扫描所有报告 to_dict 结果，确保绝不泄露宿主机物理绝对路径。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    manifest_path, _, _ = _create_synthetic_run(out_root / "run1", run_id="r1", topic="/home/dudu/secret/topic")
    promote_rep = promote_review_baseline(manifest_path, reg_path, str(out_root))
    check_rep = check_review_candidate(manifest_path, reg_path, str(out_root))
    list_rep = list_review_baselines(reg_path, str(out_root))

    forbidden = [str(tmp_path), "/home/", "/root/", "/tmp/"]
    _assert_no_path_leak_recursive(promote_rep.to_dict(), forbidden)
    _assert_no_path_leak_recursive(check_rep.to_dict(), forbidden)
    _assert_no_path_leak_recursive(list_rep.to_dict(), forbidden)


def test_check_candidate_containment_and_symlink_escape_fails_early(tmp_path):
    """测试候选清单在 output_root 外部或通过 symlink 逃逸时，check_review_candidate 立即早退 FAIL。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    # 1. 注册一个合法基线
    base_m, _, _ = _create_synthetic_run(out_root / "base", run_id="b1", topic="逃逸检查")
    promote_review_baseline(base_m, reg_path, str(out_root))

    # 2. 外部候选清单
    out_cand_m, _, _ = _create_synthetic_run(outside_dir, run_id="out_c", topic="逃逸检查")
    chk_out = check_review_candidate(out_cand_m, reg_path, str(out_root))
    assert chk_out.verdict == "FAIL"
    assert chk_out.status == "candidate_invalid"
    assert any(iss.code == "DANGEROUS_PATH" for iss in chk_out.issues)
    _assert_no_path_leak_recursive(chk_out.to_dict(), [str(outside_dir), str(tmp_path)])

    # 3. symlink 逃逸候选清单
    sym_cand_m = str(out_root / "escaped_cand.manifest.json")
    os.symlink(out_cand_m, sym_cand_m)
    chk_sym = check_review_candidate(sym_cand_m, reg_path, str(out_root))
    assert chk_sym.verdict == "FAIL"
    assert chk_sym.status == "candidate_invalid"
    assert any(iss.code == "DANGEROUS_PATH" for iss in chk_sym.issues)
    _assert_no_path_leak_recursive(chk_sym.to_dict(), [str(outside_dir), str(tmp_path)])


def test_registry_semantic_integrity_mismatch_and_locator_validation(tmp_path):
    """测试注册表语义完整性校验：危险 locator、伪造 key、topic 与 key 不匹配导致 list/promote/check 全部 FAIL。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    # 先创建一个正常的基线注册表
    good_m, _, _ = _create_synthetic_run(out_root / "good", run_id="g1", topic="语义检查")
    promote_review_baseline(good_m, reg_path, str(out_root))

    new_run_m, _, _ = _create_synthetic_run(out_root / "new_run", run_id="n1", topic="新主题")

    # 1. 注入危险相对路径 locator (../../etc/passwd)
    with open(reg_path, "r", encoding="utf-8") as f:
        rdata = json.load(f)
    tkey = compute_topic_key("语义检查")
    rdata["baselines"][tkey]["manifest_locator"] = "../../etc/passwd"
    with open(reg_path, "w", encoding="utf-8") as f:
        json.dump(rdata, f)

    assert list_review_baselines(reg_path, str(out_root)).verdict == "FAIL"
    rep_pm_rej = promote_review_baseline(new_run_m, reg_path, str(out_root))
    assert rep_pm_rej.verdict == "FAIL"
    chk_rej = check_review_candidate(good_m, reg_path, str(out_root))
    assert chk_rej.verdict == "FAIL"
    assert chk_rej.status == "baseline_invalid"

    # 2. 注入伪造的 entry.topic_key (与 map key 不一致)
    with open(reg_path, "r", encoding="utf-8") as f:
        rdata = json.load(f)
    rdata["baselines"][tkey]["manifest_locator"] = "good/综述_g1.manifest.json"
    rdata["baselines"][tkey]["topic_key"] = "a" * 64
    with open(reg_path, "w", encoding="utf-8") as f:
        json.dump(rdata, f)

    assert list_review_baselines(reg_path, str(out_root)).verdict == "FAIL"
    assert promote_review_baseline(new_run_m, reg_path, str(out_root)).verdict == "FAIL"
    assert check_review_candidate(good_m, reg_path, str(out_root)).verdict == "FAIL"

    # 3. 注入与 topic_key 不匹配的 topic 文本
    with open(reg_path, "r", encoding="utf-8") as f:
        rdata = json.load(f)
    rdata["baselines"][tkey]["topic_key"] = tkey
    rdata["baselines"][tkey]["topic"] = "完全不相干的主题"
    with open(reg_path, "w", encoding="utf-8") as f:
        json.dump(rdata, f)

    assert list_review_baselines(reg_path, str(out_root)).verdict == "FAIL"
    assert promote_review_baseline(new_run_m, reg_path, str(out_root)).verdict == "FAIL"
    assert check_review_candidate(good_m, reg_path, str(out_root)).verdict == "FAIL"


def test_canonical_topic_whitespace_casefold_nfkc_equivalence_in_check_and_compare(tmp_path):
    """测试 Unicode NFKC、大小写折叠与连续空白在 check 与 compare 层的统一等价性。"""
    out_root = tmp_path / "reviews"
    out_root.mkdir()
    reg_path = str(out_root / "baselines.registry.json")

    base_topic = "Microplastic   Biodegradation Ｍｅｃｈａｎｉｓｍ"
    cand_topic = "  microplastic biodegradation mechanism  \n"

    # 1. 提升基线
    base_m, base_md, _ = _create_synthetic_run(out_root / "base_topic", run_id="b1", topic=base_topic)
    rep_pm = promote_review_baseline(base_m, reg_path, str(out_root))
    assert rep_pm.verdict == "PASS"

    # 2. 候选采用等价变体的主题 -> check_review_candidate 自动命中基线并 PASS
    cand_m, cand_md, _ = _create_synthetic_run(out_root / "cand_topic", run_id="c1", topic=cand_topic)
    # 同步 markdown 内容使得产物哈希一致，从而验证纯主题等价下的 PASS 判定
    with open(base_md, "r", encoding="utf-8") as f:
        base_md_text = f.read()
    with open(cand_md, "w", encoding="utf-8") as f:
        f.write(base_md_text)
    with open(cand_m, "r", encoding="utf-8") as f:
        cdata = json.load(f)
    cdata["artifacts"]["markdown_sha256"] = _calc_file_sha256(cand_md)
    with open(cand_m, "w", encoding="utf-8") as f:
        json.dump(cdata, f)

    rep_chk = check_review_candidate(cand_m, reg_path, str(out_root))
    assert rep_chk.verdict == "PASS"
    assert rep_chk.status == "compared"
    assert rep_chk.comparison_report is not None
    assert rep_chk.comparison_report.same_topic is True
    assert rep_chk.comparison_report.verdict == "PASS"

    # 3. 真正不同主题 -> 无法命中基线返回 WARN (no_baseline)；直接比对则 FAIL (TOPIC_MISMATCH)
    diff_m, _, _ = _create_synthetic_run(out_root / "diff_topic", run_id="d1", topic="不同主题")
    rep_chk_diff = check_review_candidate(diff_m, reg_path, str(out_root))
    assert rep_chk_diff.verdict == "WARN"
    assert rep_chk_diff.status == "no_baseline"

    rep_cmp_direct = compare_review_runs(base_m, diff_m)
    assert rep_cmp_direct.verdict == "FAIL"
    assert rep_cmp_direct.same_topic is False
    assert any(iss.code == "TOPIC_MISMATCH" for iss in rep_cmp_direct.issues)
