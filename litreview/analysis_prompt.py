"""自适应学术文献分析提示词装配器（支持双语等效折算与文献类型感知）。"""
import re
from langdetect import detect, DetectorFactory

DetectorFactory.seed = 0

def detect_language(text: str) -> str:
    try:
        sample = text[:3000]
        return detect(sample)
    except Exception:
        return "zh" if any('\u4e00' <= char <= '\u9fff' for char in text[:1000]) else "en"


def calculate_equivalent_words(text: str, lang: str = "") -> tuple[int, int]:
    """计算中英文等效字数当量，并返回 (等效字数, 档位 Tier 1/2/3)。
    
    中英文信息密度折算：
    - 中文：1 个汉字 = 1 个字数当量；
    - 英文：1 个英文单词 ≈ 1.6 个汉字信息当量。
    """
    if not lang:
        lang = detect_language(text)
    
    if lang.startswith("zh"):
        # 中文字符计数（忽略空白）
        zh_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        other_chars = len(re.findall(r'[a-zA-Z0-9]', text))
        eq_words = zh_chars + int(other_chars * 0.3)
    else:
        # 英文按单词数折算
        words_count = len(text.split())
        eq_words = int(words_count * 1.6)
        if eq_words == 0:
            eq_words = int(len(text) / 3.8)

    # 档位判定 (Tier 1: 轻量短篇, Tier 2: 中长篇, Tier 3: 大部头长篇)
    if eq_words < 8_000:
        tier = 1
    elif eq_words < 20_000:
        tier = 2
    else:
        tier = 3

    return eq_words, tier


def build_adaptive_analysis_prompt(text: str, doc_type: str = "paper", lang: str = "") -> str:
    """根据正文体量与文献类型，动态生成高信息密度的分析系统提示词。"""
    eq_words, tier = calculate_equivalent_words(text, lang)

    # 1. 动态字数预算（在原基准上增加 60 字）
    if tier == 1:
        tldr_len = "260~350字"
        imp_len = "180~260字"
        detail_depth = "聚焦核心单点改性机理、最佳对照数据与局限"
    elif tier == 2:
        tldr_len = "350~500字"
        imp_len = "250~350字"
        detail_depth = "全面展开多物料配比、反应机制、分段释放与工艺窗口"
    else:
        tldr_len = "500~700字"
        imp_len = "350~500字"
        detail_depth = "分模块/分章节全景式提炼核心理论、工艺矩阵与宏观结论"

    # 2. 文献类型定制化提取重心
    dtype = (doc_type or "paper").lower()
    if dtype == "patent":
        type_desc = "发明专利"
        tldr_structure = f"""• 【保护对象与改性方法】：明确说明发明涉及的具体物料体系、化学结构缩写与工艺技术路线；
• 【核心权利要求与发明点】：提炼独立权利要求保护的核心构想与微观改性/结构调控机制；
• 【最优实施例与量化指标突破】：列举代表性最优实施例的精确配比、工艺条件（温度/时间/压力）、耐水/控释/强度等核心指标及相对对比文件的提升倍数；
• 【工业化壁垒与未解决缺陷】：指出专利在连续化生产、原料获取、储存稳定性或设备适配上的潜在限制。"""
        imp_focus = "直击专利技术如何规避现有专利壁垒、工业生产可复现的配方窗口与改性技术抓手。"
    elif dtype == "standard":
        type_desc = "国家/行业/团体标准"
        tldr_structure = f"""• 【标准类型与适用范围】：写明标准级别（如 GB/T, HG/T）、发布状态与覆盖的产品/工艺边界；
• 【核心技术要求与限量指标表】：系统列举关键指标的限值（如养分释放率分级、初期突释限值、重金属限量、膜厚或强度要求）；
• 【指定检测方法与试验规程】：归纳标准规定的特征测试环境、取样周期与判据算法；
• 【标准演进与实施瓶颈】：指出相对旧版标准的重大技术升级，或企业在执行达标过程中的技术难点。"""
        imp_focus = "直击产品设计与生产质检如何精准对标该标准条款，避免合规风险与质量判废。"
    elif dtype == "report":
        type_desc = "行业技术报告/白皮书"
        tldr_structure = f"""• 【报告主题与产业范围】：明确报告调研的产业环节、统计周期与研究对象；
• 【市场规模与产能技术矩阵】：列出产能利用率、产销量、主流生产路线市场占有率等关键行业量化数据；
• 【经济性拆解与成本构成】：阐述原料成本、能耗、设备折旧占比及不同路线的毛利/成本竞争格局；
• 【政策壁垒与行业痛点】：指出环保准入、碳排放约束、技术代差等核心产业瓶颈。"""
        imp_focus = "直击企业投资决策、技术路线比选与降本增效的产业化切入点。"
    else:
        type_desc = "学术期刊/学位论文"
        tldr_structure = f"""• 【研究对象与改性方法】：写明具体物质全称、缩写（如 LP, PP, PAPI）与原料改性路线；
• 【微观机理与结构演化】：详细阐述化学交联反应、氢键缔合作用、表面粗糙度与孔隙结构调控机制；
• 【核心量化实验数据与对比】：详尽列出最佳配比、包膜量(%)、NCO/OH比、水接触角(°)、初期突释率(%)、释放周期(d)及相对对照组提升倍数；
• 【技术短板与工业局限】：明确指出该工艺在实际放大、耐磨机械疲劳、成本或土壤复杂环境中的潜在限制（拒绝报喜不报忧）。"""
        imp_focus = "直击配方调控、机理借鉴、工艺降本或工业放大的具体技术抓手（严禁“提供了理论指导”等空话）。"

    prompt = f"""你是一个精通化学工程、材料科学与磷化工/新型肥料研发的顶尖学术分析专家。当前分析对象为一篇【{type_desc}】（等效体量档位: Tier {tier}）。
请阅读以下 Markdown 格式的内容，进行高信息密度的深度提取，并输出严格合法的 JSON 对象。

字段提取规范：
1. tldr: 【核心简评】必须以 💡 开头（当前文献字数要求: {tldr_len}，严禁泛泛空话，信息量必须高度饱满），严格采用四元组结构深度展开：
{tldr_structure}

2. keywords: 4~6 个高概括度核心关键词/领域标签（分号隔开，如：包膜控释; 木质素多元醇; 原位交联; 养分释放动力学）。

3. fertilizer_implications: 【重点】面向新型肥料产品开发与工业化生产的具体借鉴点（字数要求: {imp_len}）。
{imp_focus}

4. abstract: 尽量逐字提取并保留作者原始摘要。
5. background: 研究背景、行业痛点与研究动机。
6. methods: 核心实验路线、反应方程式、表征手段与工艺参数（深度要求: {detail_depth}）。
7. results: 关键量化数据、对照实验结果与对比性能（详尽列出实测指标与图表数据）。
8. conclusion: 主要结论、实际应用价值与作者启示。
9. language / authors / journal / year: 基础学术著录元数据。无法提取则使用空字符串。"""

    return prompt
