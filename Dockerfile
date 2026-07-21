# 使用官方轻量级 Python 镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 设置时区为亚洲/上海，确保日志和 Excel 中的时间正确
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 优先拷贝 requirements，利用缓存层加速打包
COPY requirements.txt .
RUN apt-get update && apt-get install -y mupdf-tools git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir PyPDF2
RUN git clone --depth 1 https://github.com/caj2pdf/caj2pdf.git /app/caj2pdf-src

# 拷贝项目文件
COPY . .

# 默认命令
CMD ["python", "pipeline.py"]
