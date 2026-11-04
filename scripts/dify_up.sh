#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p vendor .runtime
if [ ! -d vendor/dify/.git ]; then
  git clone --depth 1 --branch 1.17.1 https://github.com/langgenius/dify.git vendor/dify
fi
.venv/bin/python scripts/prepare_dify.py
(
  cd vendor/dify/docker
  docker compose up -d
)
.venv/bin/python - <<'PY'
import time, httpx
for attempt in range(60):
    try:
        response=httpx.get('http://127.0.0.1:8088/console/api/setup', timeout=3)
        if response.status_code==200:
            print('Dify console ready at http://localhost:8088')
            break
    except httpx.HTTPError:
        pass
    time.sleep(2)
else:
    raise SystemExit('Dify API not ready. Check docker compose logs in vendor/dify/docker.')
PY
