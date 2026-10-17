#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [ ! -f .env ]; then
  cp .env.example .env
  echo 'Создан .env. Заполните BOT_TOKEN= токеном из @BotFather и запустите снова.'
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if [ ! -f .venv/dependencies-ready ]; then
  .venv/bin/python -m pip install -r requirements.txt
  touch .venv/dependencies-ready
fi
.venv/bin/python -m videobot --check
exec .venv/bin/python -m videobot
