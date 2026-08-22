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
  * 纯净薄 HTTP 适配器（Thin Adapter），仅负责 Starlette 路由分发、JSON 参数解析与错误码映射，禁止内联手写业务 SQL。
