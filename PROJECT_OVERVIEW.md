# 📚 Literature Analyzer 项目介绍

## 这是什么

一个把"一堆 PDF 论文"自动变成"结构化知识库 + 可检索文献库 + 可生成综述"的流水线工具。核心解决的问题是：科研/工程场景里读文献效率低、笔记零散、想写综述时手动翻找引用又慢又容易漏——这个项目把从"拿到 PDF"到"写出带引用的综述初稿"整条链路都自动化了。

整个系统分两大部分：**入库流水线**（把 PDF 变成结构化笔记）和 **综述生成器 litreview**（在已入库的文献上做检索增强生成）。两者共享同一个 SQLite 状态库，衔接得比较紧。

## 一、入库流水线

**处理链路**：`input_pdfs/*.pdf` → MinerU（OCR 解析，提取正文/公式/表格/图片）→ 大模型分析（摘要/关键词/结构化字段提取）→ 输出两份产物：
- Obsidian 笔记（带 frontmatter、原始摘要、配图，直接扔进 Obsidian vault 就能用双链检索）
- Excel 知识库（汇总表，方便筛选/统计）

**两种运行模式**，由 `daemon.py` 常驻进程调度：
- **实时模式**：每 10 分钟扫一次 `input_pdfs/`，来了新文件立刻处理
- **批处理模式**：攒一批走 SiliconFlow 的 Batch API，成本打 5 折；按固定间隔轮询提交/拉结果（默认每 30 分钟提交、每 120 分钟拉取，`BATCH_SCAN_INTERVAL_MINUTES`/`BATCH_FETCH_INTERVAL_MINUTES` 可调），容器重启时还会做冷启动检测补跑
- **教材定时入库**：与上面两种模式互不干扰，固定每天 `BOOK_INTAKE_TIME`（默认 03:00）跑一次，按 `BOOK_DAILY_PAGE_BUDGET`（默认 2000 页）预算逐本处理 `BOOK_INPUT_DIR` 里的教材，用不完的页数留到下一晚，详见下方"教材/书籍入库"

**生产级的细节打磨**（这些是踩过坑之后加的）：
- SHA-256 哈希去重，重复的 PDF 不会重复烧 token
- MinerU 解析产生的中间大文件用完自动删，不占服务器磁盘
- 网络抖动/API 超时这种临时错误会自动重试，页数超限/文件损坏这种永久错误不会瞎重试
- MinerU key 支持配置多个，额度用完自动切下一个，不会因为某个 key 失效整条流水线卡死
- 运行历史全部落盘到 `data/pipeline_history.jsonl`，配 `cli.py status/history/doctor` 几个命令行看板做日常运维

## 二、综述生成器（litreview）

这是最近几轮迭代的重点，把"文献库"升级成了"能问能写"的检索增强生成系统。

**检索链路**：文献分块 → Qwen3-Embedding-8B 向量化（4096 维，中英文跨语言检索能力强）→ 向量召回 50 个候选 → Qwen3-Reranker-8B 精排取 top 24 → 大模型逐条给证据打分（≥6 分才保留，避免拉不相关的内容进正文）→ 只依据筛选出的证据写作，并强制标注引用来源。

**结构化元数据**：额外建了 `paper_details`（中英文标题/DOI/作者/期刊/年份/关键词）、`paper_assets`（图表+图题+页码）、`paper_references`（每篇论文自己引用的参考文献逐条记录）三张表。提取策略是"免费优先"——先从 MinerU 产出的 `content_list.json` 本地解析（正则抓 DOI、按 block 类型取图表和参考文献），解析不出来的字段才调用大模型补，省了不少 token。

**输出格式**：标准论文式排版，章节标题自动编号（`1 引言` → 正文各章依次编号 → 最后一章 `结论与展望`），不含摘要和关键词块，参考文献按 GB/T 7714 最新标准编号（`[n] 作者. 标题[J]. 期刊, 年份. DOI: xxx`，超过 3 位作者截断为"等"/"et al"），英文文献用国际通行格式。生成 Markdown（写进 Obsidian）的同时用 pandoc 自动导出同名 Word 文档，方便直接拿去投稿或汇报用。

**命令行用法**：`review.py index`（建向量索引）→ `enrich`（提取结构化元数据）→ `search`（检索调试）→ `outline`（先出大纲，可手改）→ `generate`（正式生成，支持指定侧重方向/目标字数/章节数）。

**教材/书籍入库**：`review.py add-book [pdf/epub 或目录]` 走一条轻量通道——把教材当作**检索语料**加入(供 search 与综述引用)，但不做 LLM 全文分析、不进 Obsidian(省钱)，只进 Excel。按扩展名自动分流两条路径：PDF 常超 MinerU 单任务页数上限，会用 pypdf 自动拆分再拼回同一 doc_id；EPUB 是数字文本，直接用 pandoc 转 markdown 入库（无需 MinerU），元数据优先读 EPUB 自带的 OPF/dc 字段。输入输出目录与论文流水线平行、独立管理：不传路径时默认扫 `BOOK_INPUT_DIR`(`./input_books`)，解析结果按`书名_doc_id前8位`分子文件夹落进 `BOOK_OUTPUT_DIR`(`./book_output`)，不与论文的 `MINERU_OUTPUT_DIR` 混放。检索时论文与教材默认混用(可用 `--corpus` 限定)；参考文献按类型区分,论文 `[J]`、教材 `[M]`(GB/T 7714)。

**教材每日定时入库**：不用手动逐本跑 `add-book`——`daemon.py` 固定每天 `BOOK_INTAKE_TIME`（默认 03:00）扫一次 `BOOK_INPUT_DIR`，按文件名排序、PDF 累计页数不超过 `BOOK_DAILY_PAGE_BUDGET`（默认 2000）逐本入库，超预算的留到下一晚（排在最前的第一本例外，哪怕单本就超预算也会处理完，避免超大部头永远排不上）；EPUB 走 pandoc 不占用这个页数预算。成功入库后自动增量建索引（`REVIEW_AUTO_INDEX`）。想不等到凌晨立即跑一次，可执行 `python -c "import daemon; daemon.book_intake_job()"`。

## 三、MCP Server：在对话里直接用

`mcp_server.py` 把整个文献库检索和综述撰写能力包装成标准 MCP 协议 server，Claude Code / Claude Desktop / Cherry Studio 这些客户端配置一下就能在对话里直接调用，不用自己敲命令行。开放了 6 个工具：查库状态、语义检索、查单篇论文元数据、生成大纲、后台起一篇综述（因为耗时 7-30 分钟，用的是"秒回 job_id + 轮询"的异步模式）、查生成进度。

## 技术栈小结

Python 3.10+ / MinerU（PDF 解析）/ SiliconFlow 平台上的 DeepSeek + Qwen3 系列模型（嵌入、重排、生成分工明确）/ SQLite（状态与元数据）/ Jinja2（笔记与综述模板渲染）/ pandoc（Word 导出）/ FastMCP（MCP server）/ Docker + docker-compose（部署）。

## 现状

入库流水线和综述生成器都已经跑通并有真实文献库在用（100+ 篇量级）；MCP 集成、检索精度升级（换 Qwen3 嵌入+重排）、结构化元数据提取、GB 格式引用、章节编号、Word 导出这些都是这几轮迭代刚完成的，测试套件（35 个用例）全绿。目前欠缺的主要是一次**真实主题的端到端 generate 跑一遍**（用真实文献而不是造的假数据）作为最终验收，检验 GB 格式引用和排版在真实数据上的表现。
