#!/usr/bin/env bash
# Скрипт ограды: серверная часть выкладки (docs/STRATEGY_PRESETS_PLAN.md §4.6 G4).
#
# Запускается ТОЛЬКО из scripts/deploy.sh, на сервере, в юните
#   systemd-run --user --collect --unit=mentis-deploy \
#     --setenv=DEPLOY_ATTEMPT=<ATTEMPT> --setenv=DEPLOY_REVISION=<REVISION> ...
# из копии releases/deploy-fence-<ATTEMPT>.sh, которая никогда не перезаписывается.
# Имя юнита — мьютекс стека: всё, что меняет общее состояние (APP_DIR, образы,
# флаг, releases/current|history|pinned, дампы), делается только здесь.
#
# Аргументы: <revision> <attempt>. Параметры (окружение юнита):
#   DEPLOY_NO_BUILD=1      не собирать: образы :<revision> должны уже быть
#   DEPLOY_NO_BACKUP=1     без снапшота БД
#   DEPLOY_T0_REVISION     ревизия, работающая сейчас (первый запуск, T0)
#   DEPLOY_T0_CONFIRM=1    решение человека снять отказ сверки T0
#   DEPLOY_COMPOSE         команда compose (по умолчанию "docker compose")
#
# Порядок: .current → [T0: проверка и закрепление] → снапшот и его ротация →
# установка stage в APP_DIR → проверки контракта цели → сборка :<revision>
# (всё до флага: сбой здесь не оставляет ни флага, ни остановленного агента) →
# G4: (1) флаг, (2) stop agent, (3) DEL каталога, (4) retag :latest и up,
# (5) ожидание каталога до 2 мин, (6) снятие флага → healthcheck backend →
# releases/current, history, ротация образов. Код выхода — в .exit попытки.

set -Eeuo pipefail

REVISION="${1:?revision}"
ATTEMPT="${2:?attempt}"
[[ "$REVISION" =~ ^[0-9a-f]{7,40}$ ]] || { echo "bad revision: $REVISION" >&2; exit 2; }
[[ "$ATTEMPT" =~ ^[0-9]+-[0-9]+-[0-9a-f]{8}$ ]] || { echo "bad attempt: $ATTEMPT" >&2; exit 2; }

APPS="$HOME/apps"
APP_DIR="$APPS/mentis-trading-agent"
RELEASES="$APPS/releases"
BACKUP_DIR="$HOME/backups/mentis-trading-agent"
STAGE="$RELEASES/stage-$ATTEMPT"
LOG="$RELEASES/deploy-$REVISION-$ATTEMPT.log"
EXIT_FILE="$RELEASES/deploy-$REVISION-$ATTEMPT.exit"
DB_CONTAINER="${DEPLOY_DB_CONTAINER:-mentis-db-1}"
# shellcheck disable=SC2206 # команда compose разбивается на слова намеренно
COMPOSE=(${DEPLOY_COMPOSE:-docker compose} --env-file .env.prod -f compose.yml -f compose.prod.yml)
FENCE_KEY="trading:start_fence"
CATALOG_KEY="agent:strategies"
CATALOG_WAIT_S="${DEPLOY_CATALOG_WAIT_S:-120}"
HEALTH_WAIT_S="${DEPLOY_HEALTH_WAIT_S:-120}"
PAUSE_S="${DEPLOY_PAUSE_S:-2}"
KEEP_REVISIONS=10
SERVICES=(backend agent)

exec >>"$LOG" 2>&1
printf '%s %s\n' "$REVISION" "$ATTEMPT" >"$RELEASES/deploy.current"
echo "==> $(date -Is) попытка $ATTEMPT, ревизия $REVISION, скрипт ограды $(sha256sum "$0" | cut -c1-12)"

# PHASE говорит trap, что восстанавливать: before (ничего не тронуто), fenced
# (флаг стоит, агент работает), stopped (агент остановлен, up не начат), up
# (внутри up), waiting (агент цели запущен), lifted (флаг снят).
PHASE=before

compose() { (cd "$APP_DIR" && "${COMPOSE[@]}" "$@"); }
redis_cli() { compose exec -T redis redis-cli "$@"; }
image_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null || true; }
running_images() {
  local svc
  for svc in "${SERVICES[@]}"; do
    echo "    $svc: $(compose ps -q "$svc" | xargs -r docker inspect -f '{{.Config.Image}} {{.Image}}' 2>/dev/null || echo 'не запущен')"
  done
}

how_to_lift() {
  echo "    Образы сейчас:"
  running_images
  echo "    Флаг $FENCE_KEY остаётся (Start закрыт). Снять — только после проверки, по runbook"
  echo "    (docs/DEPLOY.md §6): повторный deploy.sh или redis-cli DEL $FENCE_KEY."
}

on_exit() {
  local code=$?
  set +e
  # set -e may fire inside a call redirected to /dev/null; the trap inherits
  # that redirection, so point the output back at the log.
  exec >>"$LOG" 2>&1
  if [[ $code -ne 0 ]]; then
    echo "!!! $(date -Is) сбой в фазе $PHASE (код $code)"
    case "$PHASE" in
      stopped)
        echo "==> trap: поднимаю прежнего агента (контейнер и образ не тронуты)"
        compose start agent
        how_to_lift ;;
      up)
        local previous
        previous="$(cat "$RELEASES/current" 2>/dev/null)"
        if [[ -n "$previous" && -n "$(image_id "mentis-agent:$previous")" ]]; then
          echo "==> trap: агент из releases/current ($previous) вместо частично поднятого"
          docker tag "mentis-agent:$previous" mentis-agent:latest
          compose up -d agent
        else
          echo "!!! trap: нет образа mentis-agent:$previous — агента не трогаю"
        fi
        echo "    Backend мог быть пересоздан и применить миграции: при «новый backend +"
        echo "    прежний агент» флаг снимается только повторным deploy.sh или решением человека."
        how_to_lift ;;
      fenced|waiting)
        how_to_lift ;;
    esac
  fi
  rm -rf -- "$STAGE"
  echo "$code" >"$EXIT_FILE"
  echo "==> $(date -Is) код выхода $code"
}
trap on_exit EXIT

fail() { echo "ОШИБКА: $*"; exit 1; }

# Чужие stage не удаляются: по возрасту не отличить проигравшую попытку от
# подготовленной, но ещё не запущенной (R108). Только отчёт для ручной уборки.
while IFS= read -r stale; do
  echo "    (для ручной уборки) чужой stage старше суток: $stale"
done < <(find "$RELEASES" -maxdepth 1 -name 'stage-*' ! -name "stage-$ATTEMPT" -mtime +0 2>/dev/null)

[[ -d "$STAGE/tree" ]] || fail "нет артефакта попытки $STAGE/tree"
command -v rsync >/dev/null || fail "на сервере нет rsync (нужен для чистой установки APP_DIR)"

# --- T0: первый запуск под 09a (§4.6, runbook шаг 7) -------------------------
# До нового снапшота: иначе «последний дамп» был бы уже нашим.
if [[ ! -s "$RELEASES/current" ]]; then
  T0="${DEPLOY_T0_REVISION:-}"
  [[ -n "$T0" ]] || fail "первый запуск: нужен --t0-revision <ревизия, работающая сейчас>"
  [[ "$T0" =~ ^[0-9a-f]{7,40}$ ]] || fail "плохая --t0-revision: $T0"
  echo "==> T0: сверка $T0 с последним дампом и контейнерами"
  last_dump="$(ls -1t "$BACKUP_DIR"/pre-deploy-app-*.dump 2>/dev/null | head -n 1 || true)"
  t0_ok=1
  if [[ -z "$last_dump" || "$(basename "$last_dump")" != pre-deploy-app-"$T0"-* ]]; then
    echo "    последний дамп '${last_dump:-нет}' не от ревизии $T0"
    t0_ok=0
  fi
  created_after=0
  dump_epoch="$( [[ -n "$last_dump" ]] && stat -c %Y "$last_dump" || echo 0 )"
  for svc in "${SERVICES[@]}"; do
    cid="$(compose ps -q "$svc")"
    [[ -n "$cid" ]] || { echo "    $svc не запущен"; t0_ok=0; continue; }
    if [[ "$(docker inspect -f '{{.Image}}' "$cid")" != "$(image_id "mentis-$svc:latest")" ]]; then
      echo "    образ контейнера $svc не совпадает с mentis-$svc:latest"
      t0_ok=0
    fi
    created="$(date -d "$(docker inspect -f '{{.Created}}' "$cid")" +%s)"
    (( created > dump_epoch )) && created_after=1
  done
  if (( ! created_after )); then
    echo "    ни один контейнер не создан после дампа (выкладка упала на сборке или шла без сборки?)"
    t0_ok=0
  fi
  if (( ! t0_ok )); then
    [[ "${DEPLOY_T0_CONFIRM:-0}" == 1 ]] || fail "сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком"
    echo "    сверка не пройдена, но есть --t0-confirm (решение человека)"
  fi
  for svc in "${SERVICES[@]}"; do
    docker tag "$(docker inspect -f '{{.Image}}' "$(compose ps -q "$svc")")" "mentis-$svc:$T0"
  done
  [[ -f "$APP_DIR/uv.lock" ]] && cp -p "$APP_DIR/uv.lock" "$RELEASES/$T0.uv.lock"
  grep -qx "$T0" "$RELEASES/pinned" 2>/dev/null || echo "$T0" >>"$RELEASES/pinned"
  echo "$T0" >"$RELEASES/current"
  echo "$(date -Is) $T0 t0" >>"$RELEASES/history"
  echo "    T0 = $T0: образы помечены, закреплён в releases/pinned"
fi

# --- Снапшот БД и его ротация ------------------------------------------------
if [[ "${DEPLOY_NO_BACKUP:-0}" != 1 ]]; then
  echo "==> Снапшот БД (app, agent)"
  mkdir -p "$BACKUP_DIR"
  stamp="$(date +%Y%m%d-%H%M%S)"
  for db in app agent; do
    docker exec "$DB_CONTAINER" pg_dump -Fc -U postgres "$db" \
      >"$BACKUP_DIR/pre-deploy-$db-$REVISION-$stamp.dump" \
      || echo "    (пропущено: $db ещё не существует)"
    ls -1t "$BACKUP_DIR"/pre-deploy-"$db"-*.dump 2>/dev/null | tail -n +11 | xargs -r rm --
  done
fi

# --- Установка артефакта: чистая, с удалением лишнего (.env.prod сохраняется) --
echo "==> Установка $REVISION в $APP_DIR"
mkdir -p "$APP_DIR"
if [[ ! -f "$STAGE/tree/uv.lock" && -f "$RELEASES/$REVISION.uv.lock" ]]; then
  cp -p "$RELEASES/$REVISION.uv.lock" "$STAGE/tree/uv.lock"
fi
rsync -a --delete --exclude=/.env.prod "$STAGE/tree/" "$APP_DIR/"

# --- Контракт с целью: до шага (1) --------------------------------------------
echo "==> Проверка контракта цели"
services="$(compose config --services)"
for svc in backend agent redis; do
  grep -qx "$svc" <<<"$services" || fail "в compose-файлах цели нет сервиса $svc"
done
images="$(cd "$APP_DIR" && env -u MENTIS_TAG "${COMPOSE[@]}" config --images)"
for svc in "${SERVICES[@]}"; do
  grep -qx "mentis-$svc:latest" <<<"$images" || fail "образ $svc цели без MENTIS_TAG — не mentis-$svc:latest"
done

# --- Сборка сразу как :<revision>; :latest не сдвигается ----------------------
have_images=1
for svc in "${SERVICES[@]}"; do
  [[ -n "$(image_id "mentis-$svc:$REVISION")" ]] || have_images=0
done
if (( have_images )); then
  echo "==> Образы :$REVISION уже есть — без сборки"
elif [[ "${DEPLOY_NO_BUILD:-0}" == 1 ]]; then
  fail "--no-build, а образов :$REVISION нет"
else
  [[ -f "$APP_DIR/uv.lock" ]] || fail "у ревизии $REVISION нет tracked uv.lock и нет releases/$REVISION.uv.lock — lock текущего дерева не подставляется"
  (cd "$APP_DIR" && MENTIS_TAG=probe "${COMPOSE[@]}" config --images) | grep -qx 'mentis-agent:probe' \
    || fail "compose.yml цели не знает MENTIS_TAG: сборка сдвинула бы :latest"
  echo "==> Сборка :$REVISION"
  (cd "$APP_DIR" && MENTIS_TAG="$REVISION" "${COMPOSE[@]}" build)
fi
cp -p "$APP_DIR/uv.lock" "$RELEASES/$REVISION.uv.lock" 2>/dev/null || true

# --- G4 --------------------------------------------------------------------------
echo "==> (1) флаг $FENCE_KEY"
redis_cli SET "$FENCE_KEY" "deploy $REVISION $ATTEMPT $(date -Is)" >/dev/null
PHASE=fenced
echo "==> (2) остановка агента"
PHASE=stopped
compose stop agent
echo "==> (3) удаление $CATALOG_KEY"
redis_cli DEL "$CATALOG_KEY" >/dev/null
echo "==> (4) :latest → :$REVISION и up"
PHASE=up
for svc in "${SERVICES[@]}"; do
  docker tag "mentis-$svc:$REVISION" "mentis-$svc:latest"
done
compose up -d --remove-orphans
PHASE=waiting
echo "==> (5) ожидание каталога агента цели (до ${CATALOG_WAIT_S} с)"
published=""
for ((i = 0; i < CATALOG_WAIT_S; i += 2)); do
  published="$(redis_cli GET "$CATALOG_KEY" 2>/dev/null || true)"
  [[ -n "$published" ]] && break
  sleep "$PAUSE_S"
done
[[ -n "$published" ]] || fail "агент цели не опубликовал $CATALOG_KEY за ${CATALOG_WAIT_S} с"
echo "    schema_hash: $(grep -o '"schema_hash": *"[^"]*"' <<<"$published" | head -n 1)"
echo "==> (6) снятие флага"
redis_cli DEL "$FENCE_KEY" >/dev/null
PHASE=lifted

echo "==> Healthcheck backend"
bind="$(grep -E '^WEB_BIND=' "$APP_DIR/.env.prod" 2>/dev/null | cut -d= -f2 || true)"
port="$(grep -E '^WEB_PORT=' "$APP_DIR/.env.prod" 2>/dev/null | cut -d= -f2 || true)"
url="http://${bind:-127.0.0.1}:${port:-8010}/api/v1/utils/health-check/"
healthy=0
for ((i = 0; i < HEALTH_WAIT_S; i += 3)); do
  curl -fsS "$url" >/dev/null 2>&1 && { healthy=1; break; }
  sleep "$PAUSE_S"
done
(( healthy )) || { compose logs --tail 50 backend || true; fail "backend не прошёл healthcheck $url"; }

echo "$REVISION" >"$RELEASES/current"
echo "$(date -Is) $REVISION" >>"$RELEASES/history"

# --- Ротация образов: последние $KEEP_REVISIONS по журналу, pinned и current —
keep="$( { tac "$RELEASES/history" | awk '{print $2}' | awk '!seen[$0]++' | head -n "$KEEP_REVISIONS"
           cat "$RELEASES/pinned" 2>/dev/null; cat "$RELEASES/current"; } | sort -u )"
while IFS= read -r old; do
  grep -qx "$old" <<<"$keep" && continue
  for svc in "${SERVICES[@]}"; do
    docker image rm "mentis-$svc:$old" >/dev/null 2>&1 && echo "    ротация: mentis-$svc:$old"
  done
done < <(awk '{print $2}' "$RELEASES/history" | sort -u)

echo "==> Готово: $REVISION"
