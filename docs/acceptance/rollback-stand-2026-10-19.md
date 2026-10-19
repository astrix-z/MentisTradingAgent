# PRESET-09a: приёмка выкладки на реальном стеке

Прогон 2026-10-19 04:50 – 05:20 UTC, скрипты выкладки из `e2c8986`. Стенд — контейнер с systemd (PID 1), sshd и своим Docker (`tests/deploy/stand/`); `deploy.sh` ходит в него по SSH (`DEPLOY_SSH`), скрипт ограды работает в юните `mentis-deploy` пользователя с linger. Отказы вносит сам стенд: `~/bin/compose-fault` (это `DEPLOY_COMPOSE` юнита), `~/bin/systemd-run`, chmod шины пользователя, `loginctl disable-linger`, `systemctl --user kill`, kill локального `deploy.sh`. Воспроизвести: `python3 tests/deploy/stand/run-matrix.py --out <файл>`.

Итог: **38 из 39** сценариев дали ожидаемый исход.

Состояние снято сразу после сценария: флаг `trading:start_fence`, образы (ревизии по тегам) и состояние контейнеров backend и agent, `releases/current`.

| # | Сценарий | Ожидаемо | Ревизия | Код `deploy.sh` | Флаг | backend | agent | current | Итог |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Первый запуск без `--t0-revision` | отказ до шага (1) | `ecf2162` | 1 | — | 1d2f623bcde5 running | fc3dbfdd6045 running | `—` | ✅ |
| 2 | `--t0-revision` не совпадает с последним дампом | отказ сверки T0 | `ecf2162` | 1 | — | 1d2f623bcde5 running | fc3dbfdd6045 running | `—` | ✅ |
| 3 | Оба контейнера созданы раньше последнего дампа (выкладка T0 упала на сборке) | отказ сверки T0 | `ecf2162` | 1 | — | 1d2f623bcde5 running | fc3dbfdd6045 running | `—` | ✅ |
| 4 | То же с `--t0-confirm` (сборка цели затем падает) | T0 закреплён решением человека; сбой сборки — ни флага, ни остановки агента | `ecf2162` | 1 | — | 567f4f8 running | 567f4f8 running | `567f4f8` | ✅ |
| 5 | Корректная выкладка T0: агент не пересоздавался (образ из кеша), backend — после дампа | сверка пройдена, T0 закреплён, выкладка первой ревизии | `ecf2162` | 0 | — | ecf2162 running | ecf2162 running | `ecf2162` | ✅ |
| 6 | 11 выкладок подряд (с первой), ротация образов | образы T0 на месте (pinned), ротация оставляет 10 ревизий журнала | `abb8a83` | — | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 7 | Сбой сборки цели | ни флага, ни остановки агента; APP_DIR — работающая ревизия | `72b8c51` | 1 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 8 | Сбой после сборки, до флага (SET флага не прошёл) | образы цели собраны, `:latest` — работающая ревизия, флага нет | `72b8c51` | 1 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 9 | Сбой между остановкой агента и `up` (удаление каталога упало) | `trap` поднял прежнего агента, его каталог опубликован, флаг стоит, печатает, что делать | `72b8c51` | 1 | deploy 72b8c51 1792386162-2210759-19390608 2026-10-19T05:02:45+00:00 | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 10 | Сбой внутри `up` (частичный up) | работает агент из `releases/current`, `mentis-backend:latest` = работающий backend, флаг стоит | `72b8c51` | 1 | deploy 72b8c51 1792386179-2213137-1018501d 2026-10-19T05:03:02+00:00 | 72b8c51 running | abb8a83 running | `abb8a83` | ✅ |
| 11 | Агент цели не публикует каталог (поставлен на паузу после `up`) | ожидание 2 мин, отказ, флаг стоит, агент цели запущен | `72b8c51` | 1 | deploy 72b8c51 1792386217-2217025-98db42ac 2026-10-19T05:03:40+00:00 | 72b8c51 running | 72b8c51 paused | `abb8a83` | ✅ |
| 12 | Повторный `deploy.sh` той же ревизии после неудачи (таймаут шага (5)) | новая попытка, старый `.exit` не читается, флаг снимает только новый успех | `72b8c51` | 0 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 13 | Юнит убит `systemctl --user kill -s KILL` до записи `.exit` | `deploy.sh`: «завершился без кода», не ждёт до таймаута, флаг стоит | `72b8c51` | 1 | deploy 72b8c51 1792386396-2231112-1ecc29e6 2026-10-19T05:06:39+00:00 | 72b8c51 running | 72b8c51 exited | `72b8c51` | ✅ |
| 14 | Обрыв SSH (kill локального `deploy.sh`) сразу после остановки агента | скрипт на сервере доходит до конца; лог и код выхода в `releases/` | `abb8a83` | -9 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 15 | Два одновременных `deploy.sh` одной ревизии | один юнит, второй подключается к его логу; оба получают исход одной попытки | `72b8c51` | 0 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 16 | То же при прошлой неудачной попытке этой ревизии с `.exit` на диске | ни один не сообщает её код; оба получают исход новой попытки | `abb8a83` | 0 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 17 | Второй `deploy.sh` той же ревизии с изменённым скриптом ограды, пока первый жив | первый дорабатывает свою версию (маркер в логе), второй подключается | `72b8c51` | 0 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 18 | `deploy.sh` другой ревизии, пока идёт выкладка | отказ, ничего не меняя; идущая выкладка завершается | `72b8c51` | 1 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 19 | Linger выключен у пользователя SSH | отказ до шага (1): ни флага, ни остановки агента | `abb8a83` | 1 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 20 | Цель старше `608cfa1` (агент не публикует каталог) | отказ до шага (1) | `c0c8cf3` | 1 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 21a | Цель без `compose.prod.yml` (`608cfa1`) | отказ на проверке контракта, до шага (1) | `608cfa1` | 1 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 21 | Цель без образов и без tracked `uv.lock` | отказ, lock текущего дерева не подставляется | `c26ff56` | 1 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 22 | Шина менеджера пользователя недоступна дольше 30 с | «состояние неизвестно», юнит не запускается | `abb8a83` | 1 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 23 | Шина недоступна 8 с во время ожидания исхода | повтор чтения; после восстановления — та же попытка, без нового запуска | `abb8a83` | 0 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 24 | Загруженный юнит `mentis-deploy` без `DEPLOY_ATTEMPT` (R109) | «метаданные попытки отсутствуют или некорректны», без подключения и запуска | `abb8a83` | 1 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 25 | `systemd-run` отказал, юнита нет (R106) | «юнит отсутствует: запуск не подтверждён…», stderr `systemd-run`, G4 не запускается | `abb8a83` | 1 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 26 | `systemd-run` вернул ошибку, а юнит работает со своей попыткой (R102) | `deploy.sh` ждёт свою попытку и получает её исход | `abb8a83` | 0 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 25b | `systemd-run` отказал, а юнит выгружен между снимками S0 и S1 (R106) | «юнит отсутствует…», G4 не запускается | `abb8a83` | 1 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 21b | Цель без сервиса `agent` в compose-файлах | отказ до шага (1) | `2deec0d` | 1 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 21c | Образ агента цели не `mentis-agent:latest` | отказ до шага (1) | `a428b7a` | 1 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 27 | Start во время выкладки (между флагом и каталогом цели), оба маршрута; Stop | Start — 503 «starting is closed for maintenance»; Stop принят | `72b8c51` | 0 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 28 | Барьер доставки: Start в потоке при остановленном агенте, затем Stop | барьер не проходит, пока агент не обработал обе команды; после — Stop succeeded, lag 0, pending 0, idle | `abb8a83` | — | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 30 | Подготовленная, но не запущенная попытка (пауза клиента) переживает чужую выкладку (R108) | её stage и копия скрипта сохранены; её запуск затем проходит штатно | `abb8a83` | 0 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 31 | Одновременные подготовки одной ревизии, запуск одновременно (R103) | один юнит; вторая подключается | `72b8c51/72b8c51` | 0 | — | 72b8c51 running | 72b8c51 running | `72b8c51` | ✅ |
| 32 | Одновременные подготовки двух ревизий, запуск одновременно (R103) | общее состояние меняет один юнит; другая — отказ до любых записей | `abb8a83/72b8c51` | 1 | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 33 | Start прошёл раннюю проверку до флага, приостановлен перед записью; флаг поставлен | 503; в потоке и в desired его нет | `—` | — | — | abb8a83 running | abb8a83 running | `abb8a83` | ❌ |
| 34 | Перезапуск агента на границе обработки: Start прочитан, не подтверждён | барьер держит (pending ≠ 0), после рестарта агент дочитывает; затем Stop succeeded, lag 0, pending 0 | `abb8a83` | — | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 35 | Конкурентный Start между последним наблюдением барьера и остановкой backend | 503 по обоим маршрутам (флаг поставлен на шаге 1), поток не растёт | `abb8a83` | — | — | abb8a83 running | abb8a83 running | `abb8a83` | ✅ |
| 29 | Откат на T0 по runbook после всех выкладок | без сборки, на закреплённых образах; alembic = ревизия T0; в APP_DIR нет чужих миграций; каталог T0 | `567f4f8` | 0 | — | 567f4f8 running | 567f4f8 running | `567f4f8` | ✅ |

## Соответствие приёмке карточки PRESET-09a

Каждый пункт раздела «Приёмка (локальный стек)» карточки и сценарии этого журнала, которые на него отвечают. Общий gate (ruff, mypy, biome, tsc, pytest, Playwright) — вне стенда, его прогон указан в комментарии задачи.

| Пункт приёмки | Сценарии | Итог |
|---|---|---|
| Во время deploy Start по обоим маршрутам → 503 за один EVAL (код -1, без цикла); Stop при флаге принимается и пишет снапшот | 27, 33 | ❌ |
| Start, прошедший проверку до флага и приостановленный перед записью, после флага → 503, в потоке и в desired его нет | 33 | ❌ |
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
==> Выкладка ecf2162 на jb@stand, попытка 1792385585-2160342-cd877dd3
==> 2026-10-19T04:53:06+00:00 попытка 1792385585-2160342-cd877dd3, ревизия ecf2162, скрипт ограды 5c8720901ebf
ОШИБКА: первый запуск: нужен --t0-revision <ревизия, работающая сейчас>
!!! 2026-10-19T04:53:06+00:00 сбой в фазе before (код 1)
==> 2026-10-19T04:53:06+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1792385585-2160342-cd877dd3.log) ----
==> 2026-10-19T04:53:06+00:00 попытка 1792385585-2160342-cd877dd3, ревизия ecf2162, скрипт ограды 5c8720901ebf
ОШИБКА: первый запуск: нужен --t0-revision <ревизия, работающая сейчас>
!!! 2026-10-19T04:53:06+00:00 сбой в фазе before (код 1)
==> 2026-10-19T04:53:06+00:00 код выхода 1
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
==> Выкладка ecf2162 на jb@stand, попытка 1792385592-2161275-934846c7
==> 2026-10-19T04:53:13+00:00 попытка 1792385592-2161275-934846c7, ревизия ecf2162, скрипт ограды 5c8720901ebf
==> T0: сверка bef05d5 с последним дампом и контейнерами
    последний дамп '/home/jb/backups/mentis-trading-agent/pre-deploy-app-567f4f8-20261019-045227.dump' не от ревизии bef05d5
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-19T04:53:14+00:00 сбой в фазе before (код 1)
==> 2026-10-19T04:53:14+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1792385592-2161275-934846c7.log) ----
==> 2026-10-19T04:53:13+00:00 попытка 1792385592-2161275-934846c7, ревизия ecf2162, скрипт ограды 5c8720901ebf
==> T0: сверка bef05d5 с последним дампом и контейнерами
    последний дамп '/home/jb/backups/mentis-trading-agent/pre-deploy-app-567f4f8-20261019-045227.dump' не от ревизии bef05d5
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-19T04:53:14+00:00 сбой в фазе before (код 1)
==> 2026-10-19T04:53:14+00:00 код выхода 1
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
==> Выкладка ecf2162 на jb@stand, попытка 1792385600-2162549-604460d9
==> 2026-10-19T04:53:21+00:00 попытка 1792385600-2162549-604460d9, ревизия ecf2162, скрипт ограды 5c8720901ebf
==> T0: сверка 567f4f8 с последним дампом и контейнерами
    ни один контейнер не создан после дампа (выкладка упала на сборке или шла без сборки?)
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-19T04:53:22+00:00 сбой в фазе before (код 1)
==> 2026-10-19T04:53:22+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1792385600-2162549-604460d9.log) ----
==> 2026-10-19T04:53:21+00:00 попытка 1792385600-2162549-604460d9, ревизия ecf2162, скрипт ограды 5c8720901ebf
==> T0: сверка 567f4f8 с последним дампом и контейнерами
    ни один контейнер не создан после дампа (выкладка упала на сборке или шла без сборки?)
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-19T04:53:22+00:00 сбой в фазе before (код 1)
==> 2026-10-19T04:53:22+00:00 код выхода 1
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
!!! 2026-10-19T04:53:32+00:00 сбой в фазе before (код 1)
==> 2026-10-19T04:53:32+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1792385608-2163804-60ea181d.log) ----
==> 2026-10-19T04:53:29+00:00 попытка 1792385608-2163804-60ea181d, ревизия ecf2162, скрипт ограды 5c8720901ebf
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
!!! 2026-10-19T04:53:32+00:00 сбой в фазе before (код 1)
==> 2026-10-19T04:53:32+00:00 код выхода 1
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
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
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
==> 2026-10-19T04:54:20+00:00 код выхода 0

==> Готово: ecf2162 развёрнут (попытка 1792385620-2165794-e1d811bc).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 6. 11 выкладок подряд (с первой), ротация образов

- ревизии: `1685632`, `5be53d7`, `081b08e`, `52f9561`, `8d2f95d`, `2562f2e`, `de7ff2b`, `861ecee`, `21acd51`, `abb8a83`
- образы после ротации: mentis-agent:081b08e, mentis-agent:1685632, mentis-agent:21acd51, mentis-agent:2562f2e, mentis-agent:52f9561, mentis-agent:567f4f8, mentis-agent:5be53d7, mentis-agent:861ecee, mentis-agent:8d2f95d, mentis-agent:abb8a83, mentis-agent:de7ff2b, mentis-agent:latest, mentis-backend:081b08e, mentis-backend:1685632, mentis-backend:21acd51, mentis-backend:2562f2e, mentis-backend:52f9561, mentis-backend:567f4f8, mentis-backend:5be53d7, mentis-backend:861ecee, mentis-backend:8d2f95d, mentis-backend:abb8a83, mentis-backend:de7ff2b, mentis-backend:latest

### 7. Сбой сборки цели


```
==> Выкладка 72b8c51 на jb@stand, попытка 1792386136-2207057-f55c4627
==> 2026-10-19T05:02:17+00:00 попытка 1792386136-2207057-f55c4627, ревизия 72b8c51, скрипт ограды 5c8720901ebf
==> Работает backend abb8a83: флаг — yes, по releases/current abb8a83 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Сборка :72b8c51
compose-fault: build
!!! 2026-10-19T05:02:19+00:00 сбой в фазе before (код 1)
==> 2026-10-19T05:02:19+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-72b8c51-1792386136-2207057-f55c4627.log) ----
==> 2026-10-19T05:02:17+00:00 попытка 1792386136-2207057-f55c4627, ревизия 72b8c51, скрипт ограды 5c8720901ebf
==> Работает backend abb8a83: флаг — yes, по releases/current abb8a83 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Сборка :72b8c51
compose-fault: build
!!! 2026-10-19T05:02:19+00:00 сбой в фазе before (код 1)
==> 2026-10-19T05:02:19+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-redis-1 redis:7-alpine
mentis-db-1 postgres:18
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 8. Сбой после сборки, до флага (SET флага не прошёл)


```
#35 exporting attestation manifest sha256:67adddeef519cdb48331a8f46dea7d05c2b3a3f6121ef19c69528e91dd508e3c 0.0s done
#35 exporting manifest list sha256:ee0067d848e89fbe1a3f2050e3ebb61ed8974929377618a8b1a9207b8587c22d 0.0s done
#35 naming to docker.io/library/mentis-agent:72b8c51 0.0s done
#35 unpacking to docker.io/library/mentis-agent:72b8c51 0.0s done
#35 DONE 0.1s

#36 [agent] resolving provenance for metadata file
#36 DONE 0.0s

#37 [backend] resolving provenance for metadata file
#37 DONE 0.0s
 Image mentis-agent:72b8c51 Built
 Image mentis-backend:72b8c51 Built
==> Установка 72b8c51 в /home/jb/apps/mentis-trading-agent
==> (1) флаг trading:start_fence
compose-fault: flag
ОШИБКА: флаг trading:start_fence не поставлен: нет ответа
!!! 2026-10-19T05:02:33+00:00 сбой в фазе before (код 1)
==> 2026-10-19T05:02:33+00:00 код выхода 1
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
!!! 2026-10-19T05:02:46+00:00 сбой в фазе stopped (код 1)
==> trap: поднимаю прежнего агента (контейнер и образ не тронуты)
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
    Образы сейчас:
    backend: mentis-backend:latest sha256:4c7c4e6c1ad5677c6e052186696ae3fa1e7cdef0deccf080f86de35f95436896 running
    agent: mentis-agent:latest sha256:63809b8ee1eae03c4b281d375c527f842625386b797396f8ad3b3cbffb844889 running
    backend знает флаг, флаг trading:start_fence стоит — Start закрыт.
    Флаг trading:start_fence снимать — только после проверки, по runbook (docs/DEPLOY.md §6):
    повторный deploy.sh или redis-cli DEL trading:start_fence.
==> 2026-10-19T05:02:48+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-redis-1 redis:7-alpine
mentis-db-1 postgres:18
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 10. Сбой внутри `up` (частичный up)


```
 Container mentis-db-1 Running
 Container mentis-agent-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
==> trap: mentis-backend:latest = образ контейнера backend (72b8c51)
    Backend мог быть пересоздан и применить миграции: при «новый backend +
    прежний агент» флаг снимается только повторным deploy.sh или решением человека.
    Образы сейчас:
    backend: mentis-backend:latest sha256:f2938e6568d05eaf7f5c245fc8d25572374c26ac15dbf91fe60cc3ebbd36f628 running
    agent: mentis-agent:latest sha256:63809b8ee1eae03c4b281d375c527f842625386b797396f8ad3b3cbffb844889 running
    backend знает флаг, флаг trading:start_fence стоит — Start закрыт.
    Флаг trading:start_fence снимать — только после проверки, по runbook (docs/DEPLOY.md §6):
    повторный deploy.sh или redis-cli DEL trading:start_fence.
==> 2026-10-19T05:03:28+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-redis-1 redis:7-alpine
mentis-db-1 postgres:18
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 11. Агент цели не публикует каталог (поставлен на паузу после `up`)

- `deploy.sh` ждал 142 с

```
 Container mentis-db-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Started
compose-fault: catalog-timeout
==> (5) ожидание каталога агента цели (до 120 с)
ОШИБКА: агент цели не опубликовал agent:strategies за 120 с
!!! 2026-10-19T05:05:53+00:00 сбой в фазе waiting (код 1)
==> trap: mentis-backend:latest = образ контейнера backend (72b8c51)
    Образы сейчас:
    backend: mentis-backend:latest sha256:f2938e6568d05eaf7f5c245fc8d25572374c26ac15dbf91fe60cc3ebbd36f628 running
    agent: mentis-agent:latest sha256:ee0067d848e89fbe1a3f2050e3ebb61ed8974929377618a8b1a9207b8587c22d paused
    backend знает флаг, флаг trading:start_fence стоит — Start закрыт.
    Флаг trading:start_fence снимать — только после проверки, по runbook (docs/DEPLOY.md §6):
    повторный deploy.sh или redis-cli DEL trading:start_fence.
==> 2026-10-19T05:05:54+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-redis-1 redis:7-alpine
mentis-db-1 postgres:18
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 12. Повторный `deploy.sh` той же ревизии после неудачи (таймаут шага (5))


```
 Container mentis-db-1 Running
 Container mentis-redis-1 Running
 Container mentis-backend-1 Running
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
    ротация: mentis-backend:1685632
    ротация: mentis-agent:1685632
==> Готово: 72b8c51
==> 2026-10-19T05:06:26+00:00 код выхода 0

==> Готово: 72b8c51 развёрнут (попытка 1792386365-2227894-d75a1fef).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 13. Юнит убит `systemctl --user kill -s KILL` до записи `.exit`

- агент был остановлен на шаге (2) и не поднят: так и должно быть без trap; следующая выкладка поднимает его (сценарий 14)

```
 Container mentis-agent-1 Stopping
 Container mentis-agent-1 Stopped
==> (3) удаление agent:strategies
==> (4) :latest → :72b8c51 и up
compose-fault: slow-up
ОШИБКА: скрипт ограды завершился без кода (юнит: not-found/inactive, попытка '')
---- хвост лога ("$HOME"/apps/releases/deploy-72b8c51-1792386396-2231112-1ecc29e6.log) ----
==> 2026-10-19T05:06:37+00:00 попытка 1792386396-2231112-1ecc29e6, ревизия 72b8c51, скрипт ограды 5c8720901ebf
==> Работает backend 72b8c51: флаг — yes, по releases/current 72b8c51 — yes
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
---- образы сервисов ----
mentis-backend-1 mentis-backend:latest
mentis-redis-1 redis:7-alpine
mentis-db-1 postgres:18
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 14. Обрыв SSH (kill локального `deploy.sh`) сразу после остановки агента

- попытка `1792386422-2233501-3c4abc59`, код в `.exit`: 0

```
==> Сервер получает production из GitHub
==> Выкладка abb8a83 на jb@stand, попытка 1792386422-2233501-3c4abc59
==> 2026-10-19T05:07:03+00:00 попытка 1792386422-2233501-3c4abc59, ревизия abb8a83, скрипт ограды 5c8720901ebf
==> Работает backend 72b8c51: флаг — yes, по releases/current 72b8c51 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Образы :abb8a83 уже есть — без сборки
==> Установка abb8a83 в /home/jb/apps/mentis-trading-agent
==> (1) флаг trading:start_fence
==> (2) остановка агента
 Container mentis-agent-1 Stopping
 Container mentis-agent-1 Stopped
==> (3) удаление agent:strategies
==> (4) :latest → :abb8a83 и up
compose-fault: slow-up
```

### 15. Два одновременных `deploy.sh` одной ревизии

- первый: код 0; логов попыток ревизии: 8
- ExecStart юнита: `ExecStart={ path=/usr/bin/bash ; argv[]=/usr/bin/bash /home/jb/apps/releases/deploy-fence-1792386466-2236917-763c92b2.sh 72b8c51 1792386466-2236917-763c92b2 ; i`

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
 Container mentis-backend-1 Started
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: 72b8c51
==> 2026-10-19T05:08:43+00:00 код выхода 0

==> Готово: 72b8c51 развёрнут (попытка 1792386466-2236917-763c92b2).
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
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-backend-1 Starting
 Container mentis-agent-1 Started
 Container mentis-backend-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: abb8a83
==> 2026-10-19T05:09:49+00:00 код выхода 0

==> Готово: abb8a83 развёрнут (попытка 1792386538-2243705-6c078ee3).
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
==> Готово: 72b8c51
==> 2026-10-19T05:10:52+00:00 код выхода 0

==> Готово: 72b8c51 развёрнут (попытка 1792386596-2249018-85c7f72d).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 18. `deploy.sh` другой ревизии, пока идёт выкладка


```
==> Сервер получает production из GitHub
==> Выкладка 72b8c51 на jb@stand, попытка 1792386667-2255630-25c4a528
ОШИБКА: идёт выкладка ревизии abb8a83 (попытка 1792386660-2254682-cd4db474); дождитесь её конца
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
==> Работает backend abb8a83: флаг — yes, по releases/current abb8a83 — yes
    цель 608cfa1 без исполнителя, а исполнитель мог работать (агент: yes, abb8a83: yes) — барьер исполнения (шаг 2a)
    backend цели 608cfa1 не знает флага: до запуска его проверяет изолированный экземпляр, запуск — последний шаг
==> Снапшот БД (app, agent)
==> Проверка контракта цели
open /home/jb/apps/releases/stage-1792386735-2261125-448807da/tree/compose.prod.yml: no such file or directory
!!! 2026-10-19T05:12:18+00:00 сбой в фазе before (код 1)
==> 2026-10-19T05:12:18+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-608cfa1-1792386735-2261125-448807da.log) ----
==> 2026-10-19T05:12:16+00:00 попытка 1792386735-2261125-448807da, ревизия 608cfa1, скрипт ограды 5c8720901ebf
==> Работает backend abb8a83: флаг — yes, по releases/current abb8a83 — yes
    цель 608cfa1 без исполнителя, а исполнитель мог работать (агент: yes, abb8a83: yes) — барьер исполнения (шаг 2a)
    backend цели 608cfa1 не знает флага: до запуска его проверяет изолированный экземпляр, запуск — последний шаг
==> Снапшот БД (app, agent)
==> Проверка контракта цели
open /home/jb/apps/releases/stage-1792386735-2261125-448807da/tree/compose.prod.yml: no such file or directory
!!! 2026-10-19T05:12:18+00:00 сбой в фазе before (код 1)
==> 2026-10-19T05:12:18+00:00 код выхода 1
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
==> Выкладка c26ff56 на jb@stand, попытка 1792386746-2262507-d4c9bbdb
==> 2026-10-19T05:12:27+00:00 попытка 1792386746-2262507-d4c9bbdb, ревизия c26ff56, скрипт ограды 5c8720901ebf
==> Работает backend abb8a83: флаг — yes, по releases/current abb8a83 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: у ревизии c26ff56 нет tracked uv.lock и нет releases/c26ff56.uv.lock — lock текущего дерева не подставляется
!!! 2026-10-19T05:12:28+00:00 сбой в фазе before (код 1)
==> 2026-10-19T05:12:28+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-c26ff56-1792386746-2262507-d4c9bbdb.log) ----
==> 2026-10-19T05:12:27+00:00 попытка 1792386746-2262507-d4c9bbdb, ревизия c26ff56, скрипт ограды 5c8720901ebf
==> Работает backend abb8a83: флаг — yes, по releases/current abb8a83 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: у ревизии c26ff56 нет tracked uv.lock и нет releases/c26ff56.uv.lock — lock текущего дерева не подставляется
!!! 2026-10-19T05:12:28+00:00 сбой в фазе before (код 1)
==> 2026-10-19T05:12:28+00:00 код выхода 1
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
==> Выкладка abb8a83 на jb@stand, попытка 1792386756-2263970-32fed668
ОШИБКА: состояние юнита mentis-deploy неизвестно (нет ответа менеджера за 30 с).
    Выкладка не запускалась повторно; проверьте на сервере: systemctl --user status mentis-deploy
```

### 23. Шина недоступна 8 с во время ожидания исхода


```
==> (4) :latest → :abb8a83 и up
compose-fault: slow-up
 Container mentis-db-1 Running
 Container mentis-redis-1 Running
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
==> Готово: abb8a83
==> 2026-10-19T05:13:50+00:00 код выхода 0

==> Готово: abb8a83 развёрнут (попытка 1792386793-2265595-2eaca5b2).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 24. Загруженный юнит `mentis-deploy` без `DEPLOY_ATTEMPT` (R109)


```
==> Сервер получает production из GitHub
==> Выкладка abb8a83 на jb@stand, попытка 1792386835-2268869-935207f1
ОШИБКА: метаданные попытки отсутствуют или некорректны (юнит mentis-deploy):
    LoadState=loaded ActiveState=active DEPLOY_ATTEMPT='' DEPLOY_REVISION=''
    Не подключаюсь и не запускаю; разберитесь с юнитом на сервере.
```

### 25. `systemd-run` отказал, юнита нет (R106)


```
==> Сервер получает production из GitHub
==> Выкладка abb8a83 на jb@stand, попытка 1792386840-2269615-5ddf7956
ОШИБКА: юнит mentis-deploy отсутствует: запуск не подтверждён либо попытка уже выгружена.
    systemd-run: systemd-run-fault: refused, nothing started
    Непроверенная справка (releases/deploy.current): abb8a83 1792386793-2265595-2eaca5b2
```

### 26. `systemd-run` вернул ошибку, а юнит работает со своей попыткой (R102)


```
==> (3) удаление agent:strategies
==> (4) :latest → :abb8a83 и up
 Container mentis-db-1 Running
 Container mentis-redis-1 Running
 Container mentis-backend-1 Running
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: abb8a83
==> 2026-10-19T05:14:24+00:00 код выхода 0

==> Готово: abb8a83 развёрнут (попытка 1792386846-2270414-0c3efe65).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 25b. `systemd-run` отказал, а юнит выгружен между снимками S0 и S1 (R106)


```
==> Сервер получает production из GitHub
==> Выкладка abb8a83 на jb@stand, попытка 1792386871-2273105-c937fd78
ОШИБКА: юнит mentis-deploy отсутствует: запуск не подтверждён либо попытка уже выгружена.
    systemd-run: systemd-run-fault: a unit ran and was collected
    Непроверенная справка (releases/deploy.current): abb8a83 1792386846-2270414-0c3efe65
```

### 21b. Цель без сервиса `agent` в compose-файлах

- одноразовый коммит: в compose.yml сервис agent переименован

```
==> Сервер получает stand/throwaway-targets из GitHub
==> Выкладка 2deec0d на jb@stand, попытка 1792386878-2273921-40eae509
==> 2026-10-19T05:14:39+00:00 попытка 1792386878-2273921-40eae509, ревизия 2deec0d, скрипт ограды 5c8720901ebf
==> Работает backend abb8a83: флаг — yes, по releases/current abb8a83 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: в compose-файлах цели нет сервиса agent
!!! 2026-10-19T05:14:40+00:00 сбой в фазе before (код 1)
==> 2026-10-19T05:14:40+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-2deec0d-1792386878-2273921-40eae509.log) ----
==> 2026-10-19T05:14:39+00:00 попытка 1792386878-2273921-40eae509, ревизия 2deec0d, скрипт ограды 5c8720901ebf
==> Работает backend abb8a83: флаг — yes, по releases/current abb8a83 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: в compose-файлах цели нет сервиса agent
!!! 2026-10-19T05:14:40+00:00 сбой в фазе before (код 1)
==> 2026-10-19T05:14:40+00:00 код выхода 1
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
==> Выкладка a428b7a на jb@stand, попытка 1792386888-2275264-705bcaf7
==> 2026-10-19T05:14:49+00:00 попытка 1792386888-2275264-705bcaf7, ревизия a428b7a, скрипт ограды 5c8720901ebf
==> Работает backend abb8a83: флаг — yes, по releases/current abb8a83 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: образ agent цели без MENTIS_TAG — не mentis-agent:latest
!!! 2026-10-19T05:14:50+00:00 сбой в фазе before (код 1)
==> 2026-10-19T05:14:51+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-a428b7a-1792386888-2275264-705bcaf7.log) ----
==> 2026-10-19T05:14:49+00:00 попытка 1792386888-2275264-705bcaf7, ревизия a428b7a, скрипт ограды 5c8720901ebf
==> Работает backend abb8a83: флаг — yes, по releases/current abb8a83 — yes
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: образ agent цели без MENTIS_TAG — не mentis-agent:latest
!!! 2026-10-19T05:14:50+00:00 сбой в фазе before (код 1)
==> 2026-10-19T05:14:51+00:00 код выхода 1
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
 Container mentis-backend-1 Recreate
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
 Container mentis-backend-1 Started
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: 72b8c51
==> 2026-10-19T05:15:46+00:00 код выхода 0

==> Готово: 72b8c51 развёрнут (попытка 1792386899-2276772-d1043267).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 28. Барьер доставки: Start в потоке при остановленном агенте, затем Stop

- XINFO при остановленном агенте: name agent-workers consumers 1 pending 0 last-delivered-id 1792386907270-0 entries-read 1 lag 2

### 30. Подготовленная, но не запущенная попытка (пауза клиента) переживает чужую выкладку (R108)

- попытка `1792386972-2282276-fc09cd28` ждала перед systemd-run, пока шла выкладка `72b8c51`

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
==> Готово: abb8a83
==> 2026-10-19T05:17:04+00:00 код выхода 0

==> Готово: abb8a83 развёрнут (попытка 1792386972-2282276-fc09cd28).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 31. Одновременные подготовки одной ревизии, запуск одновременно (R103)

- compose.yml: 8ab2ab9e968a → 8ab2ab9e968a; образы :latest: f2938e6568d0,ee0067d848e8 → f2938e6568d0,ee0067d848e8
- логов попыток этих ревизий всего: 11

```
 Container mentis-backend-1 Recreate
 Container mentis-agent-1 Recreated
 Container mentis-backend-1 Recreated
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-backend-1 Started
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: 72b8c51
==> 2026-10-19T05:17:57+00:00 код выхода 0

==> Готово: 72b8c51 развёрнут (попытка 1792387030-2288086-f65a511c).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 32. Одновременные подготовки двух ревизий, запуск одновременно (R103)

- compose.yml: 8ab2ab9e968a → 8ab2ab9e968a; образы :latest: 4c7c4e6c1ad5,63809b8ee1ea → 4c7c4e6c1ad5,63809b8ee1ea
- логов попыток этих ревизий всего: 18

```
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-redis-1 Waiting
 Container mentis-redis-1 Healthy
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-backend-1 Starting
 Container mentis-db-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-backend-1 Started
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: abb8a83
==> 2026-10-19T05:18:53+00:00 код выхода 0

==> Готово: abb8a83 развёрнут (попытка 1792387090-2293738-5fa54583).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
==> Сервер получает production из GitHub
==> Выкладка 72b8c51 на jb@stand, попытка 1792387090-2293740-2a8516ee
ОШИБКА: идёт выкладка ревизии abb8a83 (попытка 1792387090-2293738-5fa54583); дождитесь её конца
```

### 33. Start прошёл раннюю проверку до флага, приостановлен перед записью; флаг поставлен

- Удержание — хук стенда (`tests/deploy/stand/hooks/sitecustomize.py`, override `stand-hooks.yml`) перед `_apply_command`; отказ дал Lua-скрипт записи (`COMMIT_RUNTIME_LUA`), код продукта не менялся.
- ответ: {'detail': 'starting is closed for maintenance'}; EVAL с ключом флага после снятия паузы: 0
- НЕ ВЫПОЛНЕНО: один EVAL записи (код -1), без повторов

### 34. Перезапуск агента на границе обработки: Start прочитан, не подтверждён

- после чтения за агента: pending=1, lag=1

### 35. Конкурентный Start между последним наблюдением барьера и остановкой backend


### 29. Откат на T0 по runbook после всех выкладок


```
 Container mentis-redis-1 Started
 Container mentis-redis-1 Waiting
 Container mentis-db-1 Waiting
 Container mentis-db-1 Healthy
 Container mentis-redis-1 Healthy
 Container mentis-agent-1 Starting
 Container mentis-agent-1 Started
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 1f6a4457d33cfc28b2157c13183147a9882086f2a0620c241435f100dbe497e4
==> Приёмка backend цели в изолированном экземпляре mentis-deploy-probe-1792387210-2303381-2144ed26 (до 120 с)
 Container mentis-deploy-probe-1792387210-2303381-2144ed26 Creating
 Container mentis-deploy-probe-1792387210-2303381-2144ed26 Created
==> Каталог проверяет backend цели
==> (6) запуск backend цели (он не знает флага: с этого момента Start открыт)
 Container mentis-backend-1 Recreate
 Container mentis-backend-1 Recreated
 Container mentis-backend-1 Starting
 Container mentis-backend-1 Started
    ротация: mentis-backend:5be53d7
    ротация: mentis-agent:5be53d7
==> Готово: 567f4f8
==> 2026-10-19T05:20:47+00:00 код выхода 0

==> Готово: 567f4f8 развёрнут (попытка 1792387210-2303381-2144ed26).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```
