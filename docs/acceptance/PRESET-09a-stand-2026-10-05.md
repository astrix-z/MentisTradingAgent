# PRESET-09a: приёмка выкладки на реальном стеке

Прогон 2026-10-05 17:22 – 17:50 UTC, скрипты выкладки из `14c8517`. Стенд — контейнер с systemd (PID 1), sshd и своим Docker (`tests/deploy/stand/`); `deploy.sh` ходит в него по SSH (`DEPLOY_SSH`), скрипт ограды работает в юните `mentis-deploy` пользователя с linger. Отказы вносит сам стенд: `~/bin/compose-fault` (это `DEPLOY_COMPOSE` юнита), `~/bin/systemd-run`, chmod шины пользователя, `loginctl disable-linger`, `systemctl --user kill`, kill локального `deploy.sh`. Воспроизвести: `python3 tests/deploy/stand/run-matrix.py --out <файл>`.

Итог: **39 из 39** сценариев дали ожидаемый исход.

Состояние снято сразу после сценария: флаг `trading:start_fence`, образы (ревизии по тегам) и состояние контейнеров backend и agent, `releases/current`.

| # | Сценарий | Ожидаемо | Ревизия | Код `deploy.sh` | Флаг | backend | agent | current | Итог |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Первый запуск без `--t0-revision` | отказ до шага (1) | `ecf2162` | 1 | — | e1758f08810c running | 65a0f43a0d0c running | `—` | ✅ |
| 2 | `--t0-revision` не совпадает с последним дампом | отказ сверки T0 | `ecf2162` | 1 | — | e1758f08810c running | 65a0f43a0d0c running | `—` | ✅ |
| 3 | Оба контейнера созданы раньше последнего дампа (выкладка T0 упала на сборке) | отказ сверки T0 | `ecf2162` | 1 | — | e1758f08810c running | 65a0f43a0d0c running | `—` | ✅ |
| 4 | То же с `--t0-confirm` (сборка цели затем падает) | T0 закреплён решением человека; сбой сборки — ни флага, ни остановки агента | `ecf2162` | 1 | — | 567f4f8 running | 567f4f8 running | `567f4f8` | ✅ |
| 5 | Корректная выкладка T0: агент не пересоздавался (образ из кеша), backend — после дампа | сверка пройдена, T0 закреплён, выкладка первой ревизии | `ecf2162` | 0 | — | ecf2162 running | ecf2162 running | `ecf2162` | ✅ |
| 6 | 11 выкладок подряд (с первой), ротация образов | образы T0 на месте (pinned), ротация оставляет 10 ревизий журнала | `7b0118a` | — | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 7 | Сбой сборки цели | ни флага, ни остановки агента; APP_DIR — работающая ревизия | `8e0a24c` | 1 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 8 | Сбой после сборки, до флага (SET флага не прошёл) | образы цели собраны, `:latest` — работающая ревизия, флага нет | `8e0a24c` | 1 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 9 | Сбой между остановкой агента и `up` (удаление каталога упало) | `trap` поднял прежнего агента, его каталог опубликован, флаг стоит, печатает, что делать | `8e0a24c` | 1 | deploy 8e0a24c 1791221558-829727-495e2ce6 2026-10-05T17:32:41+00:00 | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 10 | Сбой внутри `up` (частичный up) | работает агент из `releases/current`, `mentis-backend:latest` = работающий backend, флаг стоит | `8e0a24c` | 1 | deploy 8e0a24c 1791221572-831989-48ca4787 2026-10-05T17:32:55+00:00 | 8e0a24c running | 7b0118a running | `7b0118a` | ✅ |
| 11 | Агент цели не публикует каталог (поставлен на паузу после `up`) | ожидание 2 мин, отказ, флаг стоит, агент цели запущен | `8e0a24c` | 1 | deploy 8e0a24c 1791221612-835894-38454f63 2026-10-05T17:33:35+00:00 | 8e0a24c running | 8e0a24c paused | `7b0118a` | ✅ |
| 12 | Повторный `deploy.sh` той же ревизии после неудачи (таймаут шага (5)) | новая попытка, старый `.exit` не читается, флаг снимает только новый успех | `8e0a24c` | 0 | — | 8e0a24c running | 8e0a24c running | `8e0a24c` | ✅ |
| 13 | Юнит убит `systemctl --user kill -s KILL` до записи `.exit` | `deploy.sh`: «завершился без кода», не ждёт до таймаута, флаг стоит | `8e0a24c` | 1 | deploy 8e0a24c 1791221787-850937-48fe3d1f 2026-10-05T17:36:29+00:00 | 8e0a24c running | 8e0a24c exited | `8e0a24c` | ✅ |
| 14 | Обрыв SSH (kill локального `deploy.sh`) сразу после остановки агента | скрипт на сервере доходит до конца; лог и код выхода в `releases/` | `7b0118a` | -9 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 15 | Два одновременных `deploy.sh` одной ревизии | один юнит, второй подключается к его логу; оба получают исход одной попытки | `8e0a24c` | 0 | — | 8e0a24c running | 8e0a24c running | `8e0a24c` | ✅ |
| 16 | То же при прошлой неудачной попытке этой ревизии с `.exit` на диске | ни один не сообщает её код; оба получают исход новой попытки | `7b0118a` | 0 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 17 | Второй `deploy.sh` той же ревизии с изменённым скриптом ограды, пока первый жив | первый дорабатывает свою версию (маркер в логе), второй подключается | `8e0a24c` | 0 | — | 8e0a24c running | 8e0a24c running | `8e0a24c` | ✅ |
| 18 | `deploy.sh` другой ревизии, пока идёт выкладка | отказ, ничего не меняя; идущая выкладка завершается | `8e0a24c` | 1 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 19 | Linger выключен у пользователя SSH | отказ до шага (1): ни флага, ни остановки агента | `7b0118a` | 1 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 20 | Цель старше `608cfa1` (агент не публикует каталог) | отказ до шага (1) | `c0c8cf3` | 1 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 21a | Цель без `compose.prod.yml` (`608cfa1`) | отказ на проверке контракта, до шага (1) | `608cfa1` | 1 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 21 | Цель без образов и без tracked `uv.lock` | отказ, lock текущего дерева не подставляется | `c26ff56` | 1 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 22 | Шина менеджера пользователя недоступна дольше 30 с | «состояние неизвестно», юнит не запускается | `7b0118a` | 1 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 23 | Шина недоступна 8 с во время ожидания исхода | повтор чтения; после восстановления — та же попытка, без нового запуска | `7b0118a` | 0 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 24 | Загруженный юнит `mentis-deploy` без `DEPLOY_ATTEMPT` (R109) | «метаданные попытки отсутствуют или некорректны», без подключения и запуска | `7b0118a` | 1 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 25 | `systemd-run` отказал, юнита нет (R106) | «юнит отсутствует: запуск не подтверждён…», stderr `systemd-run`, G4 не запускается | `7b0118a` | 1 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 26 | `systemd-run` вернул ошибку, а юнит работает со своей попыткой (R102) | `deploy.sh` ждёт свою попытку и получает её исход | `7b0118a` | 0 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 25b | `systemd-run` отказал, а юнит выгружен между снимками S0 и S1 (R106) | «юнит отсутствует…», G4 не запускается | `7b0118a` | 1 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 21b | Цель без сервиса `agent` в compose-файлах | отказ до шага (1) | `7458522` | 1 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 21c | Образ агента цели не `mentis-agent:latest` | отказ до шага (1) | `a1e93b8` | 1 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 27 | Start во время выкладки (между флагом и каталогом цели), оба маршрута; Stop | Start — 503 «starting is closed for maintenance»; Stop принят | `8e0a24c` | 0 | — | 8e0a24c running | 8e0a24c running | `8e0a24c` | ✅ |
| 28 | Барьер доставки: Start в потоке при остановленном агенте, затем Stop | барьер не проходит, пока агент не обработал обе команды; после — Stop succeeded, lag 0, pending 0, idle | `7b0118a` | — | — | 8e0a24c running | 8e0a24c running | `8e0a24c` | ✅ |
| 30 | Подготовленная, но не запущенная попытка (пауза клиента) переживает чужую выкладку (R108) | её stage и копия скрипта сохранены; её запуск затем проходит штатно | `7b0118a` | 0 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 31 | Одновременные подготовки одной ревизии, запуск одновременно (R103) | один юнит; вторая подключается | `8e0a24c/8e0a24c` | 0 | — | 8e0a24c running | 8e0a24c running | `8e0a24c` | ✅ |
| 32 | Одновременные подготовки двух ревизий, запуск одновременно (R103) | общее состояние меняет один юнит; другая — отказ до любых записей | `7b0118a/8e0a24c` | 1 | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 33 | Start прошёл раннюю проверку до флага, приостановлен перед записью; флаг поставлен | 503; в потоке и в desired его нет | `—` | — | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 34 | Перезапуск агента на границе обработки: Start прочитан, не подтверждён | барьер держит (pending ≠ 0), после рестарта агент дочитывает; затем Stop succeeded, lag 0, pending 0 | `7b0118a` | — | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 35 | Конкурентный Start между последним наблюдением барьера и остановкой backend | 503 по обоим маршрутам (флаг поставлен на шаге 1), поток не растёт | `7b0118a` | — | — | 7b0118a running | 7b0118a running | `7b0118a` | ✅ |
| 29 | Откат на T0 по runbook после всех выкладок | без сборки, на закреплённых образах; alembic = ревизия T0; в APP_DIR нет чужих миграций; каталог T0 | `567f4f8` | 0 | — | 567f4f8 running | 567f4f8 running | `567f4f8` | ✅ |

## Соответствие приёмке карточки PRESET-09a

Каждый пункт раздела «Приёмка (локальный стек)» карточки и сценарии этого журнала, которые на него отвечают. Общий gate (ruff, mypy, biome, tsc, pytest, Playwright) — вне стенда, его прогон указан в комментарии задачи.

| Пункт приёмки | Сценарии | Итог |
|---|---|---|
| Во время deploy Start по обоим маршрутам → 503 за один EVAL (код -1, без цикла); Stop при флаге принимается и пишет снапшот | 27, 33 | ✅ |
| Start, прошедший проверку до флага и приостановленный перед записью, после флага → 503, в потоке и в desired его нет | 33 | ✅ |
| Каталог не опубликован за 2 мин → скрипт падает, флаг остаётся, агент цели работает | 11 | ✅ |
| Сбой сборки → ни флага, ни остановки агента | 7 | ✅ |
| Сбой между остановкой агента и up → прежний агент поднят trap, его каталог опубликован, флаг стоит | 9 | ✅ |
| После отката чистым артефактом нет чужих файлов миграций; alembic current = цель | 29 | ✅ |
| После выкладки есть образы :<revision> и releases/current | 5, 6, 12 | ✅ |
| Цель без образов и без tracked uv.lock → отказ | 21 | ✅ |
| T0: без --t0-revision → отказ; не совпадает с последним дампом → отказ | 1, 2 | ✅ |
| T0: контейнер создан раньше дампа → отказ, --t0-confirm снимает | 3, 4 | ✅ |
| T0: корректная выкладка с образом из кеша проходит сверку; образы помечены :<T0> | 4, 5 | ✅ |
| Откат на T0 на этих образах без сборки, строка T0 таблицы §7.1 | 29 | ✅ |
| После 11 выкладок образы T0 на месте, откат без сборки | 6, 29 | ✅ |
| Сбой внутри up → агент из releases/current, флаг стоит, напечатаны образы обоих сервисов | 10 | ✅ |
| Сбой после сборки до флага → :latest указывает на работающую ревизию | 8 | ✅ |
| Барьер: Start в потоке, агент остановлен до обработки → Stop → барьер держит, пока Start не обработан | 28 | ✅ |
| Барьер: перезапуск агента на границе обработки — барьер держит | 34 | ✅ |
| Барьер: конкурентный Start между наблюдением барьера и остановкой backend → 503 | 35 | ✅ |
| Linger выключен у пользователя SSH → отказ до шага (1) | 19 | ✅ |
| Цель без сервиса agent / образ не mentis-agent:latest / старше 608cfa1 → отказ до шага (1) | 21b, 21c, 20 | ✅ |
| Неудачный запуск X (таймаут шага (5)), затем повторный X → новая попытка, старый .exit не прочитан, флаг снимает новый успех | 11, 12 | ✅ |
| Два одновременных deploy.sh одной ревизии; то же при старом .exit (R97) | 15, 16 | ✅ |
| Второй deploy.sh с изменённым скриптом ограды при живом юните (R98) | 17 | ✅ |
| systemd-run отказал, юнита нет (R106); юнит выгружен между S0 и S1; после фиксации ATTEMPT — «без кода» | 25, 25b, 13 | ✅ |
| Загруженный юнит без DEPLOY_ATTEMPT или с неверным форматом (R109) | 24 | ✅ |
| Подготовленная, но не запущенная попытка переживает чужую выкладку и запускается штатно (R108) | 30 | ✅ |
| Недоступная шина: повтор чтения, та же попытка; после 30 с — «состояние неизвестно» | 22, 23 | ✅ |
| systemd-run вернул ошибку, а юнит работает со своей попыткой (R102) | 26 | ✅ |
| Одновременные подготовки одной и двух ревизий: состояние меняет один юнит, compose.yml и образы не меняются, флаг снимает он (R103) | 31, 32, 18 | ✅ |
| Юнит убит до записи .exit → «без кода», не ждёт таймаута, флаг стоит | 13 | ✅ |
| Обрыв SSH сразу после остановки агента → скрипт на сервере доходит до конца, лог и код в releases/ | 14 | ✅ |

## Подробности

### 1. Первый запуск без `--t0-revision`


```
==> Выкладка ecf2162 на jb@stand, попытка 1791221092-784654-0b5ba4bf
==> 2026-10-05T17:24:54+00:00 попытка 1791221092-784654-0b5ba4bf, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
ОШИБКА: первый запуск: нужен --t0-revision <ревизия, работающая сейчас>
!!! 2026-10-05T17:24:54+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:24:54+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1791221092-784654-0b5ba4bf.log) ----
==> 2026-10-05T17:24:54+00:00 попытка 1791221092-784654-0b5ba4bf, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
ОШИБКА: первый запуск: нужен --t0-revision <ревизия, работающая сейчас>
!!! 2026-10-05T17:24:54+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:24:54+00:00 код выхода 1
---- образы сервисов ----
mentis-backend-1 mentis-backend:latest
mentis-agent-1 mentis-agent:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 2. `--t0-revision` не совпадает с последним дампом


```
==> Выкладка ecf2162 на jb@stand, попытка 1791221099-785613-f266c4ce
==> 2026-10-05T17:25:01+00:00 попытка 1791221099-785613-f266c4ce, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
==> T0: сверка bef05d5 с последним дампом и контейнерами
    последний дамп '/home/jb/backups/mentis-trading-agent/pre-deploy-app-567f4f8-20261005-172420.dump' не от ревизии bef05d5
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-05T17:25:01+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:25:01+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1791221099-785613-f266c4ce.log) ----
==> 2026-10-05T17:25:01+00:00 попытка 1791221099-785613-f266c4ce, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
==> T0: сверка bef05d5 с последним дампом и контейнерами
    последний дамп '/home/jb/backups/mentis-trading-agent/pre-deploy-app-567f4f8-20261005-172420.dump' не от ревизии bef05d5
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-05T17:25:01+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:25:01+00:00 код выхода 1
---- образы сервисов ----
mentis-backend-1 mentis-backend:latest
mentis-agent-1 mentis-agent:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 3. Оба контейнера созданы раньше последнего дампа (выкладка T0 упала на сборке)


```
==> Выкладка ecf2162 на jb@stand, попытка 1791221108-786892-2164d664
==> 2026-10-05T17:25:09+00:00 попытка 1791221108-786892-2164d664, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
==> T0: сверка 567f4f8 с последним дампом и контейнерами
    ни один контейнер не создан после дампа (выкладка упала на сборке или шла без сборки?)
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-05T17:25:10+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:25:10+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1791221108-786892-2164d664.log) ----
==> 2026-10-05T17:25:09+00:00 попытка 1791221108-786892-2164d664, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
==> T0: сверка 567f4f8 с последним дампом и контейнерами
    ни один контейнер не создан после дампа (выкладка упала на сборке или шла без сборки?)
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-05T17:25:10+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:25:10+00:00 код выхода 1
---- образы сервисов ----
mentis-backend-1 mentis-backend:latest
mentis-agent-1 mentis-agent:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 4. То же с `--t0-confirm` (сборка цели затем падает)

- образы T0: mentis-agent:567f4f8, mentis-backend:567f4f8

```
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Сборка :ecf2162
compose-fault: build
!!! 2026-10-05T17:25:20+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:25:20+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1791221116-788143-7b1905b3.log) ----
==> 2026-10-05T17:25:18+00:00 попытка 1791221116-788143-7b1905b3, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
==> T0: сверка 567f4f8 с последним дампом и контейнерами
    ни один контейнер не создан после дампа (выкладка упала на сборке или шла без сборки?)
    сверка не пройдена, но есть --t0-confirm (решение человека)
    T0 = 567f4f8: образы помечены, закреплён в releases/pinned
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Сборка :ecf2162
compose-fault: build
!!! 2026-10-05T17:25:20+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:25:20+00:00 код выхода 1
---- образы сервисов ----
mentis-backend-1 mentis-backend:latest
mentis-agent-1 mentis-agent:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 5. Корректная выкладка T0: агент не пересоздавался (образ из кеша), backend — после дампа


```
 Container mentis-backend-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-backend-1 Recreated
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-backend-1 Started
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: ecf2162
==> 2026-10-05T17:26:04+00:00 код выхода 0

==> Готово: ecf2162 развёрнут (попытка 1791221130-790058-7be54e25).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 6. 11 выкладок подряд (с первой), ротация образов

- ревизии: `245e153`, `df85295`, `d97e922`, `2b97207`, `187f1ac`, `d5ed82e`, `e06aac8`, `4c7664e`, `8848ff7`, `7b0118a`
- образы после ротации: mentis-agent:187f1ac, mentis-agent:245e153, mentis-agent:2b97207, mentis-agent:4c7664e, mentis-agent:567f4f8, mentis-agent:7b0118a, mentis-agent:8848ff7, mentis-agent:d5ed82e, mentis-agent:d97e922, mentis-agent:df85295, mentis-agent:e06aac8, mentis-agent:latest, mentis-backend:187f1ac, mentis-backend:245e153, mentis-backend:2b97207, mentis-backend:4c7664e, mentis-backend:567f4f8, mentis-backend:7b0118a, mentis-backend:8848ff7, mentis-backend:d5ed82e, mentis-backend:d97e922, mentis-backend:df85295, mentis-backend:e06aac8, mentis-backend:latest

### 7. Сбой сборки цели


```
==> Выкладка 8e0a24c на jb@stand, попытка 1791221530-825872-5a68f28f
==> 2026-10-05T17:32:11+00:00 попытка 1791221530-825872-5a68f28f, ревизия 8e0a24c, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Сборка :8e0a24c
compose-fault: build
!!! 2026-10-05T17:32:13+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:32:13+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-8e0a24c-1791221530-825872-5a68f28f.log) ----
==> 2026-10-05T17:32:11+00:00 попытка 1791221530-825872-5a68f28f, ревизия 8e0a24c, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Сборка :8e0a24c
compose-fault: build
!!! 2026-10-05T17:32:13+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:32:13+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 8. Сбой после сборки, до флага (SET флага не прошёл)


```
#35 exporting attestation manifest sha256:cee732101c12f903e3b51d5a1901b7851aec66e48b86c06ec4194eae6ee86243 0.0s done
#35 exporting manifest list sha256:3de2a9be41ca93c876b899c253bff19a4a80e5f922014fad8d368de1d378f2fb 0.0s done
#35 naming to docker.io/library/mentis-agent:8e0a24c done
#35 unpacking to docker.io/library/mentis-agent:8e0a24c 0.0s done
#35 DONE 0.2s

#36 [agent] resolving provenance for metadata file
#36 DONE 0.0s

#37 [backend] resolving provenance for metadata file
#37 DONE 0.0s
 Image mentis-agent:8e0a24c Built
 Image mentis-backend:8e0a24c Built
==> Установка 8e0a24c в /home/jb/apps/mentis-trading-agent
==> (1) флаг trading:start_fence
compose-fault: flag
ОШИБКА: флаг trading:start_fence не поставлен: нет ответа
!!! 2026-10-05T17:32:29+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:32:29+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 9. Сбой между остановкой агента и `up` (удаление каталога упало)


```
==> (2) остановка агента
 Container mentis-agent-1 Stopping
 Container mentis-agent-1 Stopped
==> (3) удаление agent:strategies
compose-fault: catalog-del
!!! 2026-10-05T17:32:42+00:00 сбой в фазе stopped (код 1)
==> trap: поднимаю прежнего агента (контейнер и образ не тронуты)
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
    Образы сейчас:
    backend: mentis-backend:latest sha256:65b88b13b7dd03ff433ad0057af4c8477f4af453ad64caa563e43ca3cc681035 running
    agent: mentis-agent:latest sha256:71ff14ab48a842c32677920f3dd05a97790800ded812a5b191a03523a99f8a3e running
    Флаг trading:start_fence остаётся (Start закрыт). Снять — только после проверки, по runbook
    (docs/DEPLOY.md §6): повторный deploy.sh или redis-cli DEL trading:start_fence.
==> 2026-10-05T17:32:43+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 10. Сбой внутри `up` (частичный up)


```
 Container mentis-db-1 Running
 Container mentis-redis-1 Running
 Container mentis-agent-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
==> trap: mentis-backend:latest = образ работающего backend
    Backend мог быть пересоздан и применить миграции: при «новый backend +
    прежний агент» флаг снимается только повторным deploy.sh или решением человека.
    Образы сейчас:
    backend: mentis-backend:latest sha256:dbe58ef20e07759e161b1c90c52bc59b04b2056fe1237215c5500bedef16b913 running
    agent: mentis-agent:latest sha256:71ff14ab48a842c32677920f3dd05a97790800ded812a5b191a03523a99f8a3e running
    Флаг trading:start_fence остаётся (Start закрыт). Снять — только после проверки, по runbook
    (docs/DEPLOY.md §6): повторный deploy.sh или redis-cli DEL trading:start_fence.
==> 2026-10-05T17:33:21+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 11. Агент цели не публикует каталог (поставлен на паузу после `up`)

- `deploy.sh` ждал 139 с

```
 Container mentis-db-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
compose-fault: catalog-timeout
==> (5) ожидание каталога агента цели (до 120 с)
ОШИБКА: агент цели не опубликовал agent:strategies за 120 с
!!! 2026-10-05T17:35:50+00:00 сбой в фазе waiting (код 1)
    Образы сейчас:
    backend: mentis-backend:latest sha256:dbe58ef20e07759e161b1c90c52bc59b04b2056fe1237215c5500bedef16b913 running
    agent: mentis-agent:latest sha256:3de2a9be41ca93c876b899c253bff19a4a80e5f922014fad8d368de1d378f2fb paused
    Флаг trading:start_fence остаётся (Start закрыт). Снять — только после проверки, по runbook
    (docs/DEPLOY.md §6): повторный deploy.sh или redis-cli DEL trading:start_fence.
==> 2026-10-05T17:35:50+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 12. Повторный `deploy.sh` той же ревизии после неудачи (таймаут шага (5))


```
 Container mentis-redis-1 Running
 Container mentis-db-1 Running
 Container mentis-backend-1 Running
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
    ротация: mentis-backend:245e153
    ротация: mentis-agent:245e153
==> Готово: 8e0a24c
==> 2026-10-05T17:36:19+00:00 код выхода 0

==> Готово: 8e0a24c развёрнут (попытка 1791221758-847918-e6f6b008).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 13. Юнит убит `systemctl --user kill -s KILL` до записи `.exit`

- агент был остановлен на шаге (2) и не поднят: так и должно быть без trap; следующая выкладка поднимает его (сценарий 14)

```
==> (2) остановка агента
 Container mentis-agent-1 Stopping
 Container mentis-agent-1 Stopped
==> (3) удаление agent:strategies
==> (4) :latest → :8e0a24c и up
compose-fault: slow-up
ОШИБКА: скрипт ограды завершился без кода (юнит: not-found/inactive, попытка '')
---- хвост лога ("$HOME"/apps/releases/deploy-8e0a24c-1791221787-850937-48fe3d1f.log) ----
==> 2026-10-05T17:36:28+00:00 попытка 1791221787-850937-48fe3d1f, ревизия 8e0a24c, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Образы :8e0a24c уже есть — без сборки
==> Установка 8e0a24c в /home/jb/apps/mentis-trading-agent
==> (1) флаг trading:start_fence
==> (2) остановка агента
 Container mentis-agent-1 Stopping
 Container mentis-agent-1 Stopped
==> (3) удаление agent:strategies
==> (4) :latest → :8e0a24c и up
compose-fault: slow-up
---- образы сервисов ----
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 14. Обрыв SSH (kill локального `deploy.sh`) сразу после остановки агента

- попытка `1791221813-853254-453ef64d`, код в `.exit`: 0

```
==> Выкладка 7b0118a на jb@stand, попытка 1791221813-853254-453ef64d
==> 2026-10-05T17:36:55+00:00 попытка 1791221813-853254-453ef64d, ревизия 7b0118a, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Образы :7b0118a уже есть — без сборки
==> Установка 7b0118a в /home/jb/apps/mentis-trading-agent
==> (1) флаг trading:start_fence
==> (2) остановка агента
 Container mentis-agent-1 Stopping
 Container mentis-agent-1 Stopped
==> (3) удаление agent:strategies
==> (4) :latest → :7b0118a и up
compose-fault: slow-up
```

### 15. Два одновременных `deploy.sh` одной ревизии

- первый: код 0; логов попыток ревизии: 8
- ExecStart юнита: `ExecStart={ path=/usr/bin/bash ; argv[]=/usr/bin/bash /home/jb/apps/releases/deploy-fence-1791221858-856641-795faded.sh 8e0a24c 1791221858-856641-795faded ; ign`

```
 Container mentis-backend-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-backend-1 Recreated
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-agent-1 Started
 Container mentis-backend-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: 8e0a24c
==> 2026-10-05T17:38:34+00:00 код выхода 0

==> Готово: 8e0a24c развёрнут (попытка 1791221858-856641-795faded).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 16. То же при прошлой неудачной попытке этой ревизии с `.exit` на диске


```
 Container mentis-backend-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-backend-1 Recreated
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-db-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-backend-1 Started
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: 7b0118a
==> 2026-10-05T17:39:39+00:00 код выхода 0

==> Готово: 7b0118a развёрнут (попытка 1791221929-863132-2ac559cb).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 17. Второй `deploy.sh` той же ревизии с изменённым скриптом ограды, пока первый жив

- маркер: `скрипт ограды 4f9aeddfb56d`, хеш первого `4f9aeddfb56d`

```
 Container mentis-backend-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-backend-1 Recreated
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
 Container mentis-backend-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: 8e0a24c
==> 2026-10-05T17:40:40+00:00 код выхода 0

==> Готово: 8e0a24c развёрнут (попытка 1791221984-868435-2742dfa4).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 18. `deploy.sh` другой ревизии, пока идёт выкладка


```
==> Выкладка 8e0a24c на jb@stand, попытка 1791222054-874940-516ae587
ОШИБКА: идёт выкладка ревизии 7b0118a (попытка 1791222048-874065-3d7ca263); дождитесь её конца
```

### 19. Linger выключен у пользователя SSH


```
ОШИБКА: у пользователя SSH на сервере выключен linger — юнит выкладки не переживёт
обрыв сессии. Один раз на сервере: sudo loginctl enable-linger <user> (docs/DEPLOY.md).
```

### 20. Цель старше `608cfa1` (агент не публикует каталог)


```
ОШИБКА: ревизия c0c8cf3 старше 608cfa1: её агент не публикует agent:strategies, G4 не пройдёт
```

### 21a. Цель без `compose.prod.yml` (`608cfa1`)

- Причина в логе — ошибка `docker compose config` (нет файла), а не отдельное сообщение скрипта ограды.

```
==> Выкладка 608cfa1 на jb@stand, попытка 1791222124-880659-dda09105
==> 2026-10-05T17:42:06+00:00 попытка 1791222124-880659-dda09105, ревизия 608cfa1, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
open /home/jb/apps/releases/stage-1791222124-880659-dda09105/tree/compose.prod.yml: no such file or directory
!!! 2026-10-05T17:42:07+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:42:07+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-608cfa1-1791222124-880659-dda09105.log) ----
==> 2026-10-05T17:42:06+00:00 попытка 1791222124-880659-dda09105, ревизия 608cfa1, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
open /home/jb/apps/releases/stage-1791222124-880659-dda09105/tree/compose.prod.yml: no such file or directory
!!! 2026-10-05T17:42:07+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:42:07+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 21. Цель без образов и без tracked `uv.lock`


```
==> Выкладка c26ff56 на jb@stand, попытка 1791222135-881907-9d6e4608
==> 2026-10-05T17:42:16+00:00 попытка 1791222135-881907-9d6e4608, ревизия c26ff56, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: у ревизии c26ff56 нет tracked uv.lock и нет releases/c26ff56.uv.lock — lock текущего дерева не подставляется
!!! 2026-10-05T17:42:17+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:42:17+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-c26ff56-1791222135-881907-9d6e4608.log) ----
==> 2026-10-05T17:42:16+00:00 попытка 1791222135-881907-9d6e4608, ревизия c26ff56, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: у ревизии c26ff56 нет tracked uv.lock и нет releases/c26ff56.uv.lock — lock текущего дерева не подставляется
!!! 2026-10-05T17:42:17+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:42:17+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 22. Шина менеджера пользователя недоступна дольше 30 с


```
==> Выкладка 7b0118a на jb@stand, попытка 1791222146-883268-ad1343e1
ОШИБКА: состояние юнита mentis-deploy неизвестно (нет ответа менеджера за 30 с).
    Выкладка не запускалась повторно; проверьте на сервере: systemctl --user status mentis-deploy
```

### 23. Шина недоступна 8 с во время ожидания исхода


```
==> (4) :latest → :7b0118a и up
compose-fault: slow-up
 Container mentis-redis-1 Running
 Container mentis-db-1 Running
 Container mentis-backend-1 Running
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: 7b0118a
==> 2026-10-05T17:43:39+00:00 код выхода 0

==> Готово: 7b0118a развёрнут (попытка 1791222183-885026-f75f943a).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 24. Загруженный юнит `mentis-deploy` без `DEPLOY_ATTEMPT` (R109)


```
==> Выкладка 7b0118a на jb@stand, попытка 1791222224-888247-7a101086
ОШИБКА: метаданные попытки отсутствуют или некорректны (юнит mentis-deploy):
    LoadState=loaded ActiveState=active DEPLOY_ATTEMPT='' DEPLOY_REVISION=''
    Не подключаюсь и не запускаю; разберитесь с юнитом на сервере.
```

### 25. `systemd-run` отказал, юнита нет (R106)


```
==> Выкладка 7b0118a на jb@stand, попытка 1791222229-888945-33335dd3
ОШИБКА: юнит mentis-deploy отсутствует: запуск не подтверждён либо попытка уже выгружена.
    systemd-run: systemd-run-fault: refused, nothing started
    Непроверенная справка (releases/deploy.current): 7b0118a 1791222183-885026-f75f943a
```

### 26. `systemd-run` вернул ошибку, а юнит работает со своей попыткой (R102)


```
==> (3) удаление agent:strategies
==> (4) :latest → :7b0118a и up
 Container mentis-redis-1 Running
 Container mentis-db-1 Running
 Container mentis-backend-1 Running
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: 7b0118a
==> 2026-10-05T17:44:12+00:00 код выхода 0

==> Готово: 7b0118a развёрнут (попытка 1791222236-889812-984feedf).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 25b. `systemd-run` отказал, а юнит выгружен между снимками S0 и S1 (R106)


```
==> Выкладка 7b0118a на jb@stand, попытка 1791222257-892163-3a04cc7f
ОШИБКА: юнит mentis-deploy отсутствует: запуск не подтверждён либо попытка уже выгружена.
    systemd-run: systemd-run-fault: a unit ran and was collected
    Непроверенная справка (releases/deploy.current): 7b0118a 1791222236-889812-984feedf
```

### 21b. Цель без сервиса `agent` в compose-файлах

- одноразовый коммит: в compose.yml сервис agent переименован

```
==> Выкладка 7458522 на jb@stand, попытка 1791222264-892971-b574092d
==> 2026-10-05T17:44:26+00:00 попытка 1791222264-892971-b574092d, ревизия 7458522, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: в compose-файлах цели нет сервиса agent
!!! 2026-10-05T17:44:27+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:44:27+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-7458522-1791222264-892971-b574092d.log) ----
==> 2026-10-05T17:44:26+00:00 попытка 1791222264-892971-b574092d, ревизия 7458522, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: в compose-файлах цели нет сервиса agent
!!! 2026-10-05T17:44:27+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:44:27+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 21c. Образ агента цели не `mentis-agent:latest`

- одноразовый коммит: образ агента `mentis-other-agent`

```
==> Выкладка a1e93b8 на jb@stand, попытка 1791222275-894228-66014969
==> 2026-10-05T17:44:37+00:00 попытка 1791222275-894228-66014969, ревизия a1e93b8, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: образ agent цели без MENTIS_TAG — не mentis-agent:latest
!!! 2026-10-05T17:44:38+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:44:38+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-a1e93b8-1791222275-894228-66014969.log) ----
==> 2026-10-05T17:44:37+00:00 попытка 1791222275-894228-66014969, ревизия a1e93b8, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: образ agent цели без MENTIS_TAG — не mentis-agent:latest
!!! 2026-10-05T17:44:38+00:00 сбой в фазе before (код 1)
==> 2026-10-05T17:44:38+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 27. Start во время выкладки (между флагом и каталогом цели), оба маршрута; Stop

- ответ: {'detail': 'starting is closed for maintenance'}
- EVAL с ключом флага за время двух Start: 0

```
 Container mentis-backend-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-backend-1 Recreated
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-backend-1 Started
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: 8e0a24c
==> 2026-10-05T17:45:33+00:00 код выхода 0

==> Готово: 8e0a24c развёрнут (попытка 1791222287-895670-1b67ed8b).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 28. Барьер доставки: Start в потоке при остановленном агенте, затем Stop

- XINFO при остановленном агенте: name agent-workers consumers 1 pending 0 last-delivered-id 1791222295669-0 entries-read 1 lag 2

### 30. Подготовленная, но не запущенная попытка (пауза клиента) переживает чужую выкладку (R108)

- попытка `1791222358-901189-25119ac5` ждала перед systemd-run, пока шла выкладка `8e0a24c`

```
 Container mentis-agent-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-backend-1 Recreated
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-redis-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-agent-1 Started
 Container mentis-backend-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: 7b0118a
==> 2026-10-05T17:46:46+00:00 код выхода 0

==> Готово: 7b0118a развёрнут (попытка 1791222358-901189-25119ac5).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 31. Одновременные подготовки одной ревизии, запуск одновременно (R103)

- compose.yml: ce0ab0ae98ea → ce0ab0ae98ea; образы :latest: dbe58ef20e07,3de2a9be41ca → dbe58ef20e07,3de2a9be41ca
- логов попыток этих ревизий всего: 11

```
 Container mentis-backend-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-backend-1 Recreated
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-backend-1 Started
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: 8e0a24c
==> 2026-10-05T17:47:41+00:00 код выхода 0

==> Готово: 8e0a24c развёрнут (попытка 1791222414-906560-b4eb1057).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 32. Одновременные подготовки двух ревизий, запуск одновременно (R103)

- compose.yml: ce0ab0ae98ea → ce0ab0ae98ea; образы :latest: 65b88b13b7dd,71ff14ab48a8 → 65b88b13b7dd,71ff14ab48a8
- логов попыток этих ревизий всего: 18

```
 Container mentis-backend-1 Recreated
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-redis-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-agent-1 Started
 Container mentis-backend-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: 7b0118a
==> 2026-10-05T17:48:36+00:00 код выхода 0

==> Готово: 7b0118a развёрнут (попытка 1791222472-912099-bf0bd30e).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
==> Выкладка 8e0a24c на jb@stand, попытка 1791222472-912101-8970de3e
ОШИБКА: идёт выкладка ревизии 7b0118a (попытка 1791222472-912099-bf0bd30e); дождитесь её конца
```

### 33. Start прошёл раннюю проверку до флага, приостановлен перед записью; флаг поставлен

- Удержание — хук стенда (`tests/deploy/stand/hooks/sitecustomize.py`, override `stand-hooks.yml`) перед `_apply_command`; отказ дал Lua-скрипт записи (`COMMIT_RUNTIME_LUA`), код продукта не менялся.
- ответ: {'detail': 'starting is closed for maintenance'}; EVAL с ключом флага после снятия паузы: 1

### 34. Перезапуск агента на границе обработки: Start прочитан, не подтверждён

- после чтения за агента: pending=1, lag=1

### 35. Конкурентный Start между последним наблюдением барьера и остановкой backend


### 29. Откат на T0 по runbook после всех выкладок


```
 Container mentis-agent-1 Recreated
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-redis-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-agent-1 Started
 Container mentis-backend-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 1f6a4457d33cfc28b2157c13183147a9882086f2a0620c241435f100dbe497e4
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
    ротация: mentis-backend:df85295
    ротация: mentis-agent:df85295
==> Готово: 567f4f8
==> 2026-10-05T17:50:05+00:00 код выхода 0

==> Готово: 567f4f8 развёрнут (попытка 1791222591-922003-e8a8901a).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```
