#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
python3 -c "import fastapi, uvicorn, requests, dotenv, instaloader, telethon" 2>/dev/null || pip install -q -r requirements.txt
exec python3 -m uvicorn api.index:app --host 0.0.0.0 --port "${PORT:-3000}"
