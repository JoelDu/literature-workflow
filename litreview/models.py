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
        return cls(title=d.get("title", topic or "文献综述"), topic=topic or d.get("topic", ""), sections=sections)


class Evidence(BaseModel):
    chunk_id: int
    doc_id: str
    score: int
    summary: str
    section_title: str = ""       # 来源片段在原论文中的小节标题
    retrieval_score: float = 0.0  # 向量检索余弦分（调试用）


class SectionDraft(BaseModel):
    heading: str
    markdown: str
    cited_doc_ids: list = Field(default_factory=list)
    track_a_doc_ids: list = Field(default_factory=list)
    track_b_doc_ids: list = Field(default_factory=list)
    source_evidence_count: int = 0  # 支持检查点续跑后准确恢复全文证据计数


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
