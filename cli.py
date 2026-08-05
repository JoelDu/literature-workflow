import os
import sys
import sqlite3
import argparse
from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.table import Table

# 引入项目依赖
from utils import get_settings, init_dirs, calculate_pdf_hash
from mineru_client import MinerUClient
from llm_router import LLMRouter
import batch_pipeline

console = Console()

try:
    settings = get_settings()
except SystemExit:
    console.print("[bold red]❌ 配置校验未通过，请优先修复环境变量配置！[/bold red]")
    sys.exit(1)


def show_status():
    """读取 SQLite，分析文献库当前的各状态分布，并使用 Rich 渲染精美的状态看板。"""
    console.rule("[bold cyan]📊 Literature Analyzer 状态看板")
    
    if not os.path.exists(settings.DB_PATH):
        console.print(f"[yellow]数据库文件 {settings.DB_PATH} 尚未创建。请先投入文献进行处理！")
        return
        
    try:
        conn = sqlite3.connect(settings.DB_PATH)
        c = conn.cursor()
        
        # 1. 统计总体分布
        c.execute("SELECT status, COUNT(*) FROM papers GROUP BY status")
        rows = c.fetchall()
        
        if not rows:
            console.print("[yellow]数据库中目前没有任何文献记录。")
            conn.close()
            return
            
        table = Table(title="文献处理状态分布统计")
        table.add_column("状态代码 (Status)", style="cyan")
        table.add_column("文献数量 (Count)", style="bold green", justify="right")
        table.add_column("状态释义 (Definition)", style="dim")
        
        definitions = {
            "PARSED": "已解析 PDF，等待批处理提交",
            "SUBMIT_FAILED": "向 AI 服务商提交 Batch 任务失败，等待重试",
            "BATCH_SUBMITTED": "已提交给云端 Batch 等待处理中",
            "BATCH_FAILED": "云端 Batch 任务在服务器端执行失败，等待重试",
            "RESULT_PARSE_FAILED": "拉取结果成功，但解析 JSON 或持久化出错，等待重试",
            "COMPLETED": "云端处理完成，等待导出",
            "EXPORTED": "已完美归档并成功导出至 Obsidian 与 Excel"
        }
        
        total = 0
        for status, count in rows:
            table.add_row(status, str(count), definitions.get(status, "未知状态"))
            total += count
            
        console.print(table)
        console.print(f"📊 数据库中文献总量: [bold green]{total}[/bold green] 篇\n")
        
        # 2. 列出最近失败的任务以便运维排查
        c.execute(
            "SELECT id, title, status, error_message FROM papers WHERE status IN ('SUBMIT_FAILED', 'BATCH_FAILED', 'RESULT_PARSE_FAILED') LIMIT 5"
        )
        failures = c.fetchall()
        if failures:
            fail_table = Table(title="最近失败/异常文献排查 (最多显示 5 篇)", show_lines=True)
            fail_table.add_column("文献 ID", style="dim", max_width=12)
            fail_table.add_column("文献标题 (Title)", style="yellow")
            fail_table.add_column("异常状态 (Status)", style="bold red")
            fail_table.add_column("故障原因 (Error Message)", style="red")
            
            for doc_id, title, status, err_msg in failures:
                fail_table.add_row(doc_id, title, status, err_msg or "未知异常")
            console.print(fail_table)
            console.print("[bold yellow]💡 提示：您可以使用 `python cli.py reset --failed` 一键恢复并自动重新提交这些失败任务！[/bold yellow]")
            
        conn.close()
    except Exception as e:
        console.print(f"[bold red]读取数据库失败: {e}")


def run_doctor():
    """全面诊断当前部署配置，对网络、密钥、文件权限进行体检。"""
    console.rule("[bold cyan]🩺 Literature Analyzer 系统健康体检 (Doctor)")
    
    # 1. 检验实际使用的凭据。DeepSeek 官方 Key 只是 SiliconFlow 未配置时的回退；
    # Gemini 已不参与新任务，不再作为健康检查前置条件。
    console.print("\n[bold]1. API 密钥合规性校验:[/bold]")
    all_keys_ok = True
    mineru_key = settings.MINERU_API_KEY
    if not mineru_key or len(mineru_key) < 5:
        console.print("  [-] [red]✖ MINERU_API_KEY 缺失或无效！[/red]")
        all_keys_ok = False
    else:
        console.print("  [+] [green]✔ MINERU_API_KEY 已配置[/green] (****)")

    sf_key = os.getenv("SILICONFLOW_API_KEY", "")
    sf_base = os.getenv("SILICONFLOW_API_BASE", "")
    if sf_key and sf_base:
        console.print("  [+] [green]✔ SiliconFlow LLM 路由已配置[/green] (****)")
    elif settings.DEEPSEEK_API_KEY:
        console.print("  [+] [green]✔ 官方 DeepSeek 回退路由已配置[/green] (****)")
    else:
        console.print("  [-] [red]✖ 未配置 SiliconFlow 或官方 DeepSeek LLM 路由！[/red]")
        all_keys_ok = False
            
    # 2. 挂载目录读写权校验
    console.print("\n[bold]2. 存储及挂载目录校验:[/bold]")
    dirs = [
        ("输入目录 (INPUT_PDF_DIR)", settings.INPUT_PDF_DIR),
        ("解析目录 (MINERU_OUTPUT_DIR)", settings.MINERU_OUTPUT_DIR),
        ("笔记目录 (OBSIDIAN_VAULT_DIR)", settings.OBSIDIAN_VAULT_DIR),
        ("归档目录 (PROCESSED_PDF_DIR)", settings.PROCESSED_PDF_DIR),
        ("失败目录 (FAILED_PDF_DIR)", settings.FAILED_PDF_DIR),
        ("教材输入目录 (BOOK_INPUT_DIR)", settings.BOOK_INPUT_DIR),
        ("教材解析目录 (BOOK_OUTPUT_DIR)", settings.BOOK_OUTPUT_DIR),
        ("持久数据 (data/)", os.path.dirname(settings.EXCEL_OUTPUT_PATH))
    ]
    all_dirs_ok = True
    for desc, path in dirs:
        try:
            os.makedirs(path, exist_ok=True)
            # 测试写入权限
            test_file = os.path.join(path, ".doctor_write_test")
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
            console.print(f"  [+] [green]✔ {desc}[/green] 读写正常 ({path})")
        except Exception as e:
            console.print(f"  [-] [red]✖ {desc} 权限异常: {e}[/red]")
            all_dirs_ok = False
            
    # 3. 基础连通性与代理连线测试
    console.print("\n[bold]3. 外部网络连通度与 AI 路由体检 (可能耗时数秒):[/bold]")
    try:
        import requests
        proxies = {}
        if os.getenv("HTTP_PROXY"):
            proxies["http"] = os.getenv("HTTP_PROXY")
        if os.getenv("HTTPS_PROXY"):
            proxies["https"] = os.getenv("HTTPS_PROXY")
            
        console.print(f"  使用系统代理配置: {proxies or '无'}")
        
        # 测试 MinerU client
        try:
            resp = requests.get("https://cve-api.open-miner.com/heartbeat", timeout=5) # 示意网络心跳
            console.print("  [+] [green]✔ MinerU 云端基础网络通顺[/green]")
        except Exception:
            console.print("  [dim]  [-] MinerU 云接口无心跳应答（非致命错误）[/dim]")
            
        # 测试当前配置的 LLM 兼容接口连通性
        llm_base = sf_base or os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
        try:
            resp = requests.get(llm_base, timeout=5, proxies=proxies)
            console.print(f"  [+] [green]✔ LLM API 物理连通正常[/green] (Status: {resp.status_code})")
        except Exception as e:
            console.print(f"  [-] [red]✖ 无法连接至 LLM API (请检查容器代理): {e}[/red]")
            all_keys_ok = False
            
    except Exception as e:
        console.print(f"  [-] [red]✖ 网络体检工具运行失败: {e}[/red]")
        
    console.print("\n")
    if all_keys_ok and all_dirs_ok:
        console.print("[bold green]🎉 体检报告：所有配置完美通畅，环境处于最佳运行状态！[/bold green]\n")
    else:
        console.print("[bold red]⚠ 体检报告：系统发现部分异常，请根据上方红字修正后再试。[/bold red]\n")


def reset_tasks(failed_only=False, doc_id=None):
    """人工介入，重置失败文献或指定文献到 PARSED 状态，以便重新进入 Batch 管道进行自动提交重试。"""
    if not os.path.exists(settings.DB_PATH):
        console.print("[red]数据库尚未创建，无需重置。")
        return
        
    conn = sqlite3.connect(settings.DB_PATH)
    c = conn.cursor()
    
    if doc_id:
        c.execute("SELECT title, status FROM papers WHERE id=?", (doc_id,))
        row = c.fetchone()
        if not row:
            console.print(f"[red]✖ 未在数据库中找到 ID 为 {doc_id} 的文献！[/red]")
            conn.close()
            return
        c.execute("UPDATE papers SET status=?, error_message=NULL WHERE id=?", (batch_pipeline.STATUS_PARSED, doc_id))
        conn.commit()
        console.print(f"[bold green]✔ 成功重置文献 '{row[0]}' 为 PARSED 状态，将重新处理。[/bold green]")
        
    elif failed_only:
        c.execute(
            "SELECT COUNT(*) FROM papers WHERE status IN ('SUBMIT_FAILED', 'BATCH_FAILED', 'RESULT_PARSE_FAILED')"
        )
        count = c.fetchone()[0]
        if count == 0:
            console.print("[green]目前没有任何失败或故障状态的任务需要重置。")
        else:
            c.execute(
                "UPDATE papers SET status=?, error_message=NULL WHERE status IN ('SUBMIT_FAILED', 'BATCH_FAILED', 'RESULT_PARSE_FAILED')",
                (batch_pipeline.STATUS_PARSED,)
            )
            conn.commit()
            console.print(f"[bold green]✔ 已成功将 {count} 篇失败文献一键重置为 PARSED，下一轮调度会自动重新捡起并提云端。[/bold green]")
            
    else:
        console.print("[red]请指定重置参数：--failed 重置全部失败，或 --id <文献ID> 重置指定单篇。[/red]")
        
    conn.close()


def show_history(days=7):
    """读取 data/pipeline_history.jsonl，分析最近数天的运行状况并生成可视化报告。"""
    console.rule(f"[bold cyan]📅 Literature Analyzer 运行历史 (最近 {days} 天)")

    from utils import _data_dir
    history_file = os.path.join(_data_dir(), "pipeline_history.jsonl")
    if not os.path.exists(history_file):
        console.print("[yellow]暂无运行历史记录日志。[/yellow]")
        return
        
    try:
        import json
        from datetime import datetime
        from collections import defaultdict
        
        daily_stats = defaultdict(lambda: {
            "realtime_success": 0,
            "realtime_failed": 0,
            "batch_parsed_success": 0,
            "batch_parsed_failed": 0,
            "batch_submitted": 0,
            "batch_submit_failed": 0,
            "batch_fetched_success": 0,
            "batch_fetched_failed": 0,
            "paper_exported": 0
        })
        
        with open(history_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    ts = entry.get("timestamp", "")
                    if not ts:
                        continue
                    date_str = ts.split(" ")[0]
                    # 计算与当前时间的差距天数，过滤超出天数的数据
                    try:
                        entry_date = datetime.strptime(date_str, "%Y-%m-%d")
                        delta_days = (datetime.now() - entry_date).days
                        if delta_days > days:
                            continue
                    except ValueError:
                        continue
                        
                    mode = entry.get("mode")
                    event = entry.get("event")
                    status = entry.get("status")
                    
                    stats = daily_stats[date_str]
                    if mode == "realtime":
                        if event == "paper_processed":
                            if status == "success":
                                stats["realtime_success"] += 1
                            else:
                                stats["realtime_failed"] += 1
                    elif mode == "batch":
                        if event == "paper_parsed":
                            if status == "success":
                                stats["batch_parsed_success"] += 1
                            else:
                                stats["batch_parsed_failed"] += 1
                        elif event == "batch_submitted":
                            if status == "success":
                                stats["batch_submitted"] += entry.get("count", 1)
                            else:
                                stats["batch_submit_failed"] += entry.get("count", 1)
                        elif event == "batch_fetched":
                            if status == "success":
                                stats["batch_fetched_success"] += entry.get("completed", 0)
                                stats["batch_fetched_failed"] += entry.get("failed", 0)
                        elif event == "paper_exported":
                            if status == "success":
                                stats["paper_exported"] += 1
                except Exception:
                    continue
                    
        if not daily_stats:
            console.print("[yellow]最近几日无任何管道运行事件。[/yellow]")
            return
            
        # 生成表格
        table = Table(title="每日文献处理运行汇总统计")
        table.add_column("日期", style="cyan")
        table.add_column("实时模式 (成功/失败)", style="green")
        table.add_column("Batch解析 (成功/失败)", style="magenta")
        table.add_column("Batch提交 (篇数)", style="blue")
        table.add_column("Batch拉取 (成功/夭折)", style="yellow")
        table.add_column("最终归档导出 (篇数)", style="bold green")
        
        # 按日期倒序排列
        for date_str in sorted(daily_stats.keys(), reverse=True):
            stats = daily_stats[date_str]
            realtime_str = f"{stats['realtime_success']} / [red]{stats['realtime_failed']}[/red]"
            parsed_str = f"{stats['batch_parsed_success']} / [red]{stats['batch_parsed_failed']}[/red]"
            sub_str = f"{stats['batch_submitted']} 篇" + (f" ([red]失败 {stats['batch_submit_failed']} 篇[/red])" if stats['batch_submit_failed'] > 0 else "")
            fetch_str = f"{stats['batch_fetched_success']} / [red]{stats['batch_fetched_failed']}[/red]"
            exported_str = f"{stats['paper_exported']} 篇"
            
            table.add_row(date_str, realtime_str, parsed_str, sub_str, fetch_str, exported_str)
            
        console.print(table)
        
        # 打印最近 5 次失败事件的详细原因以供排查
        # quarantined（书籍入库判定文件损坏、已挪进 failed_books 不再重试）是**终态**，
        # 比普通 failed 更需要人看一眼，所以一并列出——否则它只在发生当晚推送过一次，
        # 事后完全查不到痕迹。
        _BAD = {"failed": "失败", "quarantined": "已隔离(不再重试)"}
        console.print("\n[bold red]🚨 最近 5 次失败/隔离事件详情:[/bold red]")
        fail_count = 0
        with open(history_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            for line in reversed(lines):
                if fail_count >= 5:
                    break
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    label = _BAD.get(entry.get("status"))
                    if label:
                        ts = entry.get("timestamp")
                        mode = entry.get("mode")
                        event = entry.get("event")
                        title = entry.get("title", "")
                        error = entry.get("error", "")

                        title_info = f"文献: '{title}' | " if title else ""
                        console.print(f"  [{ts}] [{mode}] {event} {label} | {title_info}原因: [red]{error}[/red]")
                        fail_count += 1
                except Exception:
                    continue

            if fail_count == 0:
                console.print("[green]  ✔ 最近没有发现任何失败或隔离事件。[/green]")
                
    except Exception as e:
        console.print(f"[bold red]加载历史记录失败: {e}[/bold red]")


def main():
    parser = argparse.ArgumentParser(description="Literature Analyzer 运维及状态管理 CLI")
    subparsers = parser.add_subparsers(dest="command", help="运维指令集")
    
    # status 命令
    subparsers.add_parser("status", help="查看文献库各状态数据汇总看板")
    
    # history 命令
    history_parser = subparsers.add_parser("history", help="查看最近数天的文献处理运行历史与成功/失败统计")
    history_parser.add_argument("--days", type=int, default=7, help="查看天数，默认 7 天")
    
    # doctor 命令
    subparsers.add_parser("doctor", help="对系统网络、API 密钥、存储挂载做全方位系统健康度体检")
    
    # reset 命令
    reset_parser = subparsers.add_parser("reset", help="人工强制重置失败文献或指定任务以启动自动重试")
    reset_parser.add_argument("--failed", action="store_true", help="一键重置所有失败状态的任务 (SUBMIT_FAILED, BATCH_FAILED, RESULT_PARSE_FAILED)")
    reset_parser.add_argument("--id", type=str, help="根据特定的文献哈希 ID 重置指定单篇文章")
    
    # run 命令
    run_parser = subparsers.add_parser("run", help="手工强制触发任务周期动作")
    run_parser.add_argument("action", choices=["prepare", "fetch", "all"], help="触发动作: prepare(准备并提Batch), fetch(拉取并导出), all(二者连续执行)")
    
    args = parser.parse_args()
    
    if args.command == "status":
        show_status()
    elif args.command == "history":
        show_history(days=args.days)
    elif args.command == "doctor":
        run_doctor()
    elif args.command == "reset":
        reset_tasks(failed_only=args.failed, doc_id=args.id)
    elif args.command == "run":
        if args.action in ("prepare", "all"):
            batch_pipeline.prepare_batch()
        if args.action in ("fetch", "all"):
            batch_pipeline.fetch_batch()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
