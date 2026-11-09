#!/bin/bash
cd "$(dirname "$0")"

export PATH="/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/Current/bin:$PATH"

PORT=8785

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment (first run only)..."
  python3 -m venv .venv
fi

.venv/bin/pip install -q -r requirements.txt

if [ "${JIRA_TRELLO_SKIP_UPDATE:-}" != "1" ] && [ -f "update_from_github.py" ]; then
  echo "Checking GitHub for updates..."
  python3 update_from_github.py || true
fi

echo "Checking Playwright browser..."
if ! .venv/bin/python -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel='chrome')
    b.close()
" 2>/dev/null; then
  echo "Chrome not found via Playwright — downloading Chromium (one-time)..."
  .venv/bin/playwright install chromium
fi

if lsof -ti :"$PORT" >/dev/null 2>&1; then
  echo "Stopping previous server on port $PORT..."
  lsof -ti :"$PORT" | xargs kill 2>/dev/null || true
  sleep 1
fi

echo "Starting Jira → Trello Roadmap..."
open "http://127.0.0.1:$PORT"
.venv/bin/python app.py

read -p "Press Enter to close this window..."
