#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --frozen
.venv/bin/python scripts/bootstrap.py
docker compose up -d --wait db
.venv/bin/python -m commerce.seed
.venv/bin/python scripts/install_node.py
export PATH="$PWD/.runtime/node/bin:$PATH"
npm --prefix frontend ci --no-fund --no-audit
npm --prefix frontend run build
.venv/bin/python scripts/start.py --restart
