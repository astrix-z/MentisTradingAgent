#!/usr/bin/env bash
# Деплой Mentis Trading Agent на выделенный хост 192.168.55.40 (docker).
#
# Использование (из корня репозитория):
#   ./scripts/deploy.sh                          # выкладка HEAD
#   ./scripts/deploy.sh --revision <rev>         # выкладка или откат на ревизию (текущим скриптом)
#   ./scripts/deploy.sh --t0-revision <rev>      # первый запуск: какая ревизия работает сейчас (T0)
#   ./scripts/deploy.sh --t0-confirm             # снять отказ сверки T0 после проверки человеком
#   ./scripts/deploy.sh --allow-dirty            # HEAD при незакоммиченных изменениях
#   ./scripts/deploy.sh --no-build               # без сборки: образы :<rev> уже есть на сервере
#   ./scripts/deploy.sh --no-backup              # без снапшота БД (не рекомендуется)
#
# Переменные окружения (значения по умолчанию):
#   DEPLOY_SERVER=192.168.55.40   DEPLOY_USER=jb
#   DEPLOY_COMPOSE="docker compose"   # команда compose на сервере
#   DEPLOY_SSH                        # команда доступа к серверу вместо ssh (для тестов)
#
# Как устроено (docs/STRATEGY_PRESETS_PLAN.md §4.6 G4, docs/DEPLOY.md):
#   Здесь — только проверки без записи и файлы СВОЕЙ попытки: артефакт ревизии
#   в неизменяемый releases/stage-<ATTEMPT>/ и копия scripts/deploy-fence.sh
#   (из текущего дерева) в releases/deploy-fence-<ATTEMPT>.sh. Всё остальное —
#   снапшот БД, установка в APP_DIR, сборка, флаг trading:start_fence, замена
#   агента, ожидание его каталога, releases/current|history, ротация — делает
#   скрипт ограды на сервере в юните mentis-deploy (systemd --user, нужен
#   linger). Имя юнита — мьютекс стека: вторая выкладка той же ревизии
#   подключается к идущей, другой ревизии — отказывает, ничего не меняя.
#   Обрыв SSH юнит не прерывает; этот скрипт следит за логом и ждёт код выхода.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SERVER="${DEPLOY_SERVER:-192.168.55.40}"
SERVER_USER="${DEPLOY_USER:-jb}"
SSH_TARGET="${SERVER_USER}@${SERVER}"
# Раскрываются на сервере: команды уходят в run_remote строкой.
# shellcheck disable=SC2016
R_RELEASES='$HOME/apps/releases'
UNIT="mentis-deploy"
#: С этого коммита агент публикует agent:strategies (шаг (5) его ждёт).
CATALOG_SINCE="608cfa1"
WAIT_TIMEOUT_S="${DEPLOY_WAIT_TIMEOUT_S:-3600}"
READ_RETRY_S="${DEPLOY_READ_RETRY_S:-30}"
POLL_S="${DEPLOY_POLL_S:-3}"
READ_PAUSE_S="${DEPLOY_READ_PAUSE_S:-2}"

REV_ARG="HEAD"
ALLOW_DIRTY=0
NO_BUILD=0
NO_BACKUP=0
T0_REVISION=""
T0_CONFIRM=0
while (($#)); do
  case "$1" in
    --revision) REV_ARG="${2:?--revision <rev>}"; shift ;;
    --t0-revision) T0_REVISION="${2:?--t0-revision <rev>}"; shift ;;
    --t0-confirm) T0_CONFIRM=1 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --no-build) NO_BUILD=1 ;;
    --no-backup) NO_BACKUP=1 ;;
    *) echo "Неизвестный аргумент: $1"; exit 2 ;;
  esac
  shift
done

if [[ -n "${DEPLOY_SSH:-}" ]]; then
  # shellcheck disable=SC2206 # команда разбивается на слова намеренно
  SSH=(${DEPLOY_SSH})
else
  SSH=(ssh -o BatchMode=yes "$SSH_TARGET")
fi
# Одна строка — одна команда на сервере; $HOME в ней раскрывает сервер.
run_remote() { "${SSH[@]}" "$1"; }

die() { echo "ОШИБКА: $*"; exit 1; }

# --- Проверки без записи ---------------------------------------------------------

if ! run_remote true 2>/dev/null; then
  die "нет SSH-доступа к ${SSH_TARGET}. Проверьте ключи и сеть."
fi

REVISION="$(git rev-parse --short=7 "${REV_ARG}^{commit}" 2>/dev/null)" \
  || die "нет такой ревизии: $REV_ARG"
[[ "$REVISION" =~ ^[0-9a-f]{7,40}$ ]] || die "неожиданный формат ревизии: $REVISION"
if [[ "$REV_ARG" == HEAD && -n "$(git status --porcelain)" && "$ALLOW_DIRTY" -ne 1 ]]; then
  echo "ОШИБКА: в рабочей копии есть незакоммиченные изменения."
  echo "Деплой выполняется из HEAD (${REVISION}) — закоммитьте изменения"
  echo "или запустите с --allow-dirty, чтобы осознанно задеплоить HEAD без них."
  exit 1
fi
git merge-base --is-ancestor "$CATALOG_SINCE" "$REVISION" 2>/dev/null \
  || die "ревизия $REVISION старше $CATALOG_SINCE: её агент не публикует agent:strategies, G4 не пройдёт"
if [[ -n "$T0_REVISION" ]]; then
  T0_REVISION="$(git rev-parse --short=7 "${T0_REVISION}^{commit}" 2>/dev/null)" \
    || die "нет такой ревизии T0"
fi

# Юнит пользователя без linger умирает вместе с этой SSH-сессией.
# shellcheck disable=SC2016 # $USER — пользователь SSH на сервере
linger="$(run_remote 'loginctl show-user "$USER" -p Linger --value' 2>/dev/null || true)"
if [[ "$linger" != yes ]]; then
  echo "ОШИБКА: у пользователя SSH на сервере выключен linger — юнит выкладки не переживёт"
  echo "обрыв сессии. Один раз на сервере: sudo loginctl enable-linger <user> (docs/DEPLOY.md)."
  exit 1
fi

# --- Файлы своей попытки -------------------------------------------------------------

# MY_ATTEMPT — всё, что создаёт и может удалить этот запуск; WATCH_ATTEMPT —
# попытка, чей .exit мы ждём (своя или идущая той же ревизии). R108.
MY_ATTEMPT="$(date +%s)-$$-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
[[ "$MY_ATTEMPT" =~ ^[0-9]+-[0-9]+-[0-9a-f]{8}$ ]] || die "не удалось создать ATTEMPT"
MY_STAGE="$R_RELEASES/stage-$MY_ATTEMPT"
MY_SCRIPT="$R_RELEASES/deploy-fence-$MY_ATTEMPT.sh"

echo "==> Выкладка ${REVISION} на ${SSH_TARGET}, попытка ${MY_ATTEMPT}"
run_remote "mkdir -p $R_RELEASES && mkdir $MY_STAGE && mkdir $MY_STAGE/tree" \
  || die "не удалось создать $MY_STAGE"
remove_mine() { run_remote "rm -rf -- $MY_STAGE $MY_SCRIPT" || true; }
git archive "$REVISION" | run_remote "tar -x -C $MY_STAGE/tree" \
  || { remove_mine; die "не удалось загрузить артефакт"; }
# set -C: копия скрипта никогда не перезаписывается (bash читает скрипт по ходу).
run_remote "set -C; cat > $MY_SCRIPT" <scripts/deploy-fence.sh \
  || { remove_mine; die "не удалось загрузить скрипт ограды"; }

# --- Снимок юнита и запуск (R102, R106, R109) ----------------------------------------

S_LOAD="" S_ACTIVE="" S_ATTEMPT="" S_REVISION=""
# Один вызов show; ошибка чтения — повтор каждые 2 с до READ_RETRY_S, затем отказ.
# Новый запуск по ошибке чтения не делается никогда.
read_snapshot() {
  local out deadline=$((SECONDS + READ_RETRY_S))
  while :; do
    if out="$(run_remote "systemctl --user show -p LoadState -p ActiveState -p Environment $UNIT" 2>/dev/null)" \
      && grep -q '^LoadState=' <<<"$out"; then
      S_LOAD="$(sed -n 's/^LoadState=//p' <<<"$out")"
      S_ACTIVE="$(sed -n 's/^ActiveState=//p' <<<"$out")"
      local env
      env="$(sed -n 's/^Environment=//p' <<<"$out")"
      S_ATTEMPT="$(grep -o 'DEPLOY_ATTEMPT=[^ ]*' <<<"$env" | cut -d= -f2 || true)"
      S_REVISION="$(grep -o 'DEPLOY_REVISION=[^ ]*' <<<"$env" | cut -d= -f2 || true)"
      return 0
    fi
    ((SECONDS < deadline)) || return 1
    sleep "$READ_PAUSE_S"
  done
}

snapshot_or_die() {
  read_snapshot || {
    echo "ОШИБКА: состояние юнита $UNIT неизвестно (нет ответа менеджера за ${READ_RETRY_S} с)."
    echo "    Выкладка не запускалась повторно; проверьте на сервере: systemctl --user status $UNIT"
    exit 1
  }
}

# Загруженный юнит обязан нести корректные метаданные попытки (R109).
check_metadata() {
  [[ "$S_LOAD" == loaded ]] || return 0
  if [[ ! "$S_ATTEMPT" =~ ^[0-9]+-[0-9]+-[0-9a-f]{8}$ || ! "$S_REVISION" =~ ^[0-9a-f]{7,40}$ ]]; then
    remove_mine
    echo "ОШИБКА: метаданные попытки отсутствуют или некорректны (юнит $UNIT):"
    echo "    LoadState=$S_LOAD ActiveState=$S_ACTIVE DEPLOY_ATTEMPT='$S_ATTEMPT' DEPLOY_REVISION='$S_REVISION'"
    echo "    Не подключаюсь и не запускаю; разберитесь с юнитом на сервере."
    exit 1
  fi
}

alive() { [[ "$S_LOAD" == loaded && "$S_ACTIVE" != inactive && "$S_ACTIVE" != failed ]]; }

# Чужая попытка в снимке: та же ревизия — подключиться, другая — отказ без записей.
choose_by_snapshot() {
  remove_mine
  if [[ "$S_REVISION" == "$REVISION" ]]; then
    WATCH_ATTEMPT="$S_ATTEMPT"
    echo "==> Идёт выкладка той же ревизии (попытка $WATCH_ATTEMPT) — подключаюсь к ней"
  else
    die "идёт выкладка ревизии $S_REVISION (попытка $S_ATTEMPT); дождитесь её конца"
  fi
}

WATCH_ATTEMPT=""
snapshot_or_die
check_metadata
if alive; then
  choose_by_snapshot
else
  launch_err="$(run_remote "systemd-run --user --collect --unit=$UNIT \
--setenv=DEPLOY_ATTEMPT=$MY_ATTEMPT --setenv=DEPLOY_REVISION=$REVISION \
--setenv=DEPLOY_NO_BUILD=$NO_BUILD --setenv=DEPLOY_NO_BACKUP=$NO_BACKUP \
--setenv=DEPLOY_T0_REVISION=$T0_REVISION --setenv=DEPLOY_T0_CONFIRM=$T0_CONFIRM \
--setenv=DEPLOY_COMPOSE='${DEPLOY_COMPOSE:-docker compose}' \
bash \"$MY_SCRIPT\" $REVISION $MY_ATTEMPT" 2>&1 >/dev/null)" && launched=1 || launched=0
  if ((launched)); then
    WATCH_ATTEMPT="$MY_ATTEMPT"
  else
    # stderr — для диагностики; ветку выбирает только снимок.
    snapshot_or_die
    if [[ "$S_LOAD" == not-found ]]; then
      remove_mine
      echo "ОШИБКА: юнит $UNIT отсутствует: запуск не подтверждён либо попытка уже выгружена."
      echo "    systemd-run: ${launch_err:-<пусто>}"
      echo "    Непроверенная справка (releases/deploy.current): $(run_remote "cat $R_RELEASES/deploy.current" 2>/dev/null || echo '<нет>')"
      exit 1
    fi
    check_metadata
    if [[ "$S_ATTEMPT" == "$MY_ATTEMPT" ]]; then
      WATCH_ATTEMPT="$MY_ATTEMPT"
      echo "    (ответ systemd-run потерян, но юнит запущен с нашей попыткой)"
    else
      choose_by_snapshot
    fi
  fi
fi

# --- Ожидание кода выхода своей попытки ------------------------------------------------

LOG_FILE="$R_RELEASES/deploy-$REVISION-$WATCH_ATTEMPT.log"
EXIT_FILE="$R_RELEASES/deploy-$REVISION-$WATCH_ATTEMPT.exit"
offset=0

print_log() {
  local out size
  out="$(run_remote "f=$LOG_FILE; n=\$(stat -c %s \"\$f\" 2>/dev/null || echo 0); echo \$n; tail -c +$((offset + 1)) \"\$f\" 2>/dev/null | head -c \$((n - $offset))" 2>/dev/null)" || return 0
  size="$(head -n 1 <<<"$out")"
  [[ "$size" =~ ^[0-9]+$ ]] || return 0
  if ((size > offset)); then
    tail -n +2 <<<"$out"
    offset="$size"
  fi
}

exit_code() { run_remote "cat $EXIT_FILE 2>/dev/null" 2>/dev/null || true; }

report_and_fail() {
  echo "ОШИБКА: $1"
  echo "---- хвост лога ($LOG_FILE) ----"
  run_remote "tail -n 40 $LOG_FILE" 2>/dev/null || true
  echo "---- образы сервисов ----"
  run_remote "docker ps --filter name=mentis --format '{{.Names}} {{.Image}}'" 2>/dev/null || true
  echo "Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6)."
  exit 1
}

deadline=$((SECONDS + WAIT_TIMEOUT_S))
while :; do
  print_log
  code="$(exit_code)"
  if [[ -n "$code" ]]; then
    print_log
    if [[ "$code" == 0 ]]; then
      echo
      echo "==> Готово: ${REVISION} развёрнут (попытка ${WATCH_ATTEMPT})."
      echo "    Доступ: ssh -L 8010:localhost:8010 ${SSH_TARGET}  затем http://localhost:8010"
      exit 0
    fi
    report_and_fail "скрипт ограды завершился с кодом $code"
  fi
  snapshot_or_die
  if ! alive || [[ "$S_ATTEMPT" != "$WATCH_ATTEMPT" ]]; then
    code="$(exit_code)"  # .exit мог появиться между чтениями
    [[ -n "$code" ]] && continue
    report_and_fail "скрипт ограды завершился без кода (юнит: $S_LOAD/$S_ACTIVE, попытка '${S_ATTEMPT}')"
  fi
  ((SECONDS < deadline)) || report_and_fail "таймаут ${WAIT_TIMEOUT_S} с; юнит не остановлен и продолжает работу"
  sleep "$POLL_S"
done
