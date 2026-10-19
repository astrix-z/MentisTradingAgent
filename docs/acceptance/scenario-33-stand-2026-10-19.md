# PRESET-09a: приёмка выкладки на реальном стеке

Прогон 2026-10-19 12:31 – 13:02 UTC, скрипты выкладки из `44a32f4`. Стенд — контейнер с systemd (PID 1), sshd и своим Docker (`tests/deploy/stand/`); `deploy.sh` ходит в него по SSH (`DEPLOY_SSH`), скрипт ограды работает в юните `mentis-deploy` пользователя с linger. Отказы вносит сам стенд: `~/bin/compose-fault` (это `DEPLOY_COMPOSE` юнита), `~/bin/systemd-run`, chmod шины пользователя, `loginctl disable-linger`, `systemctl --user kill`, kill локального `deploy.sh`. Воспроизвести: `python3 tests/deploy/stand/run-matrix.py --out <файл>`.

Итог: **39 из 39** сценариев дали ожидаемый исход.

Состояние снято сразу после сценария: флаг `trading:start_fence`, образы (ревизии по тегам) и состояние контейнеров backend и agent, `releases/current`.

| # | Сценарий | Ожидаемо | Ревизия | Код `deploy.sh` | Флаг | backend | agent | current | Итог |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Первый запуск без `--t0-revision` | отказ до шага (1) | `ecf2162` | 1 | — | b28cafeff6c8 running | 6df4b8fc39e9 running | `—` | ✅ |
| 2 | `--t0-revision` не совпадает с последним дампом | отказ сверки T0 | `ecf2162` | 1 | — | b28cafeff6c8 running | 6df4b8fc39e9 running | `—` | ✅ |
| 3 | Оба контейнера созданы раньше последнего дампа (выкладка T0 упала на сборке) | отказ сверки T0 | `ecf2162` | 1 | — | b28cafeff6c8 running | 6df4b8fc39e9 running | `—` | ✅ |
| 4 | То же с `--t0-confirm` (сборка цели затем падает) | T0 закреплён решением человека; сбой сборки — ни флага, ни остановки агента | `ecf2162` | 1 | — | 567f4f8 running | 567f4f8 running | `567f4f8` | ✅ |
| 5 | Корректная выкладка T0: агент не пересоздавался (образ из кеша), backend — после дампа | сверка пройдена, T0 закреплён, выкладка первой ревизии | `ecf2162` | 0 | — | ecf2162 running | ecf2162 running | `ecf2162` | ✅ |
| 6 | 11 выкладок подряд (с первой), ротация образов | образы T0 на месте (pinned), ротация оставляет 10 ревизий журнала | `72b8c51` | — | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 7 | Сбой сборки цели | ни флага, ни остановки агента; APP_DIR — работающая ревизия | `e2c8986` | 1 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 8 | Сбой после сборки, до флага (SET флага не прошёл) | образы цели собраны, `:latest` — работающая ревизия, флага нет | `e2c8986` | 1 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 9 | Сбой между остановкой агента и `up` (удаление каталога упало) | `trap` поднял прежнего агента, его каталог опубликован, флаг стоит, печатает, что делать | `e2c8986` | 1 | deploy e2c8986 1792413842-2660343-f099b801 2026-10-19T12:44:05+00:00 | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 10 | Сбой внутри `up` (частичный up) | работает агент из `releases/current`, `mentis-backend:latest` = работающий backend, флаг стоит | `e2c8986` | 1 | deploy e2c8986 1792413859-2662754-db984847 2026-10-19T12:44:22+00:00 | e2c8986 running | 72b8c51 running | `72b8c51` | ✅ |
| 11 | Агент цели не публикует каталог (поставлен на паузу после `up`) | ожидание 2 мин, отказ, флаг стоит, агент цели запущен | `e2c8986` | 1 | deploy e2c8986 1792413898-2666664-efa4a24b 2026-10-19T12:45:01+00:00 | e2c8986 running | e2c8986 paused | `72b8c51` | ✅ |
| 12 | Повторный `deploy.sh` той же ревизии после неудачи (таймаут шага (5)) | новая попытка, старый `.exit` не читается, флаг снимает только новый успех | `e2c8986` | 0 | — | e2c8986 running | e2c8986 running | `e2c8986` | ✅ |
| 13 | Юнит убит `systemctl --user kill -s KILL` до записи `.exit` | `deploy.sh`: «завершился без кода», не ждёт до таймаута, флаг стоит | `e2c8986` | 1 | deploy e2c8986 1792414078-2680724-c3fd2bfe 2026-10-19T12:48:01+00:00 | e2c8986 running | e2c8986 exited | `e2c8986` | ✅ |
| 14 | Обрыв SSH (kill локального `deploy.sh`) сразу после остановки агента | скрипт на сервере доходит до конца; лог и код выхода в `releases/` | `72b8c51` | -9 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 15 | Два одновременных `deploy.sh` одной ревизии | один юнит, второй подключается к его логу; оба получают исход одной попытки | `e2c8986` | 0 | — | e2c8986 running | e2c8986 running | `e2c8986` | ✅ |
| 16 | То же при прошлой неудачной попытке этой ревизии с `.exit` на диске | ни один не сообщает её код; оба получают исход новой попытки | `72b8c51` | 0 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 17 | Второй `deploy.sh` той же ревизии с изменённым скриптом ограды, пока первый жив | первый дорабатывает свою версию (маркер в логе), второй подключается | `e2c8986` | 0 | — | e2c8986 running | e2c8986 running | `e2c8986` | ✅ |
| 18 | `deploy.sh` другой ревизии, пока идёт выкладка | отказ, ничего не меняя; идущая выкладка завершается | `e2c8986` | 1 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 19 | Linger выключен у пользователя SSH | отказ до шага (1): ни флага, ни остановки агента | `72b8c51` | 1 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 20 | Цель старше `608cfa1` (агент не публикует каталог) | отказ до шага (1) | `c0c8cf3` | 1 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 21a | Цель без `compose.prod.yml` (`608cfa1`) | отказ на проверке контракта, до шага (1) | `608cfa1` | 1 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 21 | Цель без образов и без tracked `uv.lock` | отказ, lock текущего дерева не подставляется | `c26ff56` | 1 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 22 | Шина менеджера пользователя недоступна дольше 30 с | «состояние неизвестно», юнит не запускается | `72b8c51` | 1 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 23 | Шина недоступна 8 с во время ожидания исхода | повтор чтения; после восстановления — та же попытка, без нового запуска | `72b8c51` | 0 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 24 | Загруженный юнит `mentis-deploy` без `DEPLOY_ATTEMPT` (R109) | «метаданные попытки отсутствуют или некорректны», без подключения и запуска | `72b8c51` | 1 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 25 | `systemd-run` отказал, юнита нет (R106) | «юнит отсутствует: запуск не подтверждён…», stderr `systemd-run`, G4 не запускается | `72b8c51` | 1 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 26 | `systemd-run` вернул ошибку, а юнит работает со своей попыткой (R102) | `deploy.sh` ждёт свою попытку и получает её исход | `72b8c51` | 0 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 25b | `systemd-run` отказал, а юнит выгружен между снимками S0 и S1 (R106) | «юнит отсутствует…», G4 не запускается | `72b8c51` | 1 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 21b | Цель без сервиса `agent` в compose-файлах | отказ до шага (1) | `e093615` | 1 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 21c | Образ агента цели не `mentis-agent:latest` | отказ до шага (1) | `26fb242` | 1 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 27 | Start во время выкладки (между флагом и каталогом цели), оба маршрута; Stop | Start — 503 «starting is closed for maintenance»; Stop принят | `e2c8986` | 0 | — | e2c8986 running | e2c8986 running | `e2c8986` | ✅ |
| 28 | Барьер доставки: Start в потоке при остановленном агенте, затем Stop | барьер не проходит, пока агент не обработал обе команды; после — Stop succeeded, lag 0, pending 0, idle | `72b8c51` | — | — | e2c8986 running | e2c8986 running | `e2c8986` | ✅ |
| 30 | Подготовленная, но не запущенная попытка (пауза клиента) переживает чужую выкладку (R108) | её stage и копия скрипта сохранены; её запуск затем проходит штатно | `72b8c51` | 0 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 31 | Одновременные подготовки одной ревизии, запуск одновременно (R103) | один юнит; вторая подключается | `e2c8986/e2c8986` | 0 | — | e2c8986 running | e2c8986 running | `e2c8986` | ✅ |
| 32 | Одновременные подготовки двух ревизий, запуск одновременно (R103) | общее состояние меняет один юнит; другая — отказ до любых записей | `72b8c51/e2c8986` | 0 | — | e2c8986 running | e2c8986 running | `e2c8986` | ✅ |
| 33 | Start прошёл раннюю проверку до флага, приостановлен перед записью; флаг поставлен | 503; в потоке и в desired его нет | `—` | — | — | e2c8986 running | e2c8986 running | `e2c8986` | ✅ |
| 34 | Перезапуск агента на границе обработки: Start прочитан, не подтверждён | барьер держит (pending ≠ 0), после рестарта агент дочитывает; затем Stop succeeded, lag 0, pending 0 | `72b8c51` | — | — | e2c8986 running | e2c8986 running | `e2c8986` | ✅ |
| 35 | Конкурентный Start между последним наблюдением барьера и остановкой backend | 503 по обоим маршрутам (флаг поставлен на шаге 1), поток не растёт | `72b8c51` | — | — | e2c8986 running | e2c8986 running | `e2c8986` | ✅ |
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
==> Сервер получает production из GitHub
==> Выкладка ecf2162 на jb@stand, попытка 1792413250-2609516-a6eaf935
==> 2026-10-19T12:34:11+00:00 попытка 1792413250-2609516-a6eaf935, ревизия ecf2162, скрипт ограды 5c8720901ebf
ОШИБКА: первый запуск: нужен --t0-revision <ревизия, работающая сейчас>
!!! 2026-10-19T12:34:11+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:34:11+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1792413250-2609516-a6eaf935.log) ----
==> 2026-10-19T12:34:11+00:00 попытка 1792413250-2609516-a6eaf935, ревизия ecf2162, скрипт ограды 5c8720901ebf
ОШИБКА: первый запуск: нужен --t0-revision <ревизия, работающая сейчас>
!!! 2026-10-19T12:34:11+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:34:11+00:00 код выхода 1
---- образы сервисов ----
mentis-backend-1 mentis-backend:latest
mentis-agent-1 mentis-agent:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 2. `--t0-revision` не совпадает с последним дампом


```
==> Сервер получает production из GitHub
==> Выкладка ecf2162 на jb@stand, попытка 1792413257-2610452-63318fb4
==> 2026-10-19T12:34:19+00:00 попытка 1792413257-2610452-63318fb4, ревизия ecf2162, скрипт ограды 5c8720901ebf
==> T0: сверка bef05d5 с последним дампом и контейнерами
    последний дамп '/home/jb/backups/mentis-trading-agent/pre-deploy-app-567f4f8-20261019-123332.dump' не от ревизии bef05d5
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-19T12:34:19+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:34:19+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1792413257-2610452-63318fb4.log) ----
==> 2026-10-19T12:34:19+00:00 попытка 1792413257-2610452-63318fb4, ревизия ecf2162, скрипт ограды 5c8720901ebf
==> T0: сверка bef05d5 с последним дампом и контейнерами
    последний дамп '/home/jb/backups/mentis-trading-agent/pre-deploy-app-567f4f8-20261019-123332.dump' не от ревизии bef05d5
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-19T12:34:19+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:34:19+00:00 код выхода 1
---- образы сервисов ----
mentis-backend-1 mentis-backend:latest
mentis-agent-1 mentis-agent:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 3. Оба контейнера созданы раньше последнего дампа (выкладка T0 упала на сборке)


```
==> Сервер получает production из GitHub
==> Выкладка ecf2162 на jb@stand, попытка 1792413266-2611716-bdb8e73c
==> 2026-10-19T12:34:27+00:00 попытка 1792413266-2611716-bdb8e73c, ревизия ecf2162, скрипт ограды 5c8720901ebf
==> T0: сверка 567f4f8 с последним дампом и контейнерами
    ни один контейнер не создан после дампа (выкладка упала на сборке или шла без сборки?)
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-19T12:34:27+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:34:27+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1792413266-2611716-bdb8e73c.log) ----
==> 2026-10-19T12:34:27+00:00 попытка 1792413266-2611716-bdb8e73c, ревизия ecf2162, скрипт ограды 5c8720901ebf
==> T0: сверка 567f4f8 с последним дампом и контейнерами
    ни один контейнер не создан после дампа (выкладка упала на сборке или шла без сборки?)
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-19T12:34:27+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:34:27+00:00 код выхода 1
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
==> Сборка :ecf2162
compose-fault: build
!!! 2026-10-19T12:34:38+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:34:38+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1792413274-2612974-e7267f8f.log) ----
==> 2026-10-19T12:34:35+00:00 попытка 1792413274-2612974-e7267f8f, ревизия ecf2162, скрипт ограды 5c8720901ebf
==> T0: сверка 567f4f8 с последним дампом и контейнерами
    ни один контейнер не создан после дампа (выкладка упала на сборке или шла без сборки?)
    сверка не пройдена, но есть --t0-confirm (решение человека)
    T0 = 567f4f8: образы помечены, закреплён в releases/pinned
==> Работает backend 567f4f8: флаг — no, по releases/current 567f4f8 — no
    знание флага не доказано — backend будет остановлен до флага (шаг 0)
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Сборка :ecf2162
compose-fault: build
!!! 2026-10-19T12:34:38+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:34:38+00:00 код выхода 1
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
 Container mentis-backend-1 Recreated
 Container mentis-agent-1 Recreated
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-redis-1 Healthy
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
==> Готово: ecf2162
==> 2026-10-19T12:35:27+00:00 код выхода 0

==> Готово: ecf2162 развёрнут (попытка 1792413286-2614977-189452b8).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 6. 11 выкладок подряд (с первой), ротация образов

- ревизии: `1685632`, `19c9fea`, `081b08e`, `52f9561`, `8d2f95d`, `8937f4b`, `7b0118a`, `861ecee`, `ff59bed`, `72b8c51`
- образы после ротации: mentis-agent:081b08e, mentis-agent:1685632, mentis-agent:19c9fea, mentis-agent:52f9561, mentis-agent:567f4f8, mentis-agent:72b8c51, mentis-agent:7b0118a, mentis-agent:861ecee, mentis-agent:8937f4b, mentis-agent:8d2f95d, mentis-agent:ff59bed, mentis-agent:latest, mentis-backend:081b08e, mentis-backend:1685632, mentis-backend:19c9fea, mentis-backend:52f9561, mentis-backend:567f4f8, mentis-backend:72b8c51, mentis-backend:7b0118a, mentis-backend:861ecee, mentis-backend:8937f4b, mentis-backend:8d2f95d, mentis-backend:ff59bed, mentis-backend:latest

### 7. Сбой сборки цели


```
==> Выкладка e2c8986 на jb@stand, попытка 1792413816-2656664-2c8bcb0e
==> 2026-10-19T12:43:37+00:00 попытка 1792413816-2656664-2c8bcb0e, ревизия e2c8986, скрипт ограды 5c8720901ebf
==> Работает backend 72b8c51: флаг — yes, по releases/current 72b8c51 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Сборка :e2c8986
compose-fault: build
!!! 2026-10-19T12:43:39+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:43:39+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-e2c8986-1792413816-2656664-2c8bcb0e.log) ----
==> 2026-10-19T12:43:37+00:00 попытка 1792413816-2656664-2c8bcb0e, ревизия e2c8986, скрипт ограды 5c8720901ebf
==> Работает backend 72b8c51: флаг — yes, по releases/current 72b8c51 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Сборка :e2c8986
compose-fault: build
!!! 2026-10-19T12:43:39+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:43:39+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-redis-1 redis:7-alpine
mentis-db-1 postgres:18
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 8. Сбой после сборки, до флага (SET флага не прошёл)


```
#35 exporting attestation manifest sha256:42293751f6a6ae16e8b25d3b296fb440d6800e63db0f96ee620b75acbac5c42a 0.1s done
#35 exporting manifest list sha256:bb9ba280a86af6aeb0539ebd68e4b9dd855b44b57e596e8ab9e9f5ec10be23a0 0.0s done
#35 naming to docker.io/library/mentis-backend:e2c8986 done
#35 unpacking to docker.io/library/mentis-backend:e2c8986 0.0s done
#35 DONE 0.2s

#36 [agent] resolving provenance for metadata file
#36 DONE 0.0s

#37 [backend] resolving provenance for metadata file
#37 DONE 0.0s
 Image mentis-agent:e2c8986 Built
 Image mentis-backend:e2c8986 Built
==> Установка e2c8986 в /home/jb/apps/mentis-trading-agent
==> (1) флаг trading:start_fence
compose-fault: flag
ОШИБКА: флаг trading:start_fence не поставлен: нет ответа
!!! 2026-10-19T12:43:54+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:43:54+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-redis-1 redis:7-alpine
mentis-db-1 postgres:18
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 9. Сбой между остановкой агента и `up` (удаление каталога упало)


```
 Container mentis-agent-1 Stopping
 Container mentis-agent-1 Stopped
==> (3) удаление agent:strategies
compose-fault: catalog-del
!!! 2026-10-19T12:44:06+00:00 сбой в фазе stopped (код 1)
==> trap: поднимаю прежнего агента (контейнер и образ не тронуты)
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
    Образы сейчас:
    backend: mentis-backend:latest sha256:8e3ce1e38ce618e178987096771be8af7a64242d348af33922b7fe1941f182a5 running
    agent: mentis-agent:latest sha256:1e2469df516cb88234cceb70d0e7d822d3932904e3e19def26c1f251a421f716 running
    backend знает флаг, флаг trading:start_fence стоит — Start закрыт.
    Флаг trading:start_fence снимать — только после проверки, по runbook (docs/DEPLOY.md §6):
    повторный deploy.sh или redis-cli DEL trading:start_fence.
==> 2026-10-19T12:44:08+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-redis-1 redis:7-alpine
mentis-db-1 postgres:18
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 10. Сбой внутри `up` (частичный up)


```
 Container mentis-redis-1 Running
 Container mentis-agent-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
==> trap: mentis-backend:latest = образ контейнера backend (e2c8986)
    Backend мог быть пересоздан и применить миграции: при «новый backend +
    прежний агент» флаг снимается только повторным deploy.sh или решением человека.
    Образы сейчас:
    backend: mentis-backend:latest sha256:bb9ba280a86af6aeb0539ebd68e4b9dd855b44b57e596e8ab9e9f5ec10be23a0 running
    agent: mentis-agent:latest sha256:1e2469df516cb88234cceb70d0e7d822d3932904e3e19def26c1f251a421f716 running
    backend знает флаг, флаг trading:start_fence стоит — Start закрыт.
    Флаг trading:start_fence снимать — только после проверки, по runbook (docs/DEPLOY.md §6):
    повторный deploy.sh или redis-cli DEL trading:start_fence.
==> 2026-10-19T12:44:49+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-redis-1 redis:7-alpine
mentis-db-1 postgres:18
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 11. Агент цели не публикует каталог (поставлен на паузу после `up`)

- `deploy.sh` ждал 143 с

```
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Started
compose-fault: catalog-timeout
==> (5) ожидание каталога агента цели (до 120 с)
ОШИБКА: агент цели не опубликовал agent:strategies за 120 с
!!! 2026-10-19T12:47:14+00:00 сбой в фазе waiting (код 1)
==> trap: mentis-backend:latest = образ контейнера backend (e2c8986)
    Образы сейчас:
    backend: mentis-backend:latest sha256:bb9ba280a86af6aeb0539ebd68e4b9dd855b44b57e596e8ab9e9f5ec10be23a0 running
    agent: mentis-agent:latest sha256:a89de53bb447dbc88f7a358e82826271ab18ee44dcf8cbc93cd6c73c90060da0 paused
    backend знает флаг, флаг trading:start_fence стоит — Start закрыт.
    Флаг trading:start_fence снимать — только после проверки, по runbook (docs/DEPLOY.md §6):
    повторный deploy.sh или redis-cli DEL trading:start_fence.
==> 2026-10-19T12:47:16+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-redis-1 redis:7-alpine
mentis-db-1 postgres:18
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 12. Повторный `deploy.sh` той же ревизии после неудачи (таймаут шага (5))


```
 Container mentis-redis-1 Running
 Container mentis-db-1 Running
 Container mentis-backend-1 Running
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
    ротация: mentis-backend:1685632
    ротация: mentis-agent:1685632
==> Готово: e2c8986
==> 2026-10-19T12:47:48+00:00 код выхода 0

==> Готово: e2c8986 развёрнут (попытка 1792414046-2677482-34af3b33).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 13. Юнит убит `systemctl --user kill -s KILL` до записи `.exit`

- агент был остановлен на шаге (2) и не поднят: так и должно быть без trap; следующая выкладка поднимает его (сценарий 14)

```
 Container mentis-agent-1 Stopping
 Container mentis-agent-1 Stopped
==> (3) удаление agent:strategies
==> (4) :latest → :e2c8986 и up
compose-fault: slow-up
ОШИБКА: скрипт ограды завершился без кода (юнит: not-found/inactive, попытка '')
---- хвост лога ("$HOME"/apps/releases/deploy-e2c8986-1792414078-2680724-c3fd2bfe.log) ----
==> 2026-10-19T12:47:59+00:00 попытка 1792414078-2680724-c3fd2bfe, ревизия e2c8986, скрипт ограды 5c8720901ebf
==> Работает backend e2c8986: флаг — yes, по releases/current e2c8986 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Образы :e2c8986 уже есть — без сборки
==> Установка e2c8986 в /home/jb/apps/mentis-trading-agent
==> (1) флаг trading:start_fence
==> (2) остановка агента
 Container mentis-agent-1 Stopping
 Container mentis-agent-1 Stopped
==> (3) удаление agent:strategies
==> (4) :latest → :e2c8986 и up
compose-fault: slow-up
---- образы сервисов ----
mentis-backend-1 mentis-backend:latest
mentis-redis-1 redis:7-alpine
mentis-db-1 postgres:18
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 14. Обрыв SSH (kill локального `deploy.sh`) сразу после остановки агента

- попытка `1792414104-2683126-be3bb7b3`, код в `.exit`: 0

```
==> Сервер получает production из GitHub
==> Выкладка 72b8c51 на jb@stand, попытка 1792414104-2683126-be3bb7b3
==> 2026-10-19T12:48:25+00:00 попытка 1792414104-2683126-be3bb7b3, ревизия 72b8c51, скрипт ограды 5c8720901ebf
==> Работает backend e2c8986: флаг — yes, по releases/current e2c8986 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Образы :72b8c51 уже есть — без сборки
==> Установка 72b8c51 в /home/jb/apps/mentis-trading-agent
==> (1) флаг trading:start_fence
==> (2) остановка агента
 Container mentis-agent-1 Stopping
 Container mentis-agent-1 Stopped
==> (3) удаление agent:strategies
==> (4) :latest → :72b8c51 и up
compose-fault: slow-up
```

### 15. Два одновременных `deploy.sh` одной ревизии

- первый: код 0; логов попыток ревизии: 8
- ExecStart юнита: `ExecStart={ path=/usr/bin/bash ; argv[]=/usr/bin/bash /home/jb/apps/releases/deploy-fence-1792414148-2686541-9920d561.sh e2c8986 1792414148-2686541-9920d561 ; i`

```
 Container mentis-agent-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-backend-1 Recreated
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-backend-1 Started
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: e2c8986
==> 2026-10-19T12:50:05+00:00 код выхода 0

==> Готово: e2c8986 развёрнут (попытка 1792414148-2686541-9920d561).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 16. То же при прошлой неудачной попытке этой ревизии с `.exit` на диске


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
 Container mentis-redis-1 Healthy
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
==> Готово: 72b8c51
==> 2026-10-19T12:51:14+00:00 код выхода 0

==> Готово: 72b8c51 развёрнут (попытка 1792414220-2693329-52aca7a2).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 17. Второй `deploy.sh` той же ревизии с изменённым скриптом ограды, пока первый жив

- маркер: `скрипт ограды 5c8720901ebf`, хеш первого `5c8720901ebf`

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
 Container mentis-agent-1 Starting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-backend-1 Started
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: e2c8986
==> 2026-10-19T12:52:19+00:00 код выхода 0

==> Готово: e2c8986 развёрнут (попытка 1792414282-2698800-b8f63df9).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 18. `deploy.sh` другой ревизии, пока идёт выкладка


```
==> Сервер получает production из GitHub
==> Выкладка e2c8986 на jb@stand, попытка 1792414354-2705546-9efa3564
ОШИБКА: идёт выкладка ревизии 72b8c51 (попытка 1792414347-2704581-ec4da52e); дождитесь её конца
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
==> Работает backend 72b8c51: флаг — yes, по releases/current 72b8c51 — yes
    цель 608cfa1 без исполнителя, а исполнитель мог работать (агент: yes, 72b8c51: yes) — барьер исполнения (шаг 2a)
    backend цели 608cfa1 не знает флага: до запуска его проверяет изолированный экземпляр, запуск — последний шаг
==> Снапшот БД (app, agent)
==> Проверка контракта цели
open /home/jb/apps/releases/stage-1792414422-2710935-59bf0715/tree/compose.prod.yml: no such file or directory
!!! 2026-10-19T12:53:45+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:53:45+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-608cfa1-1792414422-2710935-59bf0715.log) ----
==> 2026-10-19T12:53:43+00:00 попытка 1792414422-2710935-59bf0715, ревизия 608cfa1, скрипт ограды 5c8720901ebf
==> Работает backend 72b8c51: флаг — yes, по releases/current 72b8c51 — yes
    цель 608cfa1 без исполнителя, а исполнитель мог работать (агент: yes, 72b8c51: yes) — барьер исполнения (шаг 2a)
    backend цели 608cfa1 не знает флага: до запуска его проверяет изолированный экземпляр, запуск — последний шаг
==> Снапшот БД (app, agent)
==> Проверка контракта цели
open /home/jb/apps/releases/stage-1792414422-2710935-59bf0715/tree/compose.prod.yml: no such file or directory
!!! 2026-10-19T12:53:45+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:53:45+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-redis-1 redis:7-alpine
mentis-db-1 postgres:18
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 21. Цель без образов и без tracked `uv.lock`


```
==> Сервер получает production из GitHub
==> Выкладка c26ff56 на jb@stand, попытка 1792414433-2712359-17ff4193
==> 2026-10-19T12:53:54+00:00 попытка 1792414433-2712359-17ff4193, ревизия c26ff56, скрипт ограды 5c8720901ebf
==> Работает backend 72b8c51: флаг — yes, по releases/current 72b8c51 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: у ревизии c26ff56 нет tracked uv.lock и нет releases/c26ff56.uv.lock — lock текущего дерева не подставляется
!!! 2026-10-19T12:53:55+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:53:55+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-c26ff56-1792414433-2712359-17ff4193.log) ----
==> 2026-10-19T12:53:54+00:00 попытка 1792414433-2712359-17ff4193, ревизия c26ff56, скрипт ограды 5c8720901ebf
==> Работает backend 72b8c51: флаг — yes, по releases/current 72b8c51 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: у ревизии c26ff56 нет tracked uv.lock и нет releases/c26ff56.uv.lock — lock текущего дерева не подставляется
!!! 2026-10-19T12:53:55+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:53:55+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-redis-1 redis:7-alpine
mentis-db-1 postgres:18
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 22. Шина менеджера пользователя недоступна дольше 30 с


```
==> Сервер получает production из GitHub
==> Выкладка 72b8c51 на jb@stand, попытка 1792414443-2713781-caa94e50
ОШИБКА: состояние юнита mentis-deploy неизвестно (нет ответа менеджера за 30 с).
    Выкладка не запускалась повторно; проверьте на сервере: systemctl --user status mentis-deploy
```

### 23. Шина недоступна 8 с во время ожидания исхода


```
==> (4) :latest → :72b8c51 и up
compose-fault: slow-up
 Container mentis-redis-1 Running
 Container mentis-db-1 Running
 Container mentis-backend-1 Running
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: 72b8c51
==> 2026-10-19T12:55:17+00:00 код выхода 0

==> Готово: 72b8c51 развёрнут (попытка 1792414480-2715384-a4ce1a06).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 24. Загруженный юнит `mentis-deploy` без `DEPLOY_ATTEMPT` (R109)


```
==> Сервер получает production из GitHub
==> Выкладка 72b8c51 на jb@stand, попытка 1792414525-2718771-755bfedd
ОШИБКА: метаданные попытки отсутствуют или некорректны (юнит mentis-deploy):
    LoadState=loaded ActiveState=active DEPLOY_ATTEMPT='' DEPLOY_REVISION=''
    Не подключаюсь и не запускаю; разберитесь с юнитом на сервере.
```

### 25. `systemd-run` отказал, юнита нет (R106)


```
==> Сервер получает production из GitHub
==> Выкладка 72b8c51 на jb@stand, попытка 1792414530-2719458-fa3c7496
ОШИБКА: юнит mentis-deploy отсутствует: запуск не подтверждён либо попытка уже выгружена.
    systemd-run: systemd-run-fault: refused, nothing started
    Непроверенная справка (releases/deploy.current): 72b8c51 1792414480-2715384-a4ce1a06
```

### 26. `systemd-run` вернул ошибку, а юнит работает со своей попыткой (R102)


```
==> (3) удаление agent:strategies
==> (4) :latest → :72b8c51 и up
 Container mentis-db-1 Running
 Container mentis-redis-1 Running
 Container mentis-backend-1 Running
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: 72b8c51
==> 2026-10-19T12:55:53+00:00 код выхода 0

==> Готово: 72b8c51 развёрнут (попытка 1792414537-2720337-2d6fe046).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 25b. `systemd-run` отказал, а юнит выгружен между снимками S0 и S1 (R106)


```
==> Сервер получает production из GitHub
==> Выкладка 72b8c51 на jb@stand, попытка 1792414558-2722869-ea2930af
ОШИБКА: юнит mentis-deploy отсутствует: запуск не подтверждён либо попытка уже выгружена.
    systemd-run: systemd-run-fault: a unit ran and was collected
    Непроверенная справка (releases/deploy.current): 72b8c51 1792414537-2720337-2d6fe046
```

### 21b. Цель без сервиса `agent` в compose-файлах

- одноразовый коммит: в compose.yml сервис agent переименован

```
==> Сервер получает stand/throwaway-targets из GitHub
==> Выкладка e093615 на jb@stand, попытка 1792414565-2723677-9ea1a3d3
==> 2026-10-19T12:56:06+00:00 попытка 1792414565-2723677-9ea1a3d3, ревизия e093615, скрипт ограды 5c8720901ebf
==> Работает backend 72b8c51: флаг — yes, по releases/current 72b8c51 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: в compose-файлах цели нет сервиса agent
!!! 2026-10-19T12:56:07+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:56:07+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-e093615-1792414565-2723677-9ea1a3d3.log) ----
==> 2026-10-19T12:56:06+00:00 попытка 1792414565-2723677-9ea1a3d3, ревизия e093615, скрипт ограды 5c8720901ebf
==> Работает backend 72b8c51: флаг — yes, по releases/current 72b8c51 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: в compose-файлах цели нет сервиса agent
!!! 2026-10-19T12:56:07+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:56:07+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-redis-1 redis:7-alpine
mentis-db-1 postgres:18
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 21c. Образ агента цели не `mentis-agent:latest`

- одноразовый коммит: образ агента `mentis-other-agent`

```
==> Сервер получает stand/throwaway-targets из GitHub
==> Выкладка 26fb242 на jb@stand, попытка 1792414576-2725003-4548c8d9
==> 2026-10-19T12:56:17+00:00 попытка 1792414576-2725003-4548c8d9, ревизия 26fb242, скрипт ограды 5c8720901ebf
==> Работает backend 72b8c51: флаг — yes, по releases/current 72b8c51 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: образ agent цели без MENTIS_TAG — не mentis-agent:latest
!!! 2026-10-19T12:56:18+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:56:18+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-26fb242-1792414576-2725003-4548c8d9.log) ----
==> 2026-10-19T12:56:17+00:00 попытка 1792414576-2725003-4548c8d9, ревизия 26fb242, скрипт ограды 5c8720901ebf
==> Работает backend 72b8c51: флаг — yes, по releases/current 72b8c51 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: образ agent цели без MENTIS_TAG — не mentis-agent:latest
!!! 2026-10-19T12:56:18+00:00 сбой в фазе before (код 1)
==> 2026-10-19T12:56:18+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-redis-1 redis:7-alpine
mentis-db-1 postgres:18
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 27. Start во время выкладки (между флагом и каталогом цели), оба маршрута; Stop

- ответ: {'detail': 'starting is closed for maintenance'}
- EVAL с ключом флага за время двух Start: 0

```
 Container mentis-agent-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-backend-1 Recreated
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-redis-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-backend-1 Started
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: e2c8986
==> 2026-10-19T12:57:14+00:00 код выхода 0

==> Готово: e2c8986 развёрнут (попытка 1792414587-2726516-73646a2c).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 28. Барьер доставки: Start в потоке при остановленном агенте, затем Stop

- XINFO при остановленном агенте: name agent-workers consumers 1 pending 0 last-delivered-id 1792414595435-0 entries-read 1 lag 2

### 30. Подготовленная, но не запущенная попытка (пауза клиента) переживает чужую выкладку (R108)

- попытка `1792414661-2732037-9fb5fa6f` ждала перед systemd-run, пока шла выкладка `e2c8986`

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
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-db-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-agent-1 Started
 Container mentis-backend-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: 72b8c51
==> 2026-10-19T12:58:33+00:00 код выхода 0

==> Готово: 72b8c51 развёрнут (попытка 1792414661-2732037-9fb5fa6f).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 31. Одновременные подготовки одной ревизии, запуск одновременно (R103)

- compose.yml: 8ab2ab9e968a → 8ab2ab9e968a; образы :latest: bb9ba280a86a,a89de53bb447 → bb9ba280a86a,a89de53bb447
- логов попыток этих ревизий всего: 11

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
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-agent-1 Started
 Container mentis-backend-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: e2c8986
==> 2026-10-19T12:59:27+00:00 код выхода 0

==> Готово: e2c8986 развёрнут (попытка 1792414720-2737872-a593d9f9).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 32. Одновременные подготовки двух ревизий, запуск одновременно (R103)

- compose.yml: 8ab2ab9e968a → 8ab2ab9e968a; образы :latest: bb9ba280a86a,a89de53bb447 → bb9ba280a86a,a89de53bb447
- логов попыток этих ревизий всего: 18

```
==> (4) :latest → :e2c8986 и up
compose-fault: slow-up
 Container mentis-redis-1 Running
 Container mentis-db-1 Running
 Container mentis-backend-1 Running
 Container mentis-db-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: e2c8986
==> 2026-10-19T13:00:19+00:00 код выхода 0

==> Готово: e2c8986 развёрнут (попытка 1792414780-2743586-50a6a534).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 33. Start прошёл раннюю проверку до флага, приостановлен перед записью; флаг поставлен

- Удержание — хук стенда (`tests/deploy/stand/hooks/sitecustomize.py`, override `stand-hooks.yml`) перед `_apply_command`; отказ дал Lua-скрипт записи (`COMMIT_RUNTIME_LUA`), код продукта не менялся.
- ответ: {'detail': 'starting is closed for maintenance'}; EVAL с ключом флага после снятия паузы: 1; MONITOR слушал через 1.9 с, строк: 21

### 34. Перезапуск агента на границе обработки: Start прочитан, не подтверждён

- после чтения за агента: pending=1, lag=1

### 35. Конкурентный Start между последним наблюдением барьера и остановкой backend


### 29. Откат на T0 по runbook после всех выкладок


```
 Container mentis-redis-1 Started
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 1f6a4457d33cfc28b2157c13183147a9882086f2a0620c241435f100dbe497e4
==> Приёмка backend цели в изолированном экземпляре mentis-deploy-probe-1792414896-2753001-890b69c4 (до 120 с)
 Container mentis-deploy-probe-1792414896-2753001-890b69c4 Creating
 Container mentis-deploy-probe-1792414896-2753001-890b69c4 Created
==> Каталог проверяет backend цели
==> (6) запуск backend цели (он не знает флага: с этого момента Start открыт)
 Container mentis-backend-1 Recreate
 Container mentis-backend-1 Recreated
 Container mentis-backend-1 Starting
 Container mentis-backend-1 Started
    ротация: mentis-backend:19c9fea
    ротация: mentis-agent:19c9fea
==> Готово: 567f4f8
==> 2026-10-19T13:02:13+00:00 код выхода 0

==> Готово: 567f4f8 развёрнут (попытка 1792414896-2753001-890b69c4).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```
