"""元数据结构化提取：本地解析 content_list.json 为主（免费），LLM 仅补缺。

产出三张表：paper_details（标题中英/DOI/作者/期刊/关键词）、
paper_assets（图/表/图表 + 图题 + 页码）、paper_references（该论文引用的参考文献逐条）。
"""
import os
import re
import json
import glob

from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from .store import VectorStore

_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
_ASSET_TYPES = ("image", "chart", "table")


def _resolve_paper_dir(images_dir: str, db_path: str) -> str:
    """images_dir（DB 里的相对路径，如 ./mineru_output/X/images）→ 论文目录绝对路径。
    容器内相对 cwd 即可解析；宿主机上退回到 DB 所在目录（数据根目录）拼接。"""
    paper_dir = os.path.dirname(images_dir)
    if os.path.isdir(paper_dir):
        return paper_dir
    candidate = os.path.join(os.path.dirname(os.path.abspath(db_path)), paper_dir.lstrip("./"))
    return candidate if os.path.isdir(candidate) else ""


def _clean_doi(text: str) -> str:
    m = _DOI_RE.search(text or "")
    return m.group(0).rstrip(".,;）)]}") if m else ""


def parse_content_list(blocks: list, folder_name: str = "") -> dict:
    """从 MinerU content_list 块列表中提取标题/DOI/资产/参考文献（纯本地，无网络）。"""
    out = {"title": "", "doi": "", "assets": [], "refs": []}
    for b in blocks:
        btype = b.get("type", "")
        text = b.get("text", "") or ""

        if not out["title"] and btype == "text" and b.get("text_level") == 1 and text.strip():
            out["title"] = text.strip()

        if not out["doi"] and btype in ("header", "text", "page_footnote", "footer"):
            out["doi"] = _clean_doi(text)

        if btype in _ASSET_TYPES:
            caption_parts = []
            for k, v in b.items():
                if k.endswith("_caption") and isinstance(v, list):
                    caption_parts.extend(str(x) for x in v if x)
            img_path = b.get("img_path", "") or ""
            if folder_name and img_path:
                img_path = os.path.join(folder_name, img_path)
            out["assets"].append({
                "asset_type": btype,
                "img_path": img_path,
                "caption": " ".join(caption_parts).strip(),
                "page_idx": b.get("page_idx"),
            })

        if btype == "ref_text" and text.strip():
            out["refs"].append(text.strip())
    return out


def _load_content_list(paper_dir: str) -> list:
    """读取论文目录下的 content_list.json（排除 v2 版本）。"""
    files = [f for f in glob.glob(os.path.join(paper_dir, "*_content_list.json"))
             if "_v2" not in os.path.basename(f)]
    if not files:
        return []
    with open(files[0], "r", encoding="utf-8") as f:
        return json.load(f)


def _llm_fill(client, model, title_file: str, md_head: str, local: dict, existing: dict) -> dict:
    """LLM 补缺：中英标题配对、关键词、规范化 authors/journal/year。只填空缺，不覆盖本地解析结果。"""
    from .stages import _chat_json
    from .prompts import ENRICH_PROMPT
    known = {
        "文件名标题": title_file,
        "正文首标题": local.get("title", ""),
        "已知DOI": local.get("doi", ""),
        "已知作者": existing.get("authors", ""),
        "已知期刊": existing.get("journal", ""),
        "已知年份": existing.get("year", ""),
    }
    prompt = ENRICH_PROMPT.format(
        known=json.dumps(known, ensure_ascii=False), md_head=md_head or "")
    data = _chat_json(client, model, prompt)
    return {k: str(data.get(k, "") or "") for k in
            ("title_zh", "title_en", "doi", "keywords", "authors", "journal", "year")}


def run_enrich(settings, console, client=None, force: bool = False, use_llm: bool = True) -> dict:
    """幂等增量提取。单篇失败不影响其余。"""
    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    todo = store.docs_needing_enrich(force=force)
    report = {"total": len(todo), "enriched": 0, "failed": 0,
              "with_doi": 0, "with_refs": 0, "assets": 0}
    if not todo:
        console.print("[green]✔ 元数据已是最新，0 篇待提取。")
        return report

    paper_meta = store.get_paper_meta([r[0] for r in todo])
    console.print(f"[cyan]共 {len(todo)} 篇文献待提取元数据"
                  f"（本地解析{' + LLM 补缺' if use_llm and client else ''}）...")
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), console=console) as progress:
        task = progress.add_task("[cyan]提取中...", total=len(todo))
        for doc_id, title, images_dir, md_head in todo:
            progress.update(task, description=f"[cyan]提取 {doc_id[:8]}...")
            try:
                # 1) 本地解析 content_list.json
                local = {"title": "", "doi": "", "assets": [], "refs": []}
                paper_dir = _resolve_paper_dir(images_dir or "", settings.DB_PATH)
                if paper_dir:
                    blocks = _load_content_list(paper_dir)
                    local = parse_content_list(blocks, os.path.basename(paper_dir))
                if not local["doi"]:
                    local["doi"] = _clean_doi(md_head or "")

                existing = paper_meta.get(doc_id, {})
                details = {
                    "title": title or "",
                    "title_zh": "", "title_en": "",
                    "doi": local["doi"],
                    "authors": existing.get("authors", ""),
                    "journal": existing.get("journal", ""),
                    "year": existing.get("year", ""),
                    "keywords": "",
                    "n_figures": sum(1 for a in local["assets"] if a["asset_type"] in ("image", "chart")),
                    "n_tables": sum(1 for a in local["assets"] if a["asset_type"] == "table"),
                    "source": "local",
                }

                # 2) LLM 补缺（不覆盖已有非空字段）
                if use_llm and client is not None:
                    try:
                        filled = _llm_fill(client, settings.REVIEW_MODEL, title or "",
                                           md_head or "", local, existing)
                        for k in ("title_zh", "title_en", "keywords"):
                            details[k] = filled.get(k, "")
                        for k in ("doi", "authors", "journal", "year"):
                            if not details.get(k):
                                details[k] = filled.get(k, "")
                        details["source"] = "local+llm"
                    except Exception as e:
                        console.print(f"[yellow]⚠️ LLM 补缺失败 {doc_id[:8]}（保留本地结果）: {e}")

                store.save_enrichment(doc_id, details, local["assets"], local["refs"])
                report["enriched"] += 1
                report["with_doi"] += 1 if details["doi"] else 0
                report["with_refs"] += 1 if local["refs"] else 0
                report["assets"] += len(local["assets"])
            except Exception as e:
                report["failed"] += 1
                console.print(f"[red]✖ 提取失败 {doc_id[:8]}: {e}")
            progress.advance(task)

    console.print(f"[bold green]✔ 元数据提取完成：成功 {report['enriched']} 篇（含 DOI {report['with_doi']} 篇、"
                  f"含参考文献 {report['with_refs']} 篇、图表 {report['assets']} 项），失败 {report['failed']} 篇。")
    return report
