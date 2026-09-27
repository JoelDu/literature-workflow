# Literature Analyzer — 领域上下文与统一词汇表 (CONTEXT.md)

本文档定义当前项目的核心领域模型、实体分区与架构词汇规范。所有工程技能、Agent 与新增代码均须严格遵循此统一定义。

---

## 1. 核心实体模型 (Domain Entities)

* **Work (学术实体)**：
  * 引文拓扑网络中的一等公民节点；
  * 分为 `in_library = 1`（已在馆藏中）和 `in_library = 0`（外部学术占位节点）；
  * 唯一标识符为 `work_id`（馆藏文献为 SHA-256 字符串，OpenAlex 来源为 `work_oa_w<ID>`，候选为 `cand_<ID>`）。
* **Document Asset (文献资产)**：
  * 物理文件在存储盘上的映射记录（包含本地路径、文件大小、PDF 物理页数、魔数验真状态、SHA-256 哈希）。
* **Reference Mention (参考文献著录项)**：
  * 从文献正文引文列表中提取的原始参考文献记录，关联至对应的 `citing_work_id`。
* **Citation Edge (引文拓扑边)**：
  * 有向关系：`citing_work_id` ➔ `cited_work_id`；
  * 状态三态：`CONFIRMED`（双方均在馆藏收录且强匹配）、`CANDIDATE`（待复核候选引用）、`UNRESOLVED`（外部未解析占位边）。
* **Discovered Candidate (发现候选文献)**：
  * 滚雪球引擎自 OpenAlex 获取的高价值文献，具备严格生命周期状态机：
    $$	ext{DISCOVERED} \longrightarrow 	ext{APPROVED} \longrightarrow 	ext{ACQUIRED} \longrightarrow 	ext{PARSED} \longrightarrow 	ext{GRAPH\_INDEXED} \longrightarrow 	ext{VECTOR\_INDEXED}$$

---

## 2. 5 级互斥实体分区与封闭约束 (Stratified Partitions)

整个学术网络严格划分为 5 组互斥子集，代数求和严格恒等于 $9,114$：

1. **生产正常馆藏基线 (`production_baseline_normal`)**：$341$ 篇（`in_library=1, NORMAL`）；
2. **生产隔离脏数据 (`production_baseline_quarantined`)**：$3$ 篇（`in_library=1, QUARANTINED`）；
3. **测试已入库 OA 文献 (`verified_ingested_works`)**：$3$ 篇（`work_oa_*, in_library=1, NORMAL`）；
4. **测试孤儿隔离文献 (`test_quarantined_works`)**：$1$ 篇（`work_oa_w4391143884, QUARANTINED_INCOMPLETE`）；
5. **外部未解析占位节点 (`external_unresolved_placeholders`)**：$8,766$ 篇（`in_library=0, UNRESOLVED`）。

（以下为 2026-08-19 快照数字，随馆藏增长而变化；以 `LibraryStore` 实时统计为准。）

$$\sum = 341 + 3 + 3 + 1 + 8,766 = 9,114 	ext{ Works (100\% 严格闭合)}$$

---

## 3. 核心架构接缝与深模块 (Architectural Seams & Deep Modules)

* **`litreview.analysis_record` (`PaperAnalysisRecord`)**：
  * 论文深度分析结构化领域实体与产物发布深模块，统一封装 LLM 响应解析、物理摘要双向去幻觉融合、Obsidian 双链笔记与附件转存、Excel 黄金列序列化与持久化载荷构建，彻底消除跨流水线的散装字典霰弹式修改。
* **`litreview.library_store` (`LibraryStore`)**：
  * 负责多数据库（生产只读库、拓扑库、候选审计库）的联合查询、5 级分区严格统计、文献目录分页搜索与详情抽屉聚合。
* **`litreview.citation_network` (`CitationNetwork`)**：
  * 负责力导向图谱构建、60 FPS 骨干网络（762 节点）提取、9,110 全量闭合图谱过滤与单文献局部子图聚焦。
* **`web_server` (`Web Server`)**：
  * 目标是纯净薄 HTTP 适配器（Thin Adapter），新增代码禁止内联手写业务 SQL；现存 6 处针对发现缓存/拓扑库的内联连接属待下沉的技术债（见 ADR-0001 实施现状）。
* **`litreview.intake_lock`**：
  * 入库互斥锁（`dirname(DB_PATH)/.intake.lock` 上的 flock），Web 上传与 daemon 实时/Batch 任务共用；锁忙时 daemon 跳过本轮。
* **`litreview.facade` / `litreview.ingestion_engine`**：
  * 已实现但未接入生产入口（见 ADR-0002/0003 实施现状），不要假定三端经由它们调用。

---

## 4. 综述任务状态 (Review Job States)

* `queued` → `running` → `completed`（Web 端显示为 `done`）/ `failed` / `interrupted`；
* **`paused_quota`**：GPT-5.4 日额度硬阀触发时暂停（大纲阶段或章节阶段均可），记录 `resume_after`（下一个 UTC 00:00）；`REVIEW_AUTO_RESUME_QUOTA=1`（默认）时 MCP 服务到点自动续跑，否则手动 `resume_review`。CLI 在同一情形以退出码 **75** 退出，可用 `--resume-from` 续跑；
* 任务归属按创建端划分：MCP 任务存 `DB_PATH` 库的 `review_jobs` 表，Web 任务存 `WEB_REVIEW_JOBS_DB_PATH`；Web 页只读展示 MCP 任务但不能续跑它们。

## 5. 论文分析状态补充

* **`ANALYSIS_FAILED`**：同一篇论文送进 LLM Batch 达 `BATCH_MAX_ANALYSIS_ATTEMPTS`（默认 3）次仍拿不到可用结果的终态，不再自动重提交；
* **`RESULT_PARSE_FAILED`**：结果 JSON 解析失败，下一轮 `prepare_batch` 重新提交（计入尝试次数）；导出阶段的环境类异常（磁盘、Excel 锁）保持 `COMPLETED` 待重试，不重花额度。
