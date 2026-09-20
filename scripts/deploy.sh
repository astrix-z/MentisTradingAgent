#!/usr/bin/env bash
# Деплой Mentis Trading Agent на выделенный хост 192.168.55.40 (docker).
#
# Использование (из корня репозитория):
#   ./scripts/deploy.sh                 # обычный деплой HEAD
#   ./scripts/deploy.sh --allow-dirty   # разрешить деплой при незакоммиченных изменениях
#   ./scripts/deploy.sh --no-build      # только пересоздать контейнеры из уже собранных образов
#   ./scripts/deploy.sh --no-backup     # пропустить снапшот БД перед деплоем (не рекомендуется)
#
# Переменные окружения (значения по умолчанию):
#   DEPLOY_SERVER=192.168.55.40   DEPLOY_USER=jb
#   DEPLOY_COMPOSE="docker compose"   # команда compose на сервере
#
# Что делает:
#   1. Проверяет, что деплоится закоммиченное состояние (git archive HEAD).
#   2. Снимает снапшот обеих боевых БД (app, agent) на сервере.
#   3. Копирует код HEAD на сервер в ~/apps/mentis-trading-agent (.env.prod не трогает).
#   4. Собирает образы и пересоздаёт контейнеры (миграции выполняются при старте).
#   5. Ждёт healthcheck backend и печатает итог.
#
# Наружу стек не публикуется: backend слушает только на ${WEB_BIND:-127.0.0.1}
# хоста (см. compose.prod.yml, docs/DEPLOY.md). Разработка ведётся здесь,
# на сервере крутится только длительный live/testnet-прогон.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SERVER="${DEPLOY_SERVER:-192.168.55.40}"
SERVER_USER="${DEPLOY_USER:-jb}"
SSH_TARGET="${SERVER_USER}@${SERVER}"
APP_DIR="\$HOME/apps/mentis-trading-agent"
BACKUP_DIR="\$HOME/backups/mentis-trading-agent"
DB_CONTAINER="mentis-db-1"

ALLOW_DIRTY=0
NO_BUILD=0
NO_BACKUP=0
for arg in "$@"; do
  case "$arg" in
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --no-build) NO_BUILD=1 ;;
    --no-backup) NO_BACKUP=1 ;;
    *) echo "Неизвестный аргумент: $arg"; exit 2 ;;
  esac
done

run_remote() { ssh -o BatchMode=yes "$SSH_TARGET" "$@"; }

if ! run_remote true 2>/dev/null; then
  echo "ОШИБКА: нет SSH-доступа к ${SSH_TARGET}. Проверьте ключи и сеть."
  exit 1
fi

COMPOSE_CMD="${DEPLOY_COMPOSE:-docker compose} --env-file .env.prod -f compose.yml -f compose.prod.yml"

# Деплоится ровно HEAD: незакоммиченные изменения на сервер не попадают.
REVISION="$(git rev-parse --short HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ -n "$(git status --porcelain)" && "$ALLOW_DIRTY" -ne 1 ]]; then
  echo "ОШИБКА: в рабочей копии есть незакоммиченные изменения."
  echo "Деплой выполняется из HEAD (${REVISION}, ${BRANCH}) — закоммитьте изменения"
  echo "или запустите с --allow-dirty, чтобы осознанно задеплоить HEAD без них."
  exit 1
fi

echo "==> Деплой ${REVISION} (${BRANCH}) на ${SSH_TARGET}"

if [[ "$NO_BACKUP" -ne 1 ]]; then
  echo "==> Снапшот БД (app, agent) перед деплоем"
  run_remote "mkdir -p ${BACKUP_DIR}"
  run_remote "docker exec ${DB_CONTAINER} pg_dump -Fc -U postgres app > ${BACKUP_DIR}/pre-deploy-app-${REVISION}-\$(date +%Y%m%d-%H%M%S).dump" || \
    echo "    (пропущено: app ещё не существует — это ожидаемо при первом деплое)"
  run_remote "docker exec ${DB_CONTAINER} pg_dump -Fc -U postgres agent > ${BACKUP_DIR}/pre-deploy-agent-${REVISION}-\$(date +%Y%m%d-%H%M%S).dump" || \
    echo "    (пропущено: agent ещё не существует — это ожидаемо при первом деплое)"
  # Храним последние 10 преддеплойных снапшотов на базу.
  run_remote "ls -1t ${BACKUP_DIR}/pre-deploy-app-*.dump 2>/dev/null | tail -n +11 | xargs -r rm --"
  run_remote "ls -1t ${BACKUP_DIR}/pre-deploy-agent-*.dump 2>/dev/null | tail -n +11 | xargs -r rm --"
fi

echo "==> Копирование кода (${REVISION}) в ${APP_DIR}"
run_remote "mkdir -p ${APP_DIR}"
git archive HEAD | run_remote "tar -x -C ${APP_DIR}"

if [[ "$NO_BUILD" -ne 1 ]]; then
  echo "==> Сборка образов (это может занять несколько минут)"
  run_remote "cd ${APP_DIR} && ${COMPOSE_CMD} build"
fi

echo "==> Пересоздание контейнеров"
run_remote "cd ${APP_DIR} && ${COMPOSE_CMD} up -d --remove-orphans"

WEB_BIND="$(run_remote "grep -E '^WEB_BIND=' ${APP_DIR}/.env.prod 2>/dev/null | cut -d= -f2 || true")"
WEB_PORT="$(run_remote "grep -E '^WEB_PORT=' ${APP_DIR}/.env.prod 2>/dev/null | cut -d= -f2 || true")"
WEB_BIND="${WEB_BIND:-127.0.0.1}"
WEB_PORT="${WEB_PORT:-8010}"

echo "==> Ожидание healthcheck (${WEB_BIND}:${WEB_PORT})"
HEALTH_OK=0
for _ in $(seq 1 40); do
  if run_remote "curl -fsS http://${WEB_BIND}:${WEB_PORT}/api/v1/utils/health-check/ >/dev/null 2>&1"; then
    HEALTH_OK=1
    break
  fi
  sleep 3
done

if [[ "$HEALTH_OK" -ne 1 ]]; then
  echo "ОШИБКА: backend не прошёл healthcheck за 2 минуты. Логи:"
  run_remote "cd ${APP_DIR} && ${COMPOSE_CMD} logs --tail 50 backend" || true
  exit 1
fi

echo
echo "==> Готово: ${REVISION} развёрнут (healthcheck ${WEB_BIND}:${WEB_PORT} на сервере)."
echo "    Доступ: ssh -L ${WEB_PORT}:localhost:${WEB_PORT} ${SSH_TARGET}  затем http://localhost:${WEB_PORT}"
echo "    Откат: задеплойте предыдущий коммит; при необратимых миграциях — восстановите"
echo "    БД из ${BACKUP_DIR}/pre-deploy-*.dump (docs/DEPLOY.md, раздел «Откат»)."
