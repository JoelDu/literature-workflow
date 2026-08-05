#!/bin/sh
# Portable stdio launcher for the literature-review MCP server.
# Defaults are relative to this repository; every machine-specific value can be
# overridden through the environment or the repository's .env file.

set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE=${MCP_ENV_FILE:-"$SCRIPT_DIR/.env"}

if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
elif [ -n "${MCP_ENV_FILE:-}" ]; then
    echo "MCP_ENV_FILE does not exist or is unreadable: $ENV_FILE" >&2
    exit 1
fi

# Some hosts export loopback/socks proxies that are invalid inside the MCP
# process. Opt in to clearing them instead of silently overriding user config.
if [ "${MCP_UNSET_PROXY:-false}" = "true" ]; then
    unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy
fi

: "${DB_PATH:=$SCRIPT_DIR/data/batch_tracking.db}"
: "${REVIEW_OUTPUT_DIR:=$SCRIPT_DIR/data/obsidian_vault/reviews}"
export DB_PATH REVIEW_OUTPUT_DIR

PYTHON_BIN=${MCP_PYTHON:-python3}
cd "$SCRIPT_DIR"
exec "$PYTHON_BIN" mcp_server.py
