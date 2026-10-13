# PRESET-09a: приёмка выкладки на реальном стеке

Прогон 2026-10-07 06:07 – 06:38 UTC, скрипты выкладки — рабочее дерево ветки `feat/deploy-server-fetch` поверх `61ed815` (сервер получает код fetch из «GitHub» стенда). Стенд — контейнер с systemd (PID 1), sshd и своим Docker (`tests/deploy/stand/`); `deploy.sh` ходит в него по SSH (`DEPLOY_SSH`), скрипт ограды работает в юните `mentis-deploy` пользователя с linger. Отказы вносит сам стенд: `~/bin/compose-fault` (это `DEPLOY_COMPOSE` юнита), `~/bin/systemd-run`, chmod шины пользователя, `loginctl disable-linger`, `systemctl --user kill`, kill локального `deploy.sh`. Воспроизвести: `python3 tests/deploy/stand/run-matrix.py --out <файл>`.

Итог: **39 из 39** сценариев дали ожидаемый исход.

Состояние снято сразу после сценария: флаг `trading:start_fence`, образы (ревизии по тегам) и состояние контейнеров backend и agent, `releases/current`.

| # | Сценарий | Ожидаемо | Ревизия | Код `deploy.sh` | Флаг | backend | agent | current | Итог |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Первый запуск без `--t0-revision` | отказ до шага (1) | `ecf2162` | 1 | — | bf86af82c3f1 running | 69e03119ceeb running | `—` | ✅ |
| 2 | `--t0-revision` не совпадает с последним дампом | отказ сверки T0 | `ecf2162` | 1 | — | bf86af82c3f1 running | 69e03119ceeb running | `—` | ✅ |
| 3 | Оба контейнера созданы раньше последнего дампа (выкладка T0 упала на сборке) | отказ сверки T0 | `ecf2162` | 1 | — | bf86af82c3f1 running | 69e03119ceeb running | `—` | ✅ |
| 4 | То же с `--t0-confirm` (сборка цели затем падает) | T0 закреплён решением человека; сбой сборки — ни флага, ни остановки агента | `ecf2162` | 1 | — | 567f4f8 running | 567f4f8 running | `567f4f8` | ✅ |
| 5 | Корректная выкладка T0: агент не пересоздавался (образ из кеша), backend — после дампа | сверка пройдена, T0 закреплён, выкладка первой ревизии | `ecf2162` | 0 | — | ecf2162 running | ecf2162 running | `ecf2162` | ✅ |
| 6 | 11 выкладок подряд (с первой), ротация образов | образы T0 на месте (pinned), ротация оставляет 10 ревизий журнала | `ac52218` | — | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 7 | Сбой сборки цели | ни флага, ни остановки агента; APP_DIR — работающая ревизия | `a9d4647` | 1 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 8 | Сбой после сборки, до флага (SET флага не прошёл) | образы цели собраны, `:latest` — работающая ревизия, флага нет | `a9d4647` | 1 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 9 | Сбой между остановкой агента и `up` (удаление каталога упало) | `trap` поднял прежнего агента, его каталог опубликован, флаг стоит, печатает, что делать | `a9d4647` | 1 | deploy a9d4647 1791354045-2847685-59183376 2026-10-07T06:20:47+00:00 | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 10 | Сбой внутри `up` (частичный up) | работает агент из `releases/current`, `mentis-backend:latest` = работающий backend, флаг стоит | `a9d4647` | 1 | deploy a9d4647 1791354063-2849998-2b131067 2026-10-07T06:21:05+00:00 | a9d4647 running | ac52218 running | `ac52218` | ✅ |
| 11 | Агент цели не публикует каталог (поставлен на паузу после `up`) | ожидание 2 мин, отказ, флаг стоит, агент цели запущен | `a9d4647` | 1 | deploy a9d4647 1791354099-2853490-a71c2dfb 2026-10-07T06:21:41+00:00 | a9d4647 running | a9d4647 paused | `ac52218` | ✅ |
| 12 | Повторный `deploy.sh` той же ревизии после неудачи (таймаут шага (5)) | новая попытка, старый `.exit` не читается, флаг снимает только новый успех | `a9d4647` | 0 | — | a9d4647 running | a9d4647 running | `a9d4647` | ✅ |
| 13 | Юнит убит `systemctl --user kill -s KILL` до записи `.exit` | `deploy.sh`: «завершился без кода», не ждёт до таймаута, флаг стоит | `a9d4647` | 1 | deploy a9d4647 1791354276-2867995-f5c880d7 2026-10-07T06:24:38+00:00 | a9d4647 running | a9d4647 exited | `a9d4647` | ✅ |
| 14 | Обрыв SSH (kill локального `deploy.sh`) сразу после остановки агента | скрипт на сервере доходит до конца; лог и код выхода в `releases/` | `ac52218` | -9 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 15 | Два одновременных `deploy.sh` одной ревизии | один юнит, второй подключается к его логу; оба получают исход одной попытки | `a9d4647` | 0 | — | a9d4647 running | a9d4647 running | `a9d4647` | ✅ |
| 16 | То же при прошлой неудачной попытке этой ревизии с `.exit` на диске | ни один не сообщает её код; оба получают исход новой попытки | `ac52218` | 0 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 17 | Второй `deploy.sh` той же ревизии с изменённым скриптом ограды, пока первый жив | первый дорабатывает свою версию (маркер в логе), второй подключается | `a9d4647` | 0 | — | a9d4647 running | a9d4647 running | `a9d4647` | ✅ |
| 18 | `deploy.sh` другой ревизии, пока идёт выкладка | отказ, ничего не меняя; идущая выкладка завершается | `a9d4647` | 1 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 19 | Linger выключен у пользователя SSH | отказ до шага (1): ни флага, ни остановки агента | `ac52218` | 1 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 20 | Цель старше `608cfa1` (агент не публикует каталог) | отказ до шага (1) | `c0c8cf3` | 1 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 21a | Цель без `compose.prod.yml` (`608cfa1`) | отказ на проверке контракта, до шага (1) | `608cfa1` | 1 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 21 | Цель без образов и без tracked `uv.lock` | отказ, lock текущего дерева не подставляется | `c26ff56` | 1 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 22 | Шина менеджера пользователя недоступна дольше 30 с | «состояние неизвестно», юнит не запускается | `ac52218` | 1 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 23 | Шина недоступна 8 с во время ожидания исхода | повтор чтения; после восстановления — та же попытка, без нового запуска | `ac52218` | 0 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 24 | Загруженный юнит `mentis-deploy` без `DEPLOY_ATTEMPT` (R109) | «метаданные попытки отсутствуют или некорректны», без подключения и запуска | `ac52218` | 1 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 25 | `systemd-run` отказал, юнита нет (R106) | «юнит отсутствует: запуск не подтверждён…», stderr `systemd-run`, G4 не запускается | `ac52218` | 1 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 26 | `systemd-run` вернул ошибку, а юнит работает со своей попыткой (R102) | `deploy.sh` ждёт свою попытку и получает её исход | `ac52218` | 0 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 25b | `systemd-run` отказал, а юнит выгружен между снимками S0 и S1 (R106) | «юнит отсутствует…», G4 не запускается | `ac52218` | 1 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 21b | Цель без сервиса `agent` в compose-файлах | отказ до шага (1) | `616b6f8` | 1 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 21c | Образ агента цели не `mentis-agent:latest` | отказ до шага (1) | `1bd70d5` | 1 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 27 | Start во время выкладки (между флагом и каталогом цели), оба маршрута; Stop | Start — 503 «starting is closed for maintenance»; Stop принят | `a9d4647` | 0 | — | a9d4647 running | a9d4647 running | `a9d4647` | ✅ |
| 28 | Барьер доставки: Start в потоке при остановленном агенте, затем Stop | барьер не проходит, пока агент не обработал обе команды; после — Stop succeeded, lag 0, pending 0, idle | `ac52218` | — | — | a9d4647 running | a9d4647 running | `a9d4647` | ✅ |
| 30 | Подготовленная, но не запущенная попытка (пауза клиента) переживает чужую выкладку (R108) | её stage и копия скрипта сохранены; её запуск затем проходит штатно | `ac52218` | 0 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 31 | Одновременные подготовки одной ревизии, запуск одновременно (R103) | один юнит; вторая подключается | `a9d4647/a9d4647` | 0 | — | a9d4647 running | a9d4647 running | `a9d4647` | ✅ |
| 32 | Одновременные подготовки двух ревизий, запуск одновременно (R103) | общее состояние меняет один юнит; другая — отказ до любых записей | `ac52218/a9d4647` | 1 | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 33 | Start прошёл раннюю проверку до флага, приостановлен перед записью; флаг поставлен | 503; в потоке и в desired его нет | `—` | — | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 34 | Перезапуск агента на границе обработки: Start прочитан, не подтверждён | барьер держит (pending ≠ 0), после рестарта агент дочитывает; затем Stop succeeded, lag 0, pending 0 | `ac52218` | — | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
| 35 | Конкурентный Start между последним наблюдением барьера и остановкой backend | 503 по обоим маршрутам (флаг поставлен на шаге 1), поток не растёт | `ac52218` | — | — | ac52218 running | ac52218 running | `ac52218` | ✅ |
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
==> Выкладка ecf2162 на jb@stand, попытка 1791353518-2798142-fb3598a2
==> 2026-10-07T06:11:59+00:00 попытка 1791353518-2798142-fb3598a2, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
ОШИБКА: первый запуск: нужен --t0-revision <ревизия, работающая сейчас>
!!! 2026-10-07T06:11:59+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:11:59+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1791353518-2798142-fb3598a2.log) ----
==> 2026-10-07T06:11:59+00:00 попытка 1791353518-2798142-fb3598a2, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
ОШИБКА: первый запуск: нужен --t0-revision <ревизия, работающая сейчас>
!!! 2026-10-07T06:11:59+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:11:59+00:00 код выхода 1
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
==> Выкладка ecf2162 на jb@stand, попытка 1791353525-2799191-a02fda4f
==> 2026-10-07T06:12:06+00:00 попытка 1791353525-2799191-a02fda4f, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
==> T0: сверка bef05d5 с последним дампом и контейнерами
    последний дамп '/home/jb/backups/mentis-trading-agent/pre-deploy-app-567f4f8-20261007-061124.dump' не от ревизии bef05d5
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-07T06:12:07+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:12:07+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1791353525-2799191-a02fda4f.log) ----
==> 2026-10-07T06:12:06+00:00 попытка 1791353525-2799191-a02fda4f, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
==> T0: сверка bef05d5 с последним дампом и контейнерами
    последний дамп '/home/jb/backups/mentis-trading-agent/pre-deploy-app-567f4f8-20261007-061124.dump' не от ревизии bef05d5
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-07T06:12:07+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:12:07+00:00 код выхода 1
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
==> Выкладка ecf2162 на jb@stand, попытка 1791353534-2800544-034ec782
==> 2026-10-07T06:12:15+00:00 попытка 1791353534-2800544-034ec782, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
==> T0: сверка 567f4f8 с последним дампом и контейнерами
    ни один контейнер не создан после дампа (выкладка упала на сборке или шла без сборки?)
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-07T06:12:15+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:12:15+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1791353534-2800544-034ec782.log) ----
==> 2026-10-07T06:12:15+00:00 попытка 1791353534-2800544-034ec782, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
==> T0: сверка 567f4f8 с последним дампом и контейнерами
    ни один контейнер не создан после дампа (выкладка упала на сборке или шла без сборки?)
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-07T06:12:15+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:12:15+00:00 код выхода 1
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
!!! 2026-10-07T06:12:26+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:12:26+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1791353542-2801860-6490cb47.log) ----
==> 2026-10-07T06:12:23+00:00 попытка 1791353542-2801860-6490cb47, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
==> T0: сверка 567f4f8 с последним дампом и контейнерами
    ни один контейнер не создан после дампа (выкладка упала на сборке или шла без сборки?)
    сверка не пройдена, но есть --t0-confirm (решение человека)
    T0 = 567f4f8: образы помечены, закреплён в releases/pinned
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Сборка :ecf2162
compose-fault: build
!!! 2026-10-07T06:12:26+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:12:26+00:00 код выхода 1
---- образы сервисов ----
mentis-backend-1 mentis-backend:latest
mentis-agent-1 mentis-agent:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 5. Корректная выкладка T0: агент не пересоздавался (образ из кеша), backend — после дампа


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
 Container mentis-backend-1 Starting
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
 Container mentis-backend-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: ecf2162
==> 2026-10-07T06:13:10+00:00 код выхода 0

==> Готово: ecf2162 развёрнут (попытка 1791353556-2803873-af790966).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 6. 11 выкладок подряд (с первой), ротация образов

- ревизии: `8a95eac`, `d5d0dc4`, `b2c4e25`, `9c064a0`, `9ab56d7`, `e06aac8`, `398aa38`, `4e571f2`, `18fae0c`, `ac52218`
- образы после ротации: mentis-agent:18fae0c, mentis-agent:398aa38, mentis-agent:4e571f2, mentis-agent:567f4f8, mentis-agent:8a95eac, mentis-agent:9ab56d7, mentis-agent:9c064a0, mentis-agent:ac52218, mentis-agent:b2c4e25, mentis-agent:d5d0dc4, mentis-agent:e06aac8, mentis-agent:latest, mentis-backend:18fae0c, mentis-backend:398aa38, mentis-backend:4e571f2, mentis-backend:567f4f8, mentis-backend:8a95eac, mentis-backend:9ab56d7, mentis-backend:9c064a0, mentis-backend:ac52218, mentis-backend:b2c4e25, mentis-backend:d5d0dc4, mentis-backend:e06aac8, mentis-backend:latest

### 7. Сбой сборки цели


```
==> Сервер получает production из GitHub
==> Выкладка a9d4647 на jb@stand, попытка 1791353968-2841218-62f208dd
==> 2026-10-07T06:19:29+00:00 попытка 1791353968-2841218-62f208dd, ревизия a9d4647, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Сборка :a9d4647
compose-fault: build
!!! 2026-10-07T06:19:31+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:19:31+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-a9d4647-1791353968-2841218-62f208dd.log) ----
==> 2026-10-07T06:19:29+00:00 попытка 1791353968-2841218-62f208dd, ревизия a9d4647, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Сборка :a9d4647
compose-fault: build
!!! 2026-10-07T06:19:31+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:19:31+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 8. Сбой после сборки, до флага (SET флага не прошёл)


```
#35 exporting manifest sha256:2265841bc8fcd728555e717799c8a256cd0065274c3d64d663c76d191dfbb829 0.0s done
#35 exporting config sha256:6a996c8bdc2f9539a9c062b510520ec92eb4d7faafa70a6e7eca2a0976efe112 done
#35 exporting attestation manifest sha256:947e12273815efc524ee735437f1c54fac92422f050c3cf9aa848bc05a85dfe4 0.0s done
#35 exporting manifest list sha256:c1c03e7977c5747138da78c1c43c34d71172f23f493bef22ce92e06077dce60b 0.0s done
#35 naming to docker.io/library/mentis-backend:a9d4647 done
#35 unpacking to docker.io/library/mentis-backend:a9d4647
#35 unpacking to docker.io/library/mentis-backend:a9d4647 6.3s done
#35 DONE 25.8s

#37 [backend] resolving provenance for metadata file
#37 DONE 0.0s
 Image mentis-backend:a9d4647 Built
 Image mentis-agent:a9d4647 Built
==> Установка a9d4647 в /home/jb/apps/mentis-trading-agent
==> (1) флаг trading:start_fence
compose-fault: flag
ОШИБКА: флаг trading:start_fence не поставлен: нет ответа
!!! 2026-10-07T06:20:35+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:20:35+00:00 код выхода 1
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
!!! 2026-10-07T06:20:49+00:00 сбой в фазе stopped (код 1)
==> trap: поднимаю прежнего агента (контейнер и образ не тронуты)
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
    Образы сейчас:
    backend: mentis-backend:latest sha256:e6ed19f59408c7c40342930f69051886f5bb88fda0ef661e3a3e12a8b1600601 running
    agent: mentis-agent:latest sha256:d93fd6edbbd21cc14fa32f02ca3e88f5b36beefc5633a65fc494e04cce3c2817 running
    Флаг trading:start_fence остаётся (Start закрыт). Снять — только после проверки, по runbook
    (docs/DEPLOY.md §6): повторный deploy.sh или redis-cli DEL trading:start_fence.
==> 2026-10-07T06:20:51+00:00 код выхода 1
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
    backend: mentis-backend:latest sha256:c1c03e7977c5747138da78c1c43c34d71172f23f493bef22ce92e06077dce60b running
    agent: mentis-agent:latest sha256:d93fd6edbbd21cc14fa32f02ca3e88f5b36beefc5633a65fc494e04cce3c2817 running
    Флаг trading:start_fence остаётся (Start закрыт). Снять — только после проверки, по runbook
    (docs/DEPLOY.md §6): повторный deploy.sh или redis-cli DEL trading:start_fence.
==> 2026-10-07T06:21:30+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 11. Агент цели не публикует каталог (поставлен на паузу после `up`)

- `deploy.sh` ждал 141 с

```
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
compose-fault: catalog-timeout
==> (5) ожидание каталога агента цели (до 120 с)
ОШИБКА: агент цели не опубликовал agent:strategies за 120 с
!!! 2026-10-07T06:23:54+00:00 сбой в фазе waiting (код 1)
    Образы сейчас:
    backend: mentis-backend:latest sha256:c1c03e7977c5747138da78c1c43c34d71172f23f493bef22ce92e06077dce60b running
    agent: mentis-agent:latest sha256:290325f1f3b8199947ee39f15b99ad53eace214006359ba390c8afe379c3bc17 paused
    Флаг trading:start_fence остаётся (Start закрыт). Снять — только после проверки, по runbook
    (docs/DEPLOY.md §6): повторный deploy.sh или redis-cli DEL trading:start_fence.
==> 2026-10-07T06:23:55+00:00 код выхода 1
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
    ротация: mentis-backend:8a95eac
    ротация: mentis-agent:8a95eac
==> Готово: a9d4647
==> 2026-10-07T06:24:27+00:00 код выхода 0

==> Готово: a9d4647 развёрнут (попытка 1791354246-2864808-0520d4ab).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 13. Юнит убит `systemctl --user kill -s KILL` до записи `.exit`

- агент был остановлен на шаге (2) и не поднят: так и должно быть без trap; следующая выкладка поднимает его (сценарий 14)

```
==> (2) остановка агента
 Container mentis-agent-1 Stopping
 Container mentis-agent-1 Stopped
==> (3) удаление agent:strategies
==> (4) :latest → :a9d4647 и up
compose-fault: slow-up
ОШИБКА: скрипт ограды завершился без кода (юнит: not-found/inactive, попытка '')
---- хвост лога ("$HOME"/apps/releases/deploy-a9d4647-1791354276-2867995-f5c880d7.log) ----
==> 2026-10-07T06:24:37+00:00 попытка 1791354276-2867995-f5c880d7, ревизия a9d4647, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Образы :a9d4647 уже есть — без сборки
==> Установка a9d4647 в /home/jb/apps/mentis-trading-agent
==> (1) флаг trading:start_fence
==> (2) остановка агента
 Container mentis-agent-1 Stopping
 Container mentis-agent-1 Stopped
==> (3) удаление agent:strategies
==> (4) :latest → :a9d4647 и up
compose-fault: slow-up
---- образы сервисов ----
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 14. Обрыв SSH (kill локального `deploy.sh`) сразу после остановки агента

- попытка `1791354302-2870368-e1b3540e`, код в `.exit`: 0

```
==> Сервер получает production из GitHub
==> Выкладка ac52218 на jb@stand, попытка 1791354302-2870368-e1b3540e
==> 2026-10-07T06:25:03+00:00 попытка 1791354302-2870368-e1b3540e, ревизия ac52218, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Образы :ac52218 уже есть — без сборки
==> Установка ac52218 в /home/jb/apps/mentis-trading-agent
==> (1) флаг trading:start_fence
==> (2) остановка агента
 Container mentis-agent-1 Stopping
 Container mentis-agent-1 Stopped
==> (3) удаление agent:strategies
==> (4) :latest → :ac52218 и up
compose-fault: slow-up
```

### 15. Два одновременных `deploy.sh` одной ревизии

- первый: код 0; логов попыток ревизии: 8
- ExecStart юнита: `ExecStart={ path=/usr/bin/bash ; argv[]=/usr/bin/bash /home/jb/apps/releases/deploy-fence-1791354347-2873846-747c2c4f.sh a9d4647 1791354347-2873846-747c2c4f ; i`

```
 Container mentis-backend-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-backend-1 Recreated
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
 Container mentis-backend-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: a9d4647
==> 2026-10-07T06:26:43+00:00 код выхода 0

==> Готово: a9d4647 развёрнут (попытка 1791354347-2873846-747c2c4f).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 16. То же при прошлой неудачной попытке этой ревизии с `.exit` на диске


```
 Container mentis-backend-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-backend-1 Recreated
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-agent-1 Starting
 Container mentis-backend-1 Started
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: ac52218
==> 2026-10-07T06:27:49+00:00 код выхода 0

==> Готово: ac52218 развёрнут (попытка 1791354418-2880602-451d5ab1).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 17. Второй `deploy.sh` той же ревизии с изменённым скриптом ограды, пока первый жив

- маркер: `скрипт ограды 4f9aeddfb56d`, хеш первого `4f9aeddfb56d`

```
 Container mentis-agent-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-backend-1 Recreated
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-agent-1 Starting
 Container mentis-backend-1 Started
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: a9d4647
==> 2026-10-07T06:28:53+00:00 код выхода 0

==> Готово: a9d4647 развёрнут (попытка 1791354477-2886049-029627a6).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 18. `deploy.sh` другой ревизии, пока идёт выкладка


```
==> Сервер получает production из GitHub
==> Выкладка a9d4647 на jb@stand, попытка 1791354547-2892693-527617bc
ОШИБКА: идёт выкладка ревизии ac52218 (попытка 1791354540-2891812-bb6ee7b2); дождитесь её конца
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
==> Сервер получает production из GitHub
==> Выкладка 608cfa1 на jb@stand, попытка 1791354617-2898489-6b550cc3
==> 2026-10-07T06:30:18+00:00 попытка 1791354617-2898489-6b550cc3, ревизия 608cfa1, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
open /home/jb/apps/releases/stage-1791354617-2898489-6b550cc3/tree/compose.prod.yml: no such file or directory
!!! 2026-10-07T06:30:19+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:30:19+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-608cfa1-1791354617-2898489-6b550cc3.log) ----
==> 2026-10-07T06:30:18+00:00 попытка 1791354617-2898489-6b550cc3, ревизия 608cfa1, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
open /home/jb/apps/releases/stage-1791354617-2898489-6b550cc3/tree/compose.prod.yml: no such file or directory
!!! 2026-10-07T06:30:19+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:30:19+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 21. Цель без образов и без tracked `uv.lock`


```
==> Сервер получает production из GitHub
==> Выкладка c26ff56 на jb@stand, попытка 1791354627-2899802-aff02334
==> 2026-10-07T06:30:29+00:00 попытка 1791354627-2899802-aff02334, ревизия c26ff56, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: у ревизии c26ff56 нет tracked uv.lock и нет releases/c26ff56.uv.lock — lock текущего дерева не подставляется
!!! 2026-10-07T06:30:30+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:30:30+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-c26ff56-1791354627-2899802-aff02334.log) ----
==> 2026-10-07T06:30:29+00:00 попытка 1791354627-2899802-aff02334, ревизия c26ff56, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: у ревизии c26ff56 нет tracked uv.lock и нет releases/c26ff56.uv.lock — lock текущего дерева не подставляется
!!! 2026-10-07T06:30:30+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:30:30+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 22. Шина менеджера пользователя недоступна дольше 30 с


```
==> Сервер получает production из GitHub
==> Выкладка ac52218 на jb@stand, попытка 1791354639-2901278-f48aadc2
ОШИБКА: состояние юнита mentis-deploy неизвестно (нет ответа менеджера за 30 с).
    Выкладка не запускалась повторно; проверьте на сервере: systemctl --user status mentis-deploy
```

### 23. Шина недоступна 8 с во время ожидания исхода


```
==> (4) :latest → :ac52218 и up
compose-fault: slow-up
 Container mentis-db-1 Running
 Container mentis-redis-1 Running
 Container mentis-backend-1 Running
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
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
==> Готово: ac52218
==> 2026-10-07T06:31:52+00:00 код выхода 0

==> Готово: ac52218 развёрнут (попытка 1791354676-2903145-bc01eba3).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 24. Загруженный юнит `mentis-deploy` без `DEPLOY_ATTEMPT` (R109)


```
==> Сервер получает production из GitHub
==> Выкладка ac52218 на jb@stand, попытка 1791354717-2906395-0f0da374
ОШИБКА: метаданные попытки отсутствуют или некорректны (юнит mentis-deploy):
    LoadState=loaded ActiveState=active DEPLOY_ATTEMPT='' DEPLOY_REVISION=''
    Не подключаюсь и не запускаю; разберитесь с юнитом на сервере.
```

### 25. `systemd-run` отказал, юнита нет (R106)


```
==> Сервер получает production из GitHub
==> Выкладка ac52218 на jb@stand, попытка 1791354723-2907184-538dc11a
ОШИБКА: юнит mentis-deploy отсутствует: запуск не подтверждён либо попытка уже выгружена.
    systemd-run: systemd-run-fault: refused, nothing started
    Непроверенная справка (releases/deploy.current): ac52218 1791354676-2903145-bc01eba3
```

### 26. `systemd-run` вернул ошибку, а юнит работает со своей попыткой (R102)


```
==> (3) удаление agent:strategies
==> (4) :latest → :ac52218 и up
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
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: ac52218
==> 2026-10-07T06:32:26+00:00 код выхода 0

==> Готово: ac52218 развёрнут (попытка 1791354730-2908073-a1abe4a7).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 25b. `systemd-run` отказал, а юнит выгружен между снимками S0 и S1 (R106)


```
==> Сервер получает production из GitHub
==> Выкладка ac52218 на jb@stand, попытка 1791354752-2910547-bbbec2a3
ОШИБКА: юнит mentis-deploy отсутствует: запуск не подтверждён либо попытка уже выгружена.
    systemd-run: systemd-run-fault: a unit ran and was collected
    Непроверенная справка (releases/deploy.current): ac52218 1791354730-2908073-a1abe4a7
```

### 21b. Цель без сервиса `agent` в compose-файлах

- одноразовый коммит: в compose.yml сервис agent переименован

```
==> Сервер получает stand/throwaway-targets из GitHub
==> Выкладка 616b6f8 на jb@stand, попытка 1791354759-2911437-c11d26d4
==> 2026-10-07T06:32:40+00:00 попытка 1791354759-2911437-c11d26d4, ревизия 616b6f8, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: в compose-файлах цели нет сервиса agent
!!! 2026-10-07T06:32:41+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:32:41+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-616b6f8-1791354759-2911437-c11d26d4.log) ----
==> 2026-10-07T06:32:40+00:00 попытка 1791354759-2911437-c11d26d4, ревизия 616b6f8, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: в compose-файлах цели нет сервиса agent
!!! 2026-10-07T06:32:41+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:32:41+00:00 код выхода 1
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
==> Сервер получает stand/throwaway-targets из GitHub
==> Выкладка 1bd70d5 на jb@stand, попытка 1791354770-2912749-985b77e3
==> 2026-10-07T06:32:51+00:00 попытка 1791354770-2912749-985b77e3, ревизия 1bd70d5, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: образ agent цели без MENTIS_TAG — не mentis-agent:latest
!!! 2026-10-07T06:32:52+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:32:52+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-1bd70d5-1791354770-2912749-985b77e3.log) ----
==> 2026-10-07T06:32:51+00:00 попытка 1791354770-2912749-985b77e3, ревизия 1bd70d5, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: образ agent цели без MENTIS_TAG — не mentis-agent:latest
!!! 2026-10-07T06:32:52+00:00 сбой в фазе before (код 1)
==> 2026-10-07T06:32:52+00:00 код выхода 1
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
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
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
==> Готово: a9d4647
==> 2026-10-07T06:33:48+00:00 код выхода 0

==> Готово: a9d4647 развёрнут (попытка 1791354782-2914247-d4122337).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 28. Барьер доставки: Start в потоке при остановленном агенте, затем Stop

- XINFO при остановленном агенте: name agent-workers consumers 1 pending 0 last-delivered-id 1791354789913-0 entries-read 1 lag 2

### 30. Подготовленная, но не запущенная попытка (пауза клиента) переживает чужую выкладку (R108)

- попытка `1791354853-2919858-32b51a03` ждала перед systemd-run, пока шла выкладка `a9d4647`

```
 Container mentis-agent-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-backend-1 Recreated
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
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
==> Готово: ac52218
==> 2026-10-07T06:35:03+00:00 код выхода 0

==> Готово: ac52218 развёрнут (попытка 1791354853-2919858-32b51a03).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 31. Одновременные подготовки одной ревизии, запуск одновременно (R103)

- compose.yml: ce0ab0ae98ea → ce0ab0ae98ea; образы :latest: c1c03e7977c5,290325f1f3b8 → c1c03e7977c5,290325f1f3b8
- логов попыток этих ревизий всего: 11

```
 Container mentis-agent-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-backend-1 Recreated
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
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
==> Готово: a9d4647
==> 2026-10-07T06:35:57+00:00 код выхода 0

==> Готово: a9d4647 развёрнут (попытка 1791354911-2925520-4cfb9939).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 32. Одновременные подготовки двух ревизий, запуск одновременно (R103)

- compose.yml: ce0ab0ae98ea → ce0ab0ae98ea; образы :latest: e6ed19f59408,d93fd6edbbd2 → e6ed19f59408,d93fd6edbbd2
- логов попыток этих ревизий всего: 18

```
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
 Container mentis-backend-1 Started
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: ac52218
==> 2026-10-07T06:36:55+00:00 код выхода 0

==> Готово: ac52218 развёрнут (попытка 1791354969-2930905-f891cee2).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
==> Сервер получает production из GitHub
==> Выкладка a9d4647 на jb@stand, попытка 1791354969-2930907-377fb8e7
ОШИБКА: идёт выкладка ревизии ac52218 (попытка 1791354969-2930905-f891cee2); дождитесь её конца
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
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-redis-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-backend-1 Started
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 1f6a4457d33cfc28b2157c13183147a9882086f2a0620c241435f100dbe497e4
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
    ротация: mentis-backend:d5d0dc4
    ротация: mentis-agent:d5d0dc4
==> Готово: 567f4f8
==> 2026-10-07T06:38:26+00:00 код выхода 0

==> Готово: 567f4f8 развёрнут (попытка 1791355092-2940570-78534ad3).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```
