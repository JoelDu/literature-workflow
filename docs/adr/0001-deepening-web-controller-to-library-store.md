# 0001. 下沉 Web 控制器内联 SQL，构建深模块 LibraryStore 与 CitationNetwork

## 状态
已接受 (Accepted) — 2026-08-19

## 上下文
`web_server.py` 曾增长至 2,260 行，包含了大量直接针对 `batch_tracking.db`、`test_citation_network.db` 和 `external_discovery_cache.db` 的内联 SQL 查询。5 级实体互斥分区计算（9,114 Works）与引文网络拓扑闭合过滤逻辑严重泄漏在 HTTP 路由函数中，导致无法脱离 HTTP 测试客户端进行快速、纯粹的单元测试，且图谱过滤条件频繁发生逻辑漂移。

## 决策
1. **构建深模块 `litreview.library_store` (`LibraryStore`)**：统一收拢仪表盘 5 级实体分区严格闭合统计、文献多维分页检索、文献详情抽屉聚合与发现候选池分页查询；
2. **增强深模块 `litreview.citation_network` (`CitationNetwork`)**：统一收拢 60 FPS 骨干网络（762 节点）提取、9,110 全量闭合图谱过滤与单文献局部子图聚焦；
3. **瘦身 `web_server.py` 为纯薄适配器 (Thin Adapter)**：仅负责 HTTP 请求分发、参数校验与 JSON 编解码，严禁内联编写领域 SQL。

## 后果与收益
* `web_server.py` 消除 20 余处内联 SQL 泄漏；
* 核心统计与拓扑算法具备 100% 独立的纯进程内单元测试（`tests/test_library_store.py` 仅需 0.19s）；
* 全量 190+ 个单元与回归测试 100% 持续通过。
