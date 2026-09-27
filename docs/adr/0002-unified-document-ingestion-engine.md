# 0002. 统一文献入库调度引擎与策略模式 (DocumentIngestionEngine)

## 状态
部分实施 (Partially implemented) — 2026-08-19 提出；2026-09-27 核对

## 上下文
项目中存在多套平行演化的入库流程（论文批量分析、教材大文件拆分入库、外部 OpenAlex OA 候选入库）。各流水线在魔数校验（`%PDF-` / `PK`）、文件大小门限、SHA-256 哈希计算、切块与向量嵌入的异常处理上分散维护，缺乏统一的执行内核与标准生命周期接口。

## 决策
1. **建立统一入库调度内核 `litreview.ingestion_engine.DocumentIngestionEngine`**：固定执行标准 5 步通用生命周期：
   $$\text{Acquire (获取/下载)} \longrightarrow \text{Verify (魔数/哈希验真)} \longrightarrow \text{Parse (正文切块)} \longrightarrow \text{Vectorize (Qwen3密集向量)} \longrightarrow \text{Persist (多库落盘与图谱回填)}$$
2. **采用策略模式 (`IngestionStrategy`) 分离差异**：通过 `OpenAccessCandidateStrategy`、`PaperBatchStrategy` 等策略类封装不同文献来源的获取途径与持久化目标；
3. **收敛物理文件验真防线**：所有入库链路强制统一调用 `verify_document_file()`，提供唯一的死信截断与错误归因。

## 后果与收益
* 入库验真与去重逻辑收敛至单一真相源；
* 核心入库生命周期具备纯进程内独立单元测试（`tests/test_ingestion_engine.py` 4 项测试 0.26s 通过）；
* 全量测试套件扩充至 197 项用例 100% 通过。

## 实施现状（2026-09-27 核对）
* `litreview.ingestion_engine`（`DocumentIngestionEngine`、策略类、`verify_document_file()`）已存在并有单测，但**生产入库路径并未接入**：`pipeline.py`（实时）、`batch_pipeline.py`（Batch）、`book_pipeline.py`（教材）与 `litreview/pipeline.py`（OA 候选）仍各自实现获取/验真/落盘；引擎目前仅被 `litreview/__init__.py`、`facade.py` 与测试引用。
* 各入口之间的并发互斥改由 `litreview/intake_lock.py` 解决（`dirname(DB_PATH)/.intake.lock` 上的 flock，Web 上传与 daemon 的 realtime/batch 任务共用，锁忙时 daemon 本轮让路、不告警）。
* 候选全文获取不再有无头浏览器（Playwright）绕反爬分支：直链拿不到 PDF 即停在不可得，提示手动上传（2026-09-27 删除）。
* 是否把各流水线迁移到引擎上，需要单独评估；在迁移前，不要以本 ADR 作为「入库只有一条链路」的依据。
