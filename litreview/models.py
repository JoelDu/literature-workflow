"""综述生成流程的数据模型。"""
from pydantic import BaseModel, Field


class OutlineSection(BaseModel):
    heading: str
    questions: list = Field(default_factory=list)  # 2-4 个中文检索问题


class Outline(BaseModel):
    title: str
    topic: str = ""
    sections: list = Field(default_factory=list)   # list[OutlineSection]，仅主体章节

    @classmethod
    def from_dict(cls, d: dict, topic: str = "") -> "Outline":
        sections = [OutlineSection(**s) if isinstance(s, dict) else s for s in d.get("sections", [])]
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


class ReviewDoc(BaseModel):
    title: str
    topic: str
    intro: str = ""
    sections: list = Field(default_factory=list)   # list[SectionDraft]
    conclusion: str = ""
    references: list = Field(default_factory=list) # 已格式化编号的参考文献行
    doc_count: int = 0
    evidence_count: int = 0
    generated_at: str = ""
