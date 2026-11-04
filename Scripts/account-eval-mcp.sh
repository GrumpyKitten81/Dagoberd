#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
LAUNCHER="$ROOT/.account-eval/mcp"

if [ -x "$LAUNCHER" ]; then
  exec "$LAUNCHER" "$@"
fi

cd "$ROOT"
exec poetry run python -m account_eval.mcp_server "$@"
