---
tags:
  - "#综述"
  - "#auto-generated"
topic: "{{ topic }}"
date: "{{ date }}"
model: "{{ model }}"
引用文献数: {{ doc_count }}
证据条目数: {{ evidence_count }}
---

# {{ title }}

## 1 引言

{{ intro }}

{% for section in sections %}
## {{ loop.index + 1 }} {{ section.heading }}

{{ section.markdown }}

{% endfor %}
## {{ sections|length + 2 }} 结论与展望

{{ conclusion }}

## 参考文献

{% for ref in references %}{{ ref }}

{% endfor %}

---
> 本综述由 litreview 基于本地文献库自动生成（{{ date }}），共引用 {{ doc_count }} 篇文献、{{ evidence_count }} 条证据。生成内容仅依据库内文献片段，请人工核查后使用。
