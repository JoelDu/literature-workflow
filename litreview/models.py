from pydantic import BaseModel, Field


def infer_table_suggested(heading: str, questions: list | None = None) -> bool:
    """对缺失 table_suggested 的旧大纲章节做确定性意图推断。
    材料横向对比、工艺对比等章节激活为 True；纯机理、背景、历史、展望等章节不误激活。
    """
    heading_clean = (heading or "").strip().lower()
    if not heading_clean:
        return False

    pos_terms = (
        "对比", "比较", "横向", "优缺点", "优劣", "性能评估", "指标评估",
        "工艺对比", "材料对比", "技术路线", "路线评估", "工艺路线",
        "comparison", "comparative", "benchmark"
    )
    neg_terms = (
        "机理", "机制", "原理", "动力学", "背景", "概述", "现状",
        "历史", "演变", "演进", "展望", "趋势", "挑战", "未来",
        "引言", "结论", "总结", "mechanism", "overview", "introduction", "background", "history"
    )

    has_pos_heading = any(term in heading_clean for term in pos_terms)
    has_neg_heading = any(term in heading_clean for term in neg_terms)

    # 1. 标题含明确对比词：若不含纯机理/背景，或对比词更明确，激活
    if has_pos_heading:
        return True

    # 2. 标题命中纯机理/背景/展望且无对比词，坚决不激活
    if has_neg_heading:
        return False

    # 3. 检查检索问题中的明确对比意图
    if questions:
        for q in questions:
            q_clean = str(q or "").strip().lower()
            if any(term in q_clean for term in pos_terms):
                return True

    return False


class OutlineSection(BaseModel):
    heading: str
    questions: list = Field(default_factory=list)  # 2-4 个中文检索问题
    table_suggested: bool = False


class Outline(BaseModel):
    title: str
    topic: str = ""
    sections: list = Field(default_factory=list)   # list[OutlineSection]，仅主体章节
    # 作者立论三元组随大纲 JSON 持久化，保证每章写作与续跑都能看到同一核心立论。
    thesis: str = ""
    research_gap: str = ""
    mainline_logic: str = ""

    def apply_alignment(self, thesis: str = "", research_gap: str = "",
                        mainline_logic: str = "") -> "Outline":
        """显式传入的非空参数优先；缺省时沿用大纲自带值。原地更新并返回 self。"""
        self.thesis = (thesis or "").strip() or self.thesis
        self.research_gap = (research_gap or "").strip() or self.research_gap
        self.mainline_logic = (mainline_logic or "").strip() or self.mainline_logic
        return self

    @classmethod
    def from_dict(cls, d: dict, topic: str = "") -> "Outline":
        sections = []
        for s in d.get("sections", []):
            if isinstance(s, dict):
                s_dict = dict(s)
                # 仅当旧大纲反序列化缺失 table_suggested（或为 None）时执行推断，显式 False/True 严格保留
                if "table_suggested" not in s_dict or s_dict["table_suggested"] is None:
                    s_dict["table_suggested"] = infer_table_suggested(
                        s_dict.get("heading", ""), s_dict.get("questions", [])
                    )
                sections.append(OutlineSection(**s_dict))
            elif isinstance(s, OutlineSection):
                sections.append(s)
        return cls(title=d.get("title", topic or "文献综述"), topic=topic or d.get("topic", ""),
                   sections=sections,
                   thesis=str(d.get("thesis") or ""),
                   research_gap=str(d.get("research_gap") or ""),
                   mainline_logic=str(d.get("mainline_logic") or ""))


class Evidence(BaseModel):
    chunk_id: int
    doc_id: str
    score: int
    summary: str
    section_title: str = ""       # 来源片段在原论文中的小节标题
    retrieval_score: float = 0.0  # 向量检索余弦分（调试用）
    # 原始片段正文，仅供零 token 数值溯源审计；不写入 manifest（私有全文）。
    chunk_text: str = ""


class SectionDraft(BaseModel):
    heading: str
    markdown: str
    cited_doc_ids: list = Field(default_factory=list)
    track_a_doc_ids: list = Field(default_factory=list)
    track_b_doc_ids: list = Field(default_factory=list)
    source_evidence_count: int = 0  # 支持检查点续跑后准确恢复全文证据计数
    # 幻觉引用被剥离后失去全部来源的句子（剥离后的文本），供质量门禁与修复回路使用。
    unsourced_sentences: list = Field(default_factory=list)
    # doc_id -> 该文献在写作输入（证据摘要、原始片段、第二轨摘要）中出现过的规范化数值；
    # 随检查点持久化，续跑后的数值溯源审计仍可复现。
    evidence_numbers: dict = Field(default_factory=dict)


class ReviewDoc(BaseModel):
    title: str
    topic: str
    intro: str = ""
    sections: list = Field(default_factory=list)   # list[SectionDraft]
    conclusion: str = ""
    references: list = Field(default_factory=list) # 已格式化编号的参考文献行
    reference_doc_ids: list[str] = Field(default_factory=list) # 与 references 严格 1:1 对应的 doc_id 列表
    doc_count: int = 0
    evidence_count: int = 0
    generated_at: str = ""
    quality_issues: list = Field(default_factory=list)
    quality_blocking: bool = False
