#!/bin/bash

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

if [ -f config.inc.sh ]; then
	. config.inc.sh
fi

if [ ! -d ".venv" ]; then
	echo "Creating virtual environment in .venv..."
	python3 -m venv .venv
fi

REQ_HASH_FILE=".venv/.requirements.hash"
CURRENT_HASH=$(sha256sum requirements.txt 2>/dev/null || true)
STORED_HASH=$(cat "$REQ_HASH_FILE" 2>/dev/null || true)

if [ "$CURRENT_HASH" != "$STORED_HASH" ]; then
	echo "Installing/updating dependencies in .venv..."
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt
	echo "$CURRENT_HASH" > "$REQ_HASH_FILE"
fi

BOT_TOKEN="$BOT_TOKEN" \
ALLOWED_USERIDS="$ALLOWED_USERIDS" \
ADMIN_USERIDS="$ADMIN_USERIDS" \
STATE_FILE="${STATE_FILE:-ytsub-state.json}" \
CHECK_INTERVAL_SEC="${CHECK_INTERVAL_SEC:-${CHECK_INTERVAL:-300}}" \
SUBSCRIPTION_SYNC_INTERVAL_SEC="${SUBSCRIPTION_SYNC_INTERVAL_SEC:-43200}" \
MAX_POSTS_PER_MIN="${MAX_POSTS_PER_MIN:-10}" \
GOOGLE_CLIENT_ID="$GOOGLE_CLIENT_ID" \
GOOGLE_CLIENT_SECRET="$GOOGLE_CLIENT_SECRET" \
GOOGLE_CLIENT_SECRET_FILE="$GOOGLE_CLIENT_SECRET_FILE" \
exec .venv/bin/python3 main.py "$@"
