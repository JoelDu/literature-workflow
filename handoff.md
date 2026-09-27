# 磷化工文献解析器 (Literature Analyzer) - 项目交接文档

## 2026-09-27 修复与发布日志

- 完成目的与实现审核的八项修复：实时正文持久化、发现库联合检索、教材嵌入客户端隔离、UTC 配额延期、Web 转发头鉴权、MCP HTTP/SSE 守卫、Web 逐章检查点恢复、真实向量入库状态。
- 补齐发现文献唯一引用键，拒绝有歧义的缩写；无全文不再用标题代替正文，重新入库保持隔离/删除状态。
- Web 新增「综述生成与恢复」入口；任务独立保存到 `web_review_jobs.db`，按单 Web 进程运行。
- 验证：隔离集成回归 214 passed（1 warning）；最终 UTC 延期与恢复状态调整后，受影响套件 21 passed。两个页面脚本语法检查通过。未发起收费生成或修改生产文献数据。
- 配置：发现库统一 `TEST_CITATION_DB_PATH` / `CITATION_NETWORK_DB_PATH`；可信反向代理使用 `REMOTE_TRUSTED_PROXY_CIDRS`，自定义 Uvicorn 启动须加 `--no-proxy-headers`。
- 2026-09-27 08:52–08:54（Asia/Shanghai）已发布并重启：系统级 `literature-web.service`、用户级 `literature-review-mcp.service`、Docker `lit_analyzer`。重启前 Web 无生成任务、MCP 无 running/queued 任务。
- 容器依赖清单与工作区一致，基于原镜像构建代码更新镜像 `lit-analyzer:repair-20260927`，同时更新 `lit-analyzer:1.0` 并重建容器；数据挂载保持一致。旧容器镜像保留为 `lit-analyzer:rollback-20260927`。
- 发布验证：Web 首页、综述页面/脚本及任务接口均 HTTP 200；MCP initialize HTTP 200；容器 6 个关键文件 SHA-256 与工作区一致，状态 running、RestartCount=0。冷启动没有待提交/待拉取 Batch，已进入正常定时调度。输入目录已有一个不支持的 `.djvu` 文件，仅警告，未移动该文件。
- 详细记录：[修复记录](docs/project-purpose-fixes-2026-09-26.md)。

## 1. 项目概述

**Literature Analyzer** 最初是一个全自动的、基于大模型和多模态视觉模型的文献（PDF/CAJ）智能解析与结构化提取流水线，现已扩展为**入库流水线 + 综述生成器（litreview）+ 教材入库 + MCP Server** 的完整系统。它把"一堆 PDF/EPUB"自动变成结构化 Excel 知识库、可双链检索的 Obsidian 笔记，并能在已入库的文献上做检索增强生成，直接产出带引用的中文学术综述。

### 核心能力
1.  **全自动多格式支持**：论文支持 PDF 和 CAJ（底层通过 `caj2pdf` 和 `reportlab` 实现无损转码解析）；教材/书籍额外支持 EPUB（走 pandoc，无需 MinerU）。
2.  **多模态 VLM 解析**：集成 MinerU v4 API，强制使用 `vlm` 视觉大模型引擎，实现对双栏排版、复杂图表的高精度提取，支持公式（LaTeX）与表格；同时取回 docx/html/latex 全部输出格式。
3.  **大模型分析与额度控制**：论文分析固定通过 OpenAI `gpt-5.4-mini` Batch 做结构化提取；元数据补缺和证据评分也共用 mini 的 UTC 日 200 万 token 账本。论文 Batch 每批最多预留 60 万、单篇输入最多 20 万，结果取回后按实际 `total_tokens` 入账再递推。综述大纲/章节/引言结论固定 `gpt-5.4`，不走 Batch、同步返回后立刻按实际用量入账，UTC 日硬上限 20 万（账户免费额度 25 万）。任一预留越过上限即拒绝发起，等待下一个 UTC 00:00，避免进入付费额度。旧的 SiliconFlow Batch 仅保留拉取兼容，不再新建。
4.  **综述生成器（litreview）**：硅基流动 Qwen3-Embedding-8B 向量化 → Qwen3-Reranker-8B 精排 → `gpt-5.4-mini` 逐条证据打分 → 固定 `gpt-5.4` 写大纲/章节/引言结论。每章先精排 50 条、最终最多 20 条证据；综述正文另有 UTC 日 22 万 token 上限。
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

**文献类型（`litreview/doctype.py`——加类型只改这一个文件）**
- 8 种类型 + GB/T 7714-2015 码：`paper[J]` `book[M]` `patent[P]` `standard[S]` `report[R]` `thesis[D]` `conf[C]` `web[EB/OL]`。`DOC_TYPES` 是**唯一真相源**：`review.py` 的 `--corpus`/`--type`/`--status` 选项、MCP 的参数校验、`chunker` 的分块参数全部由它派生。**加一种 = 加一行词表 + 在 `stages.format_reference` 加一个分支**，`tests/test_reference_format.py::test_every_declared_type_prints_its_own_code` 会在漏了分支时当场炸。
- ⚠️ **只有 `patent` 和 `standard` 能自动识别**（`detect: True`），这不是偷懒：专利有 INID 码、标准有强制封面格式，都是硬格式；而**报告/财报/网页跟一篇长论文在版式上没有任何可靠差别**，凭关键词猜的误判率高到会污染整个库。这几类只能 `review.py set-type` 人工指定，`test_only_patent_and_standard_claim_auto_detection` 盯着这条不被人偷偷放宽。
- ⚠️ **自动识别一律要求 ≥2 个标记，绝不凭单个关键词定案**——论文正文写"按 GB/T 4945—2002 测定"是家常便饭。
- `statuses_for(t)` 对没有"会失效"概念的类型返回**空列表**（`paper`/`report`/…）。曾经的真实 bug：它兜底返回专利那套选项，于是命令行允许把一篇期刊论文标成"已授权"。
- `review.py types [--rescan [--apply]] [--only <类型>]` 是**唯一**的重扫入口（专利+标准一起扫，只跑正则、不调 LLM、默认 dry-run）。`patents --rescan` **已移除**，别再加回来。
- `review.py set-type <doc_id前缀|专利号|标准号|标题片段> --type X [--no/--url/--title/--authors/--publisher/--place/--year]`；多条命中时拒绝执行，除非显式 `--all`。

**标准（`doctype.detect_standard`）**
- 判据：`中华人民共和国国家标准` 类抬头 + 标准号 + `发布`/`实施` 双日期，同样 ≥2 个标记。抽出标准号、中英文名、发布/实施日期、发布机构。
- ⚠️ 三个真实的坑，**所有正样本都是从生产库原样抠出来的封面文本**（`tests/test_doctype.py`，以后改正则先来这里加真实样本）：①`代替 GB/T 1677—1981` 出现在真编号**前面**，先撞上就会把作废旧版号当本标准号；②MinerU 把斜杠破折号包进标签，封面 `GB/T 32952—2016` 解析成 `GB<sup>/</sup>T32952<sup>—</sup>2016`，不剥标签连编号都匹配不上；③造字子集乱码 `犌犅／犜7363—2021` = `GB/T 7363—2021`（只修了实测确认的 犌=G 犅=B 犜=T，没整表瞎猜）。
- ⚠️ **`long=False`，与论文同套分块参数**——正文是逐条条款，大块反而检得糊。好处：论文→标准重新归类**不需要重建索引**。
- 实施日期单独占 `effective_date` 列，**没有塞进专利的 `filing_date`**：两个日期含义完全不同，挤一列日后必然误读。
- 现行/被代替/废止会变、PDF 里没有 → `review.py standards --set <标准号> --status 废止` 人工录入。**不拿实施日期冒充现行状态**。

**专利（`litreview/patent.py`）**
- 无独立入口，与论文同一条流水线；`enrich` 按扉页 INID 码（`(21)申请号`/`(22)申请日`/`(72)发明人`…）判定，**必须 ≥2 个标记**（库里真有一篇 ResearchGate 章节正文提到 "United States Patent"，单关键词必误判，已固化为回归用例）。扉页同时印授权公告号与申请公布号时取授权号。
- ⚠️ **"状态"是两件不同的事，别合并成一列**：出版阶段（种别码 `A`/`B`/`U`/`S`）与保护期（申请日 + 20/10/15 年）印在 PDF 上、不会变，**读取时现算、不落库**（存成"已过期"会随时间变错且无人察觉）；驳回/撤回/欠年费失效/无效宣告这类法律状态**不在 PDF 上且会变**，只能人工核实，落 `legal_status` + `status_checked_at`（**必须带核实日期**）。没核实过一律显示"未核实"，不拿种别码冒充。
- `review.py patents [--set <专利号|文献ID> --status <状态>]`（重扫走 `types --rescan`）。
- ⚠️ `save_enrichment` 用的是 `INSERT OR REPLACE`（整行重写），所以写入前会先 SELECT 回 `legal_status`/`status_checked_at`/`doc_no`/`url`/`effective_date` 沿用——否则一次 `enrich --force` 就把人工核实结果冲成空。改那段代码务必保住这个行为，`tests/test_store.py` 有专门用例盯着。
- ⚠️ `mark_doc_type` 每列都是 `COALESCE(NULLIF(?,''), 旧值)`：一次退化的重新识别**永远不会**把已经正确的值抹成空。列名是拼进 SQL 的，所以有 `_MARKABLE_COLS` 白名单，改的时候别绕过它。

**MCP Server（`mcp_server.py`）**
- stdio / SSE / Streamable HTTP 三种传输共开放 17 个工具；综述相关为 `propose_review_angles`、`generate_outline`、`start_review`、`resume_review`、`review_status`，其中 `resume_review` 可从持久化章节检查点续跑。
- ⚠️ `patent_status` **已更名为 `doc_status`**（多一个 `doc_type` 参数，专利与标准共用）。其他设备只需重启一次 MCP client，工具是动态发现的、配置不用改。
- 检索结果带类型标签（`[标准 GB 38400-2019]`）：调用方模型据此判断这段话出自强制性文件还是某篇论文的实验结论，两者可引用分量完全不同。
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
*   `review.py`：综述生成器 CLI 入口，11 个子命令——`index`/`status`/`search`/`enrich`/`add-book`/`patents`/`standards`/`types`/`set-type`/`outline`/`generate`。
*   `litreview/`：综述生成器核心包——**`doctype.py`（8 类文献类型词表，唯一真相源，加类型只改它）**、`chunker.py`（分块）、`embedder.py`（Qwen3 向量化）、`reranker.py`（重排序）、`stages.py`（检索→打分→写作流程编排，含 `format_reference` 按类型分支著录）、`enrich.py`（结构化元数据提取）、`patent.py`（专利识别，只产出事实、不猜法律状态）、`figures.py`（自动插图）、`bookintake.py`（教材入库，PDF 拆分/EPUB 转换/定时调度）、`models.py`/`store.py`（SQLite 数据模型与读写）、`prompts.py`（各阶段提示词）。
*   `mcp_server.py` + `mcp_server.sh`：MCP 协议 server，把检索/综述能力暴露给 Claude Code 等客户端。
*   `mineru_client.py`：MinerU V4 API 客户端。已配置轮询重试、自动 Key 轮换及 ZIP 附件解压提取（Markdown、Images、DOCX、HTML、LaTeX 全格式取回）。
*   `llm_router.py`：固定 OpenAI `gpt-5.4-mini` 的论文分析请求构建与调用；历史 SiliconFlow Batch 仅保留拉取兼容，绝不创建新生成任务。
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
    - **生成模型迁移（2026-08-12）**：论文分析、元数据补缺和证据评分固定 `gpt-5.4-mini`；综述大纲/章节/引言结论固定 `gpt-5.4`，均不读取环境变量切换，也没有 fallback。嵌入与重排仍固定硅基流动 Qwen3。mini 的 Batch 和同步调用共用 UTC 日 200 万账本，论文 Batch 单批最多预留 60 万、单篇输入最多 20 万；`gpt-5.4` 综述正文不走 Batch、同步回收 usage，独立 UTC 日硬上限 20 万。超额预留一律拒绝发起。
    - **全失败的 Batch 会永久卡死**：云端返回 `completed` 但 `request_counts.completed == 0` 时没有 `output_file_id`，旧代码直接 raise 被外层"未知崩溃"吞掉，论文永远停在 `BATCH_SUBMITTED`——而这个状态不在阶段 1 的自动拾起名单里，等于死局。现在识别并落到 `BATCH_FAILED`（可自愈、会被重新提交），同时读 `error_file` 把真实原因写进 `error_message`。
    - **空解析结果会被当正常论文入库**：MinerU 返回 success 不代表抽出了字，扫描件和纯图版 PDF 常常只给个空 `full.md`，放行的话 LLM 只能靠标题编，编出来的还会写进 Obsidian 和 Excel。现在按 `MIN_MARKDOWN_CHARS=200` 在解析出口拦截并移进 `failed_pdfs`。**拦截点必须在归档 move 之前**，否则这份 PDF 会被当成"已处理"，人再也不会去看它。
17. **专利成为第三类文献**（`litreview/patent.py`，2026-08-04）：库里一直混着专利却被当论文处理。识别复用既有的 `doc_type` seam（该字段本就从 `docs_needing_index` → `chunk_params_for` → `search(doc_types=)` 全程打通，所以 `chunker.py` 一行没改），`paper_details` 加 4 个可空列。三个关键决定：① **判定要 ≥2 个 INID 码**——单关键词会把正文提到 "United States Patent" 的论文误判（真实案例已固化为回归用例）；② **派生值不落库**——出版阶段和保护期到期日读取时现算，存下来会随时间变错；③ **法律状态与出版阶段严格分离**，前者只能人工录入且强制记核实日期。另外专利跳过 LLM 补缺（INID 码已结构化），`--rescan` 走纯正则、默认 dry-run、可重复执行，避免为重分类两篇专利而 `enrich --force` 重跑全库 LLM。生产库已于当日执行 `--apply`（迁移前热备份 `batch_tracking.db.bak.20260804_101706`），扫出的 2 篇里一篇原属论文、一篇原属教材，图表/参考文献计数未受影响。
18. **文献类型扩到 8 类**（`litreview/doctype.py`，2026-08-04，紧接第 17 条）：专利刚补完就发现库里还混着国标、协会年报、企业财报、机构研究报告、网页资料，而这些的参考文献著录格式各不相同。做法是**把散在各处的类型判断收进一张 GB/T 7714-2015 码表**——`DOC_TYPES` 一处定义，CLI 的 `--corpus`/`--type`/`--status` 选项、MCP 参数校验、`chunker` 分块参数全部由它派生。三个关键决定：① **只给专利和标准写自动识别**（`detect: True`），报告/财报/网页跟长论文在版式上没有可靠差别，硬猜会污染整个库，用 `test_only_patent_and_standard_claim_auto_detection` 把这条钉死；② **`statuses_for()` 对没有"会失效"概念的类型返回空列表**——修的是一个真实 bug：它原本兜底返回专利那套选项，于是命令行允许把一篇期刊论文标成"已授权"；③ **标准 `long=False`**，正文是逐条条款、大块反而检得糊，顺带好处是论文→标准的重新归类不需要重建索引。同时修了 `stages.format_reference` 此前"非书籍一律按 `[J]` 印"的错误——专利和标准都被印成期刊论文、`[J].` 后跟一个空期刊名。`patents --rescan` 一并移除，重扫统一走 `types --rescan`。测试从 73 涨到 126 用例。当日对生产库执行了类型重扫与一次重复文献删除，见 §6。

---

## 5. 运维与部署指南

### 启动服务
项目完全容器化，依赖 Docker Compose。**全新机器只需填一份仓库根目录的 `.env`**（照 `.env.example` 抄，必填的只有 API Key），其余全走默认值：
```bash
cp .env.example .env && vi .env          # 填 MINERU_API_KEY / SILICONFLOW_API_KEY / OPENAI_API_KEY
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

### 手动干预指令（教材/类型/综述）
```bash
python review.py add-book [路径]                      # 手动入库单本/目录/默认 BOOK_INPUT_DIR
python -c "import daemon; daemon.book_intake_job()"    # 立即触发一次每日定时入库（不等凌晨）
python review.py types                                # 分类总览：8 种类型各多少篇 + GB/T 7714 码
python review.py types --rescan [--apply] [--only standard]  # 重扫认专利/标准，默认只预演
python review.py set-type <文献> --type report --publisher "FAO/IFA" --year 2024  # 人工指定类型
python review.py patents                              # 专利台账（出版阶段/保护期/法律状态）
python review.py patents --set CN110346043A --status 驳回    # 人工录入法律状态并记核实日期
python review.py standards                            # 标准台账（标准号/实施日期/发布机构/状态）
python review.py standards --set GB38400-2019 --status 废止  # 人工录入现行状态并记核实日期
python review.py index / enrich / search / outline / generate  # 综述生成器全流程见 README
```

### 环境配置
**配置只有一个入口：仓库根目录的 `.env`。** compose 的 `env_file` 先读它，再读可选的 `${SHARED_ENV_FILE}`（作者机器指向 `/opt/docker_shared/api_keys.env`，跨项目集中管 Key；两者都是 `required: false`，新装用户没有第二个也照跑）。`docker-compose.yml` 的 `environment:` 块只负责两件事：给可移植默认值（一律写成 `${VAR:-默认值}`，写死会静默盖掉用户 `.env` 里的设置），以及把容器内部路径钉死。关键分组：
- **MinerU**：`MINERU_API_KEY` 支持逗号分隔多 Key，额度用尽自动轮换。
- **大模型**：`OPENAI_API_KEY`/`OPENAI_API_BASE`（固定生成模型）；`SILICONFLOW_API_KEY`/`SILICONFLOW_API_BASE`（Qwen3 嵌入与重排）。
- **调度间隔**：`SCAN_INTERVAL_MINUTES`（实时模式）、`BATCH_SCAN_INTERVAL_MINUTES`/`BATCH_FETCH_INTERVAL_MINUTES`（批处理模式）、`BOOK_INTAKE_TIME`/`BOOK_DAILY_PAGE_BUDGET`（教材定时入库）。
- **litreview**：`EMBEDDING_MODEL`/`EMBEDDING_DIM`、`RERANK_MODEL`/`RERANK_CANDIDATES`、`REVIEW_TOP_K`/`REVIEW_MIN_SCORE`/`REVIEW_EVIDENCE_N`、`REVIEW_INSERT_FIGURES`、`REVIEW_OUTPUT_DIR`；模型值在代码中固定。
- **教材入库**：`BOOK_SPLIT_PAGES`、`BOOK_INPUT_DIR`/`BOOK_OUTPUT_DIR`。
- **本地夜间嵌入**：`REVIEW_EMBED_BACKEND`(`local`/`remote`)、`LOCAL_EMBEDDING_MODEL_PATH`、`NIGHTLY_INDEX_DEADLINE`。
- **部署环境**：`DATA_ROOT`、`CONTAINER_NAME`、`IMAGE_NAME`、`DOCKER_NETWORK_MODE`、`DOCKER_BUILD_NETWORK`、`SHARED_ENV_FILE`、`LIT_HTTP_PROXY`/`LIT_HTTPS_PROXY`、`LOG_RETENTION_DAYS`。

⚠️ **`.dockerignore` 排除 `.env` ≠ 容器读不到 `.env`。**（早期文档写反过，别再照着改）`.dockerignore` 管的是**构建期**——不把密钥烘进镜像，这是对的、要保留。而 compose 的 `env_file` 是**运行期**从宿主机现读的，所以容器确实拿得到 `.env`。结论：**新增配置项只改 `.env` 就够，不必再去 compose 的 `environment:` 里补一份**；反过来，compose 里凡是写死（不带 `${VAR:-...}`）的项会静默盖掉 `.env`，用户改了不生效且日志上看不出原因。唯一故意写死的是容器内部路径，见 §2。

⚠️ **容器代码是 `COPY . .` 打进镜像的，不是挂载的。** 改宿主机 `.py` 对运行中的容器无效且不报错，必须 `docker compose build && docker compose up -d --force-recreate`（**只 build 不 force-recreate，compose 认为配置没变会保留旧容器，改动照样不生效**）；核对用 `docker exec <容器> md5sum /app/<file>`。

完整清单以 `.env.example` 为准（有分组注释）。

### MCP Server 部署

**本机（同一台机器）**：
```bash
claude mcp add --scope user literature-review /path/to/literature-workflow/mcp_server.sh
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
| 文献总数 | **178 篇/本已 `EXPORTED`**（论文与教材同在 `papers` 表）。⚠️ `SELECT COUNT(*) FROM papers` 当时是 **214**——多出的 36 行状态是 `BATCH_SUBMITTED`，即当天正在 Batch 里跑的新专利，不是脏数据。**下面所有统计口径都是 `status='EXPORTED'`**，直接数 `papers` 表会对不上。 |
| 分类 | `doc_type`：论文 142 · 教材 20 · 标准 14 · 专利 2（标准与专利的状态均为"未核实"，待人工查证录入；报告/学位论文/会议/网页 各 0） |
| 结构化元数据 | `paper_details` 178 · `paper_assets` 13923 · `paper_references` 5301 |
| DOI 覆盖 | 77 / 176（44%），其余多为中文期刊与教材 |
| 向量索引 | 166 个文档 / 12320 块（Qwen3-Embedding-8B，4096 维） |
| Excel 汇总 | 220 行 |
| 测试套件 | 126 用例全绿（`pytest`，`pytest.ini` 已限定 `testpaths=tests`） |

入库流水线、综述生成器、MCP 集成、结构化元数据提取、GB 格式引用、章节编号、Word 导出、教材 EPUB 支持、每日定时入库、专利与标准识别及状态台账、8 类文献类型词表均已完成；部署已去宿主机化，同一份 compose 可在作者机器/云服务器/别人的电脑上跑，并已在阿里云实机部署过一轮。

**2026-08-04 类型重扫已对生产库执行完毕**（迁移前热备份 `batch_tracking.db.bak.20260804-145343`，257 MB）：`types --rescan --apply` 把 15 篇此前被当成论文的国标/行标归位（原论文数 157 → 142）。⚠️ 当初立项时按初版正则估的是 8 篇，实际 15 篇——差的 7 篇全被 MinerU 的 `<sup>` 标签和造字乱码挡住了，正则校准后才现形。因标准 `long=False`，本次重分类**不需要重建索引**。

**同日删除了一份重复文献**（热备份 `batch_tracking.db.bak.20260804-151901-删重复前`，Excel 备份 `knowledge_base.xlsx.bak.20260804-152119`）：`cffb6ff4`（原生解析）与 `aa1548c8`（OCR 版）是同一份 GB 38400-2019，源文件哈希不同所以入库查重没拦住。**删的是 OCR 版**——两版限量值完全一致，但 OCR 版把限值渲染成 `$\leqslant 10$ mg/kg`（LaTeX 包裹会实打实拉低"≤10 mg/kg"这类查询的命中率），且丢了 1 张图；它唯一的优势是规范性引用文件清单更全，那只是被引标准列表，不是实质内容。实删了 5 张表（`papers` 1 行 / `paper_details` 1 / `paper_assets` 5 / `review_chunks` 55 / `review_index_meta` 1）+ Obsidian 笔记与 attachments + `mineru_output` 目录 + Excel 行；`paper_references` 这篇本来就是 0 行（标准不著录参考文献），但**它同样挂 `doc_id`，删一篇论文时必须一起清**。`review_evidence_cache` 挂在 `chunk_id` 上（**不是 `doc_id`**），所以要先把该文献的 chunk_id 查出来再按块删，本次是 0 行。删后复核无孤儿行，各项计数正好各减 1。**源 PDF 仍留在 `processed_pdfs/`**（13.5 MB），删库不删原件，它不在扫描目录里、不会被重新入库。

> ⚠️ **仓库里没有删除文献的功能**，上面这次是手写脚本一次性做的。要再删务必按这个顺序清干净：
> **① 先** 用 `SELECT chunk_id FROM review_chunks WHERE doc_id=?` 解出块号删 `review_evidence_cache`（顺序反了就永远找不到它的缓存行）；
> **② 六张挂 doc_id 的表**——`review_chunks`、`review_index_meta`、`paper_references`、`paper_assets`、`paper_details`，最后 `papers`（注意它的主键列名是 **`id` 不是 `doc_id`**）；
> **③ 三处文件**——Obsidian 笔记 `*_<doc_id前8位>.md`、`attachments/<doc_id>/`、`mineru_output/` 或 `book_output/` 下的解析目录；
> **④ Excel 行**。
> 漏一处就留孤儿。值得做成 `review.py rm` 命令。

19. **CrossRef 权威元数据 Fetcher（2026-08-17）**：新建 `litreview/fetcher.py`，在 `enrich` 阶段若从 PDF 中提取出 DOI，优先直连 CrossRef 官方公开 API 获取权威作者、期刊全称、精确年份及标题。实现 **0 Token 消耗**与 100% 真实著录，无 DOI 或请求超时才优雅降级至大模型补缺。
20. **全生命周期级联删除指令 `review.py rm`（2026-08-17）**：在 `litreview/store.py` 中实现 `delete_doc`，提供 `review.py rm <文献> [--yes] [--all] [--delete-source]` 命令。自动按序清理 6 张数据库表、3 处文件目录（笔记及 attachments、MinerU/Book 缓存）与 Excel 记录，默认 dry-run 防误删。同步在 `cli.py` 提供 `sync-excel`（全量重对齐清除孤儿行）与 `clean-orphans`（清理孤儿索引）。
21. **Obsidian 笔记模板 8 类文献全面适配（2026-08-17）**：更新 `obsidian_template.md` 与 `utils.generate_obsidian_note`，支持标准、专利、报告、论文等各自的专属标签与信息抬头；并在 `run_enrich` / `rescan_types` 识别出类型后自动回写/同步更新对应笔记。
22. **模型解耦与 `mcp_server.sh` 跨机可移植化（2026-08-17）**：`llm_router.py` 支持通过 `ANALYSIS_MODEL` 配置模型（兼容 DeepSeek/OpenAI/中转）；`mcp_server.sh` 自动侦测环境 Python 与数据路径，不再因缺少宿主机共享文件而直接报错退出。测试集扩充至 146 个用例全绿。

---

## 6. 现状与欠缺项

**数据截至 2026-08-17（作者生产库）**：

| 指标 | 实际值 |
|---|---|
| 文献总数 | **329 篇/本已 `EXPORTED`**，另有 15 篇已完成分析待导出。 |
| 分类 | `doc_type`：论文 204 · 专利 42 · 教材 39 · 标准 14 |
| 结构化元数据 | `paper_details` 299 篇 |
| 向量索引 | 262 个文档 / 32,207 块（Qwen3-Embedding-8B，4096 维） |
| 测试套件 | **146 用例全绿**（`pytest`，含 fetcher、rm 级联删除等） |

**已解决的历史债务**：
- [x] `mcp_server.sh` 跨机可移植化完成。
- [x] `obsidian_template.md` 8 类文献类型适配与自动同步完成。
- [x] CrossRef Fetcher 免费权威元数据获取完成。
- [x] `review.py rm` 级联删除命令完成。
- [x] `cli.py sync-excel` 与 `clean-orphans` 数据对齐完成。

**剩余欠缺项（面向商业化交付版）**：
1. **Web UI（Phase 2）**：提供轻量现代 Web 界面，支持 PDF 拖拽上传、知识库检索、专利/标准台账浏览、综述一键生成与 Word 下载。
2. **商业化独立分发包与启动脚本**：Windows `start_windows.bat` 一键启动脚本、开箱即用新手文档与 API 申请图文教程。
3. **真实主题的端到端 `generate` 综述质量验收**。

---

## 7. 引文网络与 Web UI 工作台升级 (2026-08-19, v2.5)

### 新增能力与架构升级

23. **引文网络拓扑与 OpenAlex 双轨滚雪球发现 (`litreview/citation_network.py`, `litreview/snowball.py`)**：
    * **参考文献消歧**：从论文中解析结构化参考文献著录项，建立 `reference_mentions` 账本，按 `CONFIRMED`（馆内已收录）、`CANDIDATE`（待复核候选）与 `UNRESOLVED_EXTERNAL`（外部占位）三态分类消歧；
    * **引文拓扑分析**：支持出向引用（Outgoing Citations）、馆内谁引用了它（Incoming Citations）以及共同参考文献关联同行（Bibliographic Coupling）分析；
    * **OpenAlex 双轨发现**：支持以 1~3 篇核心文献为种子，沿出向引用追溯 **Foundational 经典基石文献**、沿入向施引跟踪近 3 年 **Emerging 前沿应用文献**，输出合法 OA 下载链接与推荐理由。

24. **现代化 Web UI 工作台与引文网络图谱 (`web_server.py`, `web_static/`)**：
    * **服务启动**：`python web_server.py`（默认监听 `http://127.0.0.1:8001`）；
    * **8 大核心视图**：
      1. **工作台概况 (Dashboard)**：展示 5 级严格互斥实体分区（341 生产正常 + 3 生产隔离 + 3 测试入库 + 1 孤儿隔离 + 8766 外部占位 = 9114 Works，隔离总数精确为 4）与能力指标（3 篇真实 Qwen3 向量索引）；
      2. **文献目录 (Library)**：344 篇馆藏基线文献分页检索、多类型与索引状态过滤、出入向引用数统计与详情抽屉；
      3. **文献详情 (Detail Drawer)**：中英文元数据、PDF 资产状态（`document_assets` 大小/页数/SHA-256/来源）、参考文献消歧明细表与引文关系；
      4. **引文网络图谱 (Network)**：基于 ECharts 5 的力导向图谱，默认呈现 9,110 个可见节点与 8,817 条闭合边（`missing_endpoints = 0`，自动排除隔离节点），支持全图/子图切换、边过滤与节点拖拽；
      5. **滚雪球发现 (Discovery)**：种子文献多选与双轨结果卡片展示；
      6. **候选池管理 (Candidates)**：355 篇候选多维筛选、批量审批弹窗与状态机硬约束拦截（非 `DISCOVERED` 状态禁止重复审批或状态倒退）；
      7. **入库流水线 (Pipeline)**：针对已批准候选一键执行 `OA下载 ➔ PDF验真 ➔ 正文切块 ➔ Qwen3向量化 ➔ 图谱回填`，控制台实时滚动日志；
      8. **审计与日志 (Audit)**：Append-Only 决策记录（`candidate_decisions`）、PDF 资产账本（`document_assets`）与发现批次（`discovery_runs`）。

25. **MCP Server 扩展至 15 个工具 (`mcp_server.py`)**：
    * 包含 `library_status`, `list_documents`, `library_reconcile`, `search_literature`, `get_paper_info`, `doc_status`, `generate_outline`, `start_review`, `review_status`, `get_citation_network`, `citation_expand`, `list_discovery_candidates`, `approve_candidates`, `acquire_fulltext`, `ingest_acquired_candidates`。

26. **安全加固与生产库只读物理隔离**：
    * 生产数据库 `/mnt/ripe/literature_analyzer_data/batch_tracking.db` 在所有 Web UI、图谱查询及测试连接中强制配置 `sqlite3.connect("file:...mode=ro", uri=True)`，保证底层防写保护；
    * 孤儿 Work（`work_oa_w4391143884`）标记为 `QUARANTINED_INCOMPLETE`，并在 `eligible_for_citation_graph = 0` 下从默认图谱中严格排除；
    * 全套自动化测试套件包含 **189 个用例全绿通过**（聚焦回归 14/14 passed）。


27. **多设备组网、Systemd 7x24 守护与 60 FPS 性能优化 (2026-08-19)**：
    * **Systemd 系统服务**：注册 `/etc/systemd/system/literature-web.service`，监听 `0.0.0.0:8001`，支持崩溃自愈与开机自启；
    * **Tailscale 远程直连**：服务器 Tailscale IP `100.83.137.43`，异地笔记本无需公网暴露或反代即可直接访问 `http://100.83.137.43:8001`；
    * **60 FPS 骨干图谱优化**：默认加载 762 骨干节点 / 469 条边（体积 55 KB，80ms 秒开），禁用持续力导向物理运算（`layoutAnimation: false`），客户端 CPU 占用归零；
    * **统一存储架构**：所有手动、定时及 OA 自动下载文献，统一汇聚于 `/mnt/ripe/literature_analyzer_data/`（含 `processed_pdfs`、Obsidian 库、Excel 总表与 SQLite 双层数据库）。

28. **架构深化 Phase A：Web 控制器下沉与深模块重构 (2026-08-19)**：
    * **深模块 LibraryStore (`litreview/library_store.py`)**：收拢仪表盘 5 级互斥分区严格闭合计算（9114 Works）、多维分页检索、文献详情抽屉聚合与发现候选池分页查询；
    * **增强 CitationNetwork (`litreview/citation_network.py`)**：收拢 60 FPS 极速骨干网络提取（762 节点）、9110 节点全景闭合拓扑计算与单文献局部子图聚焦；
    * **瘦身 `web_server.py`**：消除 20 余处内联 SQL 泄漏，Web 端退化为纯净薄适配器（Thin HTTP Adapter）；
    * **测试套件扩充**：新增 `tests/test_library_store.py` 纯进程内单测（0.19s 极速跑通）。

29. **架构深化 Phase B：统一文献入库调度引擎与策略分流 (2026-08-19)**：
    * **统一入库内核 `DocumentIngestionEngine` (`litreview/ingestion_engine.py`)**：标准化 5 步通用生命周期（Acquire ➔ Verify ➔ Parse ➔ Vectorize ➔ Persist）；
    * **多源策略分流 (`IngestionStrategy`)**：支持 `OpenAccessCandidateStrategy` 等不同来源策略，解耦抓取与持久化；
    * **物理验真收口 (`verify_document_file`)**：统一魔数检查（`%PDF-` / `PK`）、大小边界、页数解析与 SHA-256 计算；
    * **测试套件扩充**：新增 `tests/test_ingestion_engine.py`（4 项单测 0.26s 全过）。

30. **架构深化 Phase C：统一领域外观与异步任务注册中心 (2026-08-19)**：
    * **领域外观 `LiteratureReviewFacade` (`litreview/facade.py`)**：向 CLI、MCP 与 Web 端提供统一的一等公民函数接口，消除跨层拼装；
    * **任务注册中心 `JobRegistry`**：统一接管长耗时后台任务（综述生成、滚雪球发现、候选入库）的状态跃迁、进度推送与日志收集；
    * **全量测试套件**：扩充至 **201 项自动化测试 100% 持续全绿通过**。

31. **Modern Web Guidance 现代前端体验升级 (2026-08-19)**：
    * **原生 View Transitions 平滑转场 (`same-document-transitions`)**：在 Dashboard、Library、Network、Discovery、Pipeline 视图切换中接入 `document.startViewTransition()`，实现 60 FPS 丝滑淡入淡出动画；
    * **原生 Popover Toast 堆叠提示 (`persistent-toast-notifications`)**：引入基于 HTML5 `popover="manual"` 的轻量非阻塞 Toast 浮层，彻底替代生硬阻断的 `alert()` 弹窗，支持操作成功/警告/错误自动计时与手动关闭；
    * **极速长列表渲染性能 (`defer-rendering-heavy-content`)**：应用现代 CSS `content-visibility: auto` + `contain-intrinsic-size`，视口外元素延迟排版绘制，长列表滚动帧率稳定在 60 FPS。

32. **滚雪球动态种子管理与单文献图谱拓扑修复 (2026-08-19)**：
    * **动态种子选择与手动添加**：滚雪球发现页支持从馆藏 344 篇文献下拉框直接添加、手动输入任意 Work ID / DOI / OpenAlex ID，且每个种子芯片均支持勾选状态与 `✕` 一键删除；
    * **文献目录与抽屉直通**：文献详情抽屉新增 `❄️ 设为滚雪球种子文献` 快捷按钮，点击直接带入种子并跳转发现页；
    * **单文献 1 阶全景引文拓扑 (`/api/citations/{doc_id}`)**：接入 `get_seed_subgraph`，完整呈现种子文献关联的所有馆内闭环文献与外部参考文献叶子节点；
    * **接口参数校准**：修正 `_run_discovery_worker` 与 `expand_citations` 签名对齐，`SO_REUSEADDR` 保障 8001 端口热重启零漂移。

33. **引文图谱全量馆藏文献动态检索与聚焦 (2026-08-19)**：
    * **全量文献下拉选择**：引文网络图谱页自动载入馆藏全部 344 篇文献列表，附带出入向被引数，支持从下拉框任意切换；
    * **关键词实时搜索过滤**：新增篇名/Work ID 搜索框与 `🎯 聚焦此篇` 按钮，输入关键词实时过滤下拉菜单，回车即可一键聚焦单文献子图；
    * **一键切回全局**：顶部提供 `🌐 切回全局图谱` 快捷键，随时在 762 骨干全局图与局部聚焦星形图之间无缝切换。

34. **精简工作台：移除 Web 端 AI 综述生成模块 (2026-08-19)**：
    * **清理前端视图与导航**：从左侧侧边栏及主内容区彻底移除 `AI 综述生成` 导航标签及对应页面；
    * **聚焦核心业务链路**：工作台全面聚焦于核心文献管理、引文拓扑分析、OpenAlex 滚雪球发现、候选审批与自动化入库流水线。

35. **前端初始化时序与引用错误修复 (2026-08-19)**：
    * **修复 ReferenceError 暂时性死区**：重构 `web_static/app.js` 头部全局状态声明时序，确保 `const AppState`、`escapeHtml` 与 `showToast` 优先就绪，消除页面载入时的 JS 执行阻断；
    * **验证前端生命周期**：全量视图与事件监听（概况、目录、图谱、发现、候选池、流水线、审计）初始化 100% 顺畅执行。

36. **弹窗居中与全页面视口遮挡修复 (2026-08-19)**：
    * **模态浮层视口绝对居中 (`.modal-overlay`)**：补充缺失的 `position: fixed; inset: 0; z-index: 9999;` 与毛玻璃背景，修复此前文献详情在右侧溢出并被视口裁切的严重显示问题；
    * **自适应滚动与安全边界**：`.modal-card` 限制 `max-height: 88vh`，内部 `.modal-body` 独立平滑滚动，无论笔记本分辨率或缩放比例均能完整居中展示；
    * **全页面防溢出加固**：针对概况、目录、图谱、发现与流水线全量表格和网格容器配置 `min-width: 0; overflow-x: auto;`，杜绝横向滚动条与布局拉伸；
    * **快捷交互增强**：支持点击浮层背景或按键盘 `Escape` 键瞬间关闭弹窗。

37. **Work ID 哈希标识可读性与篇名回退增强 (2026-08-19)**：
    * **多级题名回退机制**：在 `litreview/library_store.py` 中为 `get_document_full_detail` 增加对 `works.canonical_title`、`authors_json`、`publication_year` 的深度兜底，彻底解决部分文献详情弹窗中题名缺失、直接回退显示 64 位十六进制 SHA-256 哈希（如 `b11a06f1...`）的显示缺陷；
    * **人机可读性优化**：在所有图谱详情卡片与元数据抽屉中，强制优先高亮展示中文规范译名（如《使用浸渍有有机磷萃取剂的树脂从磷酸中萃取镉》），并将 64 位底层数字指纹 ID 收缩为微小辅助标签。

38. **全站“文献题名绝对优先 (Title-First)”人机交互重构 (2026-08-19)**：
    * **文献目录首列重构**：将中英文规范篇名调整为第一核心列并加粗高亮，取消占用首列的生硬 Doc ID，改为在题名下方以微小胶囊标签辅助展示；
    * **入库流水线与审计日志题名联查**：在 `api_audit_events` 中接入 SQL 联查（`candidate_decisions` 联查 `discovered_candidates.canonical_title`，`document_assets` 联查 `works.canonical_title`），使所有审批审计与资产台账均以文献篇名为主体呈现；
    * **引文关系列表全篇名展示**：文献详情模态框中的出向引用、入向被引、共被引以及候选池列表，全面接入《文献全名》格式化渲染，彻底消除各类十六进制哈希对视觉阅读的干扰。

39. **OpenAlex & CrossRef 双轨学术反查解析器与一键权威对齐 (2026-08-19)**：
    * **学术题名反查解析引擎 (`litreview/fetcher.py`)**：实现 `resolve_work_by_title_and_author`，结合 Token Jaccard 题名相似度与作者姓氏校验，自动反查 OpenAlex 与 CrossRef 权威学术图谱，0 幻觉自动补全官方标准题名、真实 DOI、全部作者列表、出版年份与期刊；
    * **规范化实体回填 (`litreview/library_store.py`)**：`enrich_document_metadata` 将权威数据写入 `works` 主表，并规范化持久化至 `work_identifiers` (DOI/OpenAlex ID) 与 `work_titles` (英文题名) 表；
    * **Web 端一键对齐交互 (`POST /api/library/documents/{doc_id}/enrich`)**：文献详情弹窗新增 `🌐 权威对齐与元数据补全` 按钮，一键联网刷新元数据并实时渲染；
    * **测试套件扩充**：新增 `tests/test_academic_resolver.py`，全量测试套件扩充至 **205 项自动化测试 100% 全绿通过 (18.79s)**。

40. **全量文献分页系统完善与极简主目录软链接 (2026-08-19)**：
    * **文献目录全量分页 (`list_library_documents`)**：补全缺失的 `total_pages` 字段（344 篇文献共 18 页），前端重构完整分页组件，支持 `首页 / 上页 / 智能数字页码 / 下页 / 尾页`、直接输入页码跳转；
    * **动态每页容量切换**：新增 `每页展示: 20 篇 / 50 篇 / 100 篇 / 全部(一页全显)` 切换器，方便随时一屏纵览全部 344 篇馆藏；
    * **极简快捷访问路径**：在 `/home/dudu/literature_analyzer` 创建直达软链接，无需输入冗长的 `GoogleDrive/Antigravity/...` 路径即可直接 `cd ~/literature_analyzer`。

41. **分页上下页失效根因修复与响应式页码导航 (2026-08-19)**：
    * **接口数据包字段补全**：修复 `web_server.py` 中 `api_library_documents` 返回体未携带 `total_pages` 导致前端按钮误被置为 `disabled` 的缺陷；
    * **双端防呆计算兜底**：前端在缺失字段时自动通过 `Math.ceil(total / pageSize)` 计算总页数；
    * **实测 18 页全量翻页**：测试通过第 1~18 页无缝翻页跳转与 20/50/100/344 篇容量切换。

42. **MCP 服务 5 大生产级可靠性加固与全馆 100% 向量化 (2026-08-21)**：
    * **SQLite WAL 并发与超时加固**：默认开启 SQLite `WAL` 并发读写模式与 30s 锁等待；增加 `@retry_on_db_locked(max_retries=5)` 指数退避重试装饰器；
    * **启动故障自愈 (Boot Self-Healing)**：服务初始化时自动将历史残留的 `running` 僵尸任务修复为 `interrupted` 状态并保留历史断点；
    * **章节级检查点与单节容错**：每完成一节立即持久化 `checkpoint_json` 并刷新 `heartbeat_at`，单章节网络异常自动指数退避重试 3 次；
    * **Token 预算保护**：单节证据实施 18,000 字符预算截断，彻底避免大模型 API 触发 `context_length_exceeded`；
    * **全馆文献 100% 向量化闭环**：完成全馆所有有效正文文献（403 篇，38,721 个向量分块）的 Qwen3 4096 维嵌入索引。

43. **顶刊级综述 Prompt v3.1 体系与前置人机立论对齐重构 (2026-08-21)**：
    * **融合顶刊综述核心范式**：吸收 `review-writer` 与 `review-critic` 规范，实现“先立论再填料、观点驱动而非进展汇报”的范式升级；
    * **前置立论探测 (`propose_review_angles`)**：基于本地真实馆藏，为作者智能提炼 3 套差异鲜明的顶刊级立论方案（Thesis + Research Gap + 6 种主线分类逻辑 + 学术增量）；
    * **强约束大纲构建 (`generate_outline`)**：支持注入作者确认的 Thesis 与 Gap，检索问题强制按【机理机制】+【量化参数/对照数据】+【现存缺陷/工业瓶颈】三元组输出；
    * **3 档字数动力学伸缩矩阵**：精简 (<8k 字) / 标准 (8k-12k 字) / 深度 (>12k 字) 自动调整章节数、初筛篇数、重排候选池与证据门槛，支持中英双语 1.6x 当量折算；
    * **语义化学术编号与横向对比矩阵**：全面支持 `[@U-J25-001]` 国标编号引用与双向正则解析，3 篇以上多文献对比章节自动嵌入 Markdown 横向性能对比表格；
    * **引言「承-转-合」与结论「一问一策」闭环**：引言【转】精准打击作者指定的 Research Gap，结论逐条呼应正文技术抓手；
    * **双端对齐与全绿测试**：MCP 与 `review.py` CLI 工具参数 100% 镜像对称，`tests/test_mcp_server.py` 11/11 测试全部通过。

44. **Remote-Access 远程接入架构与虚拟化资源定位 (2026-08-22)**：
    * **`McpServerHost` 统一多传输宿主 (`mcp_server.py`)**：支持 `--transport [stdio|sse|streamable-http]`、`--host`、`--port` 与环境变量注入；
    * **`DocumentLocator` 资源定位与虚拟化 (`litreview/locator.py`)**：对宿主机敏感绝对物理路径（`/mnt/ripe/...`）进行安全脱敏，统一输出 `asset://pdf/{work_id}`、`vault://reviews/{job_id}.md` 虚拟 URI 及流式代理下载 URL；
    * **`RemoteAccessGuard` 安全守卫中间件 (`litreview/guard.py`)**：精准区分 localhost、Tailscale (CGNAT)、LAN (RFC1918) 与 WAN，公网请求强制校验 Bearer Token 鉴权。

45. **LLM 论文分析维度增强与 PaperAnalysisRecord 架构深化 (2026-08-22)**：
    * **提示词工业级技术抓手升级 (`llm_router.py`)**：Prompt 强制提取 `keywords`（核心领域/关键词）与 `fertilizer_implications`（磷化工生产、控释包膜、养分释放动力学、磷石膏综合利用的具体工程借鉴，拒绝假大空套话）；
    * **`PaperAnalysisRecord` 强类型深模块 (`litreview/analysis_record.py`)**：收敛 LLM JSON 解析、物理摘要正则去幻觉双向融合、Obsidian 相对路径渲染与 Excel 行格式化，彻底消除 5 文件联动的霰弹式修改；
    * **Excel 黄金列重排与双链笔记升级**：`knowledge_base.xlsx` 首屏重排为【标题 ➔ 关键词 ➔ 肥料开发借鉴 ➔ TLDR ➔ 著录信息 ➔ 双链】，`obsidian_template.md` 增加专属高亮专节；
    * **纯内存单元测试 (`tests/test_analysis_record.py`)**：4 项测试 0.62s 通过。

46. **双传输融合网关 (Dual-Transport Fusion) 与 OpenAI Codex 远程兼容 (2026-08-22)**：
    * **8002 端口双协议融合 (`mcp_server.py`)**：同一端口同时挂载 FastMCP `streamable_http_app` (`/mcp`) 与 `sse_app` (`/sse`)；
    * **Codex Desktop 握手兼容**：彻底解决 Codex Desktop 向 `/sse` 发送 POST `initialize` 导致的 HTTP 405 Method Not Allowed 阻断，Codex 可直接配置 `url = "http://100.83.137.43:8002/mcp"`；
    * **systemd 7x24 常驻服务 (`literature-review-mcp.service`)**：配置 `Restart=always` 与 `transport_security.allowed_hosts = ["*"]`，跨机访问 100% 稳定运行；
    * **全局记忆中心同步**：全套排查规则已持久化至 `/mnt/ripe/global_memory_service`。

47. **综述生成可靠性 v3.2：共享逐章编排、双轨一致性与可恢复检查点 (2026-08-24)**：
    * **共享深模块 (`litreview/review_orchestrator.py`)**：MCP 与 `review.py generate` 共用同一 Stage B→C 逐章执行路径，消除两端在双轨检索、字数分配、重试与输出校验上的行为漂移；
    * **跨章多样性真正生效**：每章正文实际引用的 `doc_id` 立即进入使用历史，后续章节按次数降权；关闭 Reranker 或重排服务降级时仍使用相同规则；
    * **双轨证据一致性**：Track A 与 Track B 共享 18,000 字符预算；Track B 只接纳具有真实 TLDR/摘要/summary 的文献，并限制为背景与学术脉络用途，检索异常自动降级为 Track A；
    * **Stage A/C 程序化质量闸门**：大纲校验章节规模及每章 2-4 个非空检索问题，失败后定向纠错一次；正文校验有效引用覆盖与 3 篇以上文献的 Markdown 横向表格，Thesis/Research Gap 显式传入引言结论；
    * **检查点可恢复**：SQLite `locked/busy` 异常不再被内部吞掉，五次指数退避实际生效；启动恢复只运行一次，`running/queued` 僵尸任务转为 `interrupted`；新增 `resume_review(job_id)` 复用规范化大纲、章节正文、引用与证据计数续跑；
    * **验证结果**：综述相关测试 38 项通过；全量回归 215 项通过，剩余 8 项为动态生产数据数量基线及既有代理配置断言，与本轮综述修改无关。

48. **综述可复现性 Phase 1A/1B：运行清单与基线对比深度模块 (2026-08-25)**：
    * **Review Run Manifest 深模块 (`litreview/manifest.py`)**：每次生成综述旁路原子写出同名 `.manifest.json` 侧车文件，记录 `schema_version=1` 规范结构（模型、Prompt 快照、检索参数、白名单元数据快照与 SHA256 指纹、按章节证据树、质量门结果与环境指纹）；严格隐私白名单保护，绝不保存未授权私有全文与密钥；
    * **运行校验与基线对比深模块 (`litreview/run_validation.py`)**：
      - 公开极简强类型接口 `validate_review_run(manifest_path) -> RunValidationReport` 与 `compare_review_runs(base, candidate) -> RunComparisonReport`；
      - 全程 0 LLM、0 网络、0 DB、0 Git 调用，严格只读；报告与错误严格基于文件名基名与逻辑位置，绝不泄露宿主机物理绝对路径；
      - 校验规则：严格校验 Schema 白名单结构、产物文件基名安全、Markdown/DOCX 物理哈希一致性、64 位 Hex 哈希格式、阻塞质量门缺陷及引用映射一致性；
      - 对比规则：基线与候选各自通过校验且主题相同；仅客观质量退化（阻塞缺陷新增、章节证据溯源从 complete/empty 退化为 unavailable_*）判 FAIL；模型/配置/语料变化与指标波动记录为确定性 Delta，绝不因引用数或证据数下降设硬失败阈值；引用映射双 complete 时输出确定性 added/removed 列表与 Jaccard 相似度；
    * **CLI 适配与机器输出 (`review.py`)**：
      - 新增 `review.py validate-run MANIFEST [--json]` 与 `review.py compare-runs BASE CANDIDATE [--json]`；
      - `--json` 模式下输出纯净机器可读 JSON，无任何 Rich 文本或日志混入 stdout；退出码对齐 `0=PASS, 1=WARN, 2=FAIL`；
    * **全量回归与测试覆盖**：
      - 新增 `tests/test_manifest.py` (13 项) 与 `tests/test_run_validation.py` (31 项)；
      - 全量自动化测试 307 项通过 (307 passed, 1 warning, 100% 全绿通过)。

49. **综述可复现性 Phase 1C-A：可信基线注册表与 CLI 闭环 (2026-08-25)**：
    * **Baseline Registry 深模块 (`litreview/run_baseline.py`)**：
      - 公开三组极简强类型接口：`promote_review_baseline(manifest, registry, output_root, allow_warn)`、`check_review_candidate(candidate_manifest, registry, output_root)`、`list_review_baselines(registry, output_root)`；
      - 核心函数显式接收 `registry_path` 与 `output_root`，业务模块零耦合 `settings`，易于隔离测试与多环境部署；
      - **统一主题规范化 (`canonicalize_topic`) 与指纹 Key**：在 `run_validation.py` 抽取无环私有 seam，统一由 `compute_topic_key` 与 `compare_review_runs` 复用（Unicode NFKC + 首尾去空 + 内部连续空白折叠 + casefold 小写化后 SHA256），彻底消除等价主题比对漂移；空主题严禁提升；
      - **注册表严格结构与语义完整性校验 (`_validate_registry_structure`)**：
        * 只记录 `topic_key`、`topic`、规范相对 `manifest_locator`、清单完整 `manifest_sha256`、`run_id`、时间戳与校验状态；零物理绝对路径、零全文与密钥；
        * 严格要求 `map_key == entry.topic_key == compute_topic_key(entry.topic)`，且 `manifest_locator` 为规范 POSIX 相对路径并通过 `output_root` containment 检验；
        * `list`、`promote`、`check` 三大入口统一复用语义校验，发现任何条目损坏即整体判 FAIL，不写回、不静默保留；
      - **提升规则 (Promote)**：优先校验 realpath containment 防御目录穿越与 symlink 逃逸；必须前置通过 `validate_review_run` 校验；FAIL 坚决拒绝且不碰旧注册表；WARN 默认拒绝（仅 `--allow-warn` 允许）；同一主题原子替换；原子写入（同目录 `.tmp` + `fsync` + `os.replace`）；
      - **候选巡检规则 (Check)**：优先校验候选 realpath containment，外部或 symlink 逃逸候选立即早退 FAIL (`DANGEROUS_PATH`)；候选校验 FAIL 直接早退；无注册表/无基线返回 WARN (`NO_BASELINE`)；基线丢失/哈希篡改/逃逸返回 FAIL；正常解析后调用 `compare_review_runs` 输出确定性比对；
      - **基线列表 (List)**：按主题与 key 确定性排序输出；
    * **CLI 适配与机器输出 (`review.py`)**：
      - 新增 `review.py baseline-promote MANIFEST [--allow-warn] [--json]`、`review.py baseline-check MANIFEST [--json]`、`review.py baseline-list [--json]`；
      - 配置绑定真实 `REVIEW_OUTPUT_DIR` 与 `REVIEW_BASELINE_REGISTRY_PATH`（默认 `obsidian_vault/reviews/baselines.registry.json`）；
      - `--json` 输出纯净 JSON，退出码严格对齐 `0=PASS, 1=WARN, 2=FAIL`；
    * **测试与阶段说明**：
      - 新增 `tests/test_run_baseline.py` (22 项隔离测试，100% 通过)；
      - 关联核心模块 (8 套件) 107 项通过 (1 warning)；
      - 全仓库全量自动化测试 329 passed, 1 warning (100% 全绿)；
      - 奠定了可信基线注册表深模块底座与 CLI 入口。

50. **综述可复现性 Phase 1C-B：自动基线检查接入与最终状态闭环 (2026-08-25)**：
    * **跨进程并发锁 (`fcntl.flock`) 强化 (`litreview/run_baseline.py`)**：
      - 在 `promote_review_baseline` 的 `read -> mutate -> replace` 全临界区增加持久 `.lock` 跨进程文件锁；
      - 超时安全返回 `FAIL (REGISTRY_LOCK_TIMEOUT)`，杜绝多进程并发提升时条目静默覆盖或注册表损坏；
    * **三入口自动巡检闭环 (`review.py`, `web_server.py`, `mcp_server.py`)**：
      - 在成功生成 Phase 1A Manifest 产物后，直接调用 `check_review_candidate`；
      - **基线状态与生成状态解耦**：记录 `baseline_verdict`、`baseline_status`、`baseline_summary`、`baseline_error`，生成成功时即使基线检查为 `FAIL` 仍保持 `status=done`（绝不伪装为 partial 或生成失败），如实呈现质量退化；
      - 检查过程自身抛异常时保持产物与 `status=done`，安全降级为 `baseline_status="check_error"`，详细错误内部路径脱敏，绝不泄漏物理路径；
      - **诚实呈现**：CLI `generate` 横幅与日志区分 PASS (绿)、WARN/NO_BASELINE (黄)、FAIL (红退化)、check_error (黄不可用)，FAIL/错误时禁用无条件庆祝文案；
      - **Web API & SSE 广播**：任务详情与 SSE 流实时暴露 4 个基线字段与结构化日志；
      - **MCP Server & SQLite 迁移**：`review_jobs` 表增量安全迁移，完整支持重启回读；`start_review` 与 `resume_review` 入队时清空陈旧字段，`review_status` 输出脱敏基线信息；
    * **全量回归与测试覆盖**：
      - `tests/test_run_baseline.py` (25 项) + `tests/test_run_validation.py` (31 项) + `tests/test_manifest.py` (15 项)；
      - 关联核心模块 (8 套件) 112 项测试通过 (1 warning)；
      - 全仓库全量自动化测试 **334 passed, 1 warning (100% 全绿通过)**。

42. **云端批处理最终一致性防御与 52 篇全量向量补建入库 (2026-08-29)**：
    * **OpenAI Batch 异步竞态加固 (`batch_pipeline.py`)**：在 `files.create` 上传后增加文件状态就绪轮询（`files.retrieve` 确认 `status == 'processed'` 与安全缓冲），根除云端存储同步延迟引发的 `Cannot find file` 报错；在拉取结果遇 `failed` 状态时解包详细错误并标记论文状态供下一轮错峰自愈；
    * **嵌入异常解包与鲁棒退避 (`litreview/embedder.py`)**：细化异常拦截，解包并透传具体 API Response Body 错误详情，将重试次数提升至 5 次并延长退避窗口至 45s；
    * **52 篇挂起文献 100% 向量化入库**：修复后重新执行索引，**52 篇文献（含 5 本大型教材共 4,838 文本块）全部成功完成 4096 维 Qwen3-Embedding 向量化**，全库待索引降为 0（已嵌入总块数达到 44,400 块）。
