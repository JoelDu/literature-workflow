# 📖 引文标注与实体对齐真值规范指南 (v1.0)

本文档定义了 Literature Analyzer 项目中用于建立黄金标准真值集（Gold Standard Ground Truth）的引文边界识别、字段抽取与实体消歧标注规范。

---

## 一、 标注任务概览与原则

1. **零数据泄漏原则**：
   * 标注样本严格划分为 `DEV`（开发调优集）、`LOCKED_TEST`（锁定盲测集）与 `NEGATIVE_CONTROL`（负向控制集）；
   * `LOCKED_TEST` 的真值在规则开发阶段对开发者完全遮蔽，严禁作为规则调试与阈值调优的输入。
2. **证据优先原则**：
   * 标注必须以原文 PDF 物理排版与印刷文本为第一真值依据；
   * 严禁依据大模型的推测或外部网络搜索补全修改原始引用串。

---

## 二、 参考文献边界抽取规范 (Mention Boundary)

* **边界定义**：一条参考文献对应施引文献正文末尾物理引用的完整字符区间（Span）。
* **跨行处理**：原文由于栏宽换行导致的跨行文本必须完整拼接，不得人为截断为多条。
* **标号处理**：保留标号（如 `[1]`, `1.`, `(1)`）作为顺序索引 `ref_index` 的校验依据。
* **短字符串处理**：对于长度 $\le 10$ 字符的引用：
  * 如为标准号或缩写（如 `ISO 17556`, `Ibid.`），保留并标记为有效短引用；
  * 如为页码或排版杂音（如 `123.`, `Sci. 133.`），标记为 `OCR_CORRUPTED`。

---

## 三、 结构化字段抽取标准 (Field Extraction)

| 字段 | 抽取规则 | 样例/要求 |
| :--- | :--- | :--- |
| **`parsed_doi`** | 提取标准的 DOI 字符串，去除 URL 前缀与尾部句点，统一转为小写。 | `10.1002/aocs.12360` |
| **`parsed_title`** | 提取被引文献题名，去除首尾引号与末尾句点，保留正文大小写。 | `Properties of Coated Controlled Release...` |
| **`parsed_authors`** | 提取第一责任人或全部作者姓名，多作者以分号 `;` 分隔。 | `Zhang, W.; Li, F.; Wang, Q.` |
| **`parsed_year`** | 提取 4 位阿拉伯数字公历出版年份。 | `2020` |
| **`parsed_venue`** | 提取期刊名、会议名或出版社名称。 | `J. Am. Oil Chem. Soc.` |

---

## 四、 实体消歧与裁决分类 (Resolution Classification)

每条提取出的引用必须归入以下四类决策之一：

1. **`LINK`（库内命中）**：
   * 明确匹配到本地馆藏中的一个规范化实体 `selected_work_id`；
   * **必要条件**：DOI 精确一致，或规范化题名与第一作者+年份完全吻合。
2. **`NO_MATCH`（库外文献）**：
   * 确认被引文献属于合法学术作品，但目前未被本地物理馆藏收录；
   * **必要条件**：`selected_work_id IS NULL`。
3. **`DEFER`（存疑复核）**：
   * 题名存在模糊相似、缺乏作者或年份信息，无法断定是否为库内文献；
   * 必须进入人工复核审计队列。
4. **`REJECT`（残损废弃）**：
   * 物理 OCR 乱码、截断页码、非学术引用杂音。

---

## 五、 评测指标与计算口径定义

* **Mention-level Boundary**：基于字符 Span 区间与规范化文本计算 Precision, Recall, F1；
* **Field-level Metrics**：DOI, Title, Author, Year 分别独立计算 Precision, Recall, F1；
* **Disambiguation Metrics**：
  * 自动链接 Precision / Recall；
  * 错误合并数（False Merge / False Positive，容忍度为 0）；
  * 错误拆分数（False Split）；
  * `NO_MATCH` Precision, Recall, F1；
  * 逃逸率（Escape Rate）：歧义样本被误判为高置信自动放行的比例。
