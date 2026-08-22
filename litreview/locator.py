"""DocumentLocator — 资源定位与虚拟化深模块 (Deep Module).

统一处理物理文件路径与便携式资源 URI (如 asset://, vault://) 的双向映射与安全隔离，
杜绝向远端 MCP / Web 客户端泄漏宿主机敏感绝对物理路径。
"""
import os
from typing import Optional, Dict, Any
from dataclasses import dataclass

from utils import get_settings


@dataclass(frozen=True)
class ResourceDescriptor:
    """便携式资源描述符。"""
    uri: str
    asset_type: str  # 'pdf', 'figure', 'review_md', 'review_docx', 'note'
    work_id: str
    display_name: str
    local_path: Optional[str]
    remote_path: str  # Web API 相对/绝对下载路径
    exists: bool
    size_bytes: int = 0

    def to_dict(self, include_local_path: bool = False) -> Dict[str, Any]:
        """序列化为字典，默认对外部客户端剥离宿主机本地绝对路径。"""
        d = {
            "uri": self.uri,
            "asset_type": self.asset_type,
            "work_id": self.work_id,
            "display_name": self.display_name,
            "remote_url": self.remote_path,
            "exists": self.exists,
            "size_bytes": self.size_bytes
        }
        if include_local_path and self.local_path:
            d["local_path"] = self.local_path
        return d


class DocumentLocator:
    """资源定位器深模块。"""

    def __init__(self, base_web_url: str = ""):
        self._settings = get_settings()
        self._base_web_url = base_web_url.rstrip("/")

    def get_pdf_descriptor(self, work_id: str, local_path: Optional[str] = None, title: str = "") -> ResourceDescriptor:
        """获取文献 PDF 资产描述符。"""
        path = local_path
        if not path and hasattr(self._settings, "PROCESSED_PDF_DIR"):
            # 尝试根据已处理目录推断
            candidate = os.path.join(self._settings.PROCESSED_PDF_DIR, f"{work_id}.pdf")
            if os.path.exists(candidate):
                path = candidate

        exists = bool(path and os.path.exists(path))
        size = os.path.getsize(path) if exists else 0
        display_name = title or f"{work_id[:8]}.pdf"
        uri = f"asset://pdf/{work_id}"
        remote_path = f"/api/documents/{work_id}/pdf"
        if self._base_web_url:
            remote_path = f"{self._base_web_url}{remote_path}"

        return ResourceDescriptor(
            uri=uri,
            asset_type="pdf",
            work_id=work_id,
            display_name=display_name,
            local_path=path if exists else None,
            remote_path=remote_path,
            exists=exists,
            size_bytes=size
        )

    def get_review_descriptor(self, job_id: str, topic: str = "", extension: str = "md") -> ResourceDescriptor:
        """获取综述生成产物描述符 (Markdown 或 Word)。"""
        out_dir = getattr(self._settings, "REVIEW_OUTPUT_DIR", "./obsidian_vault/reviews")
        ext = extension.lstrip(".")
        local_path = None
        
        # 尝试匹配目录下的文件
        if os.path.isdir(out_dir):
            for fname in os.listdir(out_dir):
                if job_id in fname and fname.endswith(f".{ext}"):
                    local_path = os.path.join(out_dir, fname)
                    break

        exists = bool(local_path and os.path.exists(local_path))
        size = os.path.getsize(local_path) if exists else 0
        uri = f"vault://reviews/{job_id}.{ext}"
        remote_path = f"/api/reviews/{job_id}/download?format={ext}"
        if self._base_web_url:
            remote_path = f"{self._base_web_url}{remote_path}"

        return ResourceDescriptor(
            uri=uri,
            asset_type=f"review_{ext}",
            work_id=job_id,
            display_name=f"《{topic or job_id}》综述文档.{ext}",
            local_path=local_path,
            remote_path=remote_path,
            exists=exists,
            size_bytes=size
        )

    def sanitize_path_for_remote(self, local_path: str, resource_hint: str = "") -> str:
        """将宿主机物理路径安全替换为便携式提示，避免在远程客户端暴露物理服务器盘符。"""
        if not local_path:
            return ""
        basename = os.path.basename(local_path)
        if "reviews" in local_path:
            return f"vault://reviews/{basename}"
        if "processed_pdfs" in local_path or local_path.endswith(".pdf"):
            return f"asset://pdf/{basename}"
        return f"resource://{basename}"


_default_locator: Optional[DocumentLocator] = None


def get_locator(base_web_url: str = "") -> DocumentLocator:
    global _default_locator
    if _default_locator is None:
        _default_locator = DocumentLocator(base_web_url=base_web_url)
    return _default_locator
