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
# проверки контракта цели и сборка :<revision> из stage → установка stage в
# APP_DIR (всё до флага: сбой здесь не оставляет ни флага, ни остановленного
# агента, а до установки и APP_DIR работающей ревизии) →
# G4: (1) флаг, (2) stop agent, (3) DEL каталога, (4) retag :latest и up,
# (5) ожидание каталога до 2 мин, healthcheck backend, (6) снятие флага →
# releases/current, history, ротация образов и файлов попыток. Код выхода — в .exit попытки.

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
#: Предел одного вызова redis-cli и curl в ожиданиях (R129).
CALL_TIMEOUT_S="${DEPLOY_CALL_TIMEOUT_S:-10}"
KEEP_REVISIONS=10
#: Файлы (.log, .exit, копия скрипта ограды) стольких последних завершённых попыток.
KEEP_ATTEMPTS=20
SERVICES=(backend agent)

exec >>"$LOG" 2>&1
printf '%s %s\n' "$REVISION" "$ATTEMPT" >"$RELEASES/deploy.current"
echo "==> $(date -Is) попытка $ATTEMPT, ревизия $REVISION, скрипт ограды $(sha256sum "$0" | cut -c1-12)"

# PHASE говорит trap, что восстанавливать: before (ничего не тронуто), fenced
# (флаг стоит, агент работает), stopped (агент остановлен, up не начат), up
# (внутри up), waiting (агент цели запущен), lifted (флаг снят).
PHASE=before

compose() { (cd "$APP_DIR" && "${COMPOSE[@]}" "$@"); }
# Compose-файлы цели до установки в APP_DIR (проверки и сборка).
stage_compose() { (cd "$STAGE/tree" && "${COMPOSE[@]}" "$@"); }
# -e: an error reply (OOM, WRONGTYPE, …) is a non-zero exit; without it
# redis-cli prints the error and exits 0.
redis_cli() { (cd "$APP_DIR" && timeout "$CALL_TIMEOUT_S" "${COMPOSE[@]}" exec -T redis redis-cli -e "$@"); }
# A catalog the agent published: JSON carrying its schema_hash, not just text.
catalog_ok() { grep -Eq '^\{.*"schema_hash": *"[^"]+"' <<<"$1"; }
# Самый свежий непустой дамп базы $1 (пустой файл — не дамп, R123).
latest_dump() {
  find "$BACKUP_DIR" -maxdepth 1 -name "pre-deploy-$1-*.dump" -size +0c -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | head -n 1 | cut -d' ' -f2-
}
image_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null || true; }
running_images() {
  local svc cid
  for svc in "${SERVICES[@]}"; do
    # -a: остановленный контейнер тоже показывается, с его состоянием.
    cid="$(compose ps -aq "$svc" 2>/dev/null | head -n 1)"
    if [[ -n "$cid" ]]; then
      echo "    $svc: $(docker inspect -f '{{.Config.Image}} {{.Image}} {{.State.Status}}' "$cid" 2>/dev/null || echo '?')"
    else
      echo "    $svc: нет контейнера"
    fi
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
        # :latest backend — образ, который реально работает: up мог пересоздать
        # его, а мог не дойти до него. Иначе ручной `compose up` молча поднял бы
        # другой backend.
        local backend_image
        backend_image="$(compose ps -aq backend | head -n 1 | xargs -r docker inspect -f '{{.Image}}' 2>/dev/null)"
        if [[ -n "$backend_image" ]] && docker tag "$backend_image" mentis-backend:latest; then
          echo "==> trap: mentis-backend:latest = образ работающего backend"
        else
          echo "!!! trap: образ backend не определён — mentis-backend:latest может указывать на цель"
        fi
        echo "    Backend мог быть пересоздан и применить миграции: при «новый backend +"
        echo "    прежний агент» флаг снимается только повторным deploy.sh или решением человека."
        how_to_lift ;;
      fenced|waiting)
        how_to_lift ;;
      lifted)
        # Сбой после снятия флага (current/history, ротация): fail closed —
        # флаг ставится снова, агент цели работает (R130).
        echo "==> trap: флаг снят, но выкладка не завершена — ставлю флаг снова"
        redis_cli SET "$FENCE_KEY" "deploy $REVISION $ATTEMPT failed after lift $(date -Is)" >/dev/null \
          || echo "!!! trap: не удалось поставить флаг — Start ОТКРЫТ, поставьте его вручную"
        how_to_lift ;;
    esac
  fi
  rm -rf -- "$STAGE"
  echo "$code" >"$EXIT_FILE"
  echo "==> $(date -Is) код выхода $code"
}
trap on_exit EXIT
# Сигнал (systemctl --user stop/kill, перезагрузка) — не успех: без этого
# EXIT-trap записал бы в .exit код последней команды, например 0.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

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
  last_dump="$(latest_dump app)"
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
  # Сначала оба образа, потом теги. T0 закрепляется (pinned/current) только
  # после обоих тегов. Сбой второго тега оставляет первый: он указывает на
  # работающий образ, а снимать его опасно — образ без тега Docker (хранилище
  # containerd) удаляет даже под работающим контейнером. Повторный запуск
  # ставит оба тега заново.
  t0_images=()
  for svc in "${SERVICES[@]}"; do
    cid="$(compose ps -q "$svc")"
    [[ -n "$cid" ]] || fail "T0: $svc не запущен — закреплять нечего"
    image="$(docker inspect -f '{{.Image}}' "$cid")"
    [[ -n "$(image_id "$image")" ]] \
      || fail "T0: образа работающего $svc ($image) уже нет в хранилище Docker — закрепить T0 нечем"
    t0_images+=("$image")
  done
  for i in "${!SERVICES[@]}"; do
    docker tag "${t0_images[$i]}" "mentis-${SERVICES[$i]}:$T0"
  done
  # Только справка: происхождение lock в APP_DIR не доказано (его перезаписывал
  # любой прежний деплой), пересобрать T0 по нему нельзя — T0 живёт на
  # закреплённых образах (R124).
  [[ -f "$APP_DIR/uv.lock" ]] && cp -p "$APP_DIR/uv.lock" "$RELEASES/$T0.appdir.uv.lock"
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
    exists="$(docker exec "$DB_CONTAINER" psql -U postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname = '$db'")" \
      || fail "не удалось проверить базу $db — без снапшота не продолжаю"
    if [[ "$exists" != 1 ]]; then
      echo "    (пропущено: базы $db ещё нет)"
      continue
    fi
    # Во временный файл; имя дампа появляется только у целого дампа (R123).
    final="$BACKUP_DIR/pre-deploy-$db-$REVISION-$stamp.dump"
    docker exec "$DB_CONTAINER" pg_dump -Fc -U postgres "$db" >"$final.part" \
      || { rm -f -- "$final.part"; fail "pg_dump $db не удался — без снапшота не продолжаю"; }
    [[ -s "$final.part" ]] || { rm -f -- "$final.part"; fail "pg_dump $db дал пустой дамп"; }
    mv -- "$final.part" "$final"
    find "$BACKUP_DIR" -maxdepth 1 -name "pre-deploy-$db-*.dump" -printf '%T@ %p\n' \
      | sort -rn | tail -n +11 | cut -d' ' -f2- | xargs -r -d '\n' rm --
  done
fi

# --- Цель в stage: lock, контракт, сборка — до установки в APP_DIR -------------
# APP_DIR до шага установки остаётся деревом работающей ревизии: отказ здесь не
# оставляет в нём код цели. .env.prod — ссылкой (rsync его не переносит).
[[ -f "$APP_DIR/.env.prod" ]] || fail "нет $APP_DIR/.env.prod (docs/DEPLOY.md §3)"
ln -sfn "$APP_DIR/.env.prod" "$STAGE/tree/.env.prod"
# Доверенный lock — только tracked lock ревизии: из её дерева или его копия
# releases/<rev>.uv.lock, сохранённая при прежней выкладке этой ревизии (R124).
tracked_lock=0
if [[ -f "$STAGE/tree/uv.lock" ]]; then
  tracked_lock=1
elif [[ -f "$RELEASES/$REVISION.uv.lock" ]]; then
  cp -p "$RELEASES/$REVISION.uv.lock" "$STAGE/tree/uv.lock"
fi

echo "==> Проверка контракта цели"
services="$(stage_compose config --services)"
for svc in backend agent redis; do
  grep -qx "$svc" <<<"$services" || fail "в compose-файлах цели нет сервиса $svc"
done
images="$(cd "$STAGE/tree" && env -u MENTIS_TAG "${COMPOSE[@]}" config --images)"
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
  [[ -f "$STAGE/tree/uv.lock" ]] || fail "у ревизии $REVISION нет tracked uv.lock и нет releases/$REVISION.uv.lock — lock текущего дерева не подставляется"
  # Оба собираемых образа должны зависеть от MENTIS_TAG, иначе сборка сдвинет
  # :latest одного из них ещё до флага (R125).
  probe="$(cd "$STAGE/tree" && MENTIS_TAG=probe "${COMPOSE[@]}" config --images)"
  for svc in "${SERVICES[@]}"; do
    grep -qx "mentis-$svc:probe" <<<"$probe" \
      || fail "compose.yml цели не знает MENTIS_TAG для $svc: сборка сдвинула бы :latest"
  done
  echo "==> Сборка :$REVISION"
  (cd "$STAGE/tree" && MENTIS_TAG="$REVISION" "${COMPOSE[@]}" build)
fi
(( tracked_lock )) && cp -p "$STAGE/tree/uv.lock" "$RELEASES/$REVISION.uv.lock"

# --- Установка артефакта: чистая, с удалением лишнего (.env.prod сохраняется) --
echo "==> Установка $REVISION в $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete --exclude=/.env.prod "$STAGE/tree/" "$APP_DIR/"

# --- G4 --------------------------------------------------------------------------
echo "==> (1) флаг $FENCE_KEY"
reply="$(redis_cli SET "$FENCE_KEY" "deploy $REVISION $ATTEMPT $(date -Is)")" \
  || fail "флаг $FENCE_KEY не поставлен: ${reply:-нет ответа}"
[[ "$reply" == OK ]] || fail "флаг $FENCE_KEY не поставлен: $reply"
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
# Предел — время, а не число попыток: каждый вызов сам ограничен (R129).
published=""
deadline=$((SECONDS + CATALOG_WAIT_S))
while :; do
  # An error reply is not a catalog, nor is text without a schema_hash.
  published="$(redis_cli GET "$CATALOG_KEY" 2>/dev/null)" || published=""
  catalog_ok "$published" && break
  ((SECONDS < deadline)) || break
  sleep "$PAUSE_S"
done
catalog_ok "$published" || fail "агент цели не опубликовал $CATALOG_KEY за ${CATALOG_WAIT_S} с"
echo "    schema_hash: $(grep -o '"schema_hash": *"[^"]*"' <<<"$published" | head -n 1)"

# Healthcheck — до снятия флага: неработающий backend оставляет Start закрытым (R130).
echo "==> Healthcheck backend (до ${HEALTH_WAIT_S} с)"
bind="$(grep -E '^WEB_BIND=' "$APP_DIR/.env.prod" 2>/dev/null | cut -d= -f2 || true)"
port="$(grep -E '^WEB_PORT=' "$APP_DIR/.env.prod" 2>/dev/null | cut -d= -f2 || true)"
url="http://${bind:-127.0.0.1}:${port:-8010}/api/v1/utils/health-check/"
healthy=0
deadline=$((SECONDS + HEALTH_WAIT_S))
while :; do
  curl -fsS --max-time "$CALL_TIMEOUT_S" "$url" >/dev/null 2>&1 && { healthy=1; break; }
  ((SECONDS < deadline)) || break
  sleep "$PAUSE_S"
done
(( healthy )) || { compose logs --tail 50 backend || true; fail "backend не прошёл healthcheck $url"; }

echo "==> (6) снятие флага"
redis_cli DEL "$FENCE_KEY" >/dev/null
PHASE=lifted

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

# --- Ротация файлов попыток: последние $KEEP_ATTEMPTS завершённых по .exit ----
# Копия скрипта без .exit не трогается: попытка ещё не запущена (клиент между
# загрузкой и запуском, R108) или убита — это ручная уборка, как чужой stage.
while IFS= read -r done_exit; do
  name="$(basename "$done_exit" .exit)"   # deploy-<revision>-<attempt>
  old_attempt="${name#deploy-*-}"
  rm -f -- "$RELEASES/$name.log" "$done_exit" "$RELEASES/deploy-fence-$old_attempt.sh"
done < <(find "$RELEASES" -maxdepth 1 -name 'deploy-*.exit' -printf '%T@ %p\n' \
           | sort -rn | tail -n +$((KEEP_ATTEMPTS + 1)) | cut -d' ' -f2-)

echo "==> Готово: $REVISION"
