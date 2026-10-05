# PRESET-09a: приёмка выкладки на реальном стеке

Прогон 2026-10-05 15:41 – 16:05 UTC, скрипты выкладки из `3646458`. Стенд — контейнер с systemd (PID 1), sshd и своим Docker (`tests/deploy/stand/`); `deploy.sh` ходит в него по SSH (`DEPLOY_SSH`), скрипт ограды работает в юните `mentis-deploy` пользователя с linger. Отказы вносит сам стенд: `~/bin/compose-fault` (это `DEPLOY_COMPOSE` юнита), `~/bin/systemd-run`, chmod шины пользователя, `loginctl disable-linger`, `systemctl --user kill`, kill локального `deploy.sh`. Воспроизвести: `python3 tests/deploy/stand/run-matrix.py --out <файл>`.

Итог: **30 из 30** сценариев дали ожидаемый исход.

Состояние снято сразу после сценария: флаг `trading:start_fence`, образы (ревизии по тегам) и состояние контейнеров backend и agent, `releases/current`.

| # | Сценарий | Ожидаемо | Ревизия | Код `deploy.sh` | Флаг | backend | agent | current | Итог |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Первый запуск без `--t0-revision` | отказ до шага (1) | `ecf2162` | 1 | — | 42b8ea980f22 running | e20769790f9b running | `—` | ✅ |
| 2 | `--t0-revision` не совпадает с последним дампом | отказ сверки T0 | `ecf2162` | 1 | — | 42b8ea980f22 running | e20769790f9b running | `—` | ✅ |
| 3 | Оба контейнера созданы раньше последнего дампа (выкладка T0 упала на сборке) | отказ сверки T0 | `ecf2162` | 1 | — | 42b8ea980f22 running | e20769790f9b running | `—` | ✅ |
| 4 | То же с `--t0-confirm` (сборка цели затем падает) | T0 закреплён решением человека; сбой сборки — ни флага, ни остановки агента | `ecf2162` | 1 | — | 567f4f8 running | 567f4f8 running | `567f4f8` | ✅ |
| 5 | Корректная выкладка T0: агент не пересоздавался (образ из кеша), backend — после дампа | сверка пройдена, T0 закреплён, выкладка первой ревизии | `ecf2162` | 0 | — | ecf2162 running | ecf2162 running | `ecf2162` | ✅ |
| 6 | 11 выкладок подряд (с первой), ротация образов | образы T0 на месте (pinned), ротация оставляет 10 ревизий журнала | `2eaa12a` | — | — | 2eaa12a running | 2eaa12a running | `2eaa12a` | ✅ |
| 7 | Сбой сборки цели | ни флага, ни остановки агента; APP_DIR — работающая ревизия | `4e571f2` | 1 | — | 2eaa12a running | 2eaa12a running | `2eaa12a` | ✅ |
| 8 | Сбой после сборки, до флага (SET флага не прошёл) | образы цели собраны, `:latest` — работающая ревизия, флага нет | `4e571f2` | 1 | — | 2eaa12a running | 2eaa12a running | `2eaa12a` | ✅ |
| 9 | Сбой между остановкой агента и `up` (удаление каталога упало) | `trap` поднял прежнего агента, его каталог опубликован, флаг стоит, печатает, что делать | `4e571f2` | 1 | deploy 4e571f2 1791215534-472065-79688698 2026-10-05T15:52:17+00:00 | 2eaa12a running | 2eaa12a running | `2eaa12a` | ✅ |
| 10 | Сбой внутри `up` (частичный up) | работает агент из `releases/current`, `mentis-backend:latest` = работающий backend, флаг стоит | `4e571f2` | 1 | deploy 4e571f2 1791215548-474063-537c8092 2026-10-05T15:52:30+00:00 | 4e571f2 running | 2eaa12a running | `2eaa12a` | ✅ |
| 11 | Агент цели не публикует каталог (поставлен на паузу после `up`) | ожидание 2 мин, отказ, флаг стоит, агент цели запущен | `4e571f2` | 1 | deploy 4e571f2 1791215583-477253-2b14abc3 2026-10-05T15:53:06+00:00 | 4e571f2 running | 4e571f2 paused | `2eaa12a` | ✅ |
| 12 | Повторный `deploy.sh` той же ревизии после неудачи (таймаут шага (5)) | новая попытка, старый `.exit` не читается, флаг снимает только новый успех | `4e571f2` | 0 | — | 4e571f2 running | 4e571f2 running | `4e571f2` | ✅ |
| 13 | Юнит убит `systemctl --user kill -s KILL` до записи `.exit` | `deploy.sh`: «завершился без кода», не ждёт до таймаута, флаг стоит | `4e571f2` | 1 | deploy 4e571f2 1791215757-490537-932c013c 2026-10-05T15:56:00+00:00 | 4e571f2 running | 4e571f2 exited | `4e571f2` | ✅ |
| 14 | Обрыв SSH (kill локального `deploy.sh`) сразу после остановки агента | скрипт на сервере доходит до конца; лог и код выхода в `releases/` | `2eaa12a` | -9 | — | 2eaa12a running | 2eaa12a running | `2eaa12a` | ✅ |
| 15 | Два одновременных `deploy.sh` одной ревизии | один юнит, второй подключается к его логу; оба получают исход одной попытки | `4e571f2` | 0 | — | 4e571f2 running | 4e571f2 running | `4e571f2` | ✅ |
| 16 | То же при прошлой неудачной попытке этой ревизии с `.exit` на диске | ни один не сообщает её код; оба получают исход новой попытки | `2eaa12a` | 0 | — | 2eaa12a running | 2eaa12a running | `2eaa12a` | ✅ |
| 17 | Второй `deploy.sh` той же ревизии с изменённым скриптом ограды, пока первый жив | первый дорабатывает свою версию (маркер в логе), второй подключается | `4e571f2` | 0 | — | 4e571f2 running | 4e571f2 running | `4e571f2` | ✅ |
| 18 | `deploy.sh` другой ревизии, пока идёт выкладка | отказ, ничего не меняя; идущая выкладка завершается | `4e571f2` | 1 | — | 2eaa12a running | 2eaa12a running | `2eaa12a` | ✅ |
| 19 | Linger выключен у пользователя SSH | отказ до шага (1): ни флага, ни остановки агента | `2eaa12a` | 1 | — | 2eaa12a running | 2eaa12a running | `2eaa12a` | ✅ |
| 20 | Цель старше `608cfa1` (агент не публикует каталог) | отказ до шага (1) | `c0c8cf3` | 1 | — | 2eaa12a running | 2eaa12a running | `2eaa12a` | ✅ |
| 21a | Цель без `compose.prod.yml` (`608cfa1`) | отказ на проверке контракта, до шага (1) | `608cfa1` | 1 | — | 2eaa12a running | 2eaa12a running | `2eaa12a` | ✅ |
| 21 | Цель без образов и без tracked `uv.lock` | отказ, lock текущего дерева не подставляется | `c26ff56` | 1 | — | 2eaa12a running | 2eaa12a running | `2eaa12a` | ✅ |
| 22 | Шина менеджера пользователя недоступна дольше 30 с | «состояние неизвестно», юнит не запускается | `2eaa12a` | 1 | — | 2eaa12a running | 2eaa12a running | `2eaa12a` | ✅ |
| 23 | Шина недоступна 8 с во время ожидания исхода | повтор чтения; после восстановления — та же попытка, без нового запуска | `2eaa12a` | 0 | — | 2eaa12a running | 2eaa12a running | `2eaa12a` | ✅ |
| 24 | Загруженный юнит `mentis-deploy` без `DEPLOY_ATTEMPT` (R109) | «метаданные попытки отсутствуют или некорректны», без подключения и запуска | `2eaa12a` | 1 | — | 2eaa12a running | 2eaa12a running | `2eaa12a` | ✅ |
| 25 | `systemd-run` отказал, юнита нет (R106) | «юнит отсутствует: запуск не подтверждён…», stderr `systemd-run`, G4 не запускается | `2eaa12a` | 1 | — | 2eaa12a running | 2eaa12a running | `2eaa12a` | ✅ |
| 26 | `systemd-run` вернул ошибку, а юнит работает со своей попыткой (R102) | `deploy.sh` ждёт свою попытку и получает её исход | `2eaa12a` | 0 | — | 2eaa12a running | 2eaa12a running | `2eaa12a` | ✅ |
| 27 | Start во время выкладки (между флагом и каталогом цели), оба маршрута; Stop | Start — 503 «starting is closed for maintenance»; Stop принят | `4e571f2` | 0 | — | 4e571f2 running | 4e571f2 running | `4e571f2` | ✅ |
| 28 | Барьер доставки: Start в потоке при остановленном агенте, затем Stop | барьер не проходит, пока агент не обработал обе команды; после — Stop succeeded, lag 0, pending 0, idle | `2eaa12a` | — | — | 4e571f2 running | 4e571f2 running | `4e571f2` | ✅ |
| 29 | Откат на T0 по runbook после всех выкладок | без сборки, на закреплённых образах; alembic = ревизия T0; в APP_DIR нет чужих миграций; каталог T0 | `567f4f8` | 0 | — | 567f4f8 running | 567f4f8 running | `567f4f8` | ✅ |

## Подробности

### 1. Первый запуск без `--t0-revision`


```
==> Выкладка ecf2162 на jb@stand, попытка 1791215071-429852-14636a5d
==> 2026-10-05T15:44:33+00:00 попытка 1791215071-429852-14636a5d, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
ОШИБКА: первый запуск: нужен --t0-revision <ревизия, работающая сейчас>
!!! 2026-10-05T15:44:33+00:00 сбой в фазе before (код 1)
==> 2026-10-05T15:44:33+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1791215071-429852-14636a5d.log) ----
==> 2026-10-05T15:44:33+00:00 попытка 1791215071-429852-14636a5d, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
ОШИБКА: первый запуск: нужен --t0-revision <ревизия, работающая сейчас>
!!! 2026-10-05T15:44:33+00:00 сбой в фазе before (код 1)
==> 2026-10-05T15:44:33+00:00 код выхода 1
---- образы сервисов ----
mentis-backend-1 mentis-backend:latest
mentis-agent-1 mentis-agent:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 2. `--t0-revision` не совпадает с последним дампом


```
==> Выкладка ecf2162 на jb@stand, попытка 1791215078-430735-63b8e9d1
==> 2026-10-05T15:44:40+00:00 попытка 1791215078-430735-63b8e9d1, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
==> T0: сверка bef05d5 с последним дампом и контейнерами
    последний дамп '/home/jb/backups/mentis-trading-agent/pre-deploy-app-567f4f8-20261005-154359.dump' не от ревизии bef05d5
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-05T15:44:41+00:00 сбой в фазе before (код 1)
==> 2026-10-05T15:44:41+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1791215078-430735-63b8e9d1.log) ----
==> 2026-10-05T15:44:40+00:00 попытка 1791215078-430735-63b8e9d1, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
==> T0: сверка bef05d5 с последним дампом и контейнерами
    последний дамп '/home/jb/backups/mentis-trading-agent/pre-deploy-app-567f4f8-20261005-154359.dump' не от ревизии bef05d5
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-05T15:44:41+00:00 сбой в фазе before (код 1)
==> 2026-10-05T15:44:41+00:00 код выхода 1
---- образы сервисов ----
mentis-backend-1 mentis-backend:latest
mentis-agent-1 mentis-agent:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 3. Оба контейнера созданы раньше последнего дампа (выкладка T0 упала на сборке)


```
==> Выкладка ecf2162 на jb@stand, попытка 1791215087-431995-ad3bbbc8
==> 2026-10-05T15:44:49+00:00 попытка 1791215087-431995-ad3bbbc8, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
==> T0: сверка 567f4f8 с последним дампом и контейнерами
    ни один контейнер не создан после дампа (выкладка упала на сборке или шла без сборки?)
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-05T15:44:49+00:00 сбой в фазе before (код 1)
==> 2026-10-05T15:44:49+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1791215087-431995-ad3bbbc8.log) ----
==> 2026-10-05T15:44:49+00:00 попытка 1791215087-431995-ad3bbbc8, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
==> T0: сверка 567f4f8 с последним дампом и контейнерами
    ни один контейнер не создан после дампа (выкладка упала на сборке или шла без сборки?)
ОШИБКА: сверка T0 не пройдена; продолжить можно только с --t0-confirm после проверки человеком
!!! 2026-10-05T15:44:49+00:00 сбой в фазе before (код 1)
==> 2026-10-05T15:44:49+00:00 код выхода 1
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
!!! 2026-10-05T15:45:00+00:00 сбой в фазе before (код 1)
==> 2026-10-05T15:45:00+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-ecf2162-1791215096-433155-33e86d75.log) ----
==> 2026-10-05T15:44:57+00:00 попытка 1791215096-433155-33e86d75, ревизия ecf2162, скрипт ограды 4f9aeddfb56d
==> T0: сверка 567f4f8 с последним дампом и контейнерами
    ни один контейнер не создан после дампа (выкладка упала на сборке или шла без сборки?)
    сверка не пройдена, но есть --t0-confirm (решение человека)
    T0 = 567f4f8: образы помечены, закреплён в releases/pinned
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Сборка :ecf2162
compose-fault: build
!!! 2026-10-05T15:45:00+00:00 сбой в фазе before (код 1)
==> 2026-10-05T15:45:00+00:00 код выхода 1
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
==> Готово: ecf2162
==> 2026-10-05T15:45:43+00:00 код выхода 0

==> Готово: ecf2162 развёрнут (попытка 1791215109-435045-24ab50b4).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 6. 11 выкладок подряд (с первой), ротация образов

- ревизии: `5046431`, `f4b91cc`, `5be53d7`, `a0467de`, `365d627`, `2f192b3`, `ecdeb03`, `dd01761`, `a0e4e81`, `2eaa12a`
- образы после ротации: mentis-agent:2eaa12a, mentis-agent:2f192b3, mentis-agent:365d627, mentis-agent:5046431, mentis-agent:567f4f8, mentis-agent:5be53d7, mentis-agent:a0467de, mentis-agent:a0e4e81, mentis-agent:dd01761, mentis-agent:ecdeb03, mentis-agent:f4b91cc, mentis-agent:latest, mentis-backend:2eaa12a, mentis-backend:2f192b3, mentis-backend:365d627, mentis-backend:5046431, mentis-backend:567f4f8, mentis-backend:5be53d7, mentis-backend:a0467de, mentis-backend:a0e4e81, mentis-backend:dd01761, mentis-backend:ecdeb03, mentis-backend:f4b91cc, mentis-backend:latest

### 7. Сбой сборки цели


```
==> Выкладка 4e571f2 на jb@stand, попытка 1791215507-468598-3e4f710f
==> 2026-10-05T15:51:48+00:00 попытка 1791215507-468598-3e4f710f, ревизия 4e571f2, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Сборка :4e571f2
compose-fault: build
!!! 2026-10-05T15:51:49+00:00 сбой в фазе before (код 1)
==> 2026-10-05T15:51:49+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-4e571f2-1791215507-468598-3e4f710f.log) ----
==> 2026-10-05T15:51:48+00:00 попытка 1791215507-468598-3e4f710f, ревизия 4e571f2, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Сборка :4e571f2
compose-fault: build
!!! 2026-10-05T15:51:49+00:00 сбой в фазе before (код 1)
==> 2026-10-05T15:51:49+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 8. Сбой после сборки, до флага (SET флага не прошёл)


```
#35 exporting attestation manifest sha256:4d606fd836f03932bb674cf938d6e17e4b535c2ffac791a61f7194e0b8c38a97 0.0s done
#35 exporting manifest list sha256:dbeb5bdc7d56e04218d2e03f02a7c6cdf15055eb70d6bb6514c39c8c94c2fa03 0.0s done
#35 naming to docker.io/library/mentis-agent:4e571f2 done
#35 unpacking to docker.io/library/mentis-agent:4e571f2 0.0s done
#35 DONE 0.1s

#36 [agent] resolving provenance for metadata file
#36 DONE 0.0s

#37 [backend] resolving provenance for metadata file
#37 DONE 0.0s
 Image mentis-agent:4e571f2 Built 
 Image mentis-backend:4e571f2 Built 
==> Установка 4e571f2 в /home/jb/apps/mentis-trading-agent
==> (1) флаг trading:start_fence
compose-fault: flag
ОШИБКА: флаг trading:start_fence не поставлен: нет ответа
!!! 2026-10-05T15:52:05+00:00 сбой в фазе before (код 1)
==> 2026-10-05T15:52:05+00:00 код выхода 1
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
!!! 2026-10-05T15:52:18+00:00 сбой в фазе stopped (код 1)
==> trap: поднимаю прежнего агента (контейнер и образ не тронуты)
 Container mentis-redis-1 Waiting 
 Container mentis-db-1 Waiting 
 Container mentis-redis-1 Healthy 
 Container mentis-db-1 Healthy 
 Container mentis-agent-1 Starting 
 Container mentis-agent-1 Started 
    Образы сейчас:
    backend: mentis-backend:latest sha256:a6b42304ff59494e281abe49eba114f98f1ae7a3d5b1d0a0869f29d0cac2cb62 running
    agent: mentis-agent:latest sha256:f7326e24a7d0edc21be820c6779da40fd4f7a6ddac36dec108dd8e649c4bfaed running
    Флаг trading:start_fence остаётся (Start закрыт). Снять — только после проверки, по runbook
    (docs/DEPLOY.md §6): повторный deploy.sh или redis-cli DEL trading:start_fence.
==> 2026-10-05T15:52:19+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 10. Сбой внутри `up` (частичный up)


```
 Container mentis-redis-1 Running 
 Container mentis-db-1 Running 
 Container mentis-agent-1 Recreate 
 Container mentis-agent-1 Recreated 
 Container mentis-redis-1 Waiting 
 Container mentis-db-1 Waiting 
 Container mentis-redis-1 Healthy 
 Container mentis-db-1 Healthy 
 Container mentis-agent-1 Starting 
 Container mentis-agent-1 Started 
==> trap: mentis-backend:latest = образ работающего backend
    Backend мог быть пересоздан и применить миграции: при «новый backend +
    прежний агент» флаг снимается только повторным deploy.sh или решением человека.
    Образы сейчас:
    backend: mentis-backend:latest sha256:ffd3802adeeae4e75f0ebc8eb55e4c9f932b4f0fbeff85bed463c4669c874fe3 running
    agent: mentis-agent:latest sha256:f7326e24a7d0edc21be820c6779da40fd4f7a6ddac36dec108dd8e649c4bfaed running
    Флаг trading:start_fence остаётся (Start закрыт). Снять — только после проверки, по runbook
    (docs/DEPLOY.md §6): повторный deploy.sh или redis-cli DEL trading:start_fence.
==> 2026-10-05T15:52:55+00:00 код выхода 1
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
 Container mentis-db-1 Healthy 
 Container mentis-redis-1 Healthy 
 Container mentis-redis-1 Healthy 
 Container mentis-db-1 Healthy 
 Container mentis-agent-1 Starting 
 Container mentis-agent-1 Started 
compose-fault: catalog-timeout
==> (5) ожидание каталога агента цели (до 120 с)
ОШИБКА: агент цели не опубликовал agent:strategies за 120 с
!!! 2026-10-05T15:55:20+00:00 сбой в фазе waiting (код 1)
    Образы сейчас:
    backend: mentis-backend:latest sha256:ffd3802adeeae4e75f0ebc8eb55e4c9f932b4f0fbeff85bed463c4669c874fe3 running
    agent: mentis-agent:latest sha256:dbeb5bdc7d56e04218d2e03f02a7c6cdf15055eb70d6bb6514c39c8c94c2fa03 paused
    Флаг trading:start_fence остаётся (Start закрыт). Снять — только после проверки, по runbook
    (docs/DEPLOY.md §6): повторный deploy.sh или redis-cli DEL trading:start_fence.
==> 2026-10-05T15:55:20+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
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
    ротация: mentis-backend:5046431
    ротация: mentis-agent:5046431
==> Готово: 4e571f2
==> 2026-10-05T15:55:49+00:00 код выхода 0

==> Готово: 4e571f2 развёрнут (попытка 1791215728-487622-39d8603c).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 13. Юнит убит `systemctl --user kill -s KILL` до записи `.exit`

- агент был остановлен на шаге (2) и не поднят: так и должно быть без trap; следующая выкладка поднимает его (сценарий 14)

```
==> (2) остановка агента
 Container mentis-agent-1 Stopping 
 Container mentis-agent-1 Stopped 
==> (3) удаление agent:strategies
==> (4) :latest → :4e571f2 и up
compose-fault: slow-up
ОШИБКА: скрипт ограды завершился без кода (юнит: not-found/inactive, попытка '')
---- хвост лога ("$HOME"/apps/releases/deploy-4e571f2-1791215757-490537-932c013c.log) ----
==> 2026-10-05T15:55:59+00:00 попытка 1791215757-490537-932c013c, ревизия 4e571f2, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Образы :4e571f2 уже есть — без сборки
==> Установка 4e571f2 в /home/jb/apps/mentis-trading-agent
==> (1) флаг trading:start_fence
==> (2) остановка агента
 Container mentis-agent-1 Stopping 
 Container mentis-agent-1 Stopped 
==> (3) удаление agent:strategies
==> (4) :latest → :4e571f2 и up
compose-fault: slow-up
---- образы сервисов ----
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence снимается только по runbook (docs/DEPLOY.md §6).
```

### 14. Обрыв SSH (kill локального `deploy.sh`) сразу после остановки агента

- попытка `1791215783-492700-a653bf75`, код в `.exit`: 0

```
==> Выкладка 2eaa12a на jb@stand, попытка 1791215783-492700-a653bf75
==> 2026-10-05T15:56:25+00:00 попытка 1791215783-492700-a653bf75, ревизия 2eaa12a, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
==> Образы :2eaa12a уже есть — без сборки
==> Установка 2eaa12a в /home/jb/apps/mentis-trading-agent
==> (1) флаг trading:start_fence
==> (2) остановка агента
 Container mentis-agent-1 Stopping 
 Container mentis-agent-1 Stopped 
==> (3) удаление agent:strategies
==> (4) :latest → :2eaa12a и up
compose-fault: slow-up
```

### 15. Два одновременных `deploy.sh` одной ревизии

- первый: код 0; логов попыток ревизии: 8
- ExecStart юнита: `ExecStart={ path=/usr/bin/bash ; argv[]=/usr/bin/bash /home/jb/apps/releases/deploy-fence-1791215828-495937-089b9f5c.sh 4e571f2 1791215828-495937-089b9f5c ; ign`

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
 Container mentis-db-1 Healthy 
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
==> Готово: 4e571f2
==> 2026-10-05T15:58:04+00:00 код выхода 0

==> Готово: 4e571f2 развёрнут (попытка 1791215828-495937-089b9f5c).
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
==> Готово: 2eaa12a
==> 2026-10-05T15:59:09+00:00 код выхода 0

==> Готово: 2eaa12a развёрнут (попытка 1791215899-502135-a5d7e5af).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 17. Второй `deploy.sh` той же ревизии с изменённым скриптом ограды, пока первый жив

- маркер: `скрипт ограды 4f9aeddfb56d`, хеш первого `4f9aeddfb56d`

```
 Container mentis-backend-1 Recreate 
 Container mentis-agent-1 Recreated 
 Container mentis-backend-1 Recreated 
 Container mentis-db-1 Waiting 
 Container mentis-redis-1 Waiting 
 Container mentis-db-1 Waiting 
 Container mentis-redis-1 Waiting 
 Container mentis-db-1 Healthy 
 Container mentis-redis-1 Healthy 
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
==> Готово: 4e571f2
==> 2026-10-05T16:00:10+00:00 код выхода 0

==> Готово: 4e571f2 развёрнут (попытка 1791215955-507139-bde38b3c).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 18. `deploy.sh` другой ревизии, пока идёт выкладка


```
==> Выкладка 4e571f2 на jb@stand, попытка 1791216025-513311-1d5d1945
ОШИБКА: идёт выкладка ревизии 2eaa12a (попытка 1791216018-512468-135953b4); дождитесь её конца
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
==> Выкладка 608cfa1 на jb@stand, попытка 1791216094-518697-2662c69c
==> 2026-10-05T16:01:36+00:00 попытка 1791216094-518697-2662c69c, ревизия 608cfa1, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
open /home/jb/apps/releases/stage-1791216094-518697-2662c69c/tree/compose.prod.yml: no such file or directory
!!! 2026-10-05T16:01:37+00:00 сбой в фазе before (код 1)
==> 2026-10-05T16:01:37+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-608cfa1-1791216094-518697-2662c69c.log) ----
==> 2026-10-05T16:01:36+00:00 попытка 1791216094-518697-2662c69c, ревизия 608cfa1, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
open /home/jb/apps/releases/stage-1791216094-518697-2662c69c/tree/compose.prod.yml: no such file or directory
!!! 2026-10-05T16:01:37+00:00 сбой в фазе before (код 1)
==> 2026-10-05T16:01:37+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 21. Цель без образов и без tracked `uv.lock`


```
==> Выкладка c26ff56 на jb@stand, попытка 1791216105-519887-505913fb
==> 2026-10-05T16:01:47+00:00 попытка 1791216105-519887-505913fb, ревизия c26ff56, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: у ревизии c26ff56 нет tracked uv.lock и нет releases/c26ff56.uv.lock — lock текущего дерева не подставляется
!!! 2026-10-05T16:01:48+00:00 сбой в фазе before (код 1)
==> 2026-10-05T16:01:48+00:00 код выхода 1
ОШИБКА: скрипт ограды завершился с кодом 1
---- хвост лога ("$HOME"/apps/releases/deploy-c26ff56-1791216105-519887-505913fb.log) ----
==> 2026-10-05T16:01:47+00:00 попытка 1791216105-519887-505913fb, ревизия c26ff56, скрипт ограды 4f9aeddfb56d
==> Снапшот БД (app, agent)
==> Проверка контракта цели
ОШИБКА: у ревизии c26ff56 нет tracked uv.lock и нет releases/c26ff56.uv.lock — lock текущего дерева не подставляется
!!! 2026-10-05T16:01:48+00:00 сбой в фазе before (код 1)
==> 2026-10-05T16:01:48+00:00 код выхода 1
---- образы сервисов ----
mentis-agent-1 mentis-agent:latest
mentis-backend-1 mentis-backend:latest
mentis-db-1 postgres:18
mentis-redis-1 redis:7-alpine
Флаг trading:start_fence не ставился, агент не останавливался: сбой до шага (1).
```

### 22. Шина менеджера пользователя недоступна дольше 30 с


```
==> Выкладка 2eaa12a на jb@stand, попытка 1791216116-521238-94c8e852
ОШИБКА: состояние юнита mentis-deploy неизвестно (нет ответа менеджера за 30 с).
    Выкладка не запускалась повторно; проверьте на сервере: systemctl --user status mentis-deploy
```

### 23. Шина недоступна 8 с во время ожидания исхода


```
==> (4) :latest → :2eaa12a и up
compose-fault: slow-up
 Container mentis-db-1 Running 
 Container mentis-redis-1 Running 
 Container mentis-backend-1 Running 
 Container mentis-redis-1 Waiting 
 Container mentis-db-1 Waiting 
 Container mentis-redis-1 Waiting 
 Container mentis-db-1 Waiting 
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
==> Готово: 2eaa12a
==> 2026-10-05T16:03:08+00:00 код выхода 0

==> Готово: 2eaa12a развёрнут (попытка 1791216151-522802-accf078e).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 24. Загруженный юнит `mentis-deploy` без `DEPLOY_ATTEMPT` (R109)


```
==> Выкладка 2eaa12a на jb@stand, попытка 1791216193-525819-08b49a9b
ОШИБКА: метаданные попытки отсутствуют или некорректны (юнит mentis-deploy):
    LoadState=loaded ActiveState=active DEPLOY_ATTEMPT='' DEPLOY_REVISION=''
    Не подключаюсь и не запускаю; разберитесь с юнитом на сервере.
```

### 25. `systemd-run` отказал, юнита нет (R106)


```
==> Выкладка 2eaa12a на jb@stand, попытка 1791216199-526506-6499cf9e
ОШИБКА: юнит mentis-deploy отсутствует: запуск не подтверждён либо попытка уже выгружена.
    systemd-run: systemd-run-fault: refused, nothing started
    Непроверенная справка (releases/deploy.current): 2eaa12a 1791216151-522802-accf078e
```

### 26. `systemd-run` вернул ошибку, а юнит работает со своей попыткой (R102)


```
==> (3) удаление agent:strategies
==> (4) :latest → :2eaa12a и up
 Container mentis-redis-1 Running 
 Container mentis-db-1 Running 
 Container mentis-backend-1 Running 
 Container mentis-redis-1 Waiting 
 Container mentis-db-1 Waiting 
 Container mentis-redis-1 Waiting 
 Container mentis-db-1 Waiting 
 Container mentis-redis-1 Healthy 
 Container mentis-db-1 Healthy 
 Container mentis-db-1 Healthy 
 Container mentis-agent-1 Starting 
 Container mentis-redis-1 Healthy 
 Container mentis-agent-1 Started 
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 8b8027b6133a825cfcf4c877ec5387b1c7740271f088d84b3d666aa3ed85d0f8
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
==> Готово: 2eaa12a
==> 2026-10-05T16:03:43+00:00 код выхода 0

==> Готово: 2eaa12a развёрнут (попытка 1791216205-527281-c967919b).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 27. Start во время выкладки (между флагом и каталогом цели), оба маршрута; Stop

- ответ: {'detail': 'starting is closed for maintenance'}

```
 Container mentis-agent-1 Recreate 
 Container mentis-agent-1 Recreated 
 Container mentis-backend-1 Recreated 
 Container mentis-redis-1 Waiting 
 Container mentis-redis-1 Waiting 
 Container mentis-db-1 Waiting 
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
==> Готово: 4e571f2
==> 2026-10-05T16:04:44+00:00 код выхода 0

==> Готово: 4e571f2 развёрнут (попытка 1791216228-529800-398850d0).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```

### 28. Барьер доставки: Start в потоке при остановленном агенте, затем Stop

- XINFO при остановленном агенте: name agent-workers consumers 1 pending 0 last-delivered-id 1791216246230-0 entries-read 1 lag 2

### 29. Откат на T0 по runbook после всех выкладок


```
 Container mentis-agent-1 Recreated 
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
 Container mentis-backend-1 Started 
 Container mentis-agent-1 Started 
==> (5) ожидание каталога агента цели (до 120 с)
    schema_hash: 1f6a4457d33cfc28b2157c13183147a9882086f2a0620c241435f100dbe497e4
==> Healthcheck backend (до 120 с)
==> Каталог проверяет backend цели
==> (6) снятие флага
    ротация: mentis-backend:f4b91cc
    ротация: mentis-agent:f4b91cc
==> Готово: 567f4f8
==> 2026-10-05T16:05:50+00:00 код выхода 0

==> Готово: 567f4f8 развёрнут (попытка 1791216335-536299-233a9cfe).
    Доступ: ssh -L 8010:localhost:8010 jb@stand  затем http://localhost:8010
```
