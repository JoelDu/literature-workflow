import os
import time
import schedule
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

from rich.console import Console

# 导入共享配置与管道
import pipeline
import batch_pipeline
from utils import get_settings, init_dirs

# ── 强前置配置校验 ──────────────────────────────────────────────────────────
settings = get_settings()

console = Console()


def send_notification(title, message):
    """发送轻量级消息推送 (如 Server酱, Bark, 飞书, 钉钉等)。
    使用 requests.get(..., params=...) 对 URL 参数进行自动安全编码，规避字符串拼接漏洞。
    """
    webhook_url = settings.WEBHOOK_URL
    if not webhook_url:
        return
    try:
        import requests
        if "api.day.app" in webhook_url or "sctapi.ftqq.com" in webhook_url:
            # requests 会自动进行百分号 URL 编码，避免空格或 & 导致请求断裂
            requests.get(webhook_url, params={"title": title, "desp": message}, timeout=10)
        else:
            # 标准 Webhook POST (飞书/钉钉等)
            requests.post(webhook_url, json={"msgtype": "text", "text": {"content": f"{title}\n{message}"}}, timeout=10)
    except Exception as e:
        console.print(f"[yellow]推送通知失败: {e}")


def realtime_job():
    try:
        processed_count = pipeline.main()
        if processed_count == 0:
            # 无新文件时只打印一行淡色日志，降低噪音
            console.print(f"[dim][{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 目录扫描完毕，暂无新文件。[/dim]")
        else:
            # 有文件处理时保留醒目输出
            console.print(f"\n[bold cyan][{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 实时扫描完成，已处理 {processed_count} 篇文献。[/bold cyan]")
            send_notification("实时文献处理完成", f"已成功处理并归档 {processed_count} 篇新文献。")
    except Exception as e:
        console.print(f"\n[bold cyan][{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始执行实时扫描...[/bold cyan]")
        console.print(f"[red]实时处理发生异常: {e}")
        send_notification("实时处理异常", f"实时处理管道发生异常:\n{str(e)}")


def batch_prepare_job():
    console.print(f"\n[bold cyan][{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始提交 Batch 任务...[/bold cyan]")
    try:
        report = batch_pipeline.prepare_batch()
        
        # 静默通知：当且仅当发生新提交、本地解析失败、或向云端 API 提交失败时触发通知
        has_action = report["submitted"] > 0 or report["pdf_parsed_failed"] > 0 or report["submit_failed"] > 0
        if has_action:
            msg = (
                f"扫描文献总数: {report['scanned']}\n"
                f"成功提交云端: {report['submitted']} 篇\n"
                f"本地解析失败: {report['pdf_parsed_failed']} 篇\n"
                f"接口提交失败: {report['submit_failed']} 篇"
            )
            send_notification("Batch 任务提交报告", msg)
        else:
            console.print("[dim]无任何待提交或报错文献，守护进程保持静默。")
    except Exception as e:
        console.print(f"[red]Batch 提交发生异常: {e}")
        send_notification("Batch 提交异常", f"发生错误:\n{str(e)}")


def book_intake_job():
    console.print(f"\n[bold cyan][{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始每日教材定时入库...[/bold cyan]")
    try:
        from litreview.bookintake import run_scheduled_intake
        from llm_router import make_deepseek_client
        # 统一构造一次 SiliconFlow 客户端：既供 add_book/add_epub 的元数据补全用，
        # 也供后面自动建索引（embeddings 无论如何都需要它，跟 REVIEW_ENRICH_LLM 无关）
        client, _ = make_deepseek_client(quiet=True)
        use_llm = settings.REVIEW_ENRICH_LLM
        report = run_scheduled_intake(settings, console, client=client if use_llm else None, use_llm=use_llm)

        if report["scanned"] == 0:
            console.print("[dim]教材输入目录为空，守护进程保持静默。")
            return

        msg = (
            f"扫描教材总数: {report['scanned']}\n"
            f"成功入库: {report['ok']} 本\n"
            f"已入库跳过并归档: {report['skipped']} 本\n"
            f"入库失败(下一晚重试): {report['failed']} 本\n"
            f"文件损坏已隔离: {report['quarantined']} 本\n"
            f"页数预算用完、留到下一晚: {report['deferred']} 本\n"
            f"本轮用页: {report['pages_used']}/{settings.BOOK_DAILY_PAGE_BUDGET}"
        )
        console.print(f"[bold green]{msg}")
        if report["ok"] > 0 or report["failed"] > 0 or report["quarantined"] > 0:
            send_notification("每日教材入库报告", msg)

        # 有新书成功入库时，自动增量更新检索索引（异常隔离：失败不影响入库结果）
        if report["ok"] > 0 and settings.REVIEW_AUTO_INDEX:
            try:
                from litreview.indexer import build_index
                build_index(settings, console, client)
            except Exception as e:
                console.print(f"[yellow]⚠️ 综述索引自动更新失败（不影响入库，可手动运行 review.py index）: {e}")
    except Exception as e:
        console.print(f"[red]教材定时入库发生异常: {e}")
        send_notification("教材定时入库异常", f"发生错误:\n{str(e)}")


def batch_fetch_job():
    console.print(f"\n[bold cyan][{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始拉取 Batch 结果...[/bold cyan]")
    try:
        report = batch_pipeline.fetch_batch()
        
        # 静默通知：仅在从云端成功拉取到结果、或者有云端任务宣布夭折、或者成功导出到知识库时通知
        has_action = report["completed"] > 0 or report["failed"] > 0 or report["exported"] > 0
        if has_action:
            msg = (
                f"检查任务总数: {report['jobs_checked']}\n"
                f"云端拉取成功: {report['completed']} 篇\n"
                f"云端任务夭折: {report['failed']} 篇\n"
                f"成功导出文献: {report['exported']} 篇"
            )
            send_notification("Batch 拉取与导出报告", msg)
        else:
            console.print("[dim]云端任务处理中或目前无已完成文献，守护进程保持静默。")
    except Exception as e:
        console.print(f"[red]Batch 拉取发生异常: {e}")
        send_notification("Batch 拉取异常", f"发生错误:\n{str(e)}")


def main():
    console.rule("[bold green]文献处理常驻服务 (Daemon) 已启动")
    console.print(f"当前运行模式: [bold yellow]{settings.RUN_MODE}[/bold yellow]")
    
    # 建立持久化数据目录，包括统一的 data/ 文件夹
    data_dir = os.path.dirname(settings.EXCEL_OUTPUT_PATH)
    init_dirs(data_dir)
    
    if settings.RUN_MODE.lower() == "batch":
        console.print(
            f"调度策略: 每 {settings.BATCH_SCAN_INTERVAL_MINUTES} 分钟扫描并提交任务, "
            f"每 {settings.BATCH_FETCH_INTERVAL_MINUTES} 分钟拉取结果（及时入库模式）"
        )
        # 冷启动/重启检测：容器冷启动瞬间立即强制运行一次，提供完美的错峰重启容错
        console.print("[bold yellow]🚀 启动/重启检测：立即执行一次 Batch 任务提交与拉取...[/bold yellow]")
        batch_prepare_job()
        batch_fetch_job()

        schedule.every(settings.BATCH_SCAN_INTERVAL_MINUTES).minutes.do(batch_prepare_job)
        schedule.every(settings.BATCH_FETCH_INTERVAL_MINUTES).minutes.do(batch_fetch_job)
    else:
        console.print(f"调度策略: 每隔 {settings.SCAN_INTERVAL_MINUTES} 分钟进行一次目录扫描和实时处理")
        # 启动即时执行一次
        realtime_job()
        schedule.every(settings.SCAN_INTERVAL_MINUTES).minutes.do(realtime_job)

    # 教材定时入库：与论文的 RUN_MODE 无关，固定每天跑一次（不做冷启动即时执行，
    # 否则容器重启时间不对就会在非预期时刻触发一次）
    console.print(f"调度策略: 每日 {settings.BOOK_INTAKE_TIME} 定时入库教材，"
                  f"每晚最多处理 {settings.BOOK_DAILY_PAGE_BUDGET} 页 PDF")
    schedule.every().day.at(settings.BOOK_INTAKE_TIME).do(book_intake_job)

    # 主循环
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
