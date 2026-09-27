# 0003. 统一领域外观 (LiteratureReviewFacade) 与异步任务注册中心 (JobRegistry)

## 状态
部分实施 (Partially implemented) — 2026-08-19 提出；2026-09-27 核对

## 上下文
系统提供多种外部接入途径（15 项 MCP 协议工具、Rich 命令行 CLI、Starlette REST Web API）。在调用底层文献检索、元数据查询、图谱分析与异步长耗时流水线（综述撰写、滚雪球拓扑扩展、候选入库）时，各接入端以往各自维护线程池、状态轮询与参数解析胶水代码，存在多端行为漂移与日志反馈不一致的隐患。

## 决策
1. **建立统一领域外观 `litreview.facade.LiteratureReviewFacade`**：提供一等公民 Python 领域函数，作为所有业务动作的唯一上层外观（Facade）；
2. **构建统一进程内任务注册中心 `JobRegistry`**：统一接管后台异步长耗时线程池、进度百分比、状态机跃迁与 Append-Only 运行日志；
3. **彻底解耦传输层**：MCP Server、CLI 与 Web Server 退化为纯协议转换器（Protocol Adapters），严禁绕过 Facade 直接跨层拼装底层内部模块。

## 后果与收益
* CLI、MCP 与 Web 端业务逻辑与进度反馈 100% 绝对一致；
* 全量单元测试与回归测试扩充至 201 项用例全绿通过；
* 新增协议接入（如 WebSocket 或 RPC）仅需对接 Facade 单一接口。

## 实施现状（2026-09-27 核对）
* `litreview.facade.LiteratureReviewFacade` 与进程内 `JobRegistry` 已存在并有单测（`tests/test_facade.py`），但 **MCP Server、CLI（`review.py`）与 Web Server 均未通过 Facade 调用**，各自直接组装底层模块。
* 实际的综述任务持久化是两套独立存储：
  * MCP：`DB_PATH` 所在库的 `review_jobs` 表（`mcp_server.py` 管理，支持 `paused_quota` 自动续跑与 `resume_review`）；
  * Web：`WEB_REVIEW_JOBS_DB_PATH`（默认 `web_review_jobs.db`，`litreview/review_jobs.py`）。
* 2026-09-27 起 Web 综述页会只读合并展示 MCP 任务（标 `[MCP]`），但 MCP 任务只能在 MCP 客户端用 `resume_review` 续跑，Web 的续跑接口对其返回 409——任务归属仍按创建端划分。
* 「三端行为 100% 一致」的收益尚未兑现；三端共享的是底层 `litreview.stages` / `review_orchestrator` / `quota`，而不是 Facade。
