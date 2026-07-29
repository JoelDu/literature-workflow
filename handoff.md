# 磷化工文献解析器 (Literature Analyzer) - 项目交接文档

## 1. 项目概述

**Literature Analyzer** 最初是一个全自动的、基于大模型和多模态视觉模型的文献（PDF/CAJ）智能解析与结构化提取流水线，现已扩展为**入库流水线 + 综述生成器（litreview）+ 教材入库 + MCP Server** 的完整系统。它把"一堆 PDF/EPUB"自动变成结构化 Excel 知识库、可双链检索的 Obsidian 笔记，并能在已入库的文献上做检索增强生成，直接产出带引用的中文学术综述。

### 核心能力
1.  **全自动多格式支持**：论文支持 PDF 和 CAJ（底层通过 `caj2pdf` 和 `reportlab` 实现无损转码解析）；教材/书籍额外支持 EPUB（走 pandoc，无需 MinerU）。
2.  **多模态 VLM 解析**：集成 MinerU v4 API，强制使用 `vlm` 视觉大模型引擎，实现对双栏排版、复杂图表的高精度提取，支持公式（LaTeX）与表格；同时取回 docx/html/latex 全部输出格式。
3.  **大模型 Batch 并发分析**：通过 SiliconFlow 上的 DeepSeek V3 Batch API 实现极低成本、高并发的文献内容结构化提取（TLDR、摘要、结论等）。
4.  **综述生成器（litreview）**：文献分块 → Qwen3-Embedding-8B 向量化 → 向量召回 + Qwen3-Reranker-8B 精排 → 大模型逐条证据打分 → 只依据筛选证据写作、强制标注引用，输出标准论文式排版（章节编号、GB/T 7714 参考文献）。
5.  **教材/书籍入库**：`review.py add-book` 把教材当检索语料轻量入库（不做 LLM 全文分析、不进 Obsidian，省钱），PDF 走 pypdf 拆分 + MinerU，EPUB 走 pandoc；`daemon.py` 每天固定时间自动扫描入库，按页数预算控制成本。
6.  **MCP Server**：`mcp_server.py` 把检索与综述撰写包装成标准 MCP 协议 server，Claude Code/Desktop 等客户端可直接调用。
7.  **多格式自动归档导出**：
    *   **Obsidian 知识库**：带有双向链接和图文混排的 Markdown 笔记（论文笔记 + 综述笔记）。
    *   **Word 原文导出**：MinerU 解析出的官方高保真 DOCX（论文）+ pandoc 生成的综述/教材 DOCX。
    *   **Excel 结构化大表 (`knowledge_base.xlsx`)**：用于全量元数据和结论的宏观检索。
8.  **容错守护进程**：容器化运行，具备状态机持久化跟踪、失败自动重试、进度断点续传能力，论文/教材两条调度线互不干扰。

---

## 2. 系统架构与数据流向

### 目录结构与数据挂载
项目通过 Docker 容器化运行（`network_mode: host`），容器内的数据目录整体挂载至宿主机的 `/mnt/ripe/literature_analyzer_data`。论文与教材走**平行、独立**的输入/输出目录，不互相混放：

```text
/mnt/ripe/literature_analyzer_data/
├── input_pdfs/          # 📥 论文输入：待处理的 PDF/CAJ。处理完成后会被移走。
├── processed_pdfs/      # 📦 论文归档：已成功被 MinerU 提取并提交给大模型处理的原文件。
├── failed_pdfs/         # ⚠️ 论文失败目录：永久解析失败的死信文件。
├── mineru_output/       # 🗂️ 论文解析缓存：MinerU 吐出的原始中间图文数据（.md/.docx/.html/.tex/images）。永久保存不清理。
├── input_books/         # 📥 教材输入：待入库的 PDF/EPUB（BOOK_INPUT_DIR）。
├── book_output/         # 🗂️ 教材解析缓存：按“书名_doc_id前8位”分子文件夹（BOOK_OUTPUT_DIR）。
├── obsidian_磷化工文献/ # 📝 笔记输出：论文笔记 + reviews/ 子目录下的综述笔记，均带图文混排。
├── word_exports/        # 📄 Word 输出：论文的 MinerU 原生 docx。
├── knowledge_base.xlsx  # 📊 表格输出：论文 + 教材的汇总总表。
├── batch_tracking.db    # 🗄️ 状态机 DB：论文/教材解析进展、Batch Job ID，以及 litreview 的
│                        #    paper_details/paper_assets/paper_references/chunks 等结构化与向量表。
└── pipeline_history.jsonl
```

综述生成的产物（Markdown + 同名 Word）落在 `REVIEW_OUTPUT_DIR`（默认 `obsidian_vault/reviews`）。

### 核心处理管道

**论文流水线（`batch_pipeline.py` / `pipeline.py`）**
1.  **扫描与预处理 (`prepare_batch`)**：扫描 `input_pdfs`（CAJ 先转 PDF）→ MinerU VLM 多模态提取 → 拼装截断后核心内容入 Batch 请求队列 → 提交，状态置 `BATCH_SUBMITTED`。
2.  **拉取与导出 (`fetch_batch`)**：轮询 Batch 任务状态 → 完成后下载结构化响应 → 渲染 Obsidian 笔记 + 汇总进 Excel → 状态置 `EXPORTED`。
3.  **调度**：`daemon.py` 按 `RUN_MODE` 走实时（每 `SCAN_INTERVAL_MINUTES` 分钟）或批处理（每 `BATCH_SCAN_INTERVAL_MINUTES` 分钟提交、每 `BATCH_FETCH_INTERVAL_MINUTES` 分钟拉取，容器重启时冷启动补跑一次）。

**综述生成器（`review.py` + `litreview/`）**
1.  `index`：对已入库论文/教材分块并向量化（`chunker.py` + `embedder.py`），写入检索表。
2.  `enrich`：优先从 MinerU 的 `content_list.json` 本地解析（正则抓 DOI，按 block 类型取图表/参考文献），解析不出来的字段才调用 LLM 补全，写入 `paper_details`/`paper_assets`/`paper_references`。
3.  `outline` → `generate`：向量召回 → `reranker.py` 精排 → 大模型逐条给证据打分（≥ `REVIEW_MIN_SCORE` 保留）→ 只依据证据写作、强制引用标记，自动插图（`figures.py`）→ 渲染 Markdown + pandoc 导出 Word。

**教材入库（`litreview/bookintake.py`）**
- 手动：`review.py add-book [路径]`，PDF 走 pypdf 按 `BOOK_SPLIT_PAGES` 拆分再拼回同一 `doc_id`；EPUB 走 pandoc 直转。
- 定时：`daemon.book_intake_job()`，每天 `BOOK_INTAKE_TIME` 扫描 `BOOK_INPUT_DIR`，按文件名排序、PDF 累计页数不超 `BOOK_DAILY_PAGE_BUDGET` 逐本处理（排最前的单本超预算大部头例外，会处理完，避免永远排不上），成功后按 `REVIEW_AUTO_INDEX` 自动增量建索引（`REVIEW_EMBED_BACKEND=local` 时只登记待办，实际嵌入留给夜间任务，见下）。

**MCP Server（`mcp_server.py`）**
- stdio 协议，6 个工具：`library_status`、`search_literature`、`get_paper_info`、`generate_outline`、`start_review`（异步，秒回 job_id）、`review_status`（轮询）。
- 由 `mcp_server.sh` 负责加载密钥、清理代理变量、指向生产库后启动。

**夜间本地嵌入（`nightly_index.py`，2026-07-29 上线）**
- **文档嵌入改在本机跑**（`/mnt/ripe/models/Qwen3-Embedding-8B`，bf16，CPU）；**重排和查询侧嵌入仍走硅基流动在线 API** —— 本地单条查询要 195 秒冷启动，做不了交互。
- `REVIEW_EMBED_BACKEND=local` 时容器内 `build_index` **只报待办、不干活**：它挂在 `batch_fetch` 上每 120 分钟就可能触发，而本地嵌一篇论文约 24 分钟、占 15G 内存，白天当场跑会把机器占死。
- 由宿主机 crontab 在 **0–7 点每小时**试跑一次（`flock -n` 防重入），脚本自带 08:00 收工。没待办 1 秒退出且不加载权重；有待办则一直跑到收工，所以 195 秒冷启动**每晚只付一次**。
- **chunk 级断点续跑**：每 4 块落一次库；`md_hash` 未变时直接续算未嵌的块，**绝不调 `replace_doc_chunks`**（它头一句 `DELETE FROM review_chunks` 会删掉上一晚的成果）；全部块都有向量才 `mark_embedded`。一本 300 页教材约 117 块 ≈ 7.8 小时，跨夜续跑是常态。
- 速度基准:**约 60 秒/块，与块长基本无关，一夜(8h) ≈ 480 块**。别用"字符/小时"估时，会跑偏数倍。
- 日志 `/mnt/ripe/literature_analyzer_data/nightly_index.log`；手动 `/usr/bin/python3 nightly_index.py --dry-run`（**必须系统 python，torch 不在 `.venv_lit` 里**）。

---

## 3. 核心模块说明

*   `batch_pipeline.py` / `pipeline.py`：论文批处理/实时处理主逻辑（扫描、MinerU 分发、LLM Batch 提交、拉取与导出）。
*   `daemon.py`：守护进程调度器——论文的 `RUN_MODE`（实时/批处理间隔轮询）与教材每日定时入库两条调度线并行、互不干扰。
*   `review.py`：综述生成器 CLI 入口（`index`/`enrich`/`search`/`outline`/`generate`/`add-book`）。
*   `litreview/`：综述生成器核心包——`chunker.py`（分块）、`embedder.py`（Qwen3 向量化）、`reranker.py`（重排序）、`stages.py`（检索→打分→写作流程编排）、`enrich.py`（结构化元数据提取）、`figures.py`（自动插图）、`bookintake.py`（教材入库，PDF 拆分/EPUB 转换/定时调度）、`models.py`/`store.py`（SQLite 数据模型与读写）、`prompts.py`（各阶段提示词）。
*   `mcp_server.py` + `mcp_server.sh`：MCP 协议 server，把检索/综述能力暴露给 Claude Code 等客户端。
*   `mineru_client.py`：MinerU V4 API 客户端。已配置轮询重试、自动 Key 轮换及 ZIP 附件解压提取（Markdown、Images、DOCX、HTML、LaTeX 全格式取回）。
*   `llm_router.py`：大模型客户端封装。处理 DeepSeek（主力）与 Gemini 的 Batch 任务构建及系统提示词管理。
*   `caj_converter.py`：CAJ 到 PDF 的转码核心（`mutool` 检测 + `caj2pdf`/`reportlab` 兜底降级）。
*   `utils.py`：工具类，数据库状态定义、Obsidian 渲染逻辑、Excel 导出逻辑、配置读取（`get_settings`）、日志路径（跟随 `DB_PATH` 所在数据盘，而非项目目录）。
*   `cli.py`：运维 CLI（`status`/`history`/`doctor`/`reset --failed`）。

---

## 4. 近期重要改动及优化记录

按时间从早到近排列：

1.  **修复 CAJ 文件底层崩溃 (-11 段错误)**：`caj_converter.py` 采用"直接抽取文本 + ReportLab 重新排版印制 PDF"双轨降级方案，实现 100% CAJ 兼容。
2.  **MinerU 图文中间件永久保留**：关闭导出后清理 `mineru_output` 的 `shutil.rmtree`，中间产物永久保存；配 `recover_mineru.py` 按哈希重建历史缓存。
3.  **自动导出 Word (DOCX)**：拦截并集中存储 MinerU 原生 `.docx` 到 `word_exports`。
4.  **去重机制升级**：提交 Batch 前加 `custom_id` 去重过滤器，防止同一文献不同副本导致批处理拒绝服务。
5.  **litreview v2**：换成 Qwen3 嵌入 + 重排序（跨中英检索），新增 `paper_details`/`paper_assets`/`paper_references` 结构化表，综述章节编号、GB/T 7714 引用、自动插图、Word 导出全部打通；测试套件（`tests/`，35 用例）全绿。
6.  **日志路径修复**：`load_dotenv()` 提前到 `import utils` 之前，避免裸跑 CLI 时 `DB_PATH` 未生效、日志误落到项目目录（系统盘）而非数据盘。
7.  **教材入库独立目录**：新增 `BOOK_INPUT_DIR`/`BOOK_OUTPUT_DIR`，与论文的 `INPUT_PDF_DIR`/`MINERU_OUTPUT_DIR` 平行管理，不再借用后者；解析结果按"书名_doc_id前8位"分子文件夹存放。
8.  **EPUB 支持**：教材入库新增 EPUB 通道，pandoc 直转 markdown（无需 MinerU），元数据优先读 EPUB 自带 OPF/dc 字段。
9.  **MinerU 全格式输出**：解压逻辑同时取回 docx/html/latex（此前只取 md/images），硬盘充足留作备用。
10. **批处理调度改为间隔轮询**：`BATCH_PREPARE_TIME`/`BATCH_FETCH_TIME_*` 等固定时间点写法已废弃，改为 `BATCH_SCAN_INTERVAL_MINUTES`（默认 30）/`BATCH_FETCH_INTERVAL_MINUTES`（默认 120）间隔轮询，容器重启仍做冷启动补跑一次。
11. **教材每日定时入库**：`daemon.py` 新增独立调度线，每天 `BOOK_INTAKE_TIME`（默认 03:00）按 `BOOK_DAILY_PAGE_BUDGET`（默认 2000 页）预算扫描 `BOOK_INPUT_DIR` 逐本入库，与论文的 `RUN_MODE` 调度完全解耦；`docker-compose.yml` 补上 `input_books`/`book_output` 的 ripe 挂载（此前遗漏会导致容器内路径退回临时目录）。

---

## 5. 运维与部署指南

### 启动服务
项目完全容器化，依赖 Docker Compose，`network_mode: host`（宿主机防火墙拦截 docker 网桥，bridge 模式会导致容器完全断网）。
```bash
cd /home/dudu/GoogleDrive/Antigravity/literature_analyzer
DOCKER_BUILDKIT=0 docker compose build   # 部分环境网络限制，默认禁用 Buildkit
docker compose up -d
```

### 查看日志
```bash
docker logs -f lit_analyzer
```

### 手动干预指令（论文）
```bash
docker compose exec literature-analyzer python batch_pipeline.py         # 立即触发解析并提交 Batch
docker compose exec literature-analyzer python batch_pipeline.py check   # 立即检查 Batch 结果并导出笔记
docker compose exec literature-analyzer python cli.py status             # 当前任务分布看板
docker compose exec literature-analyzer python cli.py history            # 每日处理统计/失败详情
docker compose exec literature-analyzer python cli.py doctor             # 网络/密钥/挂载全面体检
docker compose exec literature-analyzer python cli.py reset --failed     # 一键重置失败任务重试
```

### 手动干预指令（教材/综述）
```bash
python review.py add-book [路径]                      # 手动入库单本/目录/默认 BOOK_INPUT_DIR
python -c "import daemon; daemon.book_intake_job()"    # 立即触发一次每日定时入库（不等凌晨）
python review.py index / enrich / search / outline / generate  # 综述生成器全流程见 README
```

### 环境配置
密钥和目录配置在 `.env`（本地）/ `/opt/docker_shared/api_keys.env`（容器 `env_file`，跨项目共享）+ `docker-compose.yml` 里的 `environment` 块（项目专属覆盖）。关键分组：
- **MinerU**：`MINERU_API_KEY` 支持逗号分隔多 Key，额度用尽自动轮换。
- **大模型**：`SILICONFLOW_API_KEY`/`SILICONFLOW_API_BASE`（论文分析、litreview 重排/查询嵌入/写作都靠它；**文档嵌入已改本地夜跑**）、`DEEPSEEK_MODEL`。
- **调度间隔**：`SCAN_INTERVAL_MINUTES`（实时模式）、`BATCH_SCAN_INTERVAL_MINUTES`/`BATCH_FETCH_INTERVAL_MINUTES`（批处理模式）、`BOOK_INTAKE_TIME`/`BOOK_DAILY_PAGE_BUDGET`（教材定时入库）。
- **litreview**：`EMBEDDING_MODEL`/`EMBEDDING_DIM`、`RERANK_MODEL`/`RERANK_CANDIDATES`、`REVIEW_MODEL`、`REVIEW_MIN_SCORE`/`REVIEW_EVIDENCE_N`、`REVIEW_INSERT_FIGURES`、`REVIEW_OUTPUT_DIR`。
- **教材入库**：`BOOK_SPLIT_PAGES`、`BOOK_INPUT_DIR`/`BOOK_OUTPUT_DIR`。
- **本地夜间嵌入**：`REVIEW_EMBED_BACKEND`(`local`/`remote`)、`LOCAL_EMBEDDING_MODEL_PATH`、`NIGHTLY_INDEX_DEADLINE`。⚠️ **`.dockerignore` 排除了 `.env`,容器只认 `docker-compose.yml` 的 `environment:`** —— 这三个里凡是容器要用的必须在 compose 里也写一份。
- ⚠️ **容器代码是 `COPY . .` 打进镜像的,不是挂载的。** 改宿主机 `.py` 对运行中的容器无效且不报错,必须 `docker compose build && docker compose up -d`;核对用 `docker exec lit_analyzer md5sum /app/<file>`。

完整清单以 `.env.example` 为准（有分组注释）。

### MCP Server 部署
```bash
claude mcp add --scope user literature-review /path/to/literature_analyzer/mcp_server.sh
```
换机器部署时只需改 `mcp_server.sh` 里的三个路径（密钥文件、数据库路径、项目根目录）。**踩过的坑**：MCP stdio 要求 stdin/stdout 是未经改动的原始字节流，SSH 转发若分配了伪终端（pty，默认或 `-t`/`-tt`）会破坏协议帧，现象是客户端"已连接"后几十~几百毫秒内立刻断开且服务端日志干净——用 `-T`（禁用 pty）解决。排障先看 `data/mcp_server.log`（不走 stdout，不污染协议流）。

---

## 6. 现状与欠缺项

入库流水线和综述生成器都已跑通并有真实文献库在用（162+ 篇/本量级，向量索引 Qwen3-Embedding-8B）；MCP 集成、检索精度升级、结构化元数据提取、GB 格式引用、章节编号、Word 导出、教材 EPUB 支持、每日定时入库均已完成，测试套件（35 用例）全绿。目前欠缺的主要是一次**真实主题的端到端 `generate` 跑一遍**（真实文献而非造的假数据）作为最终验收，检验 GB 格式引用和排版在真实数据上的表现。
