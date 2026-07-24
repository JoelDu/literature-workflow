"""综述插图：为写完的章节从 paper_assets 中挑选相关图表。

选图逻辑：候选 = 本章实际引用论文的带图题资产 → 用 Reranker 按
「章节标题+检索问题 vs 图题」重排 → 过阈值的 top-N 复制到
REVIEW_OUTPUT_DIR/assets/ 并把图 markdown 追加到章节正文。
图注末尾带 [@docid8] 引用标记，随全文统一编号为 [n]。
"""

import os
import re
import shutil

_WS_RE = re.compile(r"\s+")


def _clean_caption(caption: str) -> str:
    return _WS_RE.sub(" ", caption or "").strip()


def _resolve_asset_path(img_path: str, settings) -> str:
    """paper_assets.img_path（相对 MINERU_OUTPUT_DIR 或 BOOK_OUTPUT_DIR 根）→ 绝对路径；找不到返回空。

    论文图表存在 MINERU_OUTPUT_DIR 下，教材/书籍图表存在 BOOK_OUTPUT_DIR 下（两者互不混放），
    img_path 本身不记录是哪一个，所以两个根都试；每个根既按 CWD 又按 DB 文件所在目录找一遍
    （CWD 和 DB_PATH 目录不一致时——例如 mcp_server.sh 里 DB_PATH 指向别处——仍能找到文件）。
    """
    db_dir = os.path.dirname(os.path.abspath(settings.DB_PATH))
    for out_dir in (settings.MINERU_OUTPUT_DIR, getattr(settings, "BOOK_OUTPUT_DIR", None)):
        if not out_dir:
            continue
        basename = os.path.basename(os.path.normpath(out_dir))
        for base in (os.path.join(os.getcwd(), basename), os.path.join(db_dir, basename)):
            candidate = os.path.join(base, img_path)
            if os.path.isfile(candidate):
                return candidate
    return ""


def select_section_figures(store, reranker, section, draft, settings, console,
                           used_paths: set) -> int:
    """为一个章节挑图并追加进 draft.markdown，返回插入的图数。

    reranker 缺失（--no-rerank）或 rerank 全部降级为 0 分时不插图，
    保证不会插入不相关的图。used_paths 跨章节去重。
    """
    if not getattr(settings, "REVIEW_INSERT_FIGURES", True):
        return 0
    if reranker is None or not draft.cited_doc_ids:
        return 0

    assets = [a for a in store.get_assets_for_docs(draft.cited_doc_ids)
              if a["img_path"] not in used_paths and _clean_caption(a["caption"])]
    if not assets:
        return 0

    query = section.heading
    if section.questions:
        query += "：" + "；".join(section.questions)
    captions = [_clean_caption(a["caption"]) for a in assets]
    try:
        ranked = reranker.rerank(query, captions, top_n=len(captions))
    except Exception as e:
        console.print(f"[yellow]⚠️ 章节「{section.heading}」图题重排失败，跳过插图: {e}")
        return 0

    min_score = float(getattr(settings, "REVIEW_FIGURE_MIN_SCORE", 0.2))
    per_section = int(getattr(settings, "REVIEW_FIGURES_PER_SECTION", 2))
    picked = [(idx, score) for idx, score in ranked if score >= min_score][:per_section]
    if not picked:
        return 0

    assets_dir = os.path.join(settings.REVIEW_OUTPUT_DIR, "assets")
    blocks = []
    for idx, score in picked:
        a = assets[idx]
        src = _resolve_asset_path(a["img_path"], settings)
        if not src:
            continue
        dest_name = f"{a['doc_id'][:8]}_{os.path.basename(a['img_path'])}"
        os.makedirs(assets_dir, exist_ok=True)
        dest = os.path.join(assets_dir, dest_name)
        if not os.path.exists(dest):
            shutil.copy2(src, dest)
        label = "表" if a["asset_type"] == "table" else "图"
        caption = _clean_caption(a["caption"])
        blocks.append(f"![{label}]({'assets/' + dest_name})\n"
                      f"*{label}：{caption}（源自 [@{a['doc_id'][:8]}]）*")
        used_paths.add(a["img_path"])

    if blocks:
        draft.markdown = draft.markdown.rstrip() + "\n\n" + "\n\n".join(blocks)
    return len(blocks)
