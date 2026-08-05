import os
import glob
import shutil
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.progress import (
    Progress, SpinnerColumn, TextColumn, BarColumn,
    TaskProgressColumn, TimeElapsedColumn,
)

from mineru_client import MinerUClient
from llm_router import LLMRouter
from utils import init_dirs, build_note_metadata, generate_obsidian_note, export_to_excel, get_settings, calculate_pdf_hash, extract_original_abstract, load_processed_hashes, save_processed_hash, log_run_event, MIN_MARKDOWN_CHARS

# ── 强前置校验配置 ──────────────────────────────────────────────────────────
settings = get_settings()

console = Console()


def main():
    # 自动初始化所有目录，包括统一的 data/ 文件夹
    data_dir = os.path.dirname(settings.EXCEL_OUTPUT_PATH)
    init_dirs(
        settings.INPUT_PDF_DIR,
        settings.MINERU_OUTPUT_DIR,
        settings.OBSIDIAN_VAULT_DIR,
        settings.PROCESSED_PDF_DIR,
        settings.FAILED_PDF_DIR,
        data_dir
    )

    pdf_files = glob.glob(os.path.join(settings.INPUT_PDF_DIR, "*.pdf"))
    
    # 扫描非 PDF 文件以进行 CAJ/其它不支持格式的感知
    all_files = glob.glob(os.path.join(settings.INPUT_PDF_DIR, "*"))
    unsupported_files = [f for f in all_files if os.path.isfile(f) and not f.lower().endswith(".pdf")]
    
    if unsupported_files:
        exts = sorted(list(set(os.path.splitext(f)[1].lower() for f in unsupported_files)))
        console.print(f"[yellow]⚠️ 发现 {len(unsupported_files)} 个不支持的文件格式 ({', '.join(exts)})，仅支持 PDF 格式。")

    if not pdf_files:
        return 0

    # 只有在有 PDF 文件时才打印主横幅，降低空扫描噪音
    console.rule("[bold blue]文献知识库自动构建 Pipeline (实时模式)")

    mineru_client = MinerUClient(settings.MINERU_API_KEY)
    llm_router = LLMRouter(settings.DEEPSEEK_API_KEY, settings.GEMINI_API_KEY)

    # 加载已处理的文献哈希，避免重复处理
    processed_hashes = load_processed_hashes(settings.EXCEL_OUTPUT_PATH)

    # 收集所有结果，最后批量写入 Excel
    excel_rows: list[dict] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        main_task = progress.add_task("[bold cyan]总进度...", total=len(pdf_files))

        for pdf_path in pdf_files:
            filename = os.path.basename(pdf_path)
            title = os.path.splitext(filename)[0]
            
            # 计算稳定的 SHA-256 ID，规避同名覆盖风险
            doc_id = calculate_pdf_hash(pdf_path)

            # 去重校验：如果已在历史记录中，直接跳过并前移进度条
            if doc_id in processed_hashes:
                console.print(f"[dim]跳过已处理文献: {filename}[/dim]")
                progress.advance(main_task)
                continue

            # Step 1: MinerU 解析
            progress.update(main_task, description=f"[cyan]MinerU 解析: {filename}")
            try:
                # 解析输出以 8 位哈希区分，多版本不冲突
                output_folder = os.path.join(settings.MINERU_OUTPUT_DIR, f"{title}_{doc_id[:8]}")
                mineru_res = mineru_client.process_pdf(pdf_path, output_folder)
                md_text = mineru_res["markdown"]

                # 空正文按永久失败处理（同 batch_pipeline）：MinerU 返回 success 不代表抽出了字，
                # 扫描件 / 纯图版 PDF 常常只给个空 full.md。放行的话 LLM 只能靠标题编，
                # 编出来的东西还会被当成正常结果写进 Obsidian 和 Excel。
                if len(md_text.strip()) < MIN_MARKDOWN_CHARS:
                    err_msg = f"MinerU 解析出的正文只有 {len(md_text.strip())} 字符（阈值 {MIN_MARKDOWN_CHARS}），大概率是扫描件或纯图版 PDF"
                    console.print(f"[red]正文为空，跳过 {filename}: {err_msg}")
                    shutil.move(pdf_path, os.path.join(settings.FAILED_PDF_DIR, filename))
                    log_run_event(mode="realtime", event="paper_processed", title=title, doc_id=doc_id,
                                  status="failed", error=err_msg, extra={"stage": "mineru", "type": "empty_markdown"})
                    progress.advance(main_task)
                    continue

                images = []
                if mineru_res["images_dir"] and os.path.exists(mineru_res["images_dir"]):
                    for ext in ["*.png", "*.jpg", "*.jpeg"]:
                        images.extend(
                            glob.glob(os.path.join(mineru_res["images_dir"], "**", ext), recursive=True)
                        )
                images = [os.path.abspath(img) for img in images]

            except Exception as e:
                import requests
                err_msg = str(e).lower()
                is_transient = (
                    any(kw in err_msg for kw in ["parsing failed", "try again later", "timeout"])
                    or isinstance(e, (TimeoutError, ConnectionError, requests.RequestException))
                )
                if is_transient:
                    console.print(f"[yellow]⚠️ MinerU 解析临时失败 (待重试) {filename}: {e}")
                    log_run_event(mode="realtime", event="paper_processed", title=title, doc_id=doc_id, status="failed", error=f"MinerU 临时失败: {e}", extra={"stage": "mineru", "type": "transient"})
                else:
                    console.print(f"[red]MinerU 解析永久失败 {filename}: {e}")
                    shutil.move(pdf_path, os.path.join(settings.FAILED_PDF_DIR, filename))
                    log_run_event(mode="realtime", event="paper_processed", title=title, doc_id=doc_id, status="failed", error=f"MinerU 永久失败: {e}", extra={"stage": "mineru", "type": "permanent"})
                progress.advance(main_task)
                continue

            # Step 2: LLM 分析
            progress.update(main_task, description=f"[magenta]AI 分析: {filename}")
            try:
                analysis = llm_router.analyze_paper(md_text)
                if "error" in analysis:
                    err_msg = str(analysis['error']).lower()
                    is_transient = any(kw in err_msg for kw in ["timeout", "network", "rate limit", "connection", "try again", "busy"])
                    if is_transient:
                        console.print(f"[yellow]⚠️ LLM 分析出错 (临时失败) {filename}: {analysis['error']}")
                        log_run_event(mode="realtime", event="paper_processed", title=title, doc_id=doc_id, status="failed", error=f"LLM 临时出错: {analysis['error']}", extra={"stage": "llm", "type": "transient"})
                    else:
                        console.print(f"[red]LLM 分析出错 (永久失败) {filename}: {analysis['error']}")
                        shutil.move(pdf_path, os.path.join(settings.FAILED_PDF_DIR, filename))
                        log_run_event(mode="realtime", event="paper_processed", title=title, doc_id=doc_id, status="failed", error=f"LLM 永久出错: {analysis['error']}", extra={"stage": "llm", "type": "permanent"})
                    progress.advance(main_task)
                    continue

                # 提取原始摘要，并与 LLM 提取的做融合校验，确保 100% 正确保留原始摘要
                orig_abstract = extract_original_abstract(md_text)
                analysis["abstract"] = orig_abstract or analysis.get("abstract", "")

            except Exception as e:
                import requests
                err_msg = str(e).lower()
                is_transient = (
                    any(kw in err_msg for kw in ["timeout", "network", "rate limit", "connection", "try again", "busy"])
                    or isinstance(e, (TimeoutError, ConnectionError, requests.RequestException))
                )
                if is_transient:
                    console.print(f"[yellow]⚠️ LLM 调用临时失败 (待重试) {filename}: {e}")
                    log_run_event(mode="realtime", event="paper_processed", title=title, doc_id=doc_id, status="failed", error=f"LLM 调用临时失败: {e}", extra={"stage": "llm", "type": "transient"})
                else:
                    console.print(f"[red]LLM 调用异常 (永久失败) {filename}: {e}")
                    shutil.move(pdf_path, os.path.join(settings.FAILED_PDF_DIR, filename))
                    log_run_event(mode="realtime", event="paper_processed", title=title, doc_id=doc_id, status="failed", error=f"LLM 调用永久失败: {e}", extra={"stage": "llm", "type": "permanent"})
                progress.advance(main_task)
                continue

            # Step 3: 保存 Obsidian 笔记，传入唯一 ID 防覆盖，并且图片复制并转为相对路径
            progress.update(main_task, description=f"[green]生成笔记: {filename}")
            note_meta = build_note_metadata(title, md_text, analysis.get("language", ""))
            note_path = generate_obsidian_note(note_meta, analysis, images, settings.OBSIDIAN_VAULT_DIR, doc_id)
            note_filename = os.path.basename(note_path)
            obsidian_link_name = os.path.splitext(note_filename)[0]

            # Step 3.5: 清理 MinerU 中间产物
            shutil.rmtree(output_folder, ignore_errors=True)
            console.print(f"[dim]已清理 MinerU 中间文件: {output_folder}[/dim]")

            # Step 4: 归档原始 PDF 到归档文件夹（必须在记录 Excel 路径之前移动！）
            dest_pdf_path = os.path.join(settings.PROCESSED_PDF_DIR, filename)
            try:
                shutil.move(pdf_path, dest_pdf_path)
            except Exception as e:
                console.print(f"[yellow]归档 PDF 失败 {filename}, 回退记录原路径: {e}")
                dest_pdf_path = pdf_path

            # Step 5: 收集 Excel 行（记录归档后的真实路径，绝不失效！）
            excel_rows.append({
                "文献ID": doc_id,
                "标题": title,
                "作者": analysis.get("authors", ""),
                "年份": analysis.get("year", ""),
                "期刊": analysis.get("journal", ""),
                "文件路径": dest_pdf_path,
                "Obsidian链接": f"[[{obsidian_link_name}]]",
                "TLDR": analysis.get("tldr", ""),
                "语言": analysis.get("language", ""),
                "解析时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

            # 保存已处理成功的文献哈希
            save_processed_hash(settings.EXCEL_OUTPUT_PATH, doc_id)
            processed_hashes.add(doc_id)
            log_run_event(mode="realtime", event="paper_processed", title=title, doc_id=doc_id, status="success")

            progress.advance(main_task)

    # Step 6: 批量写入 Excel（一次性，按文献ID精确去重）
    if excel_rows:
        export_to_excel(excel_rows, settings.EXCEL_OUTPUT_PATH)
        console.print(f"[green]✅ 已写入 {len(excel_rows)} 条记录到 {settings.EXCEL_OUTPUT_PATH}")

    console.rule("[bold green]✅ 所有文献处理完成！")
    return len(excel_rows)


if __name__ == "__main__":
    main()

