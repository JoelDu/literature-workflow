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
from .patent import detect_patent

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
              "with_doi": 0, "with_refs": 0, "assets": 0, "patents": 0}
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

                # 2) 专利判定：扉页 INID 码已把发明人/申请人/申请日全结构化了，
                #    识别中就直接跳过 LLM 补缺（同书籍的做法），省一次 API 调用。
                pat = detect_patent(md_head or "")
                if pat:
                    details.update({
                        "doc_type": "patent",
                        "patent_no": pat["patent_no"],
                        "filing_date": pat["filing_date"],
                        "title": pat["title"] or details["title"],
                        "title_zh": pat["title"] or "",
                        "authors": pat["inventors"] or details["authors"],
                        "publisher": pat["assignee"],
                        "keywords": pat["ipc"],
                        "year": (pat["filing_date"] or "")[:4] or details["year"],
                        "source": "patent-inid",
                    })
                    report["patents"] += 1

                # 3) LLM 补缺（不覆盖已有非空字段）
                if use_llm and client is not None and not pat:
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
                  f"含参考文献 {report['with_refs']} 篇、图表 {report['assets']} 项、"
                  f"识别为专利 {report['patents']} 篇），失败 {report['failed']} 篇。")
    return report


def rescan_patents(settings, console, apply: bool = False) -> dict:
    """重扫存量文献，把漏判成论文的专利改回 doc_type='patent'。

    只做本地正则 + 只改专利相关列，**不调用任何 LLM、不重解析 content_list**，
    所以既不花钱也不会动到已有的标题/图表/参考文献。默认 dry-run，加 apply=True 才写库。
    """
    store = VectorStore(settings.DB_PATH, settings.EMBEDDING_DIM)
    rows = store.docs_needing_enrich(force=True)      # 全量 EXPORTED：(id, title, images_dir, md头)
    # 先记下扫描前的分类，报告里才能如实说出它原来被当成了什么（论文还是教材）
    was = store._doc_type_map()
    found, changed = [], 0
    for doc_id, title, _images_dir, md_head in rows:
        pat = detect_patent(md_head or "")
        if not pat:
            continue
        found.append((doc_id, title, pat))
        if apply:
            store.mark_as_patent(doc_id, pat)
            changed += 1

    _labels = {"paper": "论文", "book": "教材"}
    n_new = sum(1 for d, _t, _p in found if was.get(d, "paper") != "patent")
    console.print(f"[cyan]扫描 {len(rows)} 篇，判定为专利 {len(found)} 篇"
                  f"（其中 {n_new} 篇是新识别出来的）。")
    for doc_id, title, pat in found:
        old = was.get(doc_id, "paper")
        flag = "" if old == "patent" else \
            f"  [yellow]← 原先被当成{_labels.get(old, old)}[/yellow]"
        console.print(f"  {doc_id[:8]}  {pat['patent_no'] or '号码未识别':<16} "
                      f"{(pat['title'] or title or '')[:34]}{flag}")
    if not apply:
        console.print("[yellow]以上为预演结果，未写库。确认无误后加 --apply 执行。")
    else:
        console.print(f"[bold green]✔ 已更新 {changed} 篇为 doc_type='patent'。")
    return {"scanned": len(rows), "found": len(found), "changed": changed}
