# 📚 Literature Analyzer (文献分析器)

一个强大的学术文献自动化处理流水线（Pipeline）。能够自动将学术 PDF 论文转换为结构化的 Obsidian 笔记，并汇总到 Excel 知识库中。

## ✨ 核心特性

- **高质量 PDF 解析**：集成 [MinerU](https://mineru.net/)，完美提取 PDF 中的复杂排版、公式和表格，输出高质量 Markdown。
- **智能大模型分析**：集成 **硅基流动 (SiliconFlow) DeepSeek V3** 进行无损文献分析（当前已统一中英文路由至 DeepSeek V3 保证稳定性）。
- **双模运行机制**：
  - **实时模式** (`RUN_MODE=realtime`)：常驻守护进程，每隔固定时间（默认 10 分钟）自动扫描 `input_pdfs/` 目录并即刻处理，生成 Obsidian 笔记与 Excel 记录。
  - **批处理模式** (`RUN_MODE=batch`)：利用大模型的 Batch API，享受 **50% 的成本折扣**。常驻守护进程会在每天凌晨 `01:00` 自动提交任务，并在 `07:30` 和 `13:30` 自动拉取结果，同时支持容器启动瞬间的冷启动检测，确保完美错峰。
- **运行历史与状态追踪**：
  - **历史日志统计**：每次运行的解析、提交、拉取、导出状态和异常均会持久化记入 `data/pipeline_history.jsonl` 中。
  - **可视化看板**：提供 `python cli.py history`（查看每日处理成功/失败统计与失败详情）以及 `python cli.py status`（查看当前任务分布）命令行看板，方便进行日常运维与统计。
- **极致的生产级优化**：
  - **哈希去重**：自动计算 PDF 的 SHA-256 哈希值并记录，避免重复处理已解析文献，节省解析 Token 额度。
  - **自动垃圾清理**：在将笔记和图片导出至 Obsidian Vault 后，自动清理 MinerU 生成的本地庞大临时中间产物文件夹，节省服务器磁盘空间。
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
        ↓ LLMRouter (统一路由至 SiliconFlow DeepSeek V3)
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
- `DEEPSEEK_MODEL`: 调用的 DeepSeek 模型名称，默认在硅基流动上为 `deepseek-ai/DeepSeek-V3`，官方为 `deepseek-chat`。您可以通过修改该变量来更换模型（例如 `deepseek-ai/DeepSeek-R1`）。
- `BATCH_SIZE_LIMIT`: 每次运行处理/导出的最大论文数量，默认为 `30`。如果您想增加每次运行处理的论文篇数，可以在环境变量中调大此数值。

---

## 🐳 Docker 部署（推荐）

本项目内置了完整的 Docker + docker-compose 支持，并以常驻守护进程（Daemon）方式运行。

### 1. 部署准备
在服务器上创建外部共享 API 密钥文件：`/opt/docker_shared/api_keys.env`，内容格式如下：
```env
MINERU_API_KEY=key1,key2,key3       # 支持配置多个，以英文逗号分隔
SILICONFLOW_API_KEY=your_siliconflow_key
SILICONFLOW_API_BASE=https://api.siliconflow.cn/v1
DEEPSEEK_MODEL=deepseek-ai/DeepSeek-V3 # 可选，更换 AI 模型
BATCH_SIZE_LIMIT=50                 # 可选，增加单次处理上限数量（默认 30）
```

### 2. 配置 docker-compose.yml
你可以通过编辑 `docker-compose.yml` 中的环境变量选择运行模式：
```yaml
environment:
  - RUN_MODE=batch  # 可选 realtime (实时模式) 或 batch (批处理模式)
```

### 3. 启动服务
在项目根目录下，执行以下命令构建并启动容器：
```bash
# 由于部分环境网络限制，默认禁用 Buildkit 进行经典构建
DOCKER_BUILDKIT=0 docker compose build
docker compose up -d
```

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
- `llm_router.py`: 大模型调用路由，内置 SiliconFlow DeepSeek V3 接口及原始摘要无损提取逻辑。
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
python review.py outline "主题" -o outline.json   # 只生成大纲（可手工编辑后再传入 generate）
python review.py generate "主题" [--outline outline.json] [--dry-run]
```

- **检索链路**：Qwen3-Embedding-8B（4096 维，跨中英）向量召回 50 候选 → Qwen3-Reranker-8B 重排取 top 24 → DeepSeek-V4-Pro 逐条打分提炼证据（≥6 分保留）→ 只依据证据写作并强制引用标记 → 全局编号与 GB/T 7714 风格参考文献（含 DOI）。
- **结构化元数据**（`batch_tracking.db` 新表）：`paper_details`（中英标题/DOI/作者/期刊/年份/关键词）、`paper_assets`（图/表/图题/页码）、`paper_references`（每篇论文自己引用的文献逐条）。提取以免费的 content_list.json 本地解析为主，LLM 仅补缺。
- 配置见 `.env.example` 的 litreview 段；依赖 `SILICONFLOW_API_KEY` / `SILICONFLOW_API_BASE`。
