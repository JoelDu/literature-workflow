# 项目目标与功能核对：第二轮修复记录

日期：2026-09-27。核对角度是「README / PROJECT_OVERVIEW / handoff 声称的功能是否真的端到端可用」，而不是代码风格。上一轮见 [2026-09-26 修复记录](project-purpose-fixes-2026-09-26.md)。

四条修复线分别在独立 worktree 完成（infra / sec / batch / review），合并到 `integ` 后再做收尾联调。

## 一、Batch 论文分析（目标：攒批省钱、失败可控）

| 问题 | 修复 |
| --- | --- |
| 解析失败的论文会被无限次重新提交，反复烧 mini 额度 | `papers.analysis_attempts` 计数；达 `BATCH_MAX_ANALYSIS_ATTEMPTS`（默认 3）标终态 `ANALYSIS_FAILED`，不再自动重提交 |
| Batch 过期时整批丢弃，已完成的结果也白算 | 过期批次保留已完成输出，只有未完成条目标 `BATCH_FAILED` |
| 创建 Batch 网络超时后重试会重复创建（重复计费） | 上传与创建分离重试；创建前按 `batches.list` 去重；找不到归属的批次记 `batch_orphaned` 事件 |
| 额度账本在网络 I/O 期间持有写锁 | 预留/释放改为短 `BEGIN IMMEDIATE` 事务（`_reserve_batch_quota` / `_release_batch_quota`） |
| Web「拉取 Batch」按钮调用了不存在的函数 | `/api/pipeline/run` 的 `batch_fetch` 改调 `batch_pipeline.fetch_batch` |
| 实时模式不支持 CAJ、不拷贝 MinerU Word、`database is locked` 被当永久错误 | 均已修复；锁冲突按临时错误重试 |
| 死代码 | 移除 Gemini Batch 通道（历史 gemini 行记日志跳过）；`GEMINI_API_KEY` 仅保留配置位 |

## 二、综述生成（目标：只依据证据写、额度内可完成）

- **额度暂停而非失败**：GPT-5.4 日硬阀触发时任务进入 `paused_quota`，记录 `resume_after`（下一个 UTC 00:00）。
  - MCP：默认到点自动续跑（`REVIEW_AUTO_RESUME_QUOTA=1`），也可 `resume_review`。**大纲阶段就耗尽额度的任务此前永远无法续跑**（续跑接口要求已有大纲），已修复为续跑时重新生成大纲。
  - CLI：写检查点并以退出码 **75** 退出，次日 `generate ... --resume-from <检查点>`；`outline` 命令与 `generate` 的大纲阶段同样以 75 退出，不再抛栈。
  - Web：状态显示「额度暂停」并注明恢复时间，可直接恢复。
- **生成前额度预估**：按章节数估算 token，与当日剩余额度对比后提示。
- **溯源闸门（零 token）**：单章无出处量化句占比 > `REVIEW_NUMERIC_UNSUPPORTED_MAX`（0.2）、因幻觉引用被剥离而失去来源的句子占比 > `REVIEW_UNSOURCED_CLAIM_MAX`（0.15）即阻塞并定向重写。
- **Thesis 贯穿各章**：`Outline.apply_alignment` 把立论/缺口/主线写进大纲，逐章写作时注入。
- DOI 规范化与格式校验；图表按「图/表 章-序」编号并在正文加引用句。
- 每章 Track A 证据按边际价值裁剪到 `REVIEW_TRACK_A_MAX_DOCS`（默认 12）篇文献。**该变量此前未在 `Settings` 中声明，`.env` 设置不生效**，已补上并加测试。
- `quota` 新增 `release` / `remaining` / `next_utc_reset`，`QuotaExceededError` 携带重置时间。

## 三、Web / MCP 服务与入库（目标：局域网/Tailscale 可用且不被网页劫持）

- **CSRF**：带浏览器 Origin/Referer 的写请求必须同源或在 `WEB_CORS_ORIGINS` 内（`Origin: null` 拒绝）；curl/MCP 客户端不带 Origin 不受影响。
- **DNS rebinding**：Host 白名单，IP 字面量、localhost、本机主机名与网卡地址默认放行；域名访问须配置 `REMOTE_ALLOWED_HOSTS`，否则 421/403。MCP 同时启用 SDK 的 `TransportSecuritySettings`。
- **入库互斥**：`litreview/intake_lock.py`（`dirname(DB_PATH)/.intake.lock` 上的 flock）。Web 上传、daemon 的实时 / Batch 准备 / Batch 拉取任务共用；daemon 拿不到锁时本轮让路，不发告警。
- **Web 端点修复**：`/api/library/documents/{id}/enrich` 缺导入（NameError → 500）；审计事件查询引用不存在的列 `canonical_title`。均加了回归测试。
- **MCP 综述任务可在 Web 看到**：综述页只读合并 `review_jobs` 表中的 MCP 任务（标 `[MCP]`，产物文件名仅在位于 `REVIEW_OUTPUT_DIR` 内且存在时返回）；Web 续跑接口对 MCP 任务返回 409，任务归属仍按创建端划分。
- **全文获取不再绕反爬**：删除 Playwright 无头浏览器兜底；OA 直链以项目 User-Agent 下载，拿不到即提示手动上传。

## 四、测试、CI 与部署

- `tests/conftest.py` 强制隔离：路径全部指向会话临时目录、禁用 dotenv 与 stdout tee、剥离真实 API key；拦截对生产数据目录的 `sqlite3.connect`（`prod_readonly` 标记且 `LIT_ALLOW_PROD_READONLY=1` 时才允许 `mode=ro`）与所有非回环网络（`network` 标记除外）。两类标记默认不选。
- `tests/lit_seed.py` 构造已知内容的生产库/拓扑库/缓存库三件套，Web/LibraryStore/图谱/Facade 测试改为对种子精确断言，不再依赖生产 id。
- `tests/data/*.json`（引文评测金标/候选集）纳入版本库；xlsx 仍忽略。提交副本中一处生产绝对路径改为相对路径。
- 代码默认路径不再硬编码 `/mnt/ripe`，统一 `./data/...`；评测脚本按仓库根解析路径。
- CI：GitHub Actions，Python 3.10/3.11 × Ubuntu/Windows 跑测试；ruff 对 `RUFF_STRICT_PATHS` 严格检查（全仓仍有约 167 条存量告警，未强制）。
- Docker：容器以非 root（默认 uid/gid 1000，`APP_UID`/`APP_GID` 可覆盖）运行；caj2pdf 固定到具体 commit；新增 `.dockerignore`；compose healthcheck 读 daemon 心跳（`DAEMON_HEARTBEAT_PATH`，默认 `${DATA_ROOT}/heartbeat`，每 30 秒），3 小时未更新判 unhealthy。

## 验证

隔离运行全量测试：**497 passed, 8 deselected**（deselected 为 `network` / `prod_readonly` 用例）。约 6 个用例会尝试访问 `api.openai.com`，被隔离层拦截后仍通过，说明其断言不依赖真实网络。未发起任何收费调用，未写生产数据。

## 仍未解决 / 已知限制

- ADR-0002（统一入库引擎）与 ADR-0003（Facade + JobRegistry）只实现了模块本身，生产入口未接入，状态已改为「部分实施」，见各 ADR 的「实施现状」。
- `web_server.py` 仍有 6 处针对发现缓存/拓扑库的内联 SQL。
- Batch 使用自己的额度预留/释放函数，未复用 `quota.release`。
- 综述 manifest 不保存证据原文（隐私白名单有意拦截），因此事后无法仅凭 manifest 复核证据内容。
- 部署注意：容器改为 uid 1000 后，`DATA_ROOT` 下目录必须预先存在且可写；用域名（如 `*.ts.net`）访问 Web/MCP 须配置 `REMOTE_ALLOWED_HOSTS`；`WEB_CORS_ORIGINS` 同时是 CSRF 白名单。
