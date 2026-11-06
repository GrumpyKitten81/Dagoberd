#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
LAUNCHER="$ROOT/.dagoberd/mcp"

if [ -x "$LAUNCHER" ]; then
  exec "$LAUNCHER" "$@"
fi

cd "$ROOT"
exec poetry run python -m dagoberd.mcp_server "$@"
