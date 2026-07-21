# 磷化工文献解析器 (Literature Analyzer) - 项目交接文档

## 1. 项目概述

**Literature Analyzer** 是一个全自动的、基于大模型和多模态视觉模型的文献（PDF/CAJ）智能解析与结构化提取流水线。它旨在将大量专业文献（当前主要针对磷化工、肥料领域）自动提炼为结构化的 Excel 数据库和适合沉淀入 Obsidian 的 Markdown 知识库笔记。

### 核心能力
1.  **全自动多格式支持**：支持 PDF 和 CAJ 文件（底层通过 `caj2pdf` 和 `reportlab` 实现无损转码解析）。
2.  **多模态 VLM 解析**：集成 MinerU v4 API，强制使用 `vlm` 视觉大模型引擎，实现对双栏排版、复杂图表的高精度提取，且支持公式（LaTeX）与表格。
3.  **大模型 Batch 并发分析**：通过 DeepSeek V3（以及 Gemini 2.5 Pro）Batch API 实现极低成本、高并发的文献内容结构化提取（TLDR、摘要、结论等）。
4.  **多格式自动归档导出**：
    *   **Obsidian 知识库 (`obsidian_磷化工文献`)**：带有双向链接和图文混排的 Markdown 笔记。
    *   **Word 原文导出 (`word_exports`)**：直接拉取 MinerU 解析出的官方高保真 DOCX 格式。
    *   **Excel 结构化大表 (`knowledge_base.xlsx`)**：用于全量元数据和结论的宏观检索。
5.  **容错守护进程**：容器化运行，具备状态机持久化跟踪、失败自动重试、进度断点续传能力。

---

## 2. 系统架构与数据流向

### 目录结构与数据挂载
项目通过 Docker 容器化运行，容器内的 `/app/data` 目录整体挂载至宿主机的 `/mnt/ripe/literature_analyzer_data`。数据目录的结构和生命周期如下：

```text
/mnt/ripe/literature_analyzer_data/
├── input_pdfs/          # 📥 输入目录：用户上传待处理的 PDF/CAJ。处理完成后会被移走。
├── processed_pdfs/      # 📦 归档目录：已成功被 MinerU 提取并提交给大模型处理的原文件。
├── failed_pdfs/         # ⚠️ 失败目录：永久解析失败的死信文件。
├── mineru_output/       # 🗂️ 缓存目录：存放 MinerU 吐出的原始中间图文数据（.md, images）。永久保存不清理。
├── obsidian_磷化工文献/ # 📝 笔记输出：生成的 Obsidian 格式图文笔记。
├── word_exports/        # 📄 Word输出：MinerU 提取生成的 `.docx` 格式文件。
├── knowledge_base.xlsx  # 📊 表格输出：所有处理成功文献的宏观汇总总表。
├── batch_tracking.db    # 🗄️ 状态机 DB：追踪文献解析进展及大模型 Batch API 的 Job ID。
└── pipeline_history.jsonl
```

### 核心处理管道 (`batch_pipeline.py`)
1.  **扫描与预处理 (`prepare_batch`)**:
    *   扫描 `input_pdfs` 下的文献。CAJ 将被转换为 PDF。
    *   调用 `MinerUClient.process_pdf` 将文献上传至云端进行 VLM 多模态提取。
    *   提取完毕后返回 Markdown、切片图片及 `.docx` 源文件路径（并自动拷贝至 `word_exports`）。
    *   拼装截断后的核心文献内容，加入到大模型的 Batch 请求队列。
    *   提交 Batch 任务，将状态更新为 `BATCH_SUBMITTED`。
2.  **拉取与导出 (`fetch_batch`)**:
    *   轮询大模型平台的 Batch 任务状态。
    *   若任务完成，下载 JSONL 格式的结构化响应。
    *   按照 `obsidian_template.md` 渲染带图片的笔记至 `obsidian_磷化工文献`。
    *   将结果汇总追加至 `knowledge_base.xlsx`。
    *   将状态更新为 `EXPORTED`。

---

## 3. 核心模块说明

*   `batch_pipeline.py`: 主业务逻辑控制器，处理扫描、MinerU 分发、LLM Batch 提交、拉取与最终导出。
*   `daemon.py`: 守护进程调度器，负责定时触发 `batch_pipeline.py`。
*   `mineru_client.py`: MinerU V4 API 客户端。已配置轮询重试、自动 Key 轮换及 ZIP 附件解压提取（Markdown、Images、DOCX）。
*   `llm_router.py`: 大模型客户端封装。主要处理 DeepSeek（主力）与 Gemini 的 Batch 任务构建及系统提示词（Prompt）管理。
*   `caj_converter.py`: CAJ 到 PDF 的转码核心。采用了 `mutool` 进行初步检测，如果底层存在段错误崩溃，则通过 `caj2pdf text-extract` 提取文本后由 `reportlab` 重新印制规整的 PDF 以兜底。
*   `utils.py`: 工具类，包含数据库状态定义、Obsidian 渲染逻辑和 Excel 导出逻辑。

---

## 4. 近期重要改动及优化记录

为了解决特定长尾问题与用户需求，系统近期进行了以下重要改造：

1.  **修复 CAJ 文件底层崩溃 (-11 段错误)**：
    *   旧版 `caj2pdf` / `mutool` 在处理某些特定格式的 CAJ（HN/C8 压缩）时会导致段错误，容器崩溃。已改写 `caj_converter.py`，采用“直接抽取文本 + ReportLab 重新排版印制 PDF”的双轨降级方案，成功实现了 100% 的 CAJ 兼容。
2.  **MinerU 图文中间件永久保留**：
    *   关闭了曾经会在导出后清理 `mineru_output` 缓存的 `shutil.rmtree`，现在所有提取出来的公式切片、表格切片将永久安全地保存在本地硬盘中，防止数据丢失。
    *   配备了 `recover_mineru.py` 脚本，可一键根据哈希从 MinerU 重建历史丢失的缓存库。
3.  **自动导出 Word (DOCX) 功能**：
    *   强化了 `mineru_client.py` 的解压逻辑。系统现在会自动拦截并捕获 MinerU 原生生成的 `.docx` 源文件，并集中存储在 `word_exports` 目录，满足用户高可读性、传阅及打印的传统需求。
4.  **去重机制升级**：
    *   在向 DeepSeek Batch 提交任务前，加入了 `custom_id` 去重过滤器，从根本上防止了同一篇文献不同格式副本导致的大模型批处理拒绝服务错误。

---

## 5. 运维与部署指南

### 启动服务
项目完全容器化，依赖 Docker Compose。
```bash
cd /home/dudu/GoogleDrive/Antigravity/literature_analyzer
docker compose up -d --build
```

### 查看日志
可以通过以下命令追踪守护进程的实时运行状况：
```bash
docker compose logs -f literature-analyzer
```

### 手动干预指令
如果您不想等待守护进程的定时调度（例如刚扔进去一批 PDF 想马上跑），可以通过 `exec` 立即触发：
*   **立即触发解析并提交 Batch**:
    ```bash
    docker compose exec literature-analyzer python batch_pipeline.py
    ```
*   **立即检查 Batch 结果并导出笔记**:
    ```bash
    docker compose exec literature-analyzer python batch_pipeline.py check
    ```

### 环境配置
密钥和目录等配置存在于项目根目录的 `.env` 文件中。如果您需要增加 MinerU 的并发限额，可以在 `MINERU_API_KEY` 中填入多个 Key，用英文逗号 `,` 隔开，`mineru_client.py` 将自动实现 Key 轮换和超限兜底。
