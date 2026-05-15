#!/bin/bash
set -euo pipefail

git fetch origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [[ "$LOCAL" != "$REMOTE" ]]; then
  echo "ERROR: HEAD is not at origin/main. git pull first." >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "ERROR: working tree is dirty. commit or stash first." >&2
  exit 1
fi

cd "$(git rev-parse --show-toplevel)/infrastructure"
exec terraform apply "$@"
