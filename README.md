# 📚 Literature Analyzer (文献分析器)

一个强大的学术文献自动化处理流水线（Pipeline）。能够自动将学术 PDF 论文转换为结构化的 Obsidian 笔记，并汇总到 Excel 知识库中。

## ✨ 核心特性

- **高质量 PDF 解析**：集成 [MinerU](https://mineru.net/)，完美提取 PDF 中的复杂排版、公式和表格，输出高质量 Markdown。
- **智能大模型分析**：集成 **硅基流动 (SiliconFlow) DeepSeek-V3.1-Terminus** 进行无损文献分析（中英文统一路由，不做语言分流）。
- **双模运行机制**：
  - **实时模式** (`RUN_MODE=realtime`)：常驻守护进程，每隔固定时间（默认 10 分钟）自动扫描 `input_pdfs/` 目录并即刻处理，生成 Obsidian 笔记与 Excel 记录。
  - **批处理模式** (`RUN_MODE=batch`)：利用大模型的 Batch API，享受 **50% 的成本折扣**。常驻守护进程按固定间隔轮询：每 `BATCH_SCAN_INTERVAL_MINUTES`（默认 30）分钟扫描并提交任务，每 `BATCH_FETCH_INTERVAL_MINUTES`（默认 120）分钟拉取结果，同时支持容器启动瞬间的冷启动检测，确保完美错峰。
  - **教材定时入库**：与上述 `RUN_MODE` 无关，固定每天 `BOOK_INTAKE_TIME`（默认 `03:00`）跑一次，详见下方「教材/书籍入库」小节。
- **运行历史与状态追踪**：
  - **历史日志统计**：每次运行的解析、提交、拉取、导出状态和异常均会持久化记入 `data/pipeline_history.jsonl` 中。
  - **可视化看板**：提供 `python cli.py history`（查看每日处理成功/失败统计与失败详情）以及 `python cli.py status`（查看当前任务分布）命令行看板，方便进行日常运维与统计。
- **极致的生产级优化**：
  - **哈希去重**：自动计算 PDF 的 SHA-256 哈希值并记录，避免重复处理已解析文献，节省解析 Token 额度。
  - **中间产物策略**：实时模式导出后自动清理 MinerU 的临时文件夹省磁盘；批处理模式**故意保留** `mineru_output/`——综述生成器的 `enrich` 要从里面的 `content_list.json` 免费抽取图表和参考文献，删了就只能花钱调大模型重来。
  - **空解析拦截**：MinerU 返回 success 不代表抽出了字（扫描件、纯图版 PDF 常常只给个空 `full.md`）。正文少于 200 字符的直接判永久失败移入 `failed_pdfs/`，不让大模型对着标题瞎编、把污染数据写进知识库。
  - **日志降噪**：守护进程在没有检测到新文件时保持静默（仅输出一行暗色扫描提示），避免无效日志刷屏。
  - **格式与错误感知**：自动感知 CAJ 等不支持的非 PDF 格式文件并进行警告；能够动态区分临时错误（网络波动、API 超时）与永久错误（页数超限、损坏文件），对临时错误进行自动重试。

---

## 🏗️ 架构概览

```text
input_pdfs/
    └── *.pdf
        ↓ MinerUClient (OCR 解析 & 图片提取)
    mineru_output/ (解析中间产物，处理完成后自动清理)
        ├── 论文名_hash/
        │   ├── *.md (结构化 Markdown)
        │   └── images/
        ↓ LLMRouter (统一路由至 SiliconFlow DeepSeek-V3.1-Terminus)
    obsidian_vault/
        └── *.md (带 Frontmatter、原始摘要与插图的 Obsidian 笔记)
    data/
        ├── batch_tracking.db (SQLite 进度跟踪数据库)
        ├── processed_hashes.json (已成功处理文献的 SHA-256 哈希库)
        └── pipeline_history.jsonl (流水线运行事件历史日志，增量追加)
    knowledge_base.xlsx (汇总知识库)
```

---

## 🚀 快速开始

### 1. 环境准备

推荐使用 Python 3.10+。克隆项目后安装依赖：

```bash
git clone https://github.com/YourUsername/literature_analyzer.git
cd literature_analyzer
pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，并填入你的 API Keys：

```bash
cp .env.example .env
```

需要配置的特色参数：
- `MINERU_API_KEY`: 申请自 MinerU 平台。**支持配置多个 Key**（使用英文逗号 `,` 分隔，例如 `key1,key2,key3`）。程序在运行中如果遇到某个 Key 额度不足 (HTTP 402/429) 或失效，会自动平滑切换到下一个备用 Key 进行处理，防止因额度问题中断流水线。
- `DEEPSEEK_MODEL`: 文献分析模型，硅基流动上默认 `deepseek-ai/DeepSeek-V3.1-Terminus`，官方 DeepSeek 默认 `deepseek-chat`。

  > ⚠️ **换模型前必须确认它支持 Batch 推理**，否则批处理模式在提交阶段就会 400 报 `20088 This model does not support batch inference`。实测（2026-08-02）硅基流动上支持 Batch 的只有 `DeepSeek-V3`、`DeepSeek-V3.1-Terminus`、`DeepSeek-R1` 三个；`V3.2` / `V4-Pro` / `V4-Flash` / `Qwen` 系列**同步调用正常但不能走 Batch**。而 `DeepSeek-V3` 自 2026-07-24 起在硅基流动上已彻底不可用（任何请求都返回 429 `System is too busy now`），`R1` 是推理模型不适合结构化抽取——所以默认值是 Terminus。
  >
  > 这个坑很隐蔽：走 Batch 时模型不可用只会在错误文件里留下一句 `Request failed: Unknown error.`，日志上完全看不出是模型的问题。怀疑模型时先用同步调用单独试一次。

- `BATCH_SIZE_LIMIT`: 每次运行处理/导出的最大论文数量，默认为 `30`。如果您想增加每次运行处理的论文篇数，可以在环境变量中调大此数值。

---

## 🐳 Docker 部署（推荐）

本项目内置了完整的 Docker + docker-compose 支持，并以常驻守护进程（Daemon）方式运行。

**你只需要填 `.env` 这一个文件，`docker-compose.yml` 不用改。** 里面所有可变项都写成了 `${VAR:-默认值}`，默认值取的是「换台机器就能跑」的那一套：桥接网络、无代理、密钥读本地 `.env`、嵌入走在线 API、数据放 `./data`。

### 1. 部署准备
```bash
cp .env.example .env
# 编辑 .env，填第 1 段的 MINERU_API_KEY 和 SILICONFLOW_API_KEY 即可，其余各段都有能用的默认值
```

### 2. 启动服务
```bash
# 部分环境网络受限，禁用 Buildkit 走经典构建
DOCKER_BUILDKIT=0 docker compose build
docker compose up -d
```

### 3. 常用配置项

| 变量 | 作用 | 什么时候需要改 |
|---|---|---|
| `RUN_MODE` | `batch`（省 50%）或 `realtime` | 想立等结果就用 realtime |
| `DATA_ROOT` | 所有数据目录的根，默认 `./data` | 数据大了指向别的盘，如 `/mnt/data/lit` |
| `CONTAINER_NAME` | 容器名，默认 `lit_analyzer` | 一台机器跑多套时必须区分 |
| `OBSIDIAN_VAULT_DIRNAME` | vault 目录名 | 想用中文库名时 |
| `DOCKER_NETWORK_MODE` | 默认 `bridge` | 仅当宿主机防火墙拦 docker 网桥、容器完全断网时改 `host` |
| `LIT_HTTP_PROXY` / `LIT_HTTPS_PROXY` | 容器用的代理，默认空 | 国内访问 MinerU / 硅基流动都不需要 |

> ⚠️ **一台机器跑多套时（比如「自己用」+「给别人用」各一份），`CONTAINER_NAME` 和 `DATA_ROOT` 必须同时改。** 只改容器名不改数据目录，两套会共用同一个数据库和 vault、互相覆盖处理记录——这比容器起不来严重得多。

> ⚠️ **代理变量为什么带 `LIT_` 前缀**：compose 做变量替换时 shell 环境的优先级高于 `.env`，而 `HTTP_PROXY` 恰恰最容易被 export。不加前缀的话，你 shell 里挂的代理会被悄悄注进容器；bridge 模式下 `127.0.0.1` 指的是容器自己的回环，所有外部请求变成 connection refused，而配置文件上完全看不出来。

### 4. 换机器 / 内存小的机器部署

镜像固定 tag 为 `lit-analyzer:1.0`（可用 `IMAGE_NAME` 覆盖），所以「本机构建 → 传镜像 → 目标机直接起」这条路是通的：

```bash
# 本机
docker save lit-analyzer:1.0 | gzip > lit.tar.gz
# 目标机
gunzip -c lit.tar.gz | docker load
docker compose up -d          # 镜像名对得上，不会触发构建
```

小内存云服务器（1~2G）建议走这条路：本地构建时 pip 装 pandas/numpy 很吃内存，直接在服务器上 build 容易 OOM。

> ⚠️ **容器代码是 `COPY . .` 打进镜像的，不是挂载的。** 改了宿主机上的 `.py` 对运行中的容器**无效且不报错**，必须 `docker compose build && docker compose up -d`。
>
> ⚠️ **改了 `.env` 之后 `docker restart` 不生效**——环境变量是容器创建时烤进去的。必须 `docker compose up -d --force-recreate`，核对用 `docker exec <容器名> printenv <变量名>`。

### 4. 运行日志监控与日常运维
```bash
# 查看实时容器运行日志
docker logs -f lit_analyzer

# 查看最近 7 天的每日文献处理统计与失败详情看板
docker compose exec literature-analyzer python cli.py history

# 查看最近 30 天的运行历史统计
docker compose exec literature-analyzer python cli.py history --days 30

# 查看当前文献库的各状态数据汇总看板
docker compose exec literature-analyzer python cli.py status

# 对系统网络、API 密钥、存储挂载做全方位系统健康度体检
docker compose exec literature-analyzer python cli.py doctor

# 人工一键重置所有失败任务以启动自动重试
docker compose exec literature-analyzer python cli.py reset --failed
```

---

## 📁 核心文件说明

- `daemon.py`: 常驻守护进程主程序，根据 `RUN_MODE` 自动调度实时扫描或定时批处理提交与拉取。
- `pipeline.py`: 实时处理逻辑实现。
- `batch_pipeline.py`: 批处理处理逻辑实现（包含基于 SQLite 的提交与结果拉取状态流转）。
- `cli.py`: 运维及状态/运行历史统计管理 CLI 终端。
- `mineru_client.py`: 封装了 MinerU 的两步上传、解析轮询及结果下载，轮询间隔已优化为 10 秒。
- `llm_router.py`: 大模型调用路由，内置 SiliconFlow / 官方 DeepSeek 双通道及原始摘要无损提取逻辑。
- `utils.py`: 共享的工具函数，提供 Excel 导出、Obsidian 模板渲染、SHA-256 去重哈希计算、运行日志追加等。
- `obsidian_template.md`: Jinja2 模板，定义了生成的 Obsidian 笔记排版样式。
- `review.py` + `litreview/`: 综述生成器（分块 → Qwen3-Embedding 向量检索 → Qwen3-Reranker 重排 → 证据打分 → 带引用归纳）。
- `review_template.md`: 综述笔记的 Jinja2 模板。

## 📝 综述生成器（litreview）

将文献库直接生成带参考文献的中文学术综述。**入库、索引与元数据提取全自动**（守护进程每 30 分钟扫描新文件、每 2 小时拉取结果、导出后自动更新向量索引和结构化元数据），**综述生成手动触发**：

```bash
python review.py status               # 索引状态
python review.py index [--force]     # 手动建/重建向量索引（换嵌入模型后需 --force）
python review.py enrich [--no-llm]   # 提取结构化元数据：中英标题/DOI/图表/参考文献 → paper_details 等新表
python review.py search "关键词"      # 检索调试（默认走重排序，--no-rerank 看纯向量结果）
python review.py types                # 文献分类总览：各类型篇数 + GB/T 7714 码（详见下文）
python review.py patents              # 专利台账：出版阶段/保护期/法律状态（详见下文）
python review.py standards            # 标准台账：标准号/实施日期/发布机构/现行状态
python review.py outline "主题" -o outline.json   # 只生成大纲（可手工编辑后再传入 generate）
python review.py generate "主题" [--outline outline.json] [--dry-run] \
    [--focus "侧重方向"] [--words 目标总字数] [--sections 章节数]
```

生成参数说明：
- `--focus`：侧重方向（如"侧重环保型材料与降解机理"），贯穿大纲设计、每章写作和引言结论；
- `--words`：目标总字数，自动摊分到各章节（引言/结论各约 8%）；不指定时每章 600-1000 字；
- `--sections`：主体章节数；不指定时由模型自定 3-6 个；
- `--outline`：外部大纲文件（JSON 或 `## 章节标题` + `- 检索问题` 格式的 markdown），完全手工控制章节与检索方向，指定后跳过大纲生成。

- **检索链路**：Qwen3-Embedding-8B（4096 维，跨中英）向量召回 50 候选 → Qwen3-Reranker-8B 重排取 top 24 → DeepSeek-V4-Pro 逐条打分提炼证据（≥6 分保留）→ 只依据证据写作并强制引用标记 → 全局编号与 GB/T 7714 风格参考文献（含 DOI）。
- **结构化元数据**（`batch_tracking.db` 新表）：`paper_details`（中英标题/DOI/作者/期刊/年份/关键词）、`paper_assets`（图/表/图题/页码）、`paper_references`（每篇论文自己引用的文献逐条）。提取以免费的 content_list.json 本地解析为主，LLM 仅补缺。
- **自动插图**：写完每章后，从本章已引用文献（论文或教材）的 `paper_assets` 里按图题与本章主题的重排相关度挑图（过阈值），把真实图片复制进 `reviews/assets/` 并插入正文，图注随全文统一编号。图片会一并嵌入导出的 Word（pandoc `--resource-path` 指向 .md 目录解析相对图路径）。
- **输出格式**：标准论文式章节编号（`1 引言` → `2..N` 正文各章 → `N+1 结论与展望`），末尾 `参考文献` 不编号；不含摘要/关键字块。生成 Markdown（写入 Obsidian `reviews` 目录）的同时，用 pandoc 自动导出同名 `.docx`（导出前去掉 Obsidian wikilink 后缀，参考文献逐条独立成段），Word 导出失败不影响 markdown 主流程。参考文献按 GB/T 7714-2015 的**文献类型标识码**区分，8 种类型各走各的著录格式（见下文「文献类型」一节）。
- 配置见 `.env.example` 的 litreview 段；依赖 `SILICONFLOW_API_KEY` / `SILICONFLOW_API_BASE`；Word 导出依赖系统装有 `pandoc`。

### 📖 教材/书籍入库（轻量检索语料）

教材、图书可作为**检索语料**加入文献库，供 `search` 与综述引用——但**不做 LLM 全文分析、不生成 Obsidian 笔记**（省钱），解析成文本入检索库、抽图入 `paper_assets`（综述可引用教材里的图）、并写一行 Excel。支持 **PDF**（走 MinerU）与 **EPUB**（走 pandoc，无需 MinerU），按扩展名自动分流：

```bash
python review.py add-book 某教材.pdf            # 单本 PDF（放哪都行，直接给路径）
python review.py add-book 某教材.epub           # 单本 EPUB
python review.py add-book ./books/              # 批量（指定目录内所有 PDF/EPUB 混合）
python review.py add-book                       # 不传路径：扫描默认书籍目录 BOOK_INPUT_DIR
python review.py add-book 大部头.pdf --no-llm    # 元数据只本地解析，出版社/版次留空待手填
python review.py index                          # 入库后建索引（与论文共用检索库）
python review.py search "某概念" --corpus book   # 只在教材中检索（默认 all=全库混检）
```

- **统一管理目录**：`BOOK_INPUT_DIR`（默认 `./input_books`）与 `MINERU_OUTPUT_DIR`平行——`add-book` 不传路径时默认扫描这里；解析结果落进 `BOOK_OUTPUT_DIR`（默认 `./book_output`，与 `MINERU_OUTPUT_DIR` 平行、不跟论文的解析结果混放），每本书按 `书名_doc_id前8位` 单独建一个子文件夹（拆分出的 PDF 分册、EPUB 抽出的图、full.md 等都在里面）。两个目录都可在 `.env` 里改。
- **PDF 自动拆分**：书籍常超 MinerU 单任务页数上限，`add-book` 会用 `pypdf` 按 `BOOK_SPLIT_PAGES`（默认 180）把大 PDF 切成多份、逐份解析后拼回**同一 doc_id**（按整本 PDF 哈希，重复运行幂等）。
- **EPUB 直接转换**：数字文本无需拆分/MinerU，`pandoc` 直接 epub→markdown 入库（`--extract-media` 抽图入 `paper_assets`）+ epub→docx 生成可读 Word（源文件同目录同名）。`--pages` 对 EPUB 无效。
- **元数据**：PDF 书名取文件名；EPUB 优先读 OPF 的 `dc:title/creator/publisher/date/identifier`（读不到才回退文件名）。年份/ISBN 本地正则提取；作者/出版社/出版地/版次缺失时由**每本一次前置页 LLM 小调用**补齐（仅送封面/版权页附近 ~2500 字，几分钱级；`--no-llm` 可关，或本地元数据已齐全时自动跳过 LLM 调用）。
- **依赖**：PDF 拆分依赖 `pypdf`；EPUB 转换依赖系统装有 `pandoc`；元数据 LLM 补齐依赖 `SILICONFLOW_API_KEY`。加入教材后语料变大，建议把 `RERANK_CANDIDATES` 调大（50→150）以保检索准确率。
- **每日定时入库（守护进程）**：`daemon.py` 固定每天 `BOOK_INTAKE_TIME`（默认 `03:00`，本地时区）自动扫描 `BOOK_INPUT_DIR`，按文件名排序逐本处理，PDF 累计页数一旦超过 `BOOK_DAILY_PAGE_BUDGET`（默认 2000）就推迟到下一晚（但排在最前、单本就超预算的大部头仍会处理完，避免永远排不上）；EPUB 走 pandoc 不占该页数预算。成功入库后若 `REVIEW_AUTO_INDEX` 开启会自动增量建索引。手动立即触发一次（不等到凌晨）：
  ```bash
  python -c "import daemon; daemon.book_intake_job()"
  ```

### 🗂 文献类型（GB/T 7714-2015 码表）

库里不止论文和教材：专利、国标行标、协会年报、企业财报、机构研究报告、网页资料都可能进来，它们的参考文献著录格式各不相同。类型词表集中在 **`litreview/doctype.py`** 一个文件里，**加一种类型 = 加一行词表 + 加一个著录分支**，命令行选项、MCP 参数校验、分块参数全部由它派生：

| 类型键 | 名称 | GB/T 7714 码 | 归类方式 | 长文本分块 |
|---|---|---|---|---|
| `paper` | 期刊论文 | `[J]` | 默认 | 否 |
| `book` | 专著/教材 | `[M]` | `add-book` 入库时指定 | 是 |
| `patent` | 专利 | `[P]` | **封面正则自动识别** | 否 |
| `standard` | 标准 | `[S]` | **封面正则自动识别** | 否 |
| `report` | 报告 | `[R]` | 人工 `set-type` | 是 |
| `thesis` | 学位论文 | `[D]` | 人工 `set-type` | 是 |
| `conf` | 会议论文 | `[C]` | 人工 `set-type` | 否 |
| `web` | 网络资源 | `[EB/OL]` | 人工 `set-type` | 否 |

```bash
python review.py types                            # 分类总览：各类型篇数 + 码 + 归类方式
python review.py types --rescan                   # 重扫存量文献，找出被误判成论文的专利/标准（仅预演）
python review.py types --rescan --apply           # 确认后写库（不调 LLM、不重解析，可重复执行）
python review.py types --rescan --only standard   # 只扫标准
python review.py set-type <doc_id或标题片段> --type report \
    --publisher "FAO/IFA" --place Rome --year 2024          # 人工指定类型，顺带补著录项
python review.py search "限量要求" --corpus standard         # 只在标准中检索
```

**⚠️ 只有专利和标准能自动识别，这是有原因的，不是偷懒。** 专利有 INID 码、标准有强制封面格式（`中华人民共和国国家标准` + 标准号 + `发布`/`实施` 双日期），都是全球/全国统一的硬格式；而**协会年报、企业财报、研究报告跟一篇长论文在版式上没有任何可靠差别**——扉页有标题有机构名有年份，再往下就是正文。硬要凭"出现了 Annual Report 字样"去猜，误判率高到会污染整个库。所以这几类**一律靠 `set-type` 人工指定**，词表里 `detect: False` 就是在写明这件事，别人想给它们加自动识别时会先撞上 `tests/test_doctype.py::test_only_patent_and_standard_claim_auto_detection`。

自动识别一律要求**命中 ≥2 个标记**，单个关键词绝不定案：一篇论文正文里写"酸值按 GB/T 4945—2002 测定"是家常便饭，只凭一个标准号就判成标准，整个库都会被污染。

### 📐 标准（自动识别，现行与否人工核实）

`enrich` 阶段扫封面，抽出标准号、中英文名称、发布日期、实施日期、发布机构，写进 `doc_no` / `effective_date` 等列。三个真实的坑都已在 `tests/test_doctype.py` 里用**生产库原样抠出来的封面文本**锁住：

- **`代替 GB/T 1677—1981` 出现在真编号前面**——先撞上它就会把被作废的旧版号当成本标准的号；
- **MinerU 把斜杠/破折号包进 HTML 标签**——封面上印的是 `GB/T 32952—2016`，解析出来是 `GB<sup>/</sup>T32952<sup>—</sup>2016`，不先剥标签连编号都匹配不上；
- **造字子集乱码**——`犌犅／犜7363—2021` 其实是 `GB/T 7363—2021`。只修了实测确认的 3 个字母（犌=G 犅=B 犜=T），没有整表瞎猜。

标准的 `long=False`（与论文同一套分块参数）：正文是一条一条的条款，一条一个意思，用大块反而检得更糊。**好处是论文→标准的重新归类不需要重建索引。**

标准也分"封面事实"和"人工结论"两类，跟专利同理：实施日期印在封面上、不会变；**现行/被代替/废止会随时间变化，PDF 里没有**，只能 `python review.py standards --set GB38400-2019 --status 废止` 人工录入。没核实过就显示「未核实」，**不会拿实施日期冒充现行状态**——一份已废止标准的限量值被当成现行的引用，后果是实打实的。

### 📄 专利（自动识别，法律状态人工核实）

专利 PDF **不需要单独的入口**——跟论文走同一条流水线，`enrich` 阶段按扉页的 **INID 码**（WIPO ST.9 标准著录项目代码，`(21)申请号` `(22)申请日` `(72)发明人` `(54)发明名称` 等，全球专利文献通用）自动判定，命中 **≥2 个**标记才算数（只出现一次 "United States Patent" 的论文不会被误判）。识别为专利后 `doc_type='patent'`，发明人/申请人/申请日直接从 INID 码取，**跳过 LLM 补缺**（同教材的做法，省一次 API 调用）。

```bash
python review.py patents                          # 专利台账
python review.py patents --set CN110346043A --status 驳回   # 人工录入法律状态，自动记核实日期
python review.py search "余热回收" --corpus patent          # 只在专利中检索
```

> 重扫存量文献统一走 `python review.py types --rescan`（专利和标准一起扫），**`patents --rescan` 已移除**——同一件事只留一个入口。

**⚠️ 状态分两类，这是本功能的设计前提，改代码前务必看清 `litreview/patent.py` 模块头部说明：**

| | 出版阶段 / 保护期至 | 法律状态 |
|---|---|---|
| 来源 | 专利号种别码 + 申请日，**印在 PDF 上** | 驳回/撤回/欠年费失效/无效宣告，**PDF 里没有** |
| 会变吗 | 不会 | **会**，随时间变化 |
| 怎么得到 | 本地现算，零成本零网络 | 只能人工核实或接外部数据源 |
| 存哪 | 不落库，读取时现算 | `legal_status` + `status_checked_at` 两列 |

- 种别码：CN `A`=发明申请公布（审查中）/ `B`=发明授权 / `U`=实用新型授权 / `S`=外观设计；US `A1`=申请公开、`B1`/`B2`=授权；EP `A*`=申请、`B*`=授权。扉页同时印着授权公告号和申请公布号时**取授权号**，它才代表当前阶段。
- 保护期：申请日 + 20 年（发明）/ 10 年（实用新型）/ 15 年（外观设计），是纯算术。但这只是**期限上限**——欠缴年费、主动放弃、被宣告无效都会让专利早于此日失效，那些事件本地看不到。
- 没人工核实过就显示「未核实」，**不会拿出版阶段冒充法律状态**（种别码 `B` 只说明它当年被授权过，不代表今天仍然有效）。
- `legal_status` / `status_checked_at` 两列**不会被 `enrich --force` 冲掉**（`save_enrichment` 是 `INSERT OR REPLACE`，写入前会先取回旧值沿用），人工成果安全；同样受保护的还有 `doc_no` / `url` / `effective_date`。
- 新增的列（`patent_no` / `filing_date` / `legal_status` / `status_checked_at` / `doc_no` / `url` / `effective_date`）全部可空、默认 NULL，随 `paper_details` 既有的热升级机制自动补列，对已有论文/教材行零影响。**标准的实施日期单独占一列 `effective_date`，没有塞进专利的 `filing_date`**——两个日期含义完全不同，挤一列日后必然误读。
- `set-type` 写库走 `mark_doc_type`，每一列都是 `COALESCE(NULLIF(?,''), 旧值)`：一次退化的重新识别**永远不会**把已经正确的值抹成空。

## 🔌 MCP Server（在大模型对话中直接调用）

`mcp_server.py` 把文献库检索与综述撰写打包成标准 MCP server（stdio），可被 Claude Code / Claude Desktop / Cherry Studio 等任意 MCP 客户端调用。

**提供 7 个工具**：

| 工具 | 说明 |
|---|---|
| `library_status` | 文献库概况（篇数/索引/元数据覆盖，按 8 种文献类型分列） |
| `search_literature(query, top_k, corpus)` | 语义检索文献片段（向量召回 + 重排序，跨中英）；`corpus` 可限定为词表里的任一类型（`paper`/`book`/`patent`/`standard`/`report`/…） |
| `get_paper_info(keyword, limit)` | 按标题/关键词/专利号/标准号查结构化元数据（DOI/作者/期刊/TLDR，专利给出阶段与保护期，标准给出实施日期与现行状态） |
| `doc_status(doc_type, keyword)` | 专利/标准台账：出版阶段、保护期至、实施日期、人工核实状态（**只读**，状态只能用 CLI 人工录入） |
| `generate_outline(topic, focus, sections)` | 生成综述大纲 JSON（同步，约 1 分钟） |
| `start_review(topic, focus, words, sections, outline_json)` | 后台启动完整综述生成，立即返回 job_id |
| `review_status(job_id)` | 查询生成进度/日志尾部/产出文件路径（markdown + Word 各一份） |

> `doc_status` 刻意**不提供写入能力**——专利的法律状态、标准的现行与否都是要对外负责的结论，不能由模型自行判断后写进生产库；工具返回「未核实」时也明确要求调用方不要拿出版阶段或实施日期替代作答。
>
> 检索结果会带类型标签：看到 `[标准 GB 38400-2019]` 才知道这段限量值出自强制性文件而非某篇论文的实验结论，两者的可引用分量完全不同。
>
> **`patent_status` 已更名为 `doc_status`**（多了个 `doc_type` 参数，专利和标准共用）。其他设备只需重启一次 MCP client——工具是动态发现的，配置文件不用改。

综述生成耗时 7-30 分钟（取决于模型），远超 MCP 工具调用超时，所以采用**后台任务模式**：`start_review` 秒回 job_id，之后随时用 `review_status` 轮询，完成后返回 Obsidian 笔记路径。

**注册（Claude Code）**：

```bash
claude mcp add --scope user literature-review /path/to/literature_analyzer/mcp_server.sh
```

**其他客户端（Claude Desktop / Cherry Studio 等）** 的 `mcpServers` 配置：

```json
{
  "mcpServers": {
    "literature-review": {
      "command": "/home/dudu/GoogleDrive/Antigravity/literature_analyzer/mcp_server.sh"
    }
  }
}
```

`mcp_server.sh` 负责加载密钥、清理代理变量、指向生产数据库后启动 server；换机器部署时需改脚本里的三个路径。可选环境变量 `MCP_OUTLINE_MODEL`（大纲生成模型，默认 Qwen2.5-72B-Instruct，比写作模型快）。

> [!WARNING]
> `mcp_server.sh` 目前仍**硬编码** `. /opt/docker_shared/api_keys.env`，没跟着去宿主机化一起改成 `${SHARED_ENV_FILE}`。
> 别人的机器上没有这个文件，脚本开头是 `set -a` 加 source，失败会直接退出、MCP 起不来。
> 自己部署时请先把这一行改成你的密钥文件路径（或直接删掉，改用 `.env`）。

**远程接入（别的设备连回跑着文献库的那台机器）**：MCP 走 stdio，所以直接用 SSH 把命令跑在对端即可。以 Windows 上 Git 自带的 `ssh.exe` 为例：

```json
{
  "mcpServers": {
    "literature-review": {
      "command": "C:\\Program Files\\Git\\usr\\bin\\ssh.exe",
      "args": [
        "-T",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=30",
        "-p", "<端口>", "<用户>@<主机>",
        "/path/to/literature_analyzer/mcp_server.sh"
      ]
    }
  }
}
```

这四个选项**都不是可有可无的**，缺任何一个的表现都是"客户端显示已连接后立刻断开"，见下方经验教训。改配置前先手动跑一次 `ssh -T -p <端口> <用户>@<主机> "whoami"` 确认链路本身通。

**日志**：server 启动/就绪/退出以及每次工具调用（入参、耗时、异常堆栈）都会写入 `data/mcp_server.log`（不经过 stdout/stderr，不会污染 stdio 协议流）。排查"客户端显示已连接却立刻断开"一类问题时先看这个文件：

```bash
tail -f /home/dudu/GoogleDrive/Antigravity/literature_analyzer/data/mcp_server.log
```

如果连"MCP server 启动中"这条都没出现，说明进程根本没跑起来（传输层/SSH 问题，而非 server 代码本身）；如果启动日志有但没有任何工具调用记录，说明客户端连上后没有发起过请求。

**经验教训**：以下三个坑现象**完全一样**——客户端显示"已连接"后几十~几百毫秒内立刻断开，服务端日志干干净净——但成因不同。根因都是同一条：MCP stdio 要求 stdin/stdout 是**未经改动的原始字节流**，任何往这条流里掺东西的行为都会破坏协议帧。

1. **伪终端（pty）**：SSH 转发时如果分配了 pty（`-t`/`-tt`，或某些客户端默认），会插入回显、换行转换等终端层处理。用 `-T` 显式禁用。
2. **`known_hosts` 缺条目**：`known_hosts` 是按 `[主机]:端口` 存的，所以**换公网 IP 或换端口都等于全新条目**，ssh 会弹 `Are you sure you want to continue connecting?`。MCP 客户端没有终端可回答，这行提示直接窜进协议流。用 `-o StrictHostKeyChecking=accept-new` 首次自动接受。
3. **找不到密钥时转去要密码**：ssh 会去读 stdin 等用户输入密码——而 stdin 正是 MCP 的协议输入流，等于把协议数据当密码吃掉。用 `-o BatchMode=yes` 让它直接失败而不是转去交互。
   Windows 上还有个额外诱因：Git for Windows 的 `ssh.exe` 被 MCP 客户端以纯 Win32 进程拉起时 `HOME` 可能没设置，于是找不到 `~/.ssh/id_ed25519`。这种情况要在 `args` 里补 `"-i", "C:/Users/<你>/.ssh/id_ed25519"`。

排查顺序：**先在终端里手动跑一遍同样的 ssh 命令**（`ssh -T -p <端口> <用户>@<主机> "whoami"`）。手动能通而 MCP 不通，基本就是上面第 2、3 条；手动就不通，那是链路或认证问题，跟 MCP 无关。
