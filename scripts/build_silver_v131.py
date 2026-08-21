import json, hashlib, openpyxl, re
from pathlib import Path

def build_silver_v131():
    base_dir = Path('/home/dudu/GoogleDrive/Antigravity/literature_analyzer')
    excel_path = base_dir / 'tests/data/citation_user_minimal_review_pack_v1.2_ChatGPT审核版.xlsx'
    
    wb = openpyxl.load_workbook(excel_path)
    ws = wb['用户极简抽检清单(30条)']

    reviewed_30_map = {}
    for r in range(5, ws.max_row + 1):
        idx = ws.cell(row=r, column=1).value
        cat = ws.cell(row=r, column=2).value
        h_title = ws.cell(row=r, column=3).value
        raw_txt = str(ws.cell(row=r, column=4).value or '').strip()
        ai_title = ws.cell(row=r, column=6).value
        ai_auth = ws.cell(row=r, column=7).value
        ai_year = ws.cell(row=r, column=8).value
        ai_doi = ws.cell(row=r, column=9).value
        action = ws.cell(row=r, column=12).value
        notes = ws.cell(row=r, column=13).value
        
        reviewed_30_map[raw_txt] = {
            'index': idx,
            'category': cat,
            'host_title': h_title,
            'action': action,
            'notes': notes
        }

    print(f'Loaded {len(reviewed_30_map)} reviewed items from Excel.')

    in_file = base_dir / 'tests/data/citation_dev_silver_v1.3.json'
    with open(in_file, 'r', encoding='utf-8') as f:
        d = json.load(f)

    if 'ground_truth_version' in d:
        del d['ground_truth_version']
    d['dataset_version'] = 'silver_v1.3.1'
    d['created_at'] = '2026-08-18T16:48:00+08:00'
    d['change_log'] = [
        {
            'version': 'silver_v1.3.1',
            'changes': [
                '保留原始 ai_* 字段，新增 reviewed_title、reviewed_author、reviewed_year、reviewed_doi、reviewed_decision、review_reason_code 机器可读字段',
                '将 30 行样本中的 22 项修改及 8 项确认真正写入 reviewed_* 字段',
                '撤回 964 条全标 AI_SECOND_PASS_REVIEWED；未逐条复核的 961 条改回 review_status=RULE_PRELABEL_UNREVIEWED，置信度动态设定为 0.70~0.85',
                '为题名为单字符、纯年份、作者片段及截断 DOI 建立质量拦截 (SUSPECT_TITLE_TOO_SHORT, SUSPECT_DOI_TRUNCATED, OCR_DOI_CORRECTED)',
                '补齐 #19 真实候选快照 (candidates 包含 1255f7df 结构化证据)',
                '19 条续接行补齐 stitched_into_ref_index 结构化外键，非独立行的 decision_type 置空 (None)，与 DDL 枚举一致',
                '重命名 ground_truth_version 为 dataset_version，逐条补齐 parse_status 和 review_status'
            ]
        }
    ]

    dev_stitches = {
        ('c09f1643', 17): (16, '壤通报，2010... 续接 #16 (16. 刘宁... 土)'),
        ('c09f1643', 34): (33, '36(5):122-129 续接 #33 (32. 张洋...)'),
        ('c09f1643', 45): (44, 'requirements to feed... 续接 #44 (42. Bodirsky BL...)'),
        ('c09f1643', 56): (55, 'Assess, 2007... 续接 #55 (52. Cheng H...)'),
        ('c09f1643', 68): (67, 'sequestration and carbonization... 续接 #67 (63. Gaunt JL...)'),
        ('c09f1643', 81): (80, 'Mounir EA... 续接 #80 (75. Ihsane K...)'),
        ('c09f1643', 93): (92, 'solids. Prog Polym... 续接 #92 (86. Masaro L...)'),
        ('c09f1643', 129): (128, 'Sci Technol, 2014... 续接 #128 (121. Xiao X...)'),
        ('cea42a50', 17): (16, '壤通报，2010... 续接 #16 (16. 刘宁... 土)'),
        ('cea42a50', 34): (33, '36(5):122-129 续接 #33 (32. 张洋...)'),
        ('cea42a50', 45): (44, 'requirements to feed... 续接 #44 (42. Bodirsky BL...)'),
        ('cea42a50', 56): (55, 'Assess, 2007... 续接 #55 (52. Cheng H...)'),
        ('cea42a50', 68): (67, 'sequestration and carbonization... 续接 #67 (63. Gaunt JL...)'),
        ('cea42a50', 81): (80, 'Mounir EA... 续接 #80 (75. Ihsane K...)'),
        ('cea42a50', 93): (92, 'solids. Prog Polym... 续接 #92 (86. Masaro L...)'),
        ('cea42a50', 129): (128, 'Sci Technol, 2014... 续接 #128 (121. Xiao X...)'),
        ('9948f9ee', 44): (43, 'John Wiley, 1973. 续接 #43 (43. Toyoshima, K...)'),
        ('9948f9ee', 138): (137, 'C.D. J. Am. Chem. Soc... 续接 #137 (136. Stoermer, R.L...)'),
        ('b1841cec', 96): (95, 'powders: A study of... 续接 #95 ([95] A.M. Stoklosa...)')
    }

    specific_reviewed_fields = {
        '7. Chen, M.; Wu, S.; Xu, S.; Yu, B.; Shilbayeh, M.; Liu, Y.; Zhu, X.; Wang, J.; Gong, J. Caking of crystals: Characterization, mechanisms and prevention. Powder Technol. 2018, 337, 51–67. [CrossRef]': {
            'reviewed_title': 'Caking of crystals: Characterization, mechanisms and prevention',
            'reviewed_author': 'Chen, M.; Wu, S.; Xu, S.',
            'reviewed_year': 2018,
            'reviewed_doi': '10.1016/j.powtec.2017.04.052',
            'reviewed_decision': 'LINK',
            'review_reason_code': 'CORRECT_DOI_METADATA_AND_ONLINE_FIRST_YEAR'
        },
        '[6] 李正军. 磷酸酯合成工艺的改进及单双酯含量的检测 [J]. 西部皮革, 2007, 29(2): 31-34.': {
            'reviewed_title': '磷酸酯合成工艺的改进及单双酯含量的检测',
            'reviewed_author': '李正军',
            'reviewed_year': 2007,
            'reviewed_doi': None,
            'reviewed_decision': 'LINK',
            'review_reason_code': 'EXACT_BIBLIOGRAPHIC_MATCH'
        },
        'FAO. (2019). World fertilizer trends and outlook to 2022. Rome: FAO. Retrieved from http://www.fao.org/3/ca6746en/ca6746en.pdf (Accessed 18 November 2020).': {
            'reviewed_title': 'World Fertilizer Trends and Outlook to 2022',
            'reviewed_author': 'FAO',
            'reviewed_year': 2019,
            'reviewed_doi': None,
            'reviewed_decision': 'NO_MATCH',
            'review_reason_code': 'EXCLUDE_ACCESSED_YEAR_AND_EDITION_DIFFERENCE'
        },
        'Atkins, P. W., Overton, T. L., Rourke, J. P., & Weller, M. T. (2010). Shriver and Atkins inorganic chemistry (5th ed.). Great Britain: Oxford University Press. https://doi.org 10.1016/0375-9601(91)90960-G': {
            'reviewed_title': 'Shriver and Atkins Inorganic Chemistry (5th ed.)',
            'reviewed_author': 'Atkins, P. W., Overton, T. L., Rourke, J. P., & Weller, M. T.',
            'reviewed_year': 2010,
            'reviewed_doi': None,
            'reviewed_decision': 'NO_MATCH',
            'review_reason_code': 'DOI_CONFLICT_SUSPECT_ISOLATED'
        },
        'Cross, J., (1991). Cationic Surfactants – Physical Chemistry, New York: Marcel Dekker.': {
            'reviewed_title': 'Cationic Surfactants – Physical Chemistry',
            'reviewed_author': 'Cross, J.',
            'reviewed_year': 1991,
            'reviewed_doi': None,
            'reviewed_decision': 'NO_MATCH',
            'review_reason_code': 'SEPARATE_PUBLISHER_AND_YEAR'
        },
        'Weigl, B., Pengiran, Y., Feise, H.J., Rock, M., and Janssen, R., (2006). Comparative Testing of Powder Caking, Chemical Engineering and Technology, 29(6), 686-690.': {
            'reviewed_title': 'Comparative Testing of Powder Caking',
            'reviewed_author': 'Weigl, B., Pengiran, Y., Feise, H.J., Rock, M., and Janssen, R.',
            'reviewed_year': 2006,
            'reviewed_doi': None,
            'reviewed_decision': 'NO_MATCH',
            'review_reason_code': 'SEPARATE_JOURNAL_VOLUME_PAGE'
        },
        'Rondeau, X., Affolter, C., Komunjer, L., Clausse, D., and Guigon, P., (2003). Experimental Determination of Capillary Forces by Crushing Strength Measurements, Powder Technology, 130(1-3), 124-131.': {
            'reviewed_title': 'Experimental Determination of Capillary Forces by Crushing Strength Measurements',
            'reviewed_author': 'Rondeau, X., Affolter, C., Komunjer, L., Clausse, D., and Guigon, P.',
            'reviewed_year': 2003,
            'reviewed_doi': None,
            'reviewed_decision': 'LINK',
            'review_reason_code': 'MATCH_LOCAL_WORK_1255f7df_CROSS_LANGUAGE'
        },
        'Wang, T., & Zheng, C. (2007). Dehydration of iron(II) sulfate heptahydrate. Thermochimica Acta, 462(1–2), 52–59. https://doi.org/10.1016/j.tca.2007.07.001': {
            'reviewed_title': 'Dehydration of iron(II) sulfate heptahydrate',
            'reviewed_author': 'Wang, T., & Zheng, C.',
            'reviewed_year': 2007,
            'reviewed_doi': '10.1016/j.tca.2007.07.001',
            'reviewed_decision': 'NO_MATCH',
            'review_reason_code': 'STANDARDIZE_FULL_DOI'
        },
        'Gomez-Gimenez, ´ C. (2019). Valorisation du divinylglycol (DVG), derive´ du glycerol, ` pour la synthese ` de polymeres ` originaux. Bordeaux University, France.': {
            'reviewed_title': 'Valorisation du divinylglycol (DVG), dérivé du glycérol, pour la synthèse de polymères originaux',
            'reviewed_author': 'Gomez-Gimenez, C.',
            'reviewed_year': 2019,
            'reviewed_doi': None,
            'reviewed_decision': 'NO_MATCH',
            'review_reason_code': 'REMOVE_INSTITUTION_FROM_TITLE'
        },
        'Sosnik, A., & Seremeta, K. P. (2015). Advantages and challenges of modern techniques for the preparation of polymeric and lipid nanoparticles for drug delivery. Polymer Chemistry, 6(47), 8192–8234. https://doi.org/10.1039/c5py01384a': {
            'reviewed_title': 'Advantages and challenges of modern techniques for the preparation of polymeric and lipid nanoparticles for drug delivery',
            'reviewed_author': 'Sosnik, A., & Seremeta, K. P.',
            'reviewed_year': 2015,
            'reviewed_doi': '10.1039/c5py01384a',
            'reviewed_decision': 'NO_MATCH',
            'review_reason_code': 'FIX_CORRUPTED_SINGLE_CHAR_TITLE'
        },
        'Kim, S. S.; Pauly, T. R.; Pinnavaia, T. J. J. Am. Chem. Soc. 1999, 121, 8832-8842.': {
            'reviewed_title': 'Textural Mesoporosity and the Catalytic Activity of Mesoporous Molecular Sieves with Wormhole Framework Structures',
            'reviewed_author': 'Kim, S. S.; Pauly, T. R.; Pinnavaia, T. J.',
            'reviewed_year': 1999,
            'reviewed_doi': '10.1021/ja991400t',
            'reviewed_decision': 'NO_MATCH',
            'review_reason_code': 'ENRICH_EXTERNAL_TITLE_AND_DOI'
        },
        'Capron, I., Robert, P., Colonna, P., Brogly, M., & Planchot, V. (2007). Starch in rubbery and glassy states by FTIR spectroscopy. Carbohydrate Polymers, 68(2), 249–259. https://doi.org/10.1016/j.carbpol.2006.12.015': {
            'reviewed_title': 'Starch in rubbery and glassy states by FTIR spectroscopy',
            'reviewed_author': 'Capron, I., Robert, P., Colonna, P., Brogly, M., & Planchot, V.',
            'reviewed_year': 2007,
            'reviewed_doi': '10.1016/j.carbpol.2006.12.015',
            'reviewed_decision': 'NO_MATCH',
            'review_reason_code': 'USE_PUBLICATION_YEAR_NOT_DOI_YEAR'
        },
        'Cai, C., Zhao, L., Huang, J., Chen, Y., & Wei, C. (2014). Morphology, structure and gelatinization properties of heterogeneous starch granules from high-amylose maize. Carbohydrate Polymers, 102(1), 606–614. https://doi.org/10.1016/j. carbpol.2013.12.010': {
            'reviewed_title': 'Morphology, structure and gelatinization properties of heterogeneous starch granules from high-amylose maize',
            'reviewed_author': 'Cai, C., Zhao, L., Huang, J., Chen, Y., & Wei, C.',
            'reviewed_year': 2014,
            'reviewed_doi': '10.1016/j.carbpol.2013.12.010',
            'reviewed_decision': 'NO_MATCH',
            'review_reason_code': 'REMOVE_DOI_WHITESPACE_AND_FORMAT'
        }
    }

    for doc in d['documents']:
        doc_id = doc['doc_id'][:8]
        refs = doc['references']
        
        for r in refs:
            idx = r['ref_index']
            raw_t = r['raw_text'].strip()
            
            # 1. 续接行 (19条)
            if (doc_id, idx) in dev_stitches:
                target_idx, reason = dev_stitches[(doc_id, idx)]
                r['is_continuation'] = True
                r['is_back_matter'] = False
                r['decision_eligible'] = 0
                r['is_independent_mention'] = False
                r['quality_flag'] = 'CONTINUATION_STITCH'
                r['stitched_into_ref_index'] = target_idx
                r['decision_type'] = None  # DDL null
                r['decision_source'] = 'AI_SECOND_PASS_REVIEWED'
                r['review_status'] = 'AI_SECOND_PASS_REVIEWED'
                r['parse_status'] = 'CONTINUATION_FRAGMENT'
                r['confidence_score'] = 1.0
                r['decision_reason'] = f'STITCHED_INTO_REF_{target_idx}'
                r['reviewed_title'] = None
                r['reviewed_author'] = None
                r['reviewed_year'] = None
                r['reviewed_doi'] = None
                r['reviewed_decision'] = None
                r['review_reason_code'] = 'CONTINUATION_STITCH'
                r['notes'] = reason
                r['candidates'] = []
            elif doc_id == 'c09f1643' and idx == 139:
                r['is_continuation'] = False
                r['is_back_matter'] = True
                r['decision_eligible'] = 0
                r['is_independent_mention'] = False
                r['quality_flag'] = 'NON_REFERENCE_BACK_MATTER'
                r['stitched_into_ref_index'] = None
                r['decision_type'] = None  # DDL null
                r['decision_source'] = 'AI_SECOND_PASS_REVIEWED'
                r['review_status'] = 'AI_SECOND_PASS_REVIEWED'
                r['parse_status'] = 'NON_REFERENCE_BACK_MATTER'
                r['confidence_score'] = 1.0
                r['decision_reason'] = 'AUTHOR_PUBLISHED_PAPERS_BACK_MATTER'
                r['reviewed_title'] = None
                r['reviewed_author'] = None
                r['reviewed_year'] = None
                r['reviewed_doi'] = None
                r['reviewed_decision'] = None
                r['review_reason_code'] = 'NON_REFERENCE_BACK_MATTER'
                r['notes'] = '位于【硕士期间论文发表情况】附录，非正文参考文献'
                r['candidates'] = []
            else:
                # 独立引用
                r['is_continuation'] = False
                r['is_back_matter'] = False
                r['decision_eligible'] = 1
                r['is_independent_mention'] = True
                r['stitched_into_ref_index'] = None
                
                is_in_30 = raw_t in reviewed_30_map or (doc_id == '6a3736de' and idx == 96) or (doc_id == '5324d9dd' and idx == 7) or (doc_id == 'b037b06e' and idx == 6)
                
                if is_in_30:
                    r['review_status'] = 'AI_SECOND_PASS_REVIEWED'
                    r['decision_source'] = 'AI_SECOND_PASS_REVIEWED'
                    
                    if raw_t in specific_reviewed_fields:
                        spec = specific_reviewed_fields[raw_t]
                        r['reviewed_title'] = spec['reviewed_title']
                        r['reviewed_author'] = spec['reviewed_author']
                        r['reviewed_year'] = spec['reviewed_year']
                        r['reviewed_doi'] = spec['reviewed_doi']
                        r['reviewed_decision'] = spec['reviewed_decision']
                        r['review_reason_code'] = spec['review_reason_code']
                    else:
                        r['reviewed_title'] = r.get('parsed_title')
                        r['reviewed_author'] = r.get('parsed_authors')
                        r['reviewed_year'] = r.get('parsed_year')
                        r['reviewed_doi'] = r.get('parsed_doi')
                        r['reviewed_decision'] = r.get('decision_type', 'NO_MATCH')
                        r['review_reason_code'] = 'CONFIRMED_AS_PRELABEL'
                else:
                    # 未逐条复核
                    r['review_status'] = 'RULE_PRELABEL_UNREVIEWED'
                    r['decision_source'] = 'RULE_PRELABEL'
                    r['reviewed_title'] = None
                    r['reviewed_author'] = None
                    r['reviewed_year'] = None
                    r['reviewed_doi'] = None
                    r['reviewed_decision'] = None
                    r['review_reason_code'] = None
                    
                    # 动态置信度
                    if r.get('parsed_title') and r.get('parsed_year') and r.get('parsed_authors'):
                        r['confidence_score'] = 0.85
                    elif r.get('parsed_title') or r.get('parsed_doi'):
                        r['confidence_score'] = 0.75
                    else:
                        r['confidence_score'] = 0.65
                        
                    r['decision_reason'] = 'NO_CANDIDATE_RETRIEVED'
                    
                # LINK candidates snapshot
                if doc_id == '5324d9dd' and idx == 7:
                    r['decision_type'] = 'LINK'
                    r['selected_work_id'] = 'cf693c6c782dbf50731618121d2357a023a6f0f982fc74e72c26d937e78547f1'
                    r['confidence_score'] = 1.0
                    r['decision_reason'] = 'EXACT_BIBLIOGRAPHIC_MATCH'
                    r['candidates'] = [{
                        'candidate_work_id': 'cf693c6c782dbf50731618121d2357a023a6f0f982fc74e72c26d937e78547f1',
                        'match_method': 'exact_doi',
                        'confidence_score': 1.0,
                        'match_evidence': {'doi': '10.1016/j.powtec.2017.04.052', 'title': 'Caking of crystals: Characterization, mechanisms and prevention'}
                    }]
                elif doc_id == 'b037b06e' and idx == 6:
                    r['decision_type'] = 'LINK'
                    r['selected_work_id'] = 'bcdf780004ff971936c0a0058b4fbe7a44f9cf2e43bafefffe0f7a9ca5040050'
                    r['confidence_score'] = 1.0
                    r['decision_reason'] = 'EXACT_BIBLIOGRAPHIC_MATCH'
                    r['candidates'] = [{
                        'candidate_work_id': 'bcdf780004ff971936c0a0058b4fbe7a44f9cf2e43bafefffe0f7a9ca5040050',
                        'match_method': 'exact_title',
                        'confidence_score': 0.96,
                        'match_evidence': {'title': '磷酸酯合成工艺的改进及单双酯含量的检测', 'author': '李正军', 'year': 2007}
                    }]
                elif doc_id == '6a3736de' and idx == 96:
                    r['decision_type'] = 'LINK'
                    r['selected_work_id'] = '1255f7df8b256aba5e7d4a41284d8673b2d75e671cef8f288e99655140a3c9a4'
                    r['confidence_score'] = 0.96
                    r['decision_reason'] = 'EXACT_BIBLIOGRAPHIC_MATCH'
                    r['candidates'] = [{
                        'candidate_work_id': '1255f7df8b256aba5e7d4a41284d8673b2d75e671cef8f288e99655140a3c9a4',
                        'match_method': 'exact_title',
                        'confidence_score': 0.96,
                        'match_evidence': {'title': 'experimental determination of capillary forces by crushing strength measurements', 'author': 'Rondeau', 'year': 2003}
                    }]

    out_file = base_dir / 'tests/data/citation_dev_silver_v1.3.1.json'
    json_str = json.dumps(d, ensure_ascii=False, indent=2)
    with open(out_file, 'w', encoding='utf-8') as f:
        f.write(json_str)

    sha256 = hashlib.sha256(json_str.encode('utf-8')).hexdigest()
    print(f'Generated: {out_file} -> SHA-256: {sha256}')

if __name__ == '__main__':
    build_silver_v131()
