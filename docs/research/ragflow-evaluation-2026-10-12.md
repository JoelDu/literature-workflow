# RAGFlow 用于 Literature Analyzer 的事实核查

核查日期：2026-10-12。范围为上游官方文档与源码的只读核查；未部署、未运行 PDF 解析或 UI 高亮验收、未调用付费模型。本仓库此前没有研究笔记目录，因此保存于 `docs/research/`。

## 建议

用户最新范围：**今天不考虑 BM25 / 混合召回，该方向已有其他项目研究**。下面早一轮讨论与检索消融仅保留为历史背景，不属于当前优化范围。

与 Claude 重新筛选后提出页码溯源和结构切块两个方向，用户本轮选择了 **表格/公式结构保护切块，并要求借鉴 RAGFlow 的优点**。现已实现默认关闭的结构切块 API 与离线预览，继续使用 MinerU；生产索引尚未切换，未调用模型或重嵌入。页码方案保留为后续候选。下文历史实验提案不代表已运行或当前默认行为。

## 核查版本

- GitHub 最新 release 接口在核查时返回 `v1.0.0-rc1`，发布日期 2026-09-29；对应 commit 为 `14bb02ee4584c1ab18f0c722c48beecd6bbf9860`。注意名称包含 rc1，但 GitHub release 的 `prerelease` 标记为 false；不能据此省略版本差异。[S1]
- 同时核查最近的 0.x release `v0.27.2`，commit 为 `a024bea0cd93f39e6652a42bf84dd20c55bc560b`，发布日期 2026-09-10。[S2]
- 当前 main 固定在 `2d34209f5d81a0d57bfa00b139e925979f095fed`（2026-10-12）。以下标明 main 的源码发现仅适用于这个提交，不能自动外推给 rc1 镜像。[S3]

## 需要修正或限定的介绍

| 原介绍中的判断 | 核查结论 |
| --- | --- |
| Paper 会自动准确区分 Abstract、Introduction、Methods、Results、References，保持方法与图表语义连贯 | 0.27.2 Paper 源码确实有摘要抽取、标题层级与结构组块；按标题 pivot 合并时仍受 `chunk_token_num` 限制。源码不构成全部 IMRaD 语义识别、标题树精度、跨页图表连续性保证。main 配置文档只说明依据摘要、章节、子章节等结构切分。[S4][S5] |
| DeepDoc 精准处理双栏、复杂表格、化学公式 | 官方 DeepDoc 文档支持 OCR、版面分类、表格结构识别，版面类别包含 Equation。但检测公式所在区域不等于正确恢复化学结构或 LaTeX；官方展示的是能力及示例，不能推导出化学语义准确率。[S6] |
| DeepDoc 对中英科技 PDF、扫描件、表格明显优于 MinerU VLM | 在此次有限核查的官方 DeepDoc 文档、Paper/MinerU 源码、仓库 benchmark 文档中，未找到固定语料、固定版本、同条件比较两者的可信准确率报告。仓库 HTTP benchmark 主要是 chat/retrieval 性能工具，不是这项解析优劣的证明。这不等于全网不存在研究，只意味着当前没有足够依据作该断言。[S4][S6][S7] |
| 答案每一句都能定位原 PDF，因而可信 | DeepDoc 有页面/矩形位置输出，API 支持 chunk 的 positions 与引用资料；这提供验核材料，不能保证每句答案有引用、引用支持该句、或解析坐标准确。高亮还依赖所选解析器保留源 PDF 坐标，必须做 UI 端到端验收。[S6][S8] |
| MinerU 输出 Markdown/JSON 导入就兼得精度与高亮 | 只导入 Markdown 不会自动携带原 PDF page/bbox。0.27.2 的原生 MinerU Python 适配器可读取 `content_list.json` 中 bbox/page_idx，并用 middle JSON 补充跨页位置；这是源码能力，尚未验证用户所用云 API 路径到 UI 的完整链路。[S9] |
| RAGFlow 原生 MinerU 接入一概保留/不保留 bbox | 存在版本差异。核查的 main Go `pdf_parser_mineru.go` 走 MinerU Local driver，最终提取 Markdown、用 Markdown parser 输出；非空结果的 pageCount 被设为 1，此路径没有恢复真实 PDF bbox。另有 mineru.net provider，不应把 provider 存在当作此 PDF 路径已保留坐标的证明。[S10][S11] |
| CPU 可行，所以本地负担轻且运行平稳 | main 官方文档明确 1.0 开源版 DeepDoc 布局/OCR/表格使用 CPU；推荐起点是 4 核 x86-64、16 GB RAM、50 GB 可用磁盘。实际随文档引擎、数据量、解析并发和本地模型变化；外部 Embedding/Reranker/LLM 不会消除本地解析与存储成本。[S12] |
| 容器只有 server、ES/Infinity、MySQL/Redis/MinIO | 这是旧式简图。main 已是 Go 部署，README 明示 Kvrocks 作缓存/Checkpoint、NATS JetStream 作队列；Compose 中有 MinIO 服务和 ClickHouse 等组件，具体启用受 profile/config 控制。部署必须依据固定版本 Compose，而不能沿用旧依赖清单。[S12][S13] |
| 其他主流 RAG 产品大多只能按固定 token 暴力切分 | 本次未核查 Dify/FastGPT/LangChain/LlamaIndex 的全部当前解析配置，故不采信这一概括，也不以此作为 RAGFlow 胜出的依据。 |

## 仅作检索后端：可行，需固定参数

官方 HTTP API 有 `POST /api/v1/retrieval`，返回 chunks、document_id、分数、positions 等；Python SDK 有 `RAGFlow.retrieve(...)`。可以只调用检索端点，把最终生成和论文身份管理留在本项目，无需迁移到其聊天界面。[S8][S14]

main 检索服务明确区分全文、向量与混合检索，并支持 reranking；Embedding 仍用于查询向量，配置 Reranker 时还可能调用该模型。不同文档引擎的融合策略并不完全一致，源码中的 weighted_sum 不能直接描述成 RRF。[S15]

建议实验时关闭跨语翻译、关键词扩展、知识图谱/知识编译等额外步骤，保留明确的全文+向量配置，并用调用日志确认只调用预期的 Embedding/Reranker、没有生成型 LLM。此处是待执行的实验配置和验收项；不能把 API 的 `keyword=false` 简单解释成关闭全部 BM25，因为混合召回与附加关键词功能不是同一个配置层。[S8][S15]

## 模型与许可证

- 主仓库 LICENSE 为 Apache-2.0。它说明主仓库代码授权，不能代替模型权重、容器依赖和外部 API 各自的许可证/服务条款审查。[S16]
- main 有 SILICONFLOW provider 配置，列出聊天、Embedding、Rerank、ASR 等模型类型，原介绍中的硅基流动可插拔方向有依据；具体 Qwen 型号/API 可用性仍需与现有供应商配置匹配，不能因列在配置里就认为当前账户一定可用。[S17]

## 本机约束（来源与实验建议分开）

主 agent 本轮只读检查提供：主机内存约 30 GiB，available 约 20 GiB，swap 已用约 4 GiB；系统盘仅余约 9.6 GiB。现有夜间本地 embedding 据 `handoff.md` 约占 15 GiB。这些是本项目现场检查/交接信息，非 RAGFlow 官方测量，也不是已测得的 RAGFlow 占用。

因此即使总内存超过官方推荐起点，当前系统盘也不满足官方 50 GB 空闲盘建议；应先规划数据盘和 Docker 镜像/volume 所在位置。若后续实验，使用独立 Compose project 与端口，限制解析并发，避开现有夜间 embedding；此次没有执行部署。[S12]

## 前一轮与 Claude 的讨论结论及本库核对

本轮通过 tmux `literature:0.1` 与 Claude 讨论，双方各调用 subagent，达成的建议顺序为：**先建证据评测集，再在本库评估 BM25 + 向量融合，最后按收益决定是否启用 RAGFlow 外部基线**。这是建议，尚未获得实验结果，也没有开始实现或迁移。

- 现有 MinerU 请求已用 `model_version="vlm"`（`mineru_client.py:47`），不应把本项目当作尚无结构化解析的系统。`pipeline.py:141`、`batch_pipeline.py:222` 的少于 200 字符拦截是空解析质控，不是所有扫描 PDF 被拒绝，也不能据此推导 MinerU 不会 OCR。
- 当前 `split_markdown` 已按标题切节，超长文本按段落、行、句递归处理；默认大小 1000 字符、重叠 150 字符（`litreview/chunker.py:63`、`:109`）。尚无对表格/公式整体边界的强保证，适合列入切块消融。
- 向量存在 SQLite BLOB，`VectorStore.search` 用 NumPy 矩阵点积排序（`litreview/store.py:54`、`:377`）；检索与重排在 `litreview/stages.py:525`、`:539` 编排。Claude 的只读代码/库审计报告尚无 BM25/FTS5/RRF 和检索 gold 题集。它报告的当时规模约 513 篇、46.7k chunks 是现场快照，不是固定项目常量。
- 现有 `Chunk` 和 `review_chunks` 没有 PDF page/bbox 字段（`litreview/chunker.py:28`、`litreview/store.py:54`）。若要补原 PDF 高亮，应保留 `Work.work_id → Document Asset SHA-256 → parser/version → backend_doc_id/chunk_id → page/bbox` 映射；不能从 Markdown 凭空恢复位置。物理页码、印刷页码和 bbox 坐标系须分别定义。

FTS5 + RRF 的融合公式简单，但完整接入需处理中文分词、化学式/上下标/LaTeX 归一化、文献过滤、索引同步与证据映射。主 agent 在 SQLite 3.46.1 **内存库**中复现：默认 `unicode61` 把连续的“磷酸铁锂的制备方法”作为一个 token，检索“磷酸铁锂”零命中；`H₂SO₄` 与 `H2SO4` 也不自动匹配。故默认分词不是中文逐字切分，直接建 FTS5 表不足以保证有效召回。归一化应只生成辅助检索字段，保留原证据中的单位、上下标、不等号和数值。

## 可执行的小规模评测建议（尚未运行）

选约 20 篇真实中英文化学 PDF，冻结文件 SHA-256；先人工标注约 30 题及标准证据，再运行系统。覆盖正文事实、表格数值及实验条件、公式参数、化学术语、标准号、跨文献比较；至少 6 题用中文问英文文献，可与上述类型重叠；另含约 3 题语料明确无答案。这个规模用于发现问题、逐题比较，不用于宣称统计性胜出。

| 实验 | 固定项 | 变化项 | 可归因范围 |
| --- | --- | --- | --- |
| 本库检索消融 | MinerU 产物、chunks、Embedding/Reranker 型号、过滤条件、候选预算 | dense / BM25 / hybrid，各自测 rerank 开关 | 混合召回与重排贡献 |
| 切块消融 | MinerU 产物、检索配置与模型 | 现有切块 / 保护表格公式边界的切块 | chunk 边界贡献 |
| 可选 RAGFlow 后端比较 | 同一 MinerU 文本与题集、模型及预算 | 本库 / 固定版本 RAGFlow | 若 RAGFlow 重新切块，只能归因后端组合；只有能导入完全相同 chunks 才能隔离检索贡献 |
| 可选原生 PDF 比较 | 同一 PDF、题集与可比模型配置 | 现有 MinerU 链路 / RAGFlow 原生链路 | 整体效果，包括解析、切块及检索，不能单独证明 DeepDoc 更好 |

主阶段**只评检索，不调用生成 LLM 或 LLM 裁判**。标准证据单位为原 PDF 句段，或表格单元连同行列标题、单位、脚注，或公式连同变量定义；可用人工核验的规范化文本处理 OCR/格式差异，不能只看标准答案字符串是否出现。一个系统 chunk 可覆盖多个标准证据，多个重叠 chunks 命中同一证据只计一次。

报告 evidence Recall@5/10、MRR@10，以及统一上下文 token 预算下的 Recall；跨文献/合取题另报全部必需证据覆盖率。无答案题不参与有答案 Recall 分母，另检查是否错误把返回片段标作可支持答案；检索始终返回 top-k 本身不等于系统误答。逐题保存原文证据、返回片段和输赢原因；不把图片 caption 命中当成图中数值识别成功。

记录解析/索引耗时、失败率、API 调用及 token、整套容器空闲/峰值内存、磁盘占用、查询冷/热 p50/p95。只有明确的质量收益或原文核验时间收益能抵偿维护成本时，才扩大 RAGFlow 试验。若主要收益来自 BM25 或结构切块，优先补入现有系统。

## 当前两个优化方向

### 已实现：RAGFlow 启发的结构切块试验

固定核查 RAGFlow v0.27.2 (`a024bea0cd93f39e6652a42bf84dd20c55bc560b`)，仅借鉴设计原则，未复制上游代码，也不引入其服务/依赖：

| 源码中实际存在的做法 | 本项目实现 |
| --- | --- |
| MinerU 路径将表格与正文分流，表格带 `table_body + table_caption + table_footnote` | 表格独立成块，绑定紧邻且可识别的 caption/脚注；缺少 caption 时不编造。上下文长度受限。 |
| 表格字符串保留 HTML 结构，媒体块可按配置附加上下文 | 小表保留，大表按闭合 rowspan 的完整行组切；每组复用表头/表题/单位/脚注。表头推断状态与行数可核查。 |
| Paper 按标题层级组织 sections；DOCX 路径给表格加 Heading 位置上下文 | 从 Markdown 维护章节路径，补局部含义；没有 PDF 坐标时只记录清洗后文本偏移。 |
| 结构组块受正文 token 合并预算控制，但不保证拆开超大单元 | 本项目额外检查总字符预算（含重复上下文），上限 5000；表格预览软目标默认 2500，正文默认 1000。这不是上游 token 预算算法的复刻。 |

大表按行、rowspan 保护、公式原子保护和数值忠实性均属于本项目增强。公式不必单独成块：内部不拆，然后与前后正文按预算合并，避免独立 LaTeX 碎片失去语境。核心片段按原有空白或中文句末边界切片，不使用按英文句点拆分后插入换行的旧方法；后者可能把 `3.52` 变为 `3.\n52`。重叠上下文仍可能用换行连接多个原文 spans，所以不宣称输出逐字节保持全部空白；可靠对位应使用 spans。旧默认行为保留，该缺陷不会因新增试验选项而自动修复已有索引。

入口：

```bash
python -m litreview.chunk_preview /path/to/mineru/full.md
python -m litreview.chunk_preview /path/to/mineru/full.md --table-chunk-size 2500 --output /tmp/chunks-preview.json
```

API 为 `split_markdown(..., strategy="structured", table_chunk_size=2500)`。生产入口没有自动启用开关，因此不会因本次代码变更触发旧库重建。预览 JSON 带原文本哈希、算法版本/参数指纹、统计和来源文本 spans；这是试验指纹，尚未接入 `review_index_meta`。对接生产前仍需明确迁移文献、处理 chunk_id/证据缓存失效并版本化实际索引。

不安全单元的处理：不规则表格能在硬预算内完整保留时原样输出并标 warning；不配平公式、无法安全拆分且超预算的表格/公式等以 `StructureChunkError` 明确阻止该文献预览成功，CLI 返回 `needs_source_review` / 退出码 2。不会通过跳过单元而把不完整文献标为成功。表头识别仍为确定性启发式；未知结构不能据此宣称恢复了源表格语义。文本 spans 使用 Python 字符索引，相对于 `clean_markdown(input)`，不是 PDF 页码/边界框。

来源补充：

- [MinerU 表体、表题和脚注绑定](https://github.com/infiniflow/ragflow/blob/a024bea0cd93f39e6652a42bf84dd20c55bc560b/deepdoc/parser/mineru_parser.py#L918-L943)。
- [表格与正文分流](https://github.com/infiniflow/ragflow/blob/a024bea0cd93f39e6652a42bf84dd20c55bc560b/deepdoc/parser/mineru_parser.py#L850-L857)。
- [整表字符串 tokenize_table](https://github.com/infiniflow/ragflow/blob/a024bea0cd93f39e6652a42bf84dd20c55bc560b/rag/nlp/__init__.py#L552-L582)：`batch_size=10` 不是 PDF 大表按 10 行分组。
- [媒体邻近正文上下文](https://github.com/infiniflow/ragflow/blob/a024bea0cd93f39e6652a42bf84dd20c55bc560b/rag/nlp/__init__.py#L799-L879)。
- [Paper 标题结构合并预算](https://github.com/infiniflow/ragflow/blob/a024bea0cd93f39e6652a42bf84dd20c55bc560b/rag/app/paper.py#L225-L256)。
- [DOCX 标题路径上下文](https://github.com/infiniflow/ragflow/blob/a024bea0cd93f39e6652a42bf84dd20c55bc560b/rag/app/naive.py#L572-L673)：不能把 DOCX 专用行为宣传成 PDF Paper 全部已有。

验证：结构相关 35 项测试通过（含原 chunker 8 项回归），隔离全量测试 524 passed / 8 deselected；最终表格未知内容保护调整后，结构模式 27 项再通过。仅测试与只读文本预览，未做重嵌入或检索质量 A/B；最终真实语料审查结果另记，不将结构指标当成 Recall 提升。

Claude 最终复核：2026-10-12 约 10:56，`structured_chunker.py` SHA-256 为 `112964fe3f1f55bd4984633f41f69431f6e04ea02bddb45dfb01ab4d85211c0c`，工作区未提交。只读扫描 `papers.mineru_md` 共 513 篇，以正文 1000/重叠 150、表格目标 2500 运行，503/513 成功，10 篇显式失败、0 崩溃。失败为公式/整表/不可分行组超预算，源结构不配平，或网格异常且无法完整保留在预算内。成功样本比较中旧版 49,751 块、新版 51,557 块，增幅约 3.6%；最大块 4885 字符。未重嵌入，所以这些数字不表示检索质量已提升。

按去空白的文本比较和逐 source_span 检查，忠实性不一致为 0；生成表格段无 table 定界不配平或 rowspan 越段。有一张未拆分的源表本身网格不规则，整表保留路径未新增 warning，这属于已知边界，不应宣传所有输入表格均已校验。另有 187 个不足 30 字的短块被保留，带编号的 References 标题未被旧清洗规则识别，算法升级需手动提高 `STRATEGY_VERSION`；均未作为本轮试验阻塞项。复核结果来自 Claude 的 `/tmp/literature-structchunk-codereview-2.txt`，脚本位于其 scratchpad，尚未作为独立基准工具入库。

### 1. 证据页码溯源：后续候选，不重算向量

问题：检索结果有文献和小节，但正文 chunk 未携带 PDF 位置。`enrich.py:88` 仅为图表资产保存 `page_idx`；正文位置信息没有进入 `Chunk` / `Evidence`。

最小范围：利用已有 MinerU `content_list.json` 缓存，对约 10 篇文献建立 chunk 到 source block / 物理页码的旁路映射。记录文件 SHA-256、chunk 内容指纹、解析来源和匹配状态；一个 chunk 跨页可关联多个 source blocks。重复段落或缺少定位依据时返回候选页或 unknown，不猜页码；索引重建后旧 chunk_id 的映射须失效，不能仅靠整数 ID 关联。首次不承诺 bbox 框选。

展示接点：MCP 检索片段（`mcp_server.py:560`）、Web 搜索结果（`web_server.py:730`）、综述 manifest 的章节证据树（`litreview/manifest.py:189`）。首期综述附“章节 → 证据片段 → PDF 物理页码”的核验表；这不是每句话的准确引文页码，也不应改写参考文献的期刊页码。`litreview/locator.py:61` 虽生成 PDF URL，但当前 Web 路由没有该对应端点；PDF 点击访问须补完整并验证后再宣称可用，对外使用资源描述符，不暴露宿主机绝对路径。

验收：人工核对已定位页码的准确率、可定位覆盖率和 unknown/歧义率，按文献分别报告；验证跨页、重复段落、缺缓存与重建后旧映射失效。向量和 chunk 内容不变，无 Embedding API 调用。覆盖率目标等小样本实测后制定，不提前编造命中率。

Claude 的只读原型（来源 `/tmp/literature-ragflow-audit.txt`）：8 篇共 439 个 chunk，438 个至少找到一个文本窗口对应页（99.8%），91 个返回跨页范围。**这是候选映射覆盖，尚未人工核验页码精度**，也未覆盖纯表格/公式、重复文本、教材分卷、专利/标准。归一化后按窗口匹配并取 min/max 页的做法可能形成过宽范围，不能直接充当生产“精确定位”。缓存扫描在 510 篇中经现有目录命名找到 461 篇，剩余 49 篇包括 48 本书和 1 专利；未找到不等于资产不存在，教材分卷目录和页偏移须单独处理。

试点提案：10 篇中含至少一本书及一篇标准/专利；分层人工抽查 60 个块（单页、跨页、表格、公式各 15）。标为 exact 的定位不得有错误，range 验证实际页集合和范围长度，candidates 与 unknown 独立报告；优先保证精度，可靠 exact/range 覆盖 95% 可作为待验证目标，不能把歧义候选计入可靠覆盖。原型 99.8% 不能视为已达到该目标。

### 2. 表格/公式结构保护切块：仅针对受影响文献试验

纯函数构造验证已有具体故障：默认 1000/150 参数下，70 行 Markdown 表被切成两块，末块保留数据却失去表头；多行 aligned 公式的两个 `$$` 以及 begin/end 分落不同块；单行长 HTML 表的 table 起止标签也会分离。验证未读写生产库、未调用网络，不代表这些故障在真实语料的发生比例。

最小范围：识别 Markdown/HTML 表格与显示公式，保护公式边界与相邻变量解释；大表按行组切，每组保留标题、表头、单位及相关脚注。不能简单把任意大表完整塞进一个超大 chunk：在线 Embedder 在 7000 字符截断（`litreview/embedder.py`），Reranker 在 5000 字符截断（`litreview/reranker.py`），因此还需核对各生产后端的输入预算。

只在试验副本重切确实被结构边界破坏的文献，比较同一模型下的表格/公式完整性和人工标准证据覆盖。内容改变的 chunks 需要重新嵌入；不自动全库重建。当前增量判断主要比较 Markdown 哈希（`litreview/store.py:175`），须将切块算法/配置版本纳入索引指纹，避免算法升级但索引仍旧。正文普通段落保持既有行为，首期不承诺恢复 MinerU 已识别错误的公式或跨页表格。

验收：表格末行与表头/单位同现、公式定界符与 begin/end 成对、必要变量定义随公式可取、无静默超预算截断、原文数值和符号不被改写；记录受影响文献数、重嵌入量及成本。真实库的受损比例由 Claude 的只读审计补充，未实测前不声称普遍受损。

Claude 真实库只读审计补充：46,744 个 chunks / 510 篇中，2,053 个块包含 `<td>/<tr` 却没有 `<table`，涉及 296 篇（Claude 已复核一致）；400 个块有开表但无闭表，涉及 168 篇（复核口径，另一个统计得到 451/170，差异待定）。这些是结构碎片风险指标，不等于每块均无法回答问题。396 个奇数 `$$` 块、345/510 篇至少一处表/公式受损等数字尚未复核，不作为确认结论。

源 Markdown 表格长度统计：p50 839 字符、p90 3777、最大 77,383；43% 超过 1000 字符。因此整表不拆不能作为普遍策略。验收应限定“原输入合法的结构不被切坏”；原文已损坏、超长不可分单行/公式单独报告，不能靠丢内容或强行硬切满足预算。模型预算包含重复表题与表头。`replace_doc_chunks` 按文献删后重插，目前没有逐块向量内容哈希复用；故受影响文献重新切块可能需整篇重嵌入，并需处理旧 `review_evidence_cache` 引用的 chunk_id 失效（审计时缓存 279 行）。试点只在副本处理这些连带变更，不触碰生产索引。

## 一手资料链接

- [S1 最新 release API](https://api.github.com/repos/infiniflow/ragflow/releases/latest)；[rc1 commit](https://github.com/infiniflow/ragflow/commit/14bb02ee4584c1ab18f0c722c48beecd6bbf9860)。
- [S2 v0.27.2 release](https://github.com/infiniflow/ragflow/releases/tag/v0.27.2)；[commit](https://github.com/infiniflow/ragflow/commit/a024bea0cd93f39e6652a42bf84dd20c55bc560b)。
- [S3 核查的 main commit](https://github.com/infiniflow/ragflow/commit/2d34209f5d81a0d57bfa00b139e925979f095fed)。
- [S4 0.27.2 Paper 源码](https://github.com/infiniflow/ragflow/blob/a024bea0cd93f39e6652a42bf84dd20c55bc560b/rag/app/paper.py)。
- [S5 main Dataset configuration](https://github.com/infiniflow/ragflow/blob/2d34209f5d81a0d57bfa00b139e925979f095fed/docs/guides/dataset/configuration.md)。
- [S6 0.27.2 DeepDoc 官方说明](https://github.com/infiniflow/ragflow/blob/a024bea0cd93f39e6652a42bf84dd20c55bc560b/deepdoc/README.md)。
- [S7 0.27.2 HTTP Benchmark](https://github.com/infiniflow/ragflow/blob/a024bea0cd93f39e6652a42bf84dd20c55bc560b/test/benchmark/README.md)。
- [S8 main HTTP API reference](https://github.com/infiniflow/ragflow/blob/2d34209f5d81a0d57bfa00b139e925979f095fed/docs/references/http_api_reference.md#retrieve-chunks)。
- [S9 0.27.2 MinerU parser](https://github.com/infiniflow/ragflow/blob/a024bea0cd93f39e6652a42bf84dd20c55bc560b/deepdoc/parser/mineru_parser.py)。
- [S10 main Go MinerU PDF parser](https://github.com/infiniflow/ragflow/blob/2d34209f5d81a0d57bfa00b139e925979f095fed/internal/parser/parser/pdf_parser_mineru.go)。
- [S11 main MinerU.net provider](https://github.com/infiniflow/ragflow/blob/2d34209f5d81a0d57bfa00b139e925979f095fed/internal/entity/models/mineru.go)。
- [S12 main README](https://github.com/infiniflow/ragflow/blob/2d34209f5d81a0d57bfa00b139e925979f095fed/README.md)；[Quickstart](https://github.com/infiniflow/ragflow/blob/2d34209f5d81a0d57bfa00b139e925979f095fed/docs/quickstart.mdx)。
- [S13 main Compose base](https://github.com/infiniflow/ragflow/blob/2d34209f5d81a0d57bfa00b139e925979f095fed/docker/docker-compose-base.yml)；[Compose](https://github.com/infiniflow/ragflow/blob/2d34209f5d81a0d57bfa00b139e925979f095fed/docker/docker-compose.yml)。
- [S14 main Python SDK reference](https://github.com/infiniflow/ragflow/blob/2d34209f5d81a0d57bfa00b139e925979f095fed/docs/references/python_api_reference.md#retrieve-chunks)。
- [S15 main retrieval service](https://github.com/infiniflow/ragflow/blob/2d34209f5d81a0d57bfa00b139e925979f095fed/internal/service/nlp/retrieval.go)。
- [S16 main LICENSE](https://github.com/infiniflow/ragflow/blob/2d34209f5d81a0d57bfa00b139e925979f095fed/LICENSE)。
- [S17 main SILICONFLOW provider](https://github.com/infiniflow/ragflow/blob/2d34209f5d81a0d57bfa00b139e925979f095fed/conf/models/siliconflow.json)。
