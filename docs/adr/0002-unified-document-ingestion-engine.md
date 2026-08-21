# 0002. 统一文献入库调度引擎与策略模式 (DocumentIngestionEngine)

## 状态
已接受 (Accepted) — 2026-08-19

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
