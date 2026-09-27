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
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
# 代理只在构建命令作用域内使用，避免把可能带认证信息的 URI 写入最终镜像 ENV。
RUN HTTP_PROXY="$HTTP_PROXY" HTTPS_PROXY="$HTTPS_PROXY" NO_PROXY="$NO_PROXY" set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            [ -f "$f" ] && sed -i "s|http://deb.debian.org|$APT_MIRROR|g" "$f" || true; \
        done; \
    fi

# 优先拷贝 requirements，利用缓存层加速打包
COPY requirements.txt .
# pandoc：EPUB 入库（epub→markdown/docx）和 MinerU 原生 Word 合并失败时的兜底拼装都要它。
# 之前没装，这两条路径在容器里是直接坏的，只有宿主机手敲 CLI 才碰得到系统 pandoc。
RUN HTTP_PROXY="$HTTP_PROXY" HTTPS_PROXY="$HTTPS_PROXY" NO_PROXY="$NO_PROXY" apt-get update && apt-get install -y --no-install-recommends \
        mupdf-tools git pandoc \
    && rm -rf /var/lib/apt/lists/*
# PyPDF2 已并入 requirements.txt（caj2pdf 的依赖），不再单独 pip install
RUN HTTP_PROXY="$HTTP_PROXY" HTTPS_PROXY="$HTTPS_PROXY" NO_PROXY="$NO_PROXY" pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
# caj2pdf 钉死到固定提交：上游没有发版，`clone --depth 1` 拿到的是构建当天的 HEAD，
# 不可复现。这个 sha 就是当前生产镜像里实测跑通的那一版（2024-01-09）。
# 升级：换 sha → 重新构建 → 拿一个 CAJ 实测转换。.git 删掉，镜像里不需要仓库历史。
ARG CAJ2PDF_SHA=6c4bc32b15ce748d211f45d536f5d5511ef9f368
RUN set -eux; \
    export HTTP_PROXY="$HTTP_PROXY" HTTPS_PROXY="$HTTPS_PROXY" NO_PROXY="$NO_PROXY"; \
    git init -q /app/caj2pdf-src; \
    cd /app/caj2pdf-src; \
    git remote add origin https://github.com/caj2pdf/caj2pdf.git; \
    git fetch -q --depth 1 origin "$CAJ2PDF_SHA"; \
    git checkout -q --detach FETCH_HEAD; \
    test "$(git rev-parse HEAD)" = "$CAJ2PDF_SHA"; \
    rm -rf .git
# 把 caj2pdf 装成可执行命令（wrapper 保证 sys.path 指向源码目录，模块导入正常）
RUN printf '#!/bin/sh\nexec python3 /app/caj2pdf-src/caj2pdf "$@"\n' > /usr/local/bin/caj2pdf \
    && chmod +x /usr/local/bin/caj2pdf

# 以非 root 用户运行。默认 uid/gid 1000 = 作者宿主机上数据盘文件的属主，
# 这样容器写出的 DB / 日志 / 笔记在宿主机上仍归本人所有，不会冒出 root 文件。
# 数据目录属主不是 1000 的机器：构建时传 --build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g)；
# 需要保持旧的 root 行为时传 APP_UID=0 APP_GID=0。
# 注意：bind mount 的宿主目录如果不存在，Docker 会以 root 身份自动创建，非 root 进程写不进去——
# 全新部署前先 `mkdir -p` 好 DATA_ROOT 下的各子目录（或 chown 给该 uid）。
ARG APP_UID=1000
ARG APP_GID=1000
RUN set -eux; \
    if [ "$APP_UID" != "0" ]; then \
        getent group "$APP_GID" >/dev/null || groupadd -g "$APP_GID" app; \
        useradd -u "$APP_UID" -g "$APP_GID" -m -d /home/app -s /usr/sbin/nologin app; \
    fi; \
    chown "$APP_UID:$APP_GID" /app

# 拷贝项目文件（web_server 的「保存配置」会在工作目录写 .env，所以 /app 要归运行用户）
COPY --chown=${APP_UID}:${APP_GID} . .

USER ${APP_UID}:${APP_GID}

# 默认命令
CMD ["python", "pipeline.py"]
