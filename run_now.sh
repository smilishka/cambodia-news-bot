#!/usr/bin/env bash
# Ручной запуск бота с MacBook.
#
#   ./run_now.sh               — опубликовать сейчас (запуск в облаке GitHub, 3 новости + закреп)
#   ./run_now.sh 2             — то же, но 2 новости
#   ./run_now.sh nopin         — опубликовать без обновления закрепа в чате
#   ./run_now.sh preview       — предпросмотр постов в терминале, ничего не отправляется (нужен .env)
#   ./run_now.sh local         — опубликовать прямо с MacBook, без GitHub (нужен .env)
#   ./run_now.sh check         — проверить источники, Gemini и права бота (нужен .env)
#   ./run_now.sh description   — записать описание (био) канала (нужен .env)
set -eo pipefail
cd "$(dirname "$0")"

MODE="${1:-cloud}"
shift || true

setup_venv() {
  if [ ! -d .venv ]; then
    echo "Первый запуск: создаю окружение Python…"
    python3 -m venv .venv
  fi
  .venv/bin/pip install -q -r requirements.txt
  if [ ! -f .env ]; then
    echo "❌ Нет файла .env. Скопируйте .env.example в .env и вставьте токен бота и ключ Gemini."
    exit 1
  fi
}

cloud_run() {
  local count="$1" pin="$2"
  if ! command -v gh >/dev/null; then
    echo "❌ Нужен GitHub CLI: brew install gh && gh auth login"
    exit 1
  fi
  gh workflow run news.yml -f count="$count" -f pin="$pin" -f dry_run=false
  echo "🚀 Запуск отправлен в GitHub, жду результат…"
  sleep 8
  local run_id
  run_id=$(gh run list --workflow=news.yml --limit 1 --json databaseId -q '.[0].databaseId')
  gh run watch "$run_id" --interval 10 >/dev/null || true
  gh run view "$run_id" --log 2>/dev/null | grep -E "✅|❌|⚠️|📌|Новых|Нечего|Gemini" | sed -E 's/^.*\t//' || true
  git pull -q --rebase 2>/dev/null || true
}

case "$MODE" in
  cloud)        cloud_run 3 true ;;
  [0-9]*)       cloud_run "$MODE" true ;;
  nopin)        cloud_run "${1:-3}" false ;;
  preview)      setup_venv; .venv/bin/python bot.py --dry-run ${@+"$@"} ;;
  local)
    setup_venv
    git pull -q --rebase 2>/dev/null || true
    .venv/bin/python bot.py ${@+"$@"}
    git add state.json && git commit -qm "state: local run" && git push -q || true
    ;;
  check)        setup_venv; .venv/bin/python bot.py --check ;;
  description)  setup_venv; .venv/bin/python bot.py --set-description ;;
  *)            sed -n '2,11p' "$0"; exit 1 ;;
esac
