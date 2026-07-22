#!/bin/sh
# literature-review MCP server 启动脚本（stdio）
# 供 Claude Code / Claude Desktop / Cherry Studio 等 MCP 客户端调用。
# 环境约定（本机 dudu-sv）：
#   1. 密钥从 /opt/docker_shared/api_keys.env 读取；
#   2. 该文件里的代理变量指向已失效的 172.17.0.1:2017x，且 ALL_PROXY 是 socks5
#      （httpx 未装 socks 支持会崩），SiliconFlow 为国内直连——必须全部 unset；
#   3. 生产数据库在 /mnt/ripe/literature_analyzer_data/。

set -a
. /opt/docker_shared/api_keys.env
set +a
unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy

export DB_PATH=/mnt/ripe/literature_analyzer_data/batch_tracking.db
export REVIEW_OUTPUT_DIR="/mnt/ripe/literature_analyzer_data/obsidian_磷化工文献/reviews"

cd /home/dudu/GoogleDrive/Antigravity/literature_analyzer || exit 1
exec /home/dudu/.venv_lit/bin/python mcp_server.py
