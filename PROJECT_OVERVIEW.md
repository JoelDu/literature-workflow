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
- MinerU 的中间产物（正文 md、公式/表格切片、`content_list.json`）在**批处理模式下故意保留**，因为综述生成器的 `enrich` 要靠它免费解析元数据、`figures.py` 要靠它自动插图——删了就只能改花钱调大模型。代价是占盘：当前论文侧 843MB、教材侧 2.0GB。只有实时模式（`RUN_MODE=realtime`）用完即删。
- 解析出口按 `MIN_MARKDOWN_CHARS=200` 拦空结果：MinerU 返回 success 不代表抽出了字，扫描件和纯图版 PDF 常常只给个空 `full.md`，放行的话大模型只能靠标题编，编出来的还会写进 Obsidian 和 Excel
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

走 stdio 协议，所以除了本机直连，也能让别的设备通过 SSH 接进来共用同一个库（经一条反向隧道落到跑着文献库的那台机器上）。6 个工具对论文库全是**只读**的，唯一的写入是把生成的综述 md/docx 落盘。

## 技术栈小结

Python 3.10+ / MinerU v4（PDF 多模态解析，强制 `vlm` 引擎）/ SiliconFlow 平台上的 DeepSeek-V3.1-Terminus（论文分析与综述写作，Batch API 打 5 折）+ Qwen3-Embedding-8B（4096 维向量化）+ Qwen3-Reranker-8B（精排）/ SQLite（状态、结构化元数据与向量，单文件）/ Jinja2（笔记与综述模板渲染）/ pandoc（Word 导出、EPUB 转换）/ FastMCP（MCP server）/ Docker + docker-compose（部署）。

> 模型选型上有个硬约束：整个 SiliconFlow 账户里只有 DeepSeek V3 / V3.1-Terminus / R1 支持 Batch 推理，其余型号提交时直接报 `20088 not support batch inference`。而 V3 已于 2026-07-24 后全量返回 429、R1 是推理模型不适合结构化抽取——**Terminus 是目前唯一跑得通的选择**，换模型前务必先确认 Batch 支持。

## 现状

入库流水线和综述生成器都已跑通，有真实文献库在用：**176 篇/本已完整入库，向量索引覆盖 169 个文档、12375 个块**，结构化元数据 176 篇（含图表 10388 条、参考文献 5301 条）。MCP 集成、检索精度升级（换 Qwen3 嵌入+重排）、结构化元数据提取、GB 格式引用、章节编号、Word 导出、教材 EPUB 支持、每日定时入库均已完成，**测试套件 62 个用例全绿**。部署也已去宿主机化——同一份 compose 靠一个 `.env` 就能在开发机、云服务器、别人的电脑上跑，并已在阿里云实机验证过一轮。

还欠着的：

1. **一次真实主题的端到端 `generate` 验收**——始终没用真实文献（而非造的假数据）完整跑过，GB 引用和排版在真实数据上的表现仍是未知数。这是最老的一笔欠账。
2. **Web UI**——目前全靠 CLI 和 MCP，"别人上传文件夹、填自己的 API Key 就能用"这个目标卡在这一步。
3. **打包分发与 LICENSE 选型**——分发之前必须定下来。
4. **元数据补缺还在多花钱**：176 篇里 173 篇调了大模型补 `DOI/作者/期刊/年份`，但其中 77 篇本来就抓到了 DOI——这些字段直接查 CrossRef 免费 API 就是权威值，比让模型猜准得多。
