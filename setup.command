#!/bin/bash
# Установка новостного бота @CambodlaNews: GitHub-репозиторий, ключи, проверка.
# Запуск: двойной щелчок по этому файлу в Finder.
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.homebrew/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
REPO=cambodia-news-bot
fail() { echo; echo "❌ $1"; echo "Окно можно закрыть. Пришлите скриншот в чат Claude."; read -r; exit 1; }

echo "=== 1/6 Ключи из прежнего бота → .env"
if [ ! -f .env ]; then
  grep -E '^(BOT_TOKEN|GEMINI_API_KEY)=' ../.env > .env || fail "не нашёл ключи в ../.env"
  cat >> .env <<'EOF'
CHANNEL=@CambodlaNews
CHAT=@Cambodia_Russ
NEWS_PER_RUN=3
EOF
fi
echo "ok"

echo "=== 2/6 Файл расписания"
mkdir -p .github/workflows
[ -f news.yml ] && mv news.yml .github/workflows/news.yml
[ -f .github/workflows/news.yml ] || fail "нет файла расписания news.yml"
chmod +x run_now.sh
echo "ok"

echo "=== 3/6 GitHub CLI и вход в GitHub"
command -v gh >/dev/null || brew install gh || fail "не удалось установить gh (brew install gh)"
if ! gh auth status >/dev/null 2>&1; then
  echo "Сейчас откроется браузер: войдите в GitHub и введите код, который покажется ниже."
  gh auth login --hostname github.com --git-protocol https --web || fail "вход в GitHub не выполнен"
fi
gh auth setup-git
LOGIN=$(gh api user -q .login) || fail "не удалось узнать ваш логин GitHub"
echo "Вы вошли как: $LOGIN"

echo "=== 4/6 Проверка файлов перед загрузкой"
[ -d .git ] || git init -q -b main
git config user.name "$LOGIN"
git config user.email "$(gh api user -q .id)+$LOGIN@users.noreply.github.com"
git add -A
if git diff --cached --name-only | grep -E '(^|/)\.env$|\.session|session\.session|posted\.json'; then
  fail "в загрузку попал секретный файл — остановился"
fi
git diff --cached --name-only
git diff --cached --quiet || git commit -qm "Cambodia news bot"
echo "ok: секретных файлов нет"

echo "=== 5/6 Репозиторий на GitHub"
if ! git remote get-url origin >/dev/null 2>&1; then
  if gh repo view "$LOGIN/$REPO" >/dev/null 2>&1; then
    git remote add origin "https://github.com/$LOGIN/$REPO.git"
  else
    gh repo create "$REPO" --public --source=. --remote=origin || fail "не удалось создать репозиторий"
  fi
fi
git push -u origin main || fail "не удалось загрузить код"
echo "https://github.com/$LOGIN/$REPO"

echo "=== 6/6 Ключи в GitHub Secrets"
set -a; source .env; set +a
gh secret set BOT_TOKEN --repo "$LOGIN/$REPO" --body "$BOT_TOKEN" || fail "BOT_TOKEN не сохранён"
gh secret set GEMINI_API_KEY --repo "$LOGIN/$REPO" --body "$GEMINI_API_KEY" || fail "GEMINI_API_KEY не сохранён"

echo
echo "=== Проверка источников, Gemini и прав бота"
./run_now.sh check
echo
echo "✅ Установка завершена. Расписание включено: 8:00, 14:00, 20:00."
echo "Вернитесь в чат Claude. Окно можно закрыть."
read -r
