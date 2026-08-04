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
项目通过 Docker 容器化运行。**所有宿主机路径都由仓库根目录 `.env` 的 `DATA_ROOT` 决定**，默认 `./data`（clone 下来即可跑），作者机器在 `.env` 里覆盖成 `/mnt/ripe/literature_analyzer_data`。网络模式同理：`DOCKER_NETWORK_MODE` 默认 `bridge`（云服务器、别人的电脑都用这个），作者机器因宿主机防火墙拦 docker 网桥必须覆盖成 `host`，否则容器完全断网。

容器**内部**路径则在 `docker-compose.yml` 的 `environment:` 里逐条写死成 `./xxx`，故意不接受 `.env` 覆盖——`.env` 同时被宿主机侧的 CLI / `nightly_index.py` 读取，那边填的是宿主机绝对路径，一旦跟着 `env_file` 注进容器就会去扫一个不存在的目录，不报错、不干活、日志上看不出原因。

论文与教材走**平行、独立**的输入/输出目录，不互相混放：

```text
${DATA_ROOT}/                # 默认 ./data；作者机器 = /mnt/ripe/literature_analyzer_data
├── input_pdfs/          # 📥 论文输入：待处理的 PDF/CAJ。处理完成后会被移走。
├── processed_pdfs/      # 📦 论文归档：已成功被 MinerU 提取并提交给大模型处理的原文件。
├── failed_pdfs/         # ⚠️ 论文失败目录：永久解析失败的死信文件。
├── mineru_output/       # 🗂️ 论文解析缓存：MinerU 吐出的原始中间图文数据（.md/.docx/.html/.tex/images）。永久保存不清理。
├── input_books/         # 📥 教材输入：待入库的 PDF/EPUB（BOOK_INPUT_DIR）。
├── processed_books/     # 📦 教材归档：已入库的教材原件（BOOK_PROCESSED_DIR）。
├── failed_books/        # ⚠️ 教材隔离目录：损坏/永久失败的教材（BOOK_FAILED_DIR）。
├── book_output/         # 🗂️ 教材解析缓存：按“书名_doc_id前8位”分子文件夹（BOOK_OUTPUT_DIR）。
├── obsidian_磷化工文献/ # 📝 笔记输出：论文笔记 + reviews/ 子目录下的综述笔记，均带图文混排。
│                        #    目录名由 OBSIDIAN_VAULT_DIRNAME 决定，默认 obsidian_vault。
├── word_exports/        # 📄 Word 输出：论文的 MinerU 原生 docx。
├── knowledge_base.xlsx  # 📊 表格输出：论文 + 教材的汇总总表。
├── batch_tracking.db    # 🗄️ 状态机 DB：论文/教材解析进展、Batch Job ID，以及 litreview 的
│                        #    paper_details/paper_assets/paper_references/chunks 等结构化与向量表。
├── app-YYYY-MM-DD.log   # 📜 运行日志，按天分文件，超 LOG_RETENTION_DAYS（默认 30）天自动删。
├── nightly_index-YYYY-MM-DD.log  # 📜 夜间嵌入日志，同样按天分，由独立 crontab 清理。
└── pipeline_history.jsonl
```

⚠️ **`processed_books/`、`failed_books/` 不是可选项。** 已入库或损坏的教材原件必须移出 `input_books`——留在输入目录的旧书每晚会被重新计一遍页数预算，几本大部头就吃光当晚额度、新书永远排不上（2026-07-30 修复的死锁）。

综述生成的产物（Markdown + 同名 Word）落在 `REVIEW_OUTPUT_DIR`（默认 `obsidian_vault/reviews`）。

### 核心处理管道

**论文流水线（`batch_pipeline.py` / `pipeline.py`）**
1.  **扫描与预处理 (`prepare_batch`)**：扫描 `input_pdfs`（CAJ 先转 PDF）→ MinerU VLM 多模态提取 → 拼装截断后核心内容入 Batch 请求队列 → 提交，状态置 `BATCH_SUBMITTED`。
2.  **拉取与导出 (`fetch_batch`)**：轮询 Batch 任务状态 → 完成后下载结构化响应 → 渲染 Obsidian 笔记 + 汇总进 Excel → 状态置 `EXPORTED`。
3.  **调度**：`daemon.py` 按 `RUN_MODE` 走实时（每 `SCAN_INTERVAL_MINUTES` 分钟）或批处理（每 `BATCH_SCAN_INTERVAL_MINUTES` 分钟提交、每 `BATCH_FETCH_INTERVAL_MINUTES` 分钟拉取，容器重启时冷启动补跑一次）。

**综述生成器（`review.py` + `litreview/`）**
1.  `index`：对已入库论文/教材分块并向量化（`chunker.py` + `embedder.py`），写入检索表。
2.  `enrich`：优先从 MinerU 的 `content_list.json` 本地解析（正则抓 DOI，按 block 类型取图表/参考文献），解析不出来的字段才调用 LLM 补全，写入 `paper_details`/`paper_assets`/`paper_references`；扉页命中 ≥2 个 INID 码的判为专利（`litreview/patent.py`），元数据直接从著录项目取、跳过 LLM。
3.  `outline` → `generate`：向量召回 → `reranker.py` 精排 → 大模型逐条给证据打分（≥ `REVIEW_MIN_SCORE` 保留）→ 只依据证据写作、强制引用标记，自动插图（`figures.py`）→ 渲染 Markdown + pandoc 导出 Word。

**教材入库（`litreview/bookintake.py`）**
- 手动：`review.py add-book [路径]`，PDF 走 pypdf 按 `BOOK_SPLIT_PAGES` 拆分再拼回同一 `doc_id`；EPUB 走 pandoc 直转。
- 定时：`daemon.book_intake_job()`，每天 `BOOK_INTAKE_TIME` 扫描 `BOOK_INPUT_DIR`，按文件名排序、PDF 累计页数不超 `BOOK_DAILY_PAGE_BUDGET` 逐本处理（排最前的单本超预算大部头例外，会处理完，避免永远排不上），成功后按 `REVIEW_AUTO_INDEX` 自动增量建索引（`REVIEW_EMBED_BACKEND=local` 时只登记待办，实际嵌入留给夜间任务，见下）。

**专利（`litreview/patent.py`）**
- 无独立入口，与论文同一条流水线；`enrich` 按扉页 INID 码（`(21)申请号`/`(22)申请日`/`(72)发明人`…）判定，**必须 ≥2 个标记**（库里真有一篇 ResearchGate 章节正文提到 "United States Patent"，单关键词必误判，已固化为回归用例）。扉页同时印授权公告号与申请公布号时取授权号。
- ⚠️ **"状态"是两件不同的事，别合并成一列**：出版阶段（种别码 `A`/`B`/`U`/`S`）与保护期（申请日 + 20/10/15 年）印在 PDF 上、不会变，**读取时现算、不落库**（存成"已过期"会随时间变错且无人察觉）；驳回/撤回/欠年费失效/无效宣告这类法律状态**不在 PDF 上且会变**，只能人工核实，落 `legal_status` + `status_checked_at`（**必须带核实日期**）。没核实过一律显示"未核实"，不拿种别码冒充。
- `review.py patents [--rescan [--apply]] [--set <专利号|文献ID> --status <状态>]`；`--rescan` 只跑正则、不调 LLM、默认 dry-run（`enrich --force` 会对全库重跑 LLM，为了重分类两篇专利不值得）。
- ⚠️ `save_enrichment` 用的是 `INSERT OR REPLACE`（整行重写），所以写入前会先 SELECT 回 `legal_status`/`status_checked_at` 沿用——否则一次 `enrich --force` 就把人工核实结果冲成空。改那段代码务必保住这个行为，`tests/test_store.py` 有专门用例盯着。

**MCP Server（`mcp_server.py`）**
- stdio 协议，7 个工具：`library_status`、`search_literature`（可 `corpus` 限定 paper/book/patent）、`get_paper_info`、`patent_status`（**只读**，法律状态不允许模型写库）、`generate_outline`、`start_review`（异步，秒回 job_id）、`review_status`（轮询）。
- 由 `mcp_server.sh` 负责加载密钥、清理代理变量、指向生产库后启动。

**夜间本地嵌入（`nightly_index.py`，2026-07-29 上线）**
- **文档嵌入改在本机跑**（`/mnt/ripe/models/Qwen3-Embedding-8B`，bf16，CPU）；**重排和查询侧嵌入仍走硅基流动在线 API** —— 本地单条查询要 195 秒冷启动，做不了交互。
- `REVIEW_EMBED_BACKEND=local` 时容器内 `build_index` **只报待办、不干活**：它挂在 `batch_fetch` 上每 120 分钟就可能触发，而本地嵌一篇论文约 24 分钟、占 15G 内存，白天当场跑会把机器占死。
- 由宿主机 crontab 在 **22–23 点和 0–7 点每小时**试跑一次（`flock -n` 防重入），脚本自带 08:00 收工，**窗口 22:00–08:00 共 10 小时**。没待办 1 秒退出且不加载权重；有待办则一直跑到收工，所以 195 秒冷启动**每晚只付一次**。
- ⚠️ 脚本里 `MAX_HOURS` 是防手动误跑的兜底上限（取 `min(收工点, 起跑+MAX_HOURS)`），**必须 ≥ 窗口长度**。改窗口时忘了同步改它，22:05 起跑那次会被砍到 06:05 收工，白少两小时且不报错。当前 `MAX_HOURS=10`。
- **chunk 级断点续跑**：每 4 块落一次库；`md_hash` 未变时直接续算未嵌的块，**绝不调 `replace_doc_chunks`**（它头一句 `DELETE FROM review_chunks` 会删掉上一晚的成果）；全部块都有向量才 `mark_embedded`。
- 速度基准：**实测 250–300 秒/块，与块长基本无关，整夜（10h 窗口）≈ 140 块**。别用"字符/小时"估时，会跑偏数倍。
  实测样本：2026-08-03 跑满 9h55m = 144 块（248 秒/块）；2026-08-02 跑 4h16m = 52 块（296 秒/块）。
  ⚠️ **这个数比早期文档写的"约 60 秒/块"慢 4–5 倍，按老数字排期会严重低估。** 一本 500 块的大部头 ≈ 连跑 3–4 晚，中途每晚靠断点续跑接力，所以"完成 0 篇"在日志里是常态，不代表卡住了。
- 日志 `${DATA_ROOT}/nightly_index-YYYY-MM-DD.log`（**按天分文件**，crontab 里由 `$(date +\%F)` 拼出来；另有一条 08:20 的 cron 删 30 天前的旧日志）。手动排查 `/usr/bin/python3 nightly_index.py --dry-run`（**必须系统 python，torch 不在 `.venv_lit` 里**）。

---

## 3. 核心模块说明

*   `batch_pipeline.py` / `pipeline.py`：论文批处理/实时处理主逻辑（扫描、MinerU 分发、LLM Batch 提交、拉取与导出）。
*   `daemon.py`：守护进程调度器——论文的 `RUN_MODE`（实时/批处理间隔轮询）与教材每日定时入库两条调度线并行、互不干扰。
*   `review.py`：综述生成器 CLI 入口（`index`/`enrich`/`search`/`patents`/`outline`/`generate`/`add-book`）。
*   `litreview/`：综述生成器核心包——`chunker.py`（分块）、`embedder.py`（Qwen3 向量化）、`reranker.py`（重排序）、`stages.py`（检索→打分→写作流程编排）、`enrich.py`（结构化元数据提取）、`patent.py`（专利识别，只产出事实、不猜法律状态）、`figures.py`（自动插图）、`bookintake.py`（教材入库，PDF 拆分/EPUB 转换/定时调度）、`models.py`/`store.py`（SQLite 数据模型与读写）、`prompts.py`（各阶段提示词）。
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
12. **嵌入改为本地夜跑**（`nightly_index.py`）：文档嵌入从在线 API 挪到本机 Qwen3-Embedding-8B（bf16/CPU），查询侧嵌入与重排仍走在线（本地单条查询要 195 秒冷启动，做不了交互）。容器内 `build_index` 在 `REVIEW_EMBED_BACKEND=local` 时只登记待办不干活。起跑时间后来从 0 点提前到 22 点、窗口 10 小时，**`MAX_HOURS` 必须同步 8→10**，否则 22:05 起跑会被 `min(08:00, 起跑+8h)` 砍到 06:05 收工，白少两小时且不报错。
13. **教材入库归档/隔离 + 三道闸收口**：已入库的书归档出 `input_books`（留在输入目录会每晚被重新计一遍页数预算，几本大部头就吃光额度、新书永远排不上，表现为连续多晚 0 实际入库却报"成功 N 本"）；查重撞锁超时不再吞成"没入过库"（会导致大部头重跑 MinerU 重新收费）；加密/DRM 书不再被当"文件损坏"永久隔离（`FileNotDecryptedError` 是 `PdfReadError` 的子类）；归档同名文件改存 `.1`/`.2` 不再静默销毁。抽出 `ingest_one()` 让 `add-book` 与定时任务共用查重/隔离逻辑——此前手动那条既不查重也不隔离。
14. **索引与夜间嵌入的四个坑**：① `load_matrix` 按 `review_index_meta` 的 `(行数, MAX(indexed_at))` 指纹判断重载，此前常驻的 MCP 进程看不见 `nightly_index.py`（另一个进程）写入的向量，现象是 `library_status` 报"已索引 N 篇"但检索搜不到；② `build_index` 的 `--force` 清库挪到 local 分支提前返回**之前**，此前 local 后端下 `index --force` 是静默空操作，换嵌入模型后没法全量重建；③ `nightly_index` 每篇独立异常隔离，一篇坏文档不再崩掉整晚；④ `LocalEmbedder` 维度超出时按 MRL 截断再归一化，与线上 `dimensions=` 等价。
15. **运行日志按天分文件**：单个只增不减的 `app.log`（已涨到 5.9MB/8000 行）改成 `app-YYYY-MM-DD.log`，超 `LOG_RETENTION_DAYS`（默认 30，设 0 = 永久）自动删。**日期必须在每次写入时现算**，不能在 `__init__` 里定死——daemon 是常驻进程，定死的话连跑三天的日志会全堆进启动那天的文件，分天等于白分。宿主机侧 `nightly_index.log` 同步按天（crontab 重定向 + 一条 `find -mtime +30` 清理）。
16. **去宿主机化 + 三个 bug**（2026-08 阿里云部署时连着暴露出来的）：
    - **可移植**：compose 路径全部收敛到 `DATA_ROOT`，容器名/网络模式/代理走变量；`ALL_PROXY` 强制置空（镜像没装 `httpx[socks]`，继承宿主机 socks 代理会让 openai 客户端初始化就崩）；代理变量加 `LIT_` 前缀，避免 shell 里 export 的代理被 compose 变量替换悄悄注进容器。新增 `pytest.ini` 限定 `testpaths=tests`——根目录的 `test_api.py` 是手动联调脚本，import 期就 `sys.exit(1)`，被收集到会 INTERNALERROR，clone 下来敲 `pytest` 一个用例都跑不了。
    - **默认模型 `deepseek-ai/DeepSeek-V3` 已废**：2026-07-24 后它在硅基流动上任何请求都返回 `429 System is too busy now`。改默认为 `DeepSeek-V3.1-Terminus`——实测整个账户里只有 V3 / V3.1-Terminus / R1 支持 batch 推理，其余（V3.2、V4-Pro、V4-Flash、Qwen 系）提交时就报 `20088 not support batch inference`，而 V3 已废、R1 是推理模型不适合结构化抽取，Terminus 是唯一跑得通的。
    - **全失败的 Batch 会永久卡死**：云端返回 `completed` 但 `request_counts.completed == 0` 时没有 `output_file_id`，旧代码直接 raise 被外层"未知崩溃"吞掉，论文永远停在 `BATCH_SUBMITTED`——而这个状态不在阶段 1 的自动拾起名单里，等于死局。现在识别并落到 `BATCH_FAILED`（可自愈、会被重新提交），同时读 `error_file` 把真实原因写进 `error_message`。
    - **空解析结果会被当正常论文入库**：MinerU 返回 success 不代表抽出了字，扫描件和纯图版 PDF 常常只给个空 `full.md`，放行的话 LLM 只能靠标题编，编出来的还会写进 Obsidian 和 Excel。现在按 `MIN_MARKDOWN_CHARS=200` 在解析出口拦截并移进 `failed_pdfs`。**拦截点必须在归档 move 之前**，否则这份 PDF 会被当成"已处理"，人再也不会去看它。
17. **专利成为第三类文献**（`litreview/patent.py`，2026-08-04）：库里一直混着专利却被当论文处理。识别复用既有的 `doc_type` seam（该字段本就从 `docs_needing_index` → `chunk_params_for` → `search(doc_types=)` 全程打通，所以 `chunker.py` 一行没改），`paper_details` 加 4 个可空列。三个关键决定：① **判定要 ≥2 个 INID 码**——单关键词会把正文提到 "United States Patent" 的论文误判（真实案例已固化为回归用例）；② **派生值不落库**——出版阶段和保护期到期日读取时现算，存下来会随时间变错；③ **法律状态与出版阶段严格分离**，前者只能人工录入且强制记核实日期。另外专利跳过 LLM 补缺（INID 码已结构化），`--rescan` 走纯正则、默认 dry-run，避免为重分类两篇专利而 `enrich --force` 重跑全库 LLM。

---

## 5. 运维与部署指南

### 启动服务
项目完全容器化，依赖 Docker Compose。**全新机器只需填一份仓库根目录的 `.env`**（照 `.env.example` 抄，必填的只有 API Key），其余全走默认值：
```bash
cp .env.example .env && vi .env          # 填 MINERU_API_KEY / SILICONFLOW_API_KEY
docker compose up -d --build             # 首次会构建并打上 IMAGE_NAME 的 tag
```

作者机器上有两处必须由 `.env` 覆盖回来，否则跑不起来：
```bash
DOCKER_NETWORK_MODE=host                 # 宿主机防火墙拦 docker 网桥，bridge 下容器完全断网
DATA_ROOT=/mnt/ripe/literature_analyzer_data
DOCKER_BUILD_NETWORK=host                # 构建期要 git clone caj2pdf，出网必须过宿主机回环代理
```
构建若卡在网络上，加 `DOCKER_BUILDKIT=0`（部分环境 Buildkit 走不通）。

⚠️ **同一台机器上跑多套**（比如"自己用"和"给别人用"各一份）时，`CONTAINER_NAME` 和 `DATA_ROOT` **必须同时改**。只改容器名会让两套共用同一个数据库和 vault，比启动冲突严重得多。

⚠️ **对已在运行的生产容器执行 `docker compose up` 前，先在 `.env` 里把 `IMAGE_NAME` 钉成它当前实际用的镜像名**（`docker inspect <容器> --format '{{.Config.Image}}'` 查）。compose 现在默认 `lit-analyzer:1.0`，跟历史上自动生成的"目录名-服务名"对不上，会把跑着的容器迁到新镜像上重建。

云服务器拉不动 Docker Hub、1.8G 内存 build（pip 装 pandas+numpy）也吃力，走导入路线：
```bash
docker save lit-analyzer:1.0 | gzip > lit.tar.gz     # 本机
docker load < lit.tar.gz && docker compose up -d      # 服务器，镜像名对得上就不会触发构建
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

### 手动干预指令（教材/专利/综述）
```bash
python review.py add-book [路径]                      # 手动入库单本/目录/默认 BOOK_INPUT_DIR
python -c "import daemon; daemon.book_intake_job()"    # 立即触发一次每日定时入库（不等凌晨）
python review.py patents                              # 专利台账（出版阶段/保护期/法律状态）
python review.py patents --rescan [--apply]           # 重扫存量文献认专利，默认只预演
python review.py patents --set CN110346043A --status 驳回   # 人工录入法律状态并记核实日期
python review.py index / enrich / search / outline / generate  # 综述生成器全流程见 README
```

### 环境配置
**配置只有一个入口：仓库根目录的 `.env`。** compose 的 `env_file` 先读它，再读可选的 `${SHARED_ENV_FILE}`（作者机器指向 `/opt/docker_shared/api_keys.env`，跨项目集中管 Key；两者都是 `required: false`，新装用户没有第二个也照跑）。`docker-compose.yml` 的 `environment:` 块只负责两件事：给可移植默认值（一律写成 `${VAR:-默认值}`，写死会静默盖掉用户 `.env` 里的设置），以及把容器内部路径钉死。关键分组：
- **MinerU**：`MINERU_API_KEY` 支持逗号分隔多 Key，额度用尽自动轮换。
- **大模型**：`SILICONFLOW_API_KEY`/`SILICONFLOW_API_BASE`（论文分析、litreview 重排/查询嵌入/写作都靠它；**文档嵌入已改本地夜跑**）、`DEEPSEEK_MODEL`。
- **调度间隔**：`SCAN_INTERVAL_MINUTES`（实时模式）、`BATCH_SCAN_INTERVAL_MINUTES`/`BATCH_FETCH_INTERVAL_MINUTES`（批处理模式）、`BOOK_INTAKE_TIME`/`BOOK_DAILY_PAGE_BUDGET`（教材定时入库）。
- **litreview**：`EMBEDDING_MODEL`/`EMBEDDING_DIM`、`RERANK_MODEL`/`RERANK_CANDIDATES`、`REVIEW_MODEL`、`REVIEW_MIN_SCORE`/`REVIEW_EVIDENCE_N`、`REVIEW_INSERT_FIGURES`、`REVIEW_OUTPUT_DIR`。
- **教材入库**：`BOOK_SPLIT_PAGES`、`BOOK_INPUT_DIR`/`BOOK_OUTPUT_DIR`。
- **本地夜间嵌入**：`REVIEW_EMBED_BACKEND`(`local`/`remote`)、`LOCAL_EMBEDDING_MODEL_PATH`、`NIGHTLY_INDEX_DEADLINE`。
- **部署环境**：`DATA_ROOT`、`CONTAINER_NAME`、`IMAGE_NAME`、`DOCKER_NETWORK_MODE`、`DOCKER_BUILD_NETWORK`、`SHARED_ENV_FILE`、`LIT_HTTP_PROXY`/`LIT_HTTPS_PROXY`、`LOG_RETENTION_DAYS`。

⚠️ **`.dockerignore` 排除 `.env` ≠ 容器读不到 `.env`。**（早期文档写反过，别再照着改）`.dockerignore` 管的是**构建期**——不把密钥烘进镜像，这是对的、要保留。而 compose 的 `env_file` 是**运行期**从宿主机现读的，所以容器确实拿得到 `.env`。结论：**新增配置项只改 `.env` 就够，不必再去 compose 的 `environment:` 里补一份**；反过来，compose 里凡是写死（不带 `${VAR:-...}`）的项会静默盖掉 `.env`，用户改了不生效且日志上看不出原因。唯一故意写死的是容器内部路径，见 §2。

⚠️ **容器代码是 `COPY . .` 打进镜像的，不是挂载的。** 改宿主机 `.py` 对运行中的容器无效且不报错，必须 `docker compose build && docker compose up -d --force-recreate`（**只 build 不 force-recreate，compose 认为配置没变会保留旧容器，改动照样不生效**）；核对用 `docker exec <容器> md5sum /app/<file>`。

完整清单以 `.env.example` 为准（有分组注释）。

### MCP Server 部署

**本机（同一台机器）**：
```bash
claude mcp add --scope user literature-review /path/to/literature_analyzer/mcp_server.sh
```
换机器部署时改 `mcp_server.sh` 里的三个路径（密钥文件、数据库路径、项目根目录）。
⚠️ 该脚本目前仍**硬编码** `. /opt/docker_shared/api_keys.env`，没跟着去宿主机化一起改成 `${SHARED_ENV_FILE}`。别人的机器上没这个文件，`set -a` 下 source 失败会让脚本直接退出、MCP 起不来。发包前要修。

**远程（别的设备连回这台机器的库）**：走反向 SSH 隧道，不是 frp。
```text
别的设备 ──ssh──> 阿里云 8.137.35.113:22223 ──隧道──> 本机 sshd:22222 ──> mcp_server.sh
```
本机 `/etc/systemd/system/reverse-tunnel.service` 用 autossh 常驻维持：
`autossh -M 0 -N ... -p 2222 -R 22223:localhost:22222 admin@8.137.35.113`
依赖三个条件，缺一不可：113 的 sshd 开了 `GatewayPorts yes`（否则只绑 127.0.0.1，公网连不上）、113 的 ufw 放行 22223、阿里云安全组放行 22223。
排障口诀：**静默超时 = 被防火墙丢包；秒回 `Connection refused` = 端口通但没人监听；`Permission denied (publickey)` = 链路全通，只是认证没过**。

客户端配置（Windows 用 Git 自带的 `ssh.exe`）：
```json
{"mcpServers": {"literature-review": {
  "command": "C:\\Program Files\\Git\\usr\\bin\\ssh.exe",
  "args": ["-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
           "-o", "ServerAliveInterval=30", "-p", "22223", "dudu@8.137.35.113",
           "/home/dudu/GoogleDrive/Antigravity/literature_analyzer/mcp_server.sh"]}}}
```

**踩过的三个坑**（都是"客户端说连上了然后立刻断"，服务端日志干净，极难定位）：
1. **pty 破坏协议帧**：MCP stdio 要求 stdin/stdout 是未经改动的原始字节流，SSH 若分配伪终端（`-t`/`-tt`，或某些客户端默认）会破坏帧。用 `-T` 显式禁掉。
2. **`known_hosts` 缺条目**：它是按 `[主机]:端口` 存的，换公网 IP（如 `8.137.202.203` → `8.137.35.113`）等于全新条目，ssh 会弹交互式确认。没有终端可回答时，这行提示会直接窜进 stdio 协议流。用 `-o StrictHostKeyChecking=accept-new`。
3. **找不到密钥时转去要密码**：ssh 会去读 stdin——而 stdin 是 MCP 的协议输入流。`-o BatchMode=yes` 让它直接失败而不是吃掉协议数据。Git for Windows 的 `ssh.exe` 被 MCP 客户端以纯 Win32 进程拉起时 `HOME` 可能没设，找不到 `~/.ssh/id_ed25519`，需要 `-i` 写绝对路径。

排障先看 `${DATA_ROOT}/mcp_server.log`（不走 stdout，不污染协议流）。

---

## 6. 现状与欠缺项

**数据截至 2026-08-04（作者生产库）**：

| 指标 | 实际值 |
|---|---|
| 文献总数 | 179 篇/本（论文与教材同在 `papers` 表），全部 `EXPORTED` |
| 分类 | `doc_type`：论文 158 · 教材 21（其中 2 篇实为专利，`patents --rescan --apply` 尚未对生产库执行） |
| 结构化元数据 | `paper_details` 179 · `paper_assets` 13928 · `paper_references` 5301 |
| DOI 覆盖 | 77 / 176（44%），其余多为中文期刊与教材 |
| 向量索引 | 167 个文档 / 12375 块（Qwen3-Embedding-8B，4096 维） |
| Excel 汇总 | 218 行 |
| 测试套件 | 73 用例全绿（`pytest`，`pytest.ini` 已限定 `testpaths=tests`） |

入库流水线、综述生成器、MCP 集成、结构化元数据提取、GB 格式引用、章节编号、Word 导出、教材 EPUB 支持、每日定时入库、专利识别与状态台账均已完成；部署已去宿主机化，同一份 compose 可在作者机器/云服务器/别人的电脑上跑，并已在阿里云实机部署过一轮。

**欠缺项，按优先级**：

1. **真实主题的端到端 `generate` 验收**——一直没用真实文献（而非造的假数据）完整跑过一遍，GB 格式引用和排版在真实数据上的表现仍未检验。这是最老的一条欠账。
2. **Web UI（Phase 2）**——目前所有操作都是 CLI + MCP，"别人上传文件夹就能用"这个产品目标卡在这里。
3. **打包分发（Phase 3）**，以及**尚未选定 LICENSE**（分发前必须定）。
4. **`mcp_server.sh` 没跟着去宿主机化**：仍硬编码 `/opt/docker_shared/api_keys.env` 和三条绝对路径，别人的机器上直接跑不起来。
5. **元数据补缺可以少烧一半 token**：现在 173/176 篇走了 LLM 补 `doi/authors/journal/year`，但其中 77 篇已经有 DOI——这些字段用 CrossRef 免费 API 查就是权威值，比 LLM 猜得准（可参考 JabRef 的 fetcher 思路，它是 MIT 协议）。
6. **夜间嵌入慢**：实测 250–300 秒/块，一本 500 块的大部头要连跑 3–4 晚。是本地 CPU bf16 推理的固有速度，除非上 GPU 或改用更小的嵌入模型。
7. **容器以 root 运行**，产出文件属主是 root；镜像比需要的大约 400MB（Debian 的 `pandoc` 拖了 49 个包）。
8. **Excel 218 行 > DB 179 篇**，两边对不上，原因未查（Excel 是只增不减地追加，可能含已重置/删除的历史行）。
9. **专利法律状态目前全靠人工**：`patent.py` 只产出 PDF 上的事实，驳回/失效/无效这些得自己去国家知识产权局或 Google Patents 查了再 `--set` 录进来。将来若要自动同步，得接 CNIPA 或 Google Patents 的数据源，并保留 `status_checked_at` 语义（自动同步同样会过期）。
