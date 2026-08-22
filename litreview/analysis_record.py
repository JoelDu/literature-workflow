"""论文分析结构化领域模型与知识库产物发布深模块 (PaperAnalysisRecord)。

统一封装：
1. LLM 结构化提取结果的 Pydantic 强类型校验与容错解析；
2. 原始摘要正则比对与去幻觉融合；
3. Obsidian 双链笔记的自动化渲染与附件相对路径转存；
4. Excel 知识库表格的标准行序列化与字段对齐；
5. SQLite 数据库持久化载荷构建。
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

from jinja2 import Template
from pydantic import BaseModel, Field


class PaperAnalysisRecord(BaseModel):
    """学术文献 LLM 深度分析结构化领域实体。"""

    tldr: str = Field(default="", description="1~2 句话核心贡献与一句话结论")
    keywords: str = Field(default="", description="3~5 个中英文核心关键词/研究领域标签")
    fertilizer_implications: str = Field(default="", description="对磷化工与新型肥料开发的工程借鉴与技术抓手")
    abstract: str = Field(default="", description="论文原始摘要")
    background: str = Field(default="", description="研究背景与动机")
    methods: str = Field(default="", description="核心实验路线与工艺参数")
    results: str = Field(default="", description="关键量化数据与对比性能")
    conclusion: str = Field(default="", description="主要结论与作者启示")
    language: str = Field(default="zh", description="语言代码 (zh/en)")
    authors: str = Field(default="", description="作者列表")
    journal: str = Field(default="", description="期刊或会议名称")
    year: str = Field(default="", description="发表年份")
    doi: str = Field(default="", description="DOI 标识符")
    doc_type: str = Field(default="paper", description="文献类型 (paper/patent/standard/report/book)")
    patent_no: str = Field(default="", description="专利号")
    doc_no: str = Field(default="", description="标准或文献编号")
    filing_date: str = Field(default="", description="专利申请日")
    effective_date: str = Field(default="", description="标准实施日期")
    publisher: str = Field(default="", description="出版者或发布机构")
    url: str = Field(default="", description="网络资源地址")
    error: Optional[str] = Field(default=None, description="错误信息 (若存在)")
    raw_content: Optional[str] = Field(default=None, description="大模型原始响应文本")

    def get(self, key: str, default: Any = None) -> Any:
        """字典兼容接口，保障历史调用代码 analysis.get('key') 不受影响。"""
        val = getattr(self, key, None)
        return val if val is not None and val != "" else default

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def __contains__(self, item: str) -> bool:
        return hasattr(self, item) and getattr(self, item) is not None

    @classmethod
    def from_llm_json(
        cls,
        raw_data: str | dict | None,
        markdown_text: str = "",
        paper_meta: Optional[Dict[str, Any]] = None,
    ) -> PaperAnalysisRecord:
        """从大模型响应安全构造强类型分析实体，并自动融合原文物理摘要。"""
        meta = paper_meta or {}
        if not raw_data:
            return cls(
                abstract=cls._extract_abstract_safe(markdown_text) or meta.get("abstract", ""),
                authors=meta.get("authors", ""),
                journal=meta.get("journal", ""),
                year=meta.get("year", ""),
                doi=meta.get("doi", ""),
                doc_type=meta.get("doc_type", "paper"),
            )

        parsed: dict = {}
        error_msg = None
        raw_str = None

        if isinstance(raw_data, dict):
            parsed = dict(raw_data)
        elif isinstance(raw_data, str):
            raw_str = raw_data
            try:
                parsed = json.loads(raw_data)
            except Exception as e:
                error_msg = f"JSONDecodeError: {e}"
                parsed = {"tldr": "结果解析 JSON 失败", "background": raw_data[:500]}

        # 融合外部元数据兜底
        for k in [
            "authors", "journal", "year", "doi", "language", "doc_type",
            "patent_no", "doc_no", "filing_date", "effective_date",
            "publisher", "url",
        ]:
            if not parsed.get(k) and meta.get(k):
                parsed[k] = meta[k]

        # 原始摘要双向融合（正则提取优先，防止 LLM 幻觉改写）
        orig_abstract = cls._extract_abstract_safe(markdown_text)
        if orig_abstract:
            parsed["abstract"] = orig_abstract
        elif not parsed.get("abstract") and meta.get("abstract"):
            parsed["abstract"] = meta["abstract"]

        # 年份格式化兜底
        if not parsed.get("year"):
            parsed["year"] = datetime.now().strftime("%Y")

        return cls(
            tldr=str(parsed.get("tldr") or ""),
            keywords=str(parsed.get("keywords") or ""),
            fertilizer_implications=str(parsed.get("fertilizer_implications") or ""),
            abstract=str(parsed.get("abstract") or ""),
            background=str(parsed.get("background") or ""),
            methods=str(parsed.get("methods") or ""),
            results=str(parsed.get("results") or ""),
            conclusion=str(parsed.get("conclusion") or ""),
            language=str(parsed.get("language") or meta.get("language") or "zh"),
            authors=str(parsed.get("authors") or meta.get("authors") or "未提取"),
            journal=str(parsed.get("journal") or meta.get("journal") or ""),
            year=str(parsed.get("year") or meta.get("year") or datetime.now().strftime("%Y")),
            doi=str(parsed.get("doi") or meta.get("doi") or ""),
            doc_type=str(parsed.get("doc_type") or meta.get("doc_type") or "paper"),
            patent_no=str(parsed.get("patent_no") or meta.get("patent_no") or ""),
            doc_no=str(parsed.get("doc_no") or meta.get("doc_no") or ""),
            filing_date=str(parsed.get("filing_date") or meta.get("filing_date") or ""),
            effective_date=str(parsed.get("effective_date") or meta.get("effective_date") or ""),
            publisher=str(parsed.get("publisher") or meta.get("publisher") or ""),
            url=str(parsed.get("url") or meta.get("url") or ""),
            error=error_msg or parsed.get("error"),
            raw_content=raw_str,
        )

    @staticmethod
    def _extract_abstract_safe(markdown_text: str) -> str:
        """从 Markdown 中正则抽取作者原始摘要。"""
        if not markdown_text:
            return ""
        try:
            from utils import extract_original_abstract
            return extract_original_abstract(markdown_text)
        except Exception:
            return ""

    def to_excel_row(
        self,
        doc_id: str,
        title: str,
        pdf_path: str,
        note_name: str,
        status: str = "EXPORTED",
        provider: str = "",
        job_id: str = "",
        processed_at: str = "",
    ) -> Dict[str, Any]:
        """输出标准 Excel 知识库行格式（黄金决策列排在首屏）。"""
        return {
            "文献ID": doc_id,
            "标题": title,
            "关键词": self.keywords,
            "肥料开发借鉴": self.fertilizer_implications,
            "TLDR": self.tldr,
            "作者": self.authors,
            "年份": self.year,
            "期刊": self.journal,
            "文件路径": pdf_path,
            "Obsidian链接": f"[[{note_name}]]" if not note_name.startswith("[[") else note_name,
            "语言": self.language,
            "状态": status,
            "服务商": provider,
            "批量任务ID": job_id,
            "解析时间": processed_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def publish_obsidian_note(
        self,
        obsidian_dir: str,
        title: str,
        doc_id: str,
        raw_images: Optional[List[str]] = None,
        template_path: Optional[str] = None,
    ) -> tuple[str, str]:
        """渲染并落地 Obsidian 双链 Markdown 笔记，自动转存附件图片为相对链接。

        返回: (note_absolute_path, note_basename_without_ext)
        """
        os.makedirs(obsidian_dir, exist_ok=True)
        attachments_dir = os.path.join(obsidian_dir, "attachments", doc_id)
        os.makedirs(attachments_dir, exist_ok=True)

        relative_image_links = []
        if raw_images:
            for img_path in raw_images:
                if os.path.exists(img_path):
                    try:
                        img_name = os.path.basename(img_path)
                        dest_img_path = os.path.join(attachments_dir, img_name)
                        shutil.copy2(img_path, dest_img_path)
                        relative_link = f"attachments/{doc_id}/{img_name}".replace("\\", "/")
                        relative_image_links.append(relative_link)
                    except Exception as e:
                        print(f"[yellow]拷贝图片到附件目录失败 {img_path}: {e}")

        # 加载模板
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tpl_path = template_path or os.path.join(base_dir, "obsidian_template.md")

        if os.path.exists(tpl_path):
            with open(tpl_path, "r", encoding="utf-8") as f:
                template = Template(f.read())
        else:
            # 极简内置兜底模板
            template = Template(
                "# {{ title }}\n\n## 💡 TL;DR\n{{ tldr }}\n\n## 🌾 肥料开发借鉴\n{{ fertilizer_implications }}\n\n## 📄 摘要\n{{ abstract }}\n"
            )

        from litreview import doctype as dt

        doc_type = dt.normalize(self.doc_type) or dt.DEFAULT_TYPE
        source_fields = {
            "paper": ("期刊/会议", self.journal),
            "conf": ("会议", self.journal),
            "patent": ("专利号", self.patent_no),
            "standard": ("标准号", self.doc_no),
            "book": ("出版社", self.publisher),
            "report": ("发布机构", self.publisher),
            "thesis": ("授予单位", self.publisher),
            "web": ("网址", self.url),
        }
        source_label, source_value = source_fields.get(doc_type, ("来源", ""))

        note_content = template.render(
            doc_type=doc_type,
            doc_type_label=dt.label(doc_type),
            source_label=source_label,
            source_value=source_value or self.journal or "未提取",
            language=self.language,
            current_date=datetime.now().strftime("%Y-%m-%d"),
            title=title or "Unknown Title",
            authors=self.authors or "未提取",
            year=self.year or datetime.now().strftime("%Y"),
            journal=self.journal or "",
            doi=self.doi or "",
            patent_no=self.patent_no,
            doc_no=self.doc_no,
            filing_date=self.filing_date,
            effective_date=self.effective_date,
            publisher=self.publisher,
            tldr=self.tldr,
            fertilizer_implications=self.fertilizer_implications,
            keywords=self.keywords,
            abstract=self.abstract,
            background=self.background,
            methods=self.methods,
            results=self.results,
            conclusion=self.conclusion,
            images=relative_image_links,
        )

        safe_title = "".join(c for c in (title or "Untitled") if c.isalnum() or c in " -_").strip() or "Untitled_Paper"
        note_filename = f"{safe_title}_{doc_id[:8]}.md"
        note_path = os.path.join(obsidian_dir, note_filename)

        with open(note_path, "w", encoding="utf-8") as f:
            f.write(note_content)

        note_name = f"{safe_title}_{doc_id[:8]}"
        return note_path, note_name
