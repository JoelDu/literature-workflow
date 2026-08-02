# 使用官方轻量级 Python 镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 设置时区为亚洲/上海，确保日志和 Excel 中的时间正确
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# apt 换国内镜像。默认 deb.debian.org 在国内实测只有 ~70KB/s，装 pandoc 这一层
# （49 个包 / 85MB）要跑 20 分钟；换清华源后半分钟。pip 早就用清华源了，这里只是补齐。
# 境外机器构建请传 --build-arg APT_MIRROR= （留空即跳过，走 Debian 官方源）。
ARG APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn
RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            [ -f "$f" ] && sed -i "s|http://deb.debian.org|$APT_MIRROR|g" "$f" || true; \
        done; \
    fi

# 优先拷贝 requirements，利用缓存层加速打包
COPY requirements.txt .
# pandoc：EPUB 入库（epub→markdown/docx）和 MinerU 原生 Word 合并失败时的兜底拼装都要它。
# 之前没装，这两条路径在容器里是直接坏的，只有宿主机手敲 CLI 才碰得到系统 pandoc。
RUN apt-get update && apt-get install -y --no-install-recommends \
        mupdf-tools git pandoc \
    && rm -rf /var/lib/apt/lists/*
# PyPDF2 已并入 requirements.txt（caj2pdf 的依赖），不再单独 pip install
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
RUN git clone --depth 1 https://github.com/caj2pdf/caj2pdf.git /app/caj2pdf-src
# 把 caj2pdf 装成可执行命令（wrapper 保证 sys.path 指向源码目录，模块导入正常）
RUN printf '#!/bin/sh\nexec python3 /app/caj2pdf-src/caj2pdf "$@"\n' > /usr/local/bin/caj2pdf \
    && chmod +x /usr/local/bin/caj2pdf

# 拷贝项目文件
COPY . .

# 默认命令
CMD ["python", "pipeline.py"]
