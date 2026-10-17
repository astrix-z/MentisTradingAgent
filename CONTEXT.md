# AGENT CONTEXT — читать в начале каждого нового диалога

Это файл-«навигатор». Он создан, чтобы агент в **новом** диалоге сразу понимал,
что это за проект, где что лежит, какая была **последняя задача** и что дальше,
вместо повторного изучения всего репозитория.

**Правило работы с этим файлом:**
1. Начиная задачу — прочитай его целиком (это быстро) и открывай **только**
   файлы, на которые ссылается последний пункт Журнала. Не исследуй репо заново.
2. Закончив задачу — добавь новый пункт в Журнал (в **верх** секции Журнала):
   кратко что сделано, какие файлы, как проверялось, какие хвосты остались.

---

## 1. Проект в двух словах

`MentisTradingAgent` — монорепо торгового агента. Три части + docker compose.
- `backend/` — FastAPI (пакет `app`), SQLModel/Alembic. Раздаёт собранный UI и REST API.
- `agent/` — автономный asyncio-демон (пакет `agent`), не зависит от lifecycle backend.
- `frontend/` — React+Vite+TanStack Router/Query, axios-клиент сгенерирован из openapi.
- Окружение: Python 3.14, **uv workspace** (members `backend`, `agent`); фронт — **bun**.
- Стек проверок: `ruff` + `ruff format` + `mypy --strict` (python); `biome` + `tsc` (frontend).

## 2. Карта «куда смотреть»

| Слой | Точки входа (главное) |
|---|---|
| Backend API | `backend/app/api/routes/trading.py` (командный контур), `backend/app/api/main.py`, `backend/app/core/redis_client.py`, `backend/app/main.py`, `backend/app/core/config.py` |
| Agent — ядро | `agent/agent/strategies/` (главное: `base.py`, `registry.py`, `tripple_screen/`) |
| Agent — обвязка | `agent/agent/engine.py` (жизненный цикл, heartbeat, run), `agent/agent/common/commands.py` (реестр `@command`), `agent/agent/main.py` (подписка/диспетчер) |
| Agent — вселенная | `agent/agent/symbols.py` (вселенная, классификация, цены), `agent/agent/selection.py` (отметки), `agent/agent/models.py` (таблицы агента) |
| Agent — биржа | `agent/agent/exchange/rest.py`, `agent/agent/exchange/streams.py` (тонкие адаптеры над `binance_sdk_spot`) |
| Frontend | `frontend/src/routes/_layout/strategy.tsx` (настройки+старт Стратегии), `working-symbols.tsx` (рабочие символы), `trading.tsx` (общий контроль), `frontend/src/client/*` (сгенерировано, НЕ править руками) |
| Док SDK | `binance-spot/docs/`, `binance-spot/examples/` (документация и примеры `binance_sdk_spot`) |
| Deploy | `compose.yml` + `compose.override.yml` (проект `mentis`), `.env` |
| Задания / долги | `TASKS.md` (известные недоработки, оформлены как задания), `REVIEW.md` (история ревью) |

## 3. Ключевая архитектура: Стратегия — это ядро Агента

**Стратегия = основа Агента** (скрипт, который анализирует данные и принимает решения).
Всё остальное — сервисная обвязка вокруг неё: потоки данных, открытие/закрытие
ордеров, жизненный цикл, heartbeat. Обвязка **не содержит торговой логики** и не
дублирует модели биржи (см. §4).

- Одна Стратегия в рантайме. Реализована **`tripple_screen`**
  (`docs/STRATEGY_TRIPPLE_SCREEN.md`). Конкретная стратегия = модуль в
  `agent/agent/strategies/`, наследник `Strategy` + декоратор `@strategy("name")`
  (`registry.py`). Чтобы добавить новую — новый модуль и импорт в
  `strategies/__init__.py`; движок и команды не трогаются.
- Стратегия получает готовые сервисы (`StrategyServices`: `rest`, `streams`, `emit`)
  и **не создаёт клиентов биржи сама**. Наружу отдаёт `Signal`
  (`ENTRY`/`EXIT`/`HOLD` + provenance) — это контракт стратегии, не модель биржи
  (`D-SIGNAL-ONLY`). Sizing/тип ордера/защита — вне стратегии.

**Запуск Стратегии (главный сценарий):**
1. Без сохранённого `agent:runtime.running=true` система стартует с **выключенной**
   Стратегией. Существующий resume сохранён: намерение `running=true` восстанавливается
   после рестарта, включая LIVE; Stop сохраняет `running=false`.
2. Отдельная страница UI `/strategy` — единственное место настройки и пуск/стоп.
   Страница рендерится из **каталога стратегий**, который публикует агент
   (`agent:strategies`): схема параметров, лейблы полей и междоменные правила
   приходят от агента, а не дублируются в UI.
3. Кнопка **Start** → backend публикует команду `start` → Redis → агент.
4. Агент валидирует `params`, создаёт Стратегию и запускает её с этими настройками.
5. **Stop** ведёт к `engine.stop()` (стратегия/потоки гасятся).

**Redis — единственный канал backend↔agent.** У каждого сервиса своя БД; никто не
пишет чужую схему.

- Backend публикует конверт в канал `agent:commands`:
  `{id, type, payload, issued_at}`. Типы: `start` `{network, strategy, symbols, params}`,
  `stop`, `balance` `{network}`, `execution_configure` `{settings}` и др.
- **Реальные сделки — отдельный переключатель владельца** (ClickUp 869fgqxah):
  `POST /trading/execution/` → документ `agent:execution` + команда
  `execution_configure`. Start его не несёт (непустое `execution` → 422), сеть и
  Start на него не влияют. Без включённых сделок агент при Start только
  идентифицирует аккаунт; lock и reconcile — лишь при включённых сделках или
  незавершённой работе (`docs/ORDER_EXECUTION_PLAN.md` §4.3).
- Агент слушает канал и вызывает обработчик по типу (реестр + `@command`).
  **Новая команда = новый обработчик в `common/commands.py`**, цикл подписки не трогается.
- Backend атомарно через Redis CAS записывает revision снапшота `agent:runtime`
  и публикует `start`/`stop` (resume после рестарта + статус UI);
  `balance` снапшот не трогает.
- Результат команды → `agent:result:<id>` с TTL 3600 секунд и legacy `agent:result`.
  Backend отдаёт `GET /trading/result/?command_id=UUID` только superuser.
  UI различает приём (202), выполнение, отказ и timeout (120 секунд).
- События сигналов → канал `agent:events` (`Engine._publish_event`).
- Живость: heartbeat `trading:heartbeat` (TTL 30 c). `GET /trading/status` =
  `{desired, actual, online}` только для superuser. `desired` включает params/revision;
  сеть/стратегия/balances факта берутся исключительно из `actual` heartbeat.
- **Каталог стратегий** — `agent:strategies` (без TTL), публикуется агентом при
  старте и периодически. Содержит JSON Schema `settings_schema` для `params`,
  междоменные `constraints` (декларативный AST) и таблицы `lookups`. Схема —
  единственный источник правды: `TripleScreenSettings` принадлежит **только**
  агенту, backend его не импортирует. Backend компилирует каталог и авторитетно
  проверяет `params` до записи в Redis (невалидное → 422); каталога нет или он не
  интерпретируем → 503, а не «считаем валидным». Спецификация формата —
  `agent/agent/protocol.py`.
- `GET /trading/strategies/` (superuser) отдаёт каталог UI целиком;
  `available: false` означает «агент ещё не публиковал», это не ошибка.
- Redis-ключи/канал держать в синхроне между
  `backend/app/api/routes/trading.py`, `agent/agent/engine.py` и
  `backend/app/core/strategy_catalog.py`; расхождение каталога ловит
  `backend/tests/test_strategy_catalog_fidelity.py`.

## 4. ОБЯЗАТЕЛЬНО: работа с биржей только через `binance_sdk_spot`

**Жёсткое требование проекта.** Любое взаимодействие с Binance (REST, websocket,
модели данных, enum'ы) строится на библиотеке **`binance_sdk_spot`** (модуль
`binance_sdk_spot`). Свои модели-дубликаты (`Quote`, `MarketData`, `NewOrder`,
`OrderStatus` и т.п.) **запрещены** — всё это уже есть в SDK.

- Документация и примеры SDK лежат в `binance-spot/docs/` и `binance-spot/examples/`.
  Не нашёл нужного — **спроси пользователя**, не изобретай своё.
- В коде агента разрешены только **тонкие адаптеры**:
  `agent/exchange/rest.py` (async-обёртка над `Spot.rest_api` через
  `asyncio.to_thread`, т.к. REST блокирующий) и `agent/exchange/streams.py`
  (обёртка над `Spot.websocket_streams`). Они возвращают **родные модели SDK**.
- Клиент: `Spot(config_rest_api=ConfigurationRestAPI(...))` /
  `Spot(config_ws_streams=ConfigurationWebSocketStreams(stream_url=...))`.
  URL и креды — по `network` (`testnet`/`live`) из config (`BINANCE_*`,
  `BINANCE_TESTNET_*`); константы URL — из `binance_sdk_spot`
  (`SPOT_REST_API_PROD_URL`, `SPOT_WS_STREAMS_TESTNET_URL`, ...).
- Полезные API (v11.3.0, см. примеры): `client.rest_api.{get_account, klines,
  new_order, delete_order, get_open_orders, get_order, exchange_info}`;
  `client.websocket_streams.{create_connection, kline, ticker, book_ticker, user_data}`.
  Ответ REST — `response.data()`.
- Собственный тип баров стратегии (`Candle` в
  `strategies/tripple_screen/indicators.py`) — это внутреннее значение стратегии
  (Decimal-поля для индикаторов), а не дубликат модели биржи.

## 5. Генерация клиента и сборка UI (при изменении моделей backend)

- Собрать схему: из `backend/` (нужен каталог `backend/app/frontend`)
  `uv run python -c "import json;from app.main import app;json.dump(app.openapi(),open('../frontend/openapi.json','w'))"`
  (env: FASTAPI_ENV=development, SECRET_KEY, DATABASE_URL, FIRST_SUPERUSER, FIRST_SUPERUSER_PASSWORD).
- Из корня репо: `bun node_modules/@hey-api/openapi-ts/bin/run.js` — клиент в `frontend/src/client/`.
- Сборка UI (раздаётся backend'ом из `backend/app/frontend`): из `frontend/`
  `bun ../node_modules/vite/bin/vite.js build` (deps подняты в корень workspace).
- **НЕ использовать `bun run build` / `bunx`** — в этой среде зависают.
- НЕ править `frontend/src/client/**` руками.

## 6. Запуск / просмотр (локально на хосте)

- **Поднять весь рабочий стек:** `docker compose up -d --build` (`db` + `redis` +
  `backend` + `agent`). Миграции/суперюзер — автоматически через
  `backend/scripts/entrypoint.sh`.
- Вспомогательные сервисы (proxy, adminer, mailpit, playwright) — profile `extras`.
- Приложение: **http://127.0.0.1:8000/** (порт 80 занят системным httpd).
- Креды/ключи — в `.env` (не хардкодить).
- Новый запуск — кнопкой Start на `/strategy`; после рестарта сохраняется resume
  ранее записанного `running=true`. Для boot-off сначала отправить Stop.

## 7. Быстрые проверки (среда)

- backend/agent: `cd backend && uv run ruff check app && uv run mypy app`; аналогично `agent`.
- agent-тесты: `cd agent && uv run pytest`.
- frontend: `bun ../node_modules/@biomejs/biome/bin/biome check src`;
  `bun ../node_modules/typescript/bin/tsc -p tsconfig.build.json`; сборка — vite (п.5).

---

## ЖУРНАЛ (последняя задача сверху)

### [2026-10-17] Списание позиции по делистингованному символу — Claude-Opus (869fjdy10)
- Команда `write_off_position` `{symbol, confirm, proceeds_quote}`: `Exits.write_off` закрывает позицию через `Protector._finalize(..., proceeds=)` без ордеров, причина `delisted` / механизм `write_off`, событие `written_off`; `proceeds_quote` добавляется к `sold_quote` в той же транзакции. Отказ (`WriteOffRefused`): `confirm` ≠ символ, отрицательная выручка, символ в `TRADING`, открытые ордера на бирже по символу (`-1121` = символа нет, ордеров нет), незавершённые попытки позиции (`journal.open_of` по всем ролям), нет блокировки исполнения.
- `ExchangeRest.exchange_info(symbol)`: `-1121` → `UnknownSymbol(ExchangeError, LookupError)` — после делистинга `FilterCache` и `PositionWorker.not_trading` видят «не листингуется», а не сетевую ошибку. Fake SDK отвечает `-1121` на `exchangeInfo`/`openOrders` неизвестного символа; `FakeExchange.delist()` отменяет ордера символа и убирает его.
- Backend: тип `write_off_position`, проверка формы (символ, `confirm`, неотрицательная конечная сумма) → 422; в агент уходит `{symbol, confirm, proceeds_quote: str}`. `types.gen.ts` дополнен вручную тем же литералом.
- Runbook §6 «Символ уже делистингован» — процедура через команду.

### [2026-10-17] HALT: OCO не снимается на символе вне TRADING — Claude-Opus (869fjdy0k)
- `PositionWorker` (`worker.py`): перед записью операции, которая отменит живой OCO (выход по времени, trailing, guard, `close_position`), статус символа читается `PositionWorker.not_trading`. `TRADING`, разрешающий отмену, всегда читается из `exchangeInfo` заново (`FilterCache.get(..., max_age=0)`); уже известная остановка (алерт `halted:` стоит) берётся из кеша не старше 60 с, чтобы не запрашивать биржу каждый круг. Не `TRADING` (или символа нет в `exchangeInfo`) → операция не начинается, `StepResult(WAITING, "<SYMBOL> is HALT: list kept")`, алерт `halted:<SYMBOL>` (не блокирует admission; снимается, когда символ снова торгуется, при закрытии позиции и при выходе из контекста). Статус не читается (ошибка `exchangeInfo`) → `StatusUnknown`: OCO тоже остаётся, `WAITING "<SYMBOL> status unknown: list kept"`, `close_position` отклоняется.
- Операция, записанная до остановки, но без отправленной отмены (`op_state.cancel is None`), отзывается: `operation_withdrawn`, статус `protected`, `unprotected_since` сброшен, `overdue:` снят. Отправленная отмена (`done`/`unknown`) не отзывается.
- `close_position` на неторгуемом символе → `SymbolNotTrading` (LookupError) → команда `ok=false`.
- Fake SDK: `new_order` и `order_list_oco` на символе вне `TRADING` отклоняются (`-2010 Market is closed.`), отмены проходят. Тесты в `test_exits.py`: выход по времени ждёт торгов и выполняется после, trailing в BREAK, отзыв записанной операции после рестарта, отправленная отмена не отзывается, `close_position` отклоняется; по ревью: закешированный `TRADING` не даёт отменить OCO после остановки, нечитаемый статус сохраняет OCO (helper `halt()` в тестах кеш больше не сбрасывает). Runbook §6 обновлён.

### [2026-10-17] Runbook исполнения дописан до live-пилота — Claude-Mentis
- `docs/EXECUTION_RUNBOOK.md`: §0 (где смотреть режим и алерты: UI, `trading:heartbeat`, лог, БД агента; какие алерты закрывают входы), §5 (БД/Redis/биржа недоступны), §6 (HALT/делистинг с позицией, ручное закрытие через `close_position` по API), §7 (429/418, бюджет веса), §8 (часы: пороги 1 с / 2,5 с, `chronyd` на AlmaLinux), §9 (ручная сверка: SQL по `position`/`order_attempt`/`fill`, read-only скрипт `all_orders` + `myTrades` за сутки, таблица расхождений). Поведение сверено с кодом `development` (`runtime.py`, `budget.py`, `trade.py`, `protection.py`, `worker.py`, `reconcile.py`, `main.py`). План §9.7 обновлён; в §2 убрано «закрыть из UI» — кнопки `close_position` в UI нет.
- SQL §9 проверен на одноразовом PG 18 с миграциями агента (`0005_signal_watch`); скрипт сверки запущен на testnet; `jq`-команда heartbeat — на production.
- Найдено по ходу (код не менялся): (1) выход по времени, trailing и `close_position` не проверяют статус символа и во время HALT снимают OCO, после чего продать нельзя — позиция без защиты до возобновления торгов; (2) после делистинга позицию нельзя закрыть ни продажей, ни командой — нужна процедура закрытия в БД; (3) Redis в production на анонимном томе: `compose down` + `up` теряет переключатель, снимок запуска и поток команд; (4) в UI нет кнопки `close_position`.

### [2026-10-16] Проверка ключа: торговые группы Binance вместо SPOT — Claude-Mentis
- На production после выкладки `d2eb225` алерт `execution: key: API key cannot trade spot (canTrade=True, permissions=['TRD_GRP_072'])`. Live-аккаунт состоит в торговой группе Binance: в `permissions` указана только группа, `SPOT` нет. По `exchangeInfo` (live, 2026-10-16) группа 072 есть в `permissionSets` 510 из 511 USDT-пар (нет только RWUSDUSDT). Прежняя проверка `"SPOT" in permissions` закрывала бы все входы (`key` блокирует admission).
- `ExecutionRuntime._check_key`: нужны `canTrade` и непустые permissions. Разрешён ли символ, проверяет `check_symbol` при каждом входе: `SymbolFilters.permission_sets` (из `permissionSets`, запасной вариант — старое `permissions`), у аккаунта должно быть хотя бы одно право из каждого набора, иначе `Skip.SYMBOL_NOT_PERMITTED`. Admission передаёт `account.permissions` из того же `get_account`.
- Доки: план §9.5; runbook §4 «Права ключа и торговые группы Binance» (модель прав, что проверяет агент, ручная проверка через `exchangeInfo`, действия оператора). Тесты: `test_sizing.py` (наборы прав, реальные фикстуры), `test_execution_runtime.py` (+2). Agent 855 passed / 1 skip (одноразовые PG 18 + Redis 7); ruff/format/mypy без замечаний.

### [2026-10-16] Start не падает из-за символов, которых нет в сети запуска — Claude-Mentis
- Симптом на production: testnet, `tripple_screen` → `error`, `strategy start failed: Binance REST call failed: (-1121, 'Invalid symbol.')`. Причина: вселенная рабочих символов читается с live (`symbols.UNIVERSE_NETWORK`), testnet listит подмножество; отбор стратегии грузит свечи всех символов одним `gather`, и один неизвестный символ обрывает весь `on_start`.
- `Engine._start_run` до `on_start` сверяет символы Start с `exchangeInfo` сети запуска (`status == "TRADING"`): остальные не входят в прогон, лог `warning`, в heartbeat — предупреждение `not trading on <network>, left out: …` (первые 20 по имени, дальше счётчиком), снимается при Stop. Если не осталось ни одного — `ValueError` с именами, состояние `error`.
- `tripple_screen._load_window`: `ExchangeError` загрузки свечей содержит символ и интервал (`FOOUSDT 4h klines: …`).
- Тесты: `test_engine_lifecycle.py` (+2, хелпер `listed_rest`), `test_strategy_runtime.py` (+1, `Rest.symbols`), `test_commands_lifecycle.py` переведён на `listed_rest`. Agent 601 passed / 252 skip без PG/Redis; ruff/format/mypy чистые.

### [2026-10-13] Отдельное управление реальными сделками (ClickUp 869fgqxah) — Claude-Mentis
- Сеть, Start/Stop и реальные сделки теперь независимы. Переключатель и лимиты задаёт `POST /trading/execution/` (документ `agent:execution` + команда `execution_configure`); Start с непустым `execution` → 422, агент игнорирует `execution` старых команд и снапшотов. Агент читает `agent:execution` при boot до resume, отказ → сделки выключены, алерт `settings`.
- `ExecutionRuntime`: `prepare_start(network, params)` без настроек; при выключенных сделках только идентификация аккаунта (ошибка не отклоняет Start), lock и reconcile — при включённых сделках или незавершённой работе (`tick`: `_lead` / `_step_down`). Это же убирает таймаут Start на testnet из 869fgqu5u, пока сделки выключены. `Engine.configure_execution` пересевает позиции работающей стратегии при переключении.
- UI: карточка «Real trading» на Strategy (переключатель с подтверждением сети, лимиты из секции каталога, Pause/Resume), режим в статусе и в диалоге live Start. Каталог отдаёт секцию `execution`, статус — желаемый `execution`.
- Файлы: `agent/agent/execution/runtime.py`, `agent/agent/engine.py`, `agent/agent/common/commands.py`, `agent/agent/main.py`, `backend/app/api/routes/trading.py`, `frontend/src/components/ExecutionControl.tsx`, `strategy.tsx`, `TradingStatusView.tsx`, клиент; тесты `agent/tests/test_execution_control.py`, `test_execution_runtime.py`, `backend/tests/test_trading_contract.py`, `frontend/tests/trading-contract.spec.ts`; доки — план §4.3/§9.4, runbook §3.
- Ревью (Claude-Mentis, отдельный поток) — «не готово», три P2, исправлены во втором коммите: (1) `test_control_integration` ожидал снапшот без `execution: {}`; (2) отклонённый Start при включённых сделках переносил `target_network` на новую сеть — теперь она меняется только после успешного входа; (3) отклонённое агентом ON оставалось в `agent:execution` и после рестарта ломало resume — backend отклоняет ON и Start не на USDT (422), агент при boot оставляет сделки выключенными с алертом `settings`, если поднимаемая стратегия не на USDT. P3: выключение не блокируется лимитами, «Save limits» при отказе агента требует подтверждения, битый документ читается как OFF, runbook — про длительность сверки и Pause.
- Проверки: agent 849 passed / 1 skip (PG18 + Redis 7 одноразовые); backend 682 passed (contract 108); Playwright (mocked) 80 passed; ruff/mypy/biome/tsc и хуки CI чистые.

### [2026-10-13] Третье ревью релиза 869fg9avr, recovery G4 — GPT-Sol
- Проверены PR #22 (open, head `048b1cc1d08d9dcf3f2f68da5e2adbea76c341e4`, base development `357d4d3`), коммиты `a175216`/`048b1cc`, комментарии Claude-Mentis в [869fgdkgu](https://app.clickup.com/t/869fgdkgu) и [869fg9avr](https://app.clickup.com/t/869fg9avr), DEPLOY §2/§6, EXECUTION_RUNBOOK §2 и журнал стенда. Чистый detached worktree; PR #21/#22 не мёрджились.
- P2 `869fgdkgu` принят: возможности определяются по тегам образов фактических контейнеров и истории серверного клона; неизвестный/нетегированный образ, неизвестная ревизия, ошибка Git и расхождение с `current` закрывают путь. Legacy backend цели не поднимается на шаге (4), проходит health/catalog на одноразовом `compose run`, затем открывается последним в `opening`; trap в `up`/`waiting`/`opening` восстанавливает `mentis-backend:latest`, останавливает недоказанный backend и сообщает фактическое состояние Start. Retry после failed rollback останавливает фактический T0 до флага; повторный rollback запускает (2a) образом с исполнителем.
- Независимые проверки: `tests/deploy/test_deploy_scripts.py` **78 passed**; отдельные recovery/(2a) probes **2 passed**; дополнительные unknown-current/Git-error/unknown-image probes **3 passed**; `bash -n` чисто. Локального `shellcheck` нет, заявленный автором результат не выдавался за независимый.
- Самостоятельный Docker-in-Docker стенд на `048b1cc`: **R1/R4a/R4b/R4c/R2/R3 — 6/6**. R1 сохраняет обычную первую выкладку; R4a отказывает до запуска T0, оставляя modern backend под флагом; R4b trap останавливает T0 после ошибки в `opening`; R4c повтор закрывает held Start и оба маршрута, stream/desired не меняются; R2 блокирует protected execution и поднимает agent; R3 успешно откатывает после closed.
- Изолированный `compose run` не публикует порт и не получает alias `backend`; уникальное имя контейнера резолвится только внутри compose-сети и не является штатным маршрутом Start. `prestart` работает с общей БД: корректный rollback уже остановил backend и выполнил downgrade; при пропуске downgrade на R4a `alembic upgrade head` T0 отказывает на неизвестной новой ревизии до запуска API, работающий modern backend остаётся доступен под флагом. В `opening` запуск legacy backend осознанно является снятием реального барьера; healthcheck следует после него, а отказ останавливает backend.
- Вердикт: исправление принято, новых замечаний нет. `869fgdkgu` → `complete`; `869fg9avr` → `need review`, готов к merge PR #22 в development, затем development → stage, обновлению PR #21 и фиксации/проверке новой вершины stage. Полные комментарии подписаны GPT-Sol. Стенд и его тома удалены; production, рабочий `mentis-*` и testnet/live не затрагивались, ордера не отправлялись.

### [2026-10-13] Повторное ревью исправлений релиза 869fg9avr — GPT-Sol
- Проверены PR #22 (открыт, не мёрджился), ветка `fix/release-review-869fg9avr`, чистый detached worktree `0946d1da7f94aab32fb3ef431238b5c030643ab0`, база development `357d4d3`; описания/комментарии родителя и трёх замечаний, журнал автора, DEPLOY §2–3/§6 и EXECUTION_RUNBOOK.
- Исходные исправления приняты: [869fgbyaf](https://app.clickup.com/t/869fgbyaf), [869fgbyar](https://app.clickup.com/t/869fgbyar), [869fgbyb5](https://app.clickup.com/t/869fgbyb5) → `complete`. Общий UNFINISHED блокирует все nonclosed positions и holding intents/attempts во всех контекстах; отказ (2a) восстанавливает прежний agent и сохраняет fence. Первая остановка T0 backend до флага закрывает in-flight Start. План фиксирует проверенный head stage явным `--revision` после обычного merge и проверки равенства деревьев.
- Новый P2 [869fgdkgu](https://app.clickup.com/t/869fgdkgu) → `update required`: после failed rollback modern → T0 в фазе waiting фактический legacy backend работает под непонятным ему флагом, current остаётся modern; retry определяет capability по stale current и пропускает stop backend. Независимый отрицательный deploy-harness probe воспроизвёл состояние и отсутствие stop backend при повторе. Нужны реальный G4 на legacy target/failure/retry и проверка возможностей фактически работающих образов. Родитель [869fg9avr](https://app.clickup.com/t/869fg9avr) остаётся **не готово**, `update required`; полные комментарии подписаны GPT-Sol.
- Независимые проверки: agent **831 passed / 1 intentional skip**, включая 8 barrier tests; backend **679 passed**, upgrade/Alembic check чистые; deploy **70 passed**; дополнительные PG/ancestry probes **17 passed** (8 nonclosed статусов позиции, 3 holding intent, 3 attempt, partial schema, отсутствующая current revision, ошибка merge-base). Ruff/format, mypy agent/tests/backend, ty backend, frontend Biome/tsc — прошли.
- Повторена настоящая изолированная Docker-in-Docker репетиция `--only release-review`: **R1/R2/R3 3/3**, включая удержанный перед записью Start T0 и оба маршрута после флага, отказ при protected после Stop и штатный rollback по runbook после closed. Новый failure/retry проверен отдельно harness; production для него не использовался.
- Одноразовые PG18/Redis7 и стенд с томами удалены. Рабочий стек/production не изменялись, ордера не отправлялись, PR #21/#22 не мёрджились. Исходники исправлений не менялись. После устранения нового P2 нужны повторная приёмка, merge development → stage, обновление PR #21 и фиксация/проверка новой вершины stage.

### [2026-10-13] Ревью релиза stage b5ada20 → production T0 — GPT-Sol
- Карточка [869fg9avr](https://app.clickup.com/t/869fg9avr), PR #21: head `b5ada20ad743fdeaeb05de3c13bcd30646833144`, base `567f4f84b88b76b845803200494ed141bb6c73d4`; PR открыт, не мёрджился. Вердикт **не готово**, полный комментарий GPT-Sol `90120275936495` опубликован и проверен; статус `update required`.
- Проверены описания/комментарии/статусы всех 41 карточки EXEC/PRESET и F1/F2/F3/R2/R3/T1: `complete`. Исправления `29272c5`, `de7ff2b`, `933850b`, `2eaa12a`, `7b0118a`, `19288c0` входят в кандидата; прежние risk/settings/watch/leadership/legacy-recovery probes проходят. F3: прочитан актуальный журнал настоящего стенда `docs/acceptance/PRESET-09a-stand-2026-10-07.md` (39/39); собственный повтор полной реальной fault matrix не выполнялся.
- Чистый detached worktree `/tmp/mentis-release-review-b5ada20`: agent **823 passed / 1 намеренный invalid-json n/a skip**, backend с настоящим Redis **679 passed**, independent probes с новым execution-barrier case **26 passed**, deploy stub harness **61 passed**, `bash -n` чистый. Ruff check/format — 98 agent/tests + 55 backend/app/tests; strict mypy — 98 agent/tests + 27 backend app; ty, frontend Biome (77 файлов) и полный `tsc --noEmit` прошли. Playwright повторно не запускался; независимое чтение CI rollup ограничено правами GitHub-токена.
- Production только прочитан: T0 поднят, linger=yes, deploy unit inactive, `releases/` ещё нет. Фактический desired `running=true, network=live` не содержит `execution`; проверка настоящего resume с mock start подтверждает `enabled=false` и перевод старых имён параметров. Биржа не вызывалась.
- Дампы реального T0 восстановлены в отдельный PG18: app — 324 signalposition / 1 user / 0 item; agent — 67 monitored_symbol / 0 working_symbol. app `b3f7a1c9d2e4 → f4b8c2d6e1a9`, agent verified adoption `create_all → 0005_signal_watch`; оба Alembic check чистые, исходные строки/поля сохранены (добавлен документированный `closed_by`). Downgrade app к T0 и повторный upgrade/check прошли без потери исходных строк.
- Новые подзадачи релиза, все `update required`: [869fgbyaf](https://app.clickup.com/t/869fgbyaf) **P1** — runbook отката разрешает T0 при незавершённом real execution (`Engine.stop → idle`, но protected position и `unfinished=1`; T0 не содержит исполнителя); [869fgbyar](https://app.clickup.com/t/869fgbyar) **P2** — legacy backend T0 игнорирует G4-флаг (на disposable Redis оба Start приняты, stream +2 и desired revision=2); [869fgbyb5](https://app.clickup.com/t/869fgbyb5) **P2** — default deploy после обычного merge берёт новый production HEAD, а приёмка ожидает `current=b5ada20`.
- До выкладки: execution-barrier перед откатом, настоящий барьер/quiesce legacy backend в первой G4 и для in-flight Start, единый выбранный SHA команды/приёмки; затем повтор соответствующих проверок и ревью. `--t0-confirm` не заменяет работающие контейнеры при закреплении T0. Live-пилот и недописанные разделы `docs/EXECUTION_RUNBOOK.md` — отдельная приёмка перед включением исполнения.
- Production и рабочие `mentis-*` не менялись, реальный deploy не запускался, testnet/live ордера не отправлялись. Два одноразовых PG18/Redis7 контейнера и тома удалены; временные production dumps/runtime snapshots удалены. Исходники продукта не менялись; добавлена только эта запись, прежние локальные изменения CONTEXT.md сохранены. Временные harness/logs/ClickUp receipts — `/tmp/release-review-*`; постоянный локальный отчёт не создавался.

### [2026-10-05] Третий прогон ревью девяти Need Review — GPT-Sol
- HEAD `8e0a24c`, исправления `de7ff2b`/`7b0118a`; отчёт `CLICKUP_REVIEW_2026-10-05_THIRD.md`. Прежние DB risk-version и atomic inbox/watch замечания исправлены: 4 independent probes + 1 дополнительный atomic/rollback/retry probe passed.
- Новый P1 R3: полный Engine Start снижает daily loss limit с 5 до 1 после risk gate, один убыток уже есть; DB risk version не учитывает settings, старый RESERVED BUY всё равно FILLED. Negative probe `tmp/review-third-2026-10-05/test_settings_risk.py` воспроизводит BUY=1.
- Создана R3 high карточка `869fc94c2`, подзадача EXEC-06, `update required`, с критериями приёмки. В соответствующих пяти карточках опубликованы ссылки/замечание.
- 4 принято (`complete`): TripleScreen repeat ENTRY `869f6m7vg`, SignalTracker `869f6m7vn`, F2 `869fc705k`, R2 `869fc8ktr`. 5 `update required`: EXEC-06/08/08b/13 и F1 `869fc7020`. Во всех девяти комментарии GPT-Sol и статусы повторно прочитаны и проверены; receipts в `tmp/review-third-2026-10-05/clickup/`.
- Проверки: agent 818 passed/1 n/a skip; backend с настоящим Redis 677 passed без skips; Alembic check чистый; Ruff 153 files, strict mypy 98 agent+27 backend files чистые. Frontend/deploy повторно не гонялись в этом ревью; PRESET-09a не была в очереди.
- Исходники исполнения/рабочий стек/production/testnet/реальные ордера не менялись и не запускались. Два одноразовых PG/Redis контейнера удалены.

### [2026-10-05] Повторное ревью семи Need Review и новой T1 — GPT-Sol
- Проверены реальные статусы, исправления `29272c5`/`2eaa12a`/`8848ff7`, стенд и журнал `704c052`. Отчёт: `CLICKUP_REVIEW_2026-10-05_REPEAT.md`; исходники не менялись.
- Прежние F1 probes и happy-path F2/SignalTracker исправлены. Новые negative probes: R1 P1 — пятый убыток после risk gate на awaited journal.prepare всё ещё отправляет BUY; R2 P2 — ошибка watch после inbox/publish, затем dedup replay навсегда оставляет ENTRY без SignalWatch/virtual feedback. Новая карточка R2 `869fc8ktr`.
- PRESET-09a: принят журнал автора 30/30, но обязательные Start-before-fence/commit, R108 prepared stage→later launch, barrier restart/race и simultaneous preparations не полностью подтверждены настоящим стендом. F3 остаётся открытым.
- Семь исходных задач переведены в `update required`; исходные F1/F2/F3 дополнены, все десять комментариев/статусов проверены. Receipts/negative probes/logs: `tmp/review-repeat-2026-10-05/`.
- Проверки `704c052`: agent 809 passed/1 n/a skip; backend 675 passed; deploy 55 passed; Chromium 72 passed/20 n/a; 8 previous probes passed; Ruff/types чистые. Новые два negative probes воспроизводят замечания.
- Во время публикации в Need Review поступила T1 `869fc707e`, фикс `19288c0`: дополнительный полный backend с настоящим Redis 677 passed без skips, исходный teardown дефект и новые regressions проходят; карточка принята (`complete`). Alembic/lint/types чистые.
- Рабочий стек/production/testnet/реальные ордера не использовались; четыре одноразовых PG/Redis контейнера обоих прогонов удалены. Итоговый Need Review с подзадачами пуст, snapshot `tmp/review-repeat-2026-10-05/need-review-final.json`.

### [2026-10-05] Замечания ревью — отдельные карточки ClickUp
- Пользователь поручил все замечания заносить в карточки ClickUp; при следующих ревью оформлять новые замечания с воспроизведением/критериями приёмки, не ограничиваться локальным отчётом. Подпись сохраняется GPT-Sol.
- Созданы и проверены F1 `869fc7020` (RESERVED risk gate, подзадача EXEC-06), F2 `869fc705k` (virtual feedback/cooldown, подзадача TripleScreen), F3 `869fc706n` (реальная fault matrix, подзадача PRESET-09a); все `update required`.
- Наблюдаемое падение Redis/TestClient teardown заведено отдельно как тестовая задача T1 `869fc707e`, `to do`, без утверждения production-дефекта.
- В исходных шести задачах опубликованы обратные ссылки с подписью GPT-Sol. Описания/родители/статусы/комментарии проверены чтением из API; receipts в `tmp/review-all-2026-10-05/clickup/findings/receipts.json`, ссылки добавлены в `CLICKUP_REVIEW_2026-10-05_ALL.md`.

### [2026-10-05] Ревью всех 13 задач Need Review — GPT-Sol
- Отчёт: `CLICKUP_REVIEW_2026-10-05_ALL.md`; основа `0e9c57a`, параллельный коммит `8848ff7` также прочитан. Исходники в рамках ревью не менялись.
- Предыдущие EXEC F1–F3 исправлены; 11 независимых probes passed. Новый P1: RESERVED BUY обходит достигнутый daily loss limit или другой UNKNOWN intent; два negative probes воспроизводят новые ордера.
- 7 задач приняты (`complete`): EXEC-05, 09/09a/09c, 11/11b, PRESET-09b. 6 требуют доработки (`update required`): EXEC-06, 08/08b, 13, TripleScreen repeat ENTRY (virtual feedback/cooldown), PRESET-09a (реальная fault matrix не подтверждена).
- Опубликованы и повторно проверены 13 комментариев с подписью GPT-Sol и статусы. Итоговый Need Review, включая подзадачи, пуст. Receipts и probes: `tmp/review-all-2026-10-05/`.
- Проверки: agent 792 passed/1 n/a skip; backend 672 passed после изоляции Redis (первый прогон упал на TestClient Redis teardown, см. отчёт); deploy 55 passed; Chromium 72 passed/20 n/a; lint/types зелёные. После `8848ff7` целевые signal tracker/lifecycle/schema/feedback — 65 passed, без суммирования повторов.
- PRESET-09b actual-running rollback принят по записи автора, GPT-Sol его не повторял. Production/testnet и рабочий стек не запускались; два отдельных контейнера ревью удалены.

### [2026-10-05] Ревью десяти задач EXEC на a0e4e81
- Отчёт: `CLICKUP_REVIEW_2026-10-05_EXEC.md`; требования и история прочитаны из ClickUp.
- Прежние BUY pre-submit и legacy EXIT upgrade блокеры исправлены (5 независимых probes passed).
- Новые замечания: P1 — OCO/cancel/SELL после замеченной потери лидерства; P1 — новый BUY при repricing/closing без защиты; P2 — неполный охват дедлайна операций/watchdog.
- Проверки: 775 agent tests + 6 Redis integration passed; 1 намеренный n/a skip; ruff/format, strict mypy чистые. Шесть negative probes воспроизводят замечания, файлы и журнал в `tmp/exec-review-2026-10-05/`.
- Исходники исполнения не изменялись; live/testnet и рабочий стек не запускались. Требуются исправления и расширение EXEC-13. По последующему явному поручению пользователя опубликованы 10 комментариев GPT-Sol и все 10 задач переведены в `update required`; комментарии и статусы повторно прочитаны и проверены.

### [2026-09-24] Реальное исполнение ордеров: этапы 1–2 плана v2.2 (EXEC-01…15)
- План: `docs/ORDER_EXECUTION_PLAN.md` v2.2 (§12.1 — DAG задач), ClickUp 869f6m8kj
  и задачи EXEC-01…15 с подзадачами 08a–c, 09a–c, 11a–b (все в `need review`).
  Одна задача — один коммит, `fb7d7cc`…`b5a5e73`.
- Код: `agent/agent/execution/` — `models.py` (таблицы §8.1), `runtime.py`
  (контекст uid+network, advisory lock, admission-блоки, heartbeat),
  `settings.py` (секция `execution` каталога), `admission.py` (inbox и резервы),
  `sizing.py`/`filters.py`/`quotes.py` (правила биржи, размер, опорная цена),
  `attempts.py` (журнал попыток, UNKNOWN), `orders.py` (вход FOK),
  `protection.py` (OCO, дедлайн 30 с, рыночные выходы), `worker.py` (протокол
  cancel → барьер → replace/exit), `trailing.py`, `exits.py` (time, guard,
  close_position), `monitor.py` + `exchange/hub.py` (StreamHub),
  `reconcile.py`, `ledger.py` (единственный писатель позиций и закрытий),
  `service.py` (сборка при boot). Миграции Alembic — `agent/agent/migrations/`.
  Торговый клиент — `agent/agent/exchange/trade.py`, бюджет — `budget.py`.
- Backend: секция `execution` каталога проверяется тем же `CompiledStrategy`;
  Start и снапшот несут `execution`; команды `execution_pause`,
  `execution_resume`, `close_position`. Клиент фронта перегенерирован.
- Проверки: агент — 484 теста (PostgreSQL на `AGENT_TEST_DB_URL`, fake-биржа
  `agent/tests/fake_exchange.py`, crash-матрица `test_crash_matrix.py`), mypy
  strict и ruff чистые; backend fidelity/contract — с `--noconftest` (конфтест
  backend чистит пользователей в БД, на которую указывает).
- Хвосты: `tests/test_control_integration.py` падает на импорте backend при
  запуске из `agent/` — было и до этой работы. UI Positions, outbox → backend,
  UDS, dry-run/testnet — этапы 3–6 плана. Исполнение выключено по умолчанию
  (`execution.enabled=false`), реальные ордера появятся только после явного
  включения в Start.
- Локальная БД compose слушает 5433, а `.env` указывает 5432: для тестов
  `AGENT_TEST_DB_URL=postgresql+psycopg://…@localhost:5433/postgres`.


### [2026-09-17] Решения владельца: memecoins и 12 `<TICKER>B`
- Владелец подтвердил оба пункта из ClickUp: memecoins остаются только меткой и не
  исключаются; все 12 найденных `suspected_tokenized_equity` должны остаться
  исключёнными, поэтому ни один не добавлен в `CRYPTO_ENDING_IN_B`.
- Для 12 записей добавлен параметризованный регрессионный тест полного опубликованного
  результата: классификация, `excluded_reason` и `usable == 0`. В старом списке задач
  исправлена опечатка `QNTUSDT` → фактический `QNTBUSDT`. Поведение memecoins уже
  покрыто тестом: метка есть, причины исключения нет, символ пригоден.
- `TASKS.md` больше не содержит ожидающих решения пунктов; решения отражены в
  `DESIGN.md`. Оба ClickUp-задания завершены с итоговым комментарием от `zed`.
- Файлы: `agent/tests/test_symbols.py`, `TASKS.md`, `DESIGN.md`, `CONTEXT.md`.
- Проверено: `test_symbols.py` **55 passed**, Ruff чистый, строгий mypy для
  `agent tests` — 0 ошибок, диагностики Zed чистые.

### [2026-09-17] Working Symbols: выбор Monitoring и инверсия
- На `/working-symbols` добавлены массовые действия `Select Monitoring` и
  `Invert selection`. Первое добавляет в черновик все пригодные символы с меткой
  Monitoring; второе инвертирует только пригодную часть выбора. Сохранённые
  непригодные отметки инверсия не снимает скрыто — их по-прежнему можно снять вручную.
- Оба действия меняют тот же локальный черновик, что чекбоксы и выбор по объёму;
  обычный `Save` отправляет итоговый полный набор без изменений API.
- Добавлен mock Playwright-сценарий `Monitoring → invert → Save`, включая проверку,
  что непригодный символ не выбирается. Обновлён `DESIGN.md` §8.
- Файлы: `frontend/src/routes/_layout/working-symbols.tsx`,
  `frontend/tests/trading-contract.spec.ts`, `DESIGN.md`, `CONTEXT.md`.
- Проверено: Biome по изменённым frontend-файлам, `tsc -p tsconfig.json --noEmit`,
  mock Playwright **24 passed**, диагностики Zed без ошибок и предупреждений.

### [2026-09-16] T01–T06 закрыты: гейты, шум форматтеров и один живой e2e
- **Сначала заведены в ClickUp** (пространство `MentisTradingAgent`, список заведён
  заново, статус `to do`, приоритеты как в сводке `TASKS.md`), затем выполнены по
  приоритету: T01 → T02 → T05 → T03 → T04 → T06, каждый — отдельным коммитом.
- **T01 (P1, `ab7ecc9`).** Backend не проверялся строгим mypy: хук `local-mypy` запускал
  `uv run mypy backend/app` **из корня**, где `[tool.mypy]` не читается вообще, — то есть
  без `strict` и без `exclude` (28 файлов против 22). Теперь одна каноническая команда
  `uv run --directory backend mypy app` — в хуке и в новом шаге `test-backend.yml`;
  устаревший `# type: ignore[arg-type]` в `strategy_catalog.py` удалён. Проверено:
  0 ошибок, а временная неаннотированная функция в `backend/app` валит команду
  (`no-untyped-def`). **Побочно:** в чекауте нет `.git/hooks/pre-commit` (`prek install`
  не запускали) — локально хуки не проверяли вообще ничего, иначе устаревший игнор не
  дожил бы до сегодня.
- **T02 (P2, `986b6ff`).** `tsc` не проверял типы тестов (`tsconfig.build.json` исключает
  `tests/**`), а `lib: ES2020` не знает `Array.prototype.at` — тест не компилировался.
  `target`/`lib` → ES2022, скрипт `typecheck` (`tsc -p tsconfig.json --noEmit`), хук
  `local-tsc-check`, шаг в `playwright.yml`. Проверено: TS2550 → 0, а искусственная ошибка
  типа в `frontend/tests` валит проверку.
- **T05 (P2, `466e2e5`).** Причин у двух `ECONNREFUSED` было две: mailpit живёт в профиле
  `extras`, и `compose.override.yml` **жёстко ставил `SMTP_HOST: mailpit`, оставляя
  `EMAILS_FROM_EMAIL` пустым** — а `send_email` без него ассертит. То есть стек просил
  складывать письма в mailpit и делал отправку невозможной. Адрес получил локальный
  дефолт; тесты, читающие письмо, **скипаются** с текстом, называющим причину, вместо
  падения с сырой сетевой ошибкой. Проверено: живой e2e 85 passed; с остановленным
  mailpit — 5 passed / 2 skipped с внятной причиной; бэкенд-сьют **201 passed**. Рецепты
  — в новом разделе `development.md`.
- **T03 (P3, `26f99a2`).** `frontend/tests/fixtures/strategy-catalog.json` — отслеживаемая
  **и** сгенерированная: генератор разворачивает короткие массивы, Biome сворачивал,
  поэтому `bun run lint` оставлял несвязанный diff, а автор его откатывал. Фикстура
  исключена в `biome.json`; генератор стал идемпотентным — `published_at` переносится из
  прежнего файла, когда остальное не изменилось.
- **T04 (P3, `ef613c6`).** Biome ругался на артефакты e2e и переписывал их. Включена
  VCS-интеграция (`vcs.useIgnoreFile`), так что `.gitignore` — единственный источник
  истины; заодно закрылся третий такой файл, `frontend/openapi.json`, которого в задаче
  не было. Проверено после живого e2e.
- **T06 (P3, `e0764b3`).** Добавлен живой `frontend/tests/working-symbols.spec.ts`,
  устойчивый к состоянию: таблица с сортировкой **или** «has not published a symbol list
  yet», и падение только если страница сломалась или не видит агента. Ветка «не
  опубликовано» не была покрыта **нигде** (mock-сьют проверяет то же для Strategy).
  Проверено на обеих ветках, включая состояние с испорченным в Redis документом
  (`available=False`), после чего документ восстановлен байт-в-байт.
- **Проверено целиком:** живой e2e **86 passed**; бэкенд **201 passed** (нужен mailpit);
  `tsc -p tsconfig.json` включая тесты и biome — чисто; backend mypy — 0.
- **Хвосты, не входившие в задачи:** `uv run ty check backend/app` даёт одну давнюю
  диагностику (`trading.py:772`, `await redis.hgetall` — типизация redis-py); `ruff`/
  форматтеры не покрывают `scripts/`; бэкенд-сьют **удаляет все строки `User`/`Item`** в
  той базе, на которую указывает (сессионная фикстура в `backend/tests/conftest.py`) — это
  и уронило локальный стек во время работы, и теперь описано в `development.md`.

### [2026-09-15] T07 закрыт: `mypy agent tests` проходит и проверяется хуком
- Починены шесть давних ошибок типов в тестах агента — по-настоящему, а не пачкой
  `# type: ignore`: `parsed[Model]()` разворачивает `from_dict` (он `Optional`),
  `reported()` проверяет `isinstance` вместо возврата `Any`, Redis-клиент аннотирован
  там, где `from_url` отдаёт `Any`, и два узких `# type: ignore[misc]` с объяснением на
  `redis.hgetall` (redis-py типизирует его как awaitable-или-нет; продакшен-код носит
  такой же — `agent/common/commands.py`). `warn_unused_ignores` включён, поэтому когда
  стабы починят, игноры сами станут ошибками.
- **Новый хук `local-mypy-agent`** — и именно в форме
  `uv run --directory agent mypy agent tests`: mypy читает конфиг из текущего каталога,
  поэтому из корня он не находит `agent/pyproject.toml` и **молча теряет `strict`**.
  Доказано пробой: временная неаннотированная функция проходит
  `uv run mypy agent/agent agent/tests` из корня (36 файлов, «success») и падает из
  `agent/` (`no-untyped-def`). Тот же корень у T01.
- Проверено: `cd agent && uv run mypy agent tests` → 0 (35 файлов);
  `prek run local-mypy-agent` проходит, а с искусственной ошибкой типов в тесте — падает.
- **Заодно применён `ruff format` ко всему агенту.** Стек проверок в `CONTEXT.md` §1
  включает `ruff format`, `agent/README.md` документирует `ruff format --check .`, а
  дерево не сходилось (12 файлов) — в том числе из-за прошлой сессии, которая решила,
  что `ruff format` «сломан», и правила код руками. Проверено: `--check` теперь 36/36.
  **Цена:** ручные таблицы в `symbols.py` (`STABLE_OR_FIAT`, `EQUITY_TICKERS`,
  `MEMECOINS`, `CRYPTO_ENDING_IN_B`) разворачиваются в один элемент на строку (+~130
  строк, зато правка таблицы становится однострочным diff'ом). Если компактный вид
  важнее — откатывается точечно.
- Backend не форматировал: там `ruff format --check` нигде не документирован, а его
  mypy-гейт — отдельный T01. Дрейф там тот же, 3 файла: `app/api/deps.py`,
  `app/api/routes/trading.py`, `app/core/strategy_catalog.py`.
- Файлы: `.pre-commit-config.yaml`, `agent/tests/{test_symbols.py,test_symbol_selection.py,test_command_delivery_integration.py}`,
  `agent/README.md`, `TASKS.md` (+ `ruff format` по каталогу `agent/`).

### [2026-09-15] Метка Monitoring: ярлык в таблице, поддельный адаптер и хранение у агента
- **Где метка (проверено на живом API):** в официальном spot-API её нет — у
  `exchangeInfo` одинаковые 26 полей на всех 3699 символах, подстрок `tag`/`monitoring`
  в ответе нет вообще; futures даёт только `underlyingType`/`underlyingSubType`.
  Безавторизационный источник один — страница `/en/markets/coinInfo-Monitoring`, но она
  закрыта AWS WAF (небраузерный клиент получает `202` + JS-челлендж), поэтому читается
  JSON, из которого она сама рендерится: `.../asset-service/product/get-products` →
  `tags` (у `SYNUSDT`: `["Monitoring", "innovation-zone", "Seed", "defi"]`).
  Покрытие: в фиде есть **все 394** наших «годных» символа; 32 из них под Monitoring,
  180 под Seed. Отсутствующие в фиде 252 — ровно те, кого мы и так исключаем.
- **`agent/monitoring.py` — единственное место, где агент ходит не через
  `binance_sdk_spot`** (это витрина сайта, а не биржа), и оно написано подчёркнуто
  поддельным: один узкий шов (`MonitoringFeed.fetch`, инъектируемый `httpx` transport),
  имя замещаемой страницы в константе и в тексте ошибки, замена/удаление — без правок в
  остальном агенте.
- **Режим отказа задокументирован в модуле и безопасен:** любая ошибка (HTTP, таймаут,
  не-JSON, неожиданная форма) → `MonitoringError`, **сохранённые метки остаются**.
  Провал чтения не должен превращать «под мониторингом» в «чисто»: «неизвестно» ≠ «нет
  метки». Валидный фид, не помечающий ничего, — тоже ошибка (биржа всегда что-то
  помечает; очистка всех меток — единственная реально вредная ошибка, и она же убила бы
  свежесть). Метка — только ярлык: не фильтрует, не блокирует, не валит команды.
- **Хранение у агента:** `agent/models.py:MonitoredSymbol` (строка = метка), читается
  при каждой публикации → `monitoring` у записи и `monitoring_updated_at` у документа
  (свежесть из `max(updated_at)` строк; это корректно именно потому, что пустой список
  не сохраняется). Раз в сутки `_monitoring_loop` в `main.py`; после успешного чтения
  вселенная перепубликовывается сразу, при неудаче публикации нет и документ сохраняет
  прежние метки. Backend отдаёт поля как есть (`SymbolEntry.monitoring`,
  `WorkingSymbols.monitoring_updated_at`), клиент перегенерирован.
- **UI:** в столбце «отметки» — `Badge` с `TriangleAlert` (треугольник с восклицательным
  знаком) и словом «Monitoring», янтарный, с `title`-пояснением; в статусной строке
  `· Monitoring: N`, рядом с ценами — «Monitoring list read …». Никакой фильтрации:
  тест проверяет, что помеченный символ по-прежнему usable, отмечаемый и сохраняется.
- Файлы: `agent/agent/{monitoring.py,models.py,symbols.py,selection.py,main.py}`,
  `agent/tests/{test_monitoring.py,test_symbols.py}`, `backend/app/api/routes/trading.py`,
  `backend/tests/test_trading_contract.py`,
  `frontend/src/{routes/_layout/working-symbols.tsx,client/*}`,
  `frontend/tests/trading-contract.spec.ts`, `DESIGN.md` §8, `agent/README.md`, `TASKS.md`.
- Проверено: agent **192 passed / 6 skipped** (+22), ruff и `mypy --strict agent` чисто;
  backend **172 passed / 1 skipped**, coverage **93%**; frontend `tsc`/biome чисто,
  Playwright mock **23 passed** (+1); живой e2e **83 passed**, те же 2 падения
  `reset-password` (mailpit). Живьём: агент сходил в фид при старте (HTTP 200), опубликовал
  `32 monitored` из 743, `monitoring_updated_at` заполнен; `SYNUSDT` —
  `monitoring=True`, `excluded_reason=None`, `selected=True`; backend отдаёт
  `monitoring: True` и 32 помеченных.
- **Найдено попутно — T07 в `TASKS.md`:** `agent/README.md` заявляет проверку
  `mypy agent tests`, а она не проходит: 6 давних ошибок в 3 тестовых файлах (проверено
  на HEAD, до этой задачи). Пакет при этом чист — `mypy --strict agent` без замечаний.
- **Хвост:** метка обновляется раз в сутки (по требованию) и только пока агент жив;
  источник неофициальный — если Binance его закроет или сменит форму, метки просто
  перестанут обновляться, а прежние останутся и в документе, и в UI.

### [2026-09-15] Цена в таблице рабочих символов: колонка и запрос кнопкой
- **`TASKS.md` привязан к навигатору** — добавлен в `CONTEXT.md` §2, рядом с `REVIEW.md`.
- Во вселенной появились `last_price` (Decimal-строка; `None`, если биржа цены не дала —
  это не то же самое, что цена 0) и `prices_updated_at`. Цена берётся из того же
  24h-тикера, который уже читается для объёма, — периодическая публикация не делает
  ни одного лишнего вызова.
- Новая команда `prices`: агент перечитывает тикеры, обновляет `last_price` **и**
  `quote_volume_24h` (их отдаёт один и тот же эндпоинт — просить только цену значило
  бы спрашивать дважды), обновляет `prices_updated_at` и перепубликовывает документ.
  Кэш `_LAST_UNIVERSE` обновляется на месте; символ, которого в тикерах больше нет,
  сохраняет прежние значения, а не обнуляется. Недоступная биржа/Redis → `None`,
  прежний документ остаётся, команда сообщает об ошибке.
- Команда — observation-only: в `_SNAPSHOT_COMMANDS` не входит, а агент выполняет её
  вне очереди start/stop (`_INDEPENDENT_COMMANDS = ("balance", "prices")`,
  `_handle_independent`), чтобы медленное чтение биржи не задерживало Stop.
- Backend: `POST /trading/prices/` (superuser, 202 + конверт), `SymbolEntry.last_price`,
  `WorkingSymbols.prices_updated_at`. Диспетчер `/commands/` сделан **исчерпывающим**:
  последняя ветка — `if type == "stop"` плюс недостижимый `raise`, чтобы новый тип
  команды не мог молча превратиться в `stop` (та же ловушка, что была с `symbols`).
- UI: колонка `Price` (сортируется численно, `—` если цены нет, точное значение в
  `title`) и кнопка `Request prices` с отметкой «когда прочитано». Обе команды
  (`symbols`, `prices`) показываются в `CommandTrackingView`. Подтверждённый refresh
  перечитывает документ, но **не** сбрасывает черновик отметок.
- По ходу починен латентный баг `DataTable`: TanStack по умолчанию
  (`autoResetPageIndex`) возвращал на первую страницу при **любом** изменении данных —
  то есть на каждом refetch (раз в минуту) и на самом запросе цен. Теперь страница
  сохраняется, индекс при необходимости ограничивается (клэмп по числу страниц), а
  смена «Rows per page» явно возвращает на первую.
- Файлы: `agent/agent/{symbols.py,selection.py,common/commands.py,main.py}`,
  `agent/tests/{test_symbols.py,test_symbol_selection.py,test_commands_lifecycle.py,test_command_delivery_integration.py}`,
  `backend/app/api/routes/trading.py`, `backend/tests/test_trading_contract.py`,
  `frontend/src/{routes/_layout/working-symbols.tsx,components/Common/DataTable.tsx,client/*}`,
  `frontend/tests/trading-contract.spec.ts`, `CONTEXT.md`, `DESIGN.md`, `agent/README.md`.
- Проверено: agent **170 passed / 6 skipped**, `ruff check` и `mypy --strict agent` чисто;
  backend полный сьют **172 passed / 1 skipped**, coverage **93%** (гейт 90%);
  frontend `tsc -p tsconfig.build.json` и biome чисто, `vite build` ок;
  Playwright mock **22 passed** (2 новых); живой e2e **82 passed**, те же 2 падения
  `reset-password` (mailpit не запущен). Живьём в стеке: `POST /trading/prices/` →
  `{'prices': 743, 'total': 743}`, во всех 743 записях появилась цена
  (BTC 75953.60, ETH 2404.13), отметки `BTCUSDT/SOLUSDT/XRPUSDT` сохранились.
- **Требовало пересборки обоих образов:** команда `prices` и `last_price` живут в
  агенте, поэтому одного `--build backend` для проверки было мало (первый прогон
  вернул пустой результат — старый агент просто не знал такой команды).
- **Хвост:** цена — последняя сделка с 24h-тикера, не order book; обновляется только
  по публикации агента (раз в 5 минут) или по кнопке.

### [2026-09-15] Working Symbols: страница и API (закрывает хвост предыдущего пункта)
- **Backend — только API**: `GET /trading/symbols/` отдаёт опубликованный `agent:symbols`
  как есть (`available=false`, если агент ещё не публиковал; нечитаемый документ —
  тоже `available=false`, а не 500), `POST /trading/symbols/` (superuser) шлёт команду
  `symbols` (202 + конверт). Backend проверяет только формат и дубли (422); «годится ли
  символ» решает агент и сообщает причину через `/result/`.
- `_SNAPSHOT_COMMANDS = ("start", "stop")`: команды-наблюдения (`balance`, `symbols`)
  больше не переписывают снапшот — иначе `symbols` молча выставляла бы `running=false`.
  `CommandType` расширен, `/commands/` тоже понимает `symbols`.
- Клиент перегенерирован ( `listWorkingSymbols`/`setWorkingSymbols`).
- UI: новая страница `/working-symbols` (superuser, как `/balance`): таблица с
  сортировкой по **каждому** столбцу, чекбокс участия, имя, 24h volume (сортируется
  численно, не как текст) и отметки (badge причины/метки). В `DataTable` добавлен
  opt-in проп `sorting` — у остальных таблиц заголовки прежние. Тумблер
  «Show unusable symbols»; непригодные нельзя отметить, но отмеченный всегда можно
  снять. Правки — локальный черновик, `Save` отправляет весь набор; после
  подтверждения команды черновик сбрасывается и документ перечитывается.
- `/strategy`: вместо поля ввода Symbols — ссылка на `/working-symbols` и сводка
  (сколько отмечено + имена). `start` несёт отмеченные **пригодные** символы;
  непригодные отмеченные перечисляются, но не торгуются. Start заблокирован, пока
  нет ни одного отмеченного символа или агент не опубликовал список.
- Docs: `DESIGN.md` — принцип 5, §7 (эндпоинты/страница) и новый §8 (вселенная,
  классификация, БД агента, команда `symbols`).
- Файлы: `backend/app/api/routes/trading.py`, `backend/tests/test_trading_contract.py`,
  `frontend/src/routes/_layout/working-symbols.tsx`, `frontend/src/routeTree.gen.ts`,
  `frontend/src/components/{Common/DataTable.tsx,Sidebar/AppSidebar.tsx}`,
  `frontend/src/routes/_layout/strategy.tsx`, `frontend/tests/trading-contract.spec.ts`,
  `frontend/src/client/*` (сгенерировано), `DESIGN.md`.
- Проверено: backend полный сьют **168 passed / 1 skipped**, coverage **93%** (гейт 90%);
  `trading-contract` отдельно 66 passed; `ruff check` и `uv run mypy backend/app` чисто. Frontend:
  `tsc -p tsconfig.build.json` и biome чисто (кроме давнего
  `frontend/tests/fixtures/strategy-catalog.json`: файл сгенерирован, biome его
  переформатирует — не трогал), `vite build` ок. Playwright **mock 18 passed** (2 новых).
  Стек пересобран (`docker compose up -d --build backend`); live e2e **78 passed**, 2
  падения — только `reset-password.spec.ts` (mailpit не запущен, `ECONNREFUSED`, профиль
  `extras` + пустой `EMAILS_FROM_EMAIL`), к правке не относятся.
- **Хвост:** реального e2e-теста для `/working-symbols` нет (поведение покрыто
  mock-сьютом); derived-предупреждений (низкий объём/широкий спред) нет — биржа их не
  публикует, поэтому отметки — только классификация и причина.

### [2026-09-15] Рабочие символы: вселенная биржи и отметки принадлежат агенту (агентская часть)
- Агент читает публичный `exchangeInfo` + 24h tickers, классифицирует и публикует в
  `agent:symbols` полный список USDT-символов с причиной непригодности. Флаги выбора в
  кэш не пишутся: они вливаются в копию документа при публикации (`apply_selection`),
  поэтому валидация и публикация не портят друг другу вход.
- **Классификация — наша, не биржевая.** В spot-API «особых отметок» биржи нет: `tags`
  отсутствует, `additional_properties` пуст у всех 3699 символов, новостей о делистинге
  тоже нет. Причина выводится из `status`/`is_spot_trading_allowed` и базового актива:
  trading halted or delisted, spot trading not allowed, stablecoin/fiat, gold (`PAXG`/`XAUT`),
  wrapped or staked, tokenized equity, non-latin name. Токенизированные акции Binance зовёт
  `<TICKER>B`; правило «заканчивается на B, кроме списка реальных монет» даёт факт для
  известного тикера и **догадку** для остальных (`suspected_tokenized_equity`) — именно это
  поймало ложные срабатывания на `CKB`/`DGB`/`TRB`. Memecoins — только метка, из вселенной
  не исключаются (пользователь снимает руками); иероглифические имена (`币安人生`, `牛来`) — исключаются.
- **Хранение — в собственной БД агента** (`agent/models.py:WorkingSymbol`, PK `symbol`):
  строка и есть отметка. `agent/core/db.py` на каждом старте идемпотентно создаёт свою БД
  (`ensure_settings_database`, имя проверяется регульркой перед `CREATE DATABASE`) и схему
  (`init_settings_schema`) — в `try/except`, отсутствие БД не должно мешать торговле.
  `_use_psycopg_driver` в конфиге переписывает `postgresql://` → `postgresql+psycopg://`
  (иначе `create_async_engine` падает на compose-URL).
- **Валидирует агент**: команда `symbols` проверяет весь набор против вселенной и
  отклоняет запрос целиком, если хоть один символ непригоден (частичная запись оставила бы
  ложное впечатление, что отмечено всё); принятое сохраняется, документ перепубликовывается
  из уже прочитанной вселенной (`latest_universe`).
- Файлы: `agent/agent/symbols.py` (коммит 2f2ab41), `agent/agent/{selection.py,models.py}`,
  `agent/agent/core/{db.py,config.py}`, `agent/agent/common/commands.py`, `agent/agent/main.py`,
  `agent/tests/{test_symbols.py,test_symbol_selection.py}`, `agent/README.md`.
- Проверено: agent 162 passed / 6 skipped; `ruff check` и `mypy --strict agent/agent` чисто.
  Живьём (публичные эндпоинты Binance): 743 USDT-символа, 394 годных (252 halted/delisted,
  64 tokenized equity, 14 stablecoin/fiat, 12 suspected equity, 3 wrapped/staked, 2 gold,
  2 non-latin); отметки `[BTCUSDT,SOLUSDT,XRPUSDT]` легли в БД и восстановились после
  `docker compose restart agent` (`3 marked (0 unusable)`, флаги True в документе);
  `AAPLBUSDT` и несуществующий символ отклонены, `[]` очищает выбор.
- **Хвост (не сделано):** backend-эндпоинты чтения/записи рабочих символов (чтение — из
  опубликованного `agent:symbols`, запись — команда `symbols`) и страница `/working-symbols`
  (таблица с сортировкой по всем столбцам, чекбоксы, тумблер исключённых), ссылка вместо
  поля Symbols на `/strategy`. Двенадцать исключённых по шаблону имён стоит просмотреть
  (`AAOIBUSDT, ALABBUSDT, BNCBUSDT, CBRSBUSDT, DRAMBUSDT, INTWBUSDT, MUUBUSDT, MVLLBUSDT,
  QNTBUSDT, SNXXBUSDT, USARBUSDT, YBUSDT`) и при необходимости добавить в `CRYPTO_ENDING_IN_B`.

### [2026-09-15] Каталог стратегий: схема настроек принадлежит только агенту
- `TripleScreenSettings` больше не импортируется backend'ом (это же чинило падение
  backend-контейнера `ModuleNotFoundError: No module named 'agent'`). Агент публикует
  `agent:strategies`: JSON Schema `params` + междоменные `constraints` (декларативный
  AST) + `lookups`; публикация валидируется на своей же схеме и падает fast при
  невычисляемом правиле.
- Backend **остаётся авторитетным валидатором**: `app/core/strategy_catalog.py`
  компилирует каталог в pydantic-модель (+ свои границы элементов массива, enum
  через membership-валидатор) и вычисляет constraints. Невалидное → 422 до Redis;
  каталога нет/он неинтерпретируем → 503. `KNOWN_STRATEGIES`/`STRATEGY_SETTINGS`
  удалены, `GET /trading/strategies/` отдаёт каталог целиком с `available`.
- UI: `strategy.tsx` больше не содержит копии схемы (`DEFAULT_PARAMS`, `FIELDS`,
  `VALID_INTERVALS`, `crossValidate` удалены); форма, лейблы (`title`), значения
  (`default`) и правила строятся из каталога (`components/strategy-form/strategySchema.ts`).
  Десятичные поля отправляются строками. `available:false` → сообщение и блокировка Start.
- Файлы: `agent/agent/protocol.py`, `agent/agent/engine.py`, `agent/agent/main.py`,
  `agent/agent/strategies/base.py`, `agent/agent/strategies/tripple_screen/{settings.py,schema.py,strategy.py}`,
  `agent/tests/test_strategy_catalog.py`,
  `backend/app/core/strategy_catalog.py`, `backend/app/api/routes/trading.py`,
  `backend/tests/{test_trading_contract.py,test_strategy_catalog_fidelity.py}`,
  `frontend/src/routes/_layout/strategy.tsx`, `frontend/src/components/strategy-form/strategySchema.ts`,
  `frontend/tests/{trading-contract.spec.ts,fixtures/strategy-catalog.json}`,
  `scripts/write_strategy_catalog_fixture.py`.
- Проверено: agent 110 passed/6 skipped (30 новых), backend trading 48 + fidelity 28
  (fidelity компилирует реальный опубликованный документ и сверяет вердикты с моделью
  агента, плюс round-trip через реальные байты и общее имя ключа); Playwright mock 13 passed;
  vite build; ruff check, strict mypy (backend/app, agent/agent).
- **Ограничения:** живые Redis/Binance и e2e против реального backend не проверены;
  полный DB-suite не запускался. `ty check` даёт 1 давнюю диагностику в
  `trading.py` (`await redis.hgetall`, чужой незакоммиченный код) — не трогал.
- Осознанные ограничения контракта: позиционные массивы с разнотипными элементами
  backend не компилирует (отказ, а не приближение); пattern для Decimal-строк
  не проверяется ни одной из сторон (только `Decimal` + `allow_inf_nan=false`).

### [2026-09-12] Исправления R01–R14 из REVIEW.md (без новых торговых слоёв)
- Стратегия: сервисы/эмиссия, snapshot по времени M15, дедуп и backfill полных окон,
  stale/expired gating, FI без вымышленного нуля, frozen-настройки, Decimal transport
  и полный provenance. Retry в памяти сохраняет signal_id и не потребляет эпоху до emit.
- Lifecycle/команды: rollback старта/отмены, listener+heartbeat во время resume,
  Stop прерывает bootstrap; результаты по ID с TTL 3600; полный Signal в Redis.
- Backend: атомарный CAS snapshot+publish с revision, superuser GET, типизированные
  desired/actual. UI: `/trading` без Start, `/strategy` восстанавливает конфигурацию
  и бережёт draft; корреляция команд, failed/timeout вместо ложного успеха 202.
- Файлы: `agent/agent/{strategies,engine.py,main.py,common/commands.py,exchange/streams.py}`,
  `agent/tests/`, `backend/app/api/routes/trading.py`, `backend/tests/test_trading_contract.py`,
  `frontend/src/{routes/_layout,components/TradingStatusView.tsx,hooks/useCommandTracking.ts,client}`,
  `frontend/tests/trading-contract.spec.ts`, `frontend/playwright.mock.config.ts`.
- Проверено: agent 74 tests; backend trading 26 tests; Playwright mock API 5 tests;
  ruff/format, strict mypy, tsc/biome, Vite build. Есть интеграционный тест настоящих
  backend commands → Engine → Strategy → Signal с Redis/SDK-заглушками.
- **Ограничения:** живые Redis/Lua/Binance и полный DB-suite не проверены;
  Pub/Sub не durable, REST worker thread нельзя принудительно прервать asyncio cancel.
  Execution/universe/protection/fills/audit storage не реализовывались. См. статус REVIEW.md.

### [2026-09-12] Ревью готового кода — задания в REVIEW.md
- Прочитаны `CONTEXT.md`, `DESIGN.md` и спецификация стратегии; проверены сигнальное ядро, lifecycle/команды/SDK-адаптеры, trading API и UI.
- Создан `REVIEW.md`: вердикт «требуются исправления», 14 заданий P1/P2 с местами ошибок, воспроизведениями и критериями приёмки. Известные TODO исполнения отделены от дефектов готового слоя.
- Ключевые проблемы: сервисы не сохраняются при запуске стратегии (эмиссия падает), временная несогласованность/дубли/gaps свечей, частичный старт без отката, гонка команд и снапшота, смешение фактической сети и намерения, сброс параметров и доступ обычного пользователя к балансам.
- Проверено: agent pytest — 6 passed; agent ruff и точечный backend ruff — чисто; frontend tsc — чисто; изолированные воспроизведения без сети. Полный backend pytest/mypy/build и живой E2E не запускались. Исходный код не исправлялся.
- **Следующая задача:** исправления по `REVIEW.md`; противоречия boot-off/resume и владельца защиты согласовать до изменения поведения.

### [2026-09-12] Сброс v1: Redis восстановлен, Стратегия — ядро, обвязка на `binance_sdk_spot`
Пользователь удалил весь слой-обвязку v1 (свои `Quote`/`MarketData`/`NewOrder`/
`OrderStatus`/`ExecutionService`/`Portfolio`/`RiskManager`/`FillTracker`/`grid`/
`protector`). Собрано заново вокруг требований:
- **Стратегия = ядро.** `agent/agent/strategies/`: `base.py` (`Strategy`,
  `StrategyServices`, `Signal`), `registry.py` (`@strategy`, `build_strategy`),
  `tripple_screen/` (`settings.py` — все параметры spec §10; `indicators.py` —
  EMA/SMA/Stochastic/Force Index на `Decimal`; `strategy.py` —
  `@strategy("tripple_screen")`, bootstrap H4/H1/M15 через REST, подписка WS
  kline, оценка на закрытии M15 → ENTRY/HOLD, cooldown/epoch/позиции).
- **Обвязка только над SDK.** `agent/agent/exchange/rest.py` (`ExchangeRest`:
  account/balances/klines/ticker_price/exchange_info/new_order/cancel_order/
  open_orders/get_order — родные модели `binance_sdk_spot`, `asyncio.to_thread`);
  `agent/agent/exchange/streams.py` (`MarketDataStreams` над
  `Spot.websocket_streams`).
- **Redis восстановлен.** `agent/agent/engine.py` (Engine: boot `idle`,
  `start_run`/`stop`, heartbeat `trading:heartbeat`, события `agent:events`);
  `agent/agent/common/commands.py` (реестр `@command`, `start`/`stop`/`balance`,
  `resume_from_runtime`). Backend-контракт не менялся по ключам.
- **Запуск Стратегии.** Boot-off; страница `/strategy` (frontend) собирает
  параметры и шлёт `start {network, strategy:"tripple_screen", symbols, params}`;
  агент валидирует и запускает. Снапшот `agent:runtime` → resume.
- **Protector убран.** Прежняя формулировка «защита — часть Стратегии» уточнена:
  spec §7 описывает правила, но их исполнение остаётся будущим слоем вне Signal-only
  стратегии (DESIGN §6); этот слой пока не реализован. Из backend
  (`StartPayload`/`RuntimeSnapshot`/`TradingStatus`, `/trading/protectors`),
  клиента и UI удалено; `KNOWN_STRATEGIES = ("tripple_screen",)`.
- **Документация.** `CONTEXT.md` §4 — обязательное требование
  `binance_sdk_spot`; `DESIGN.md` и `agent/README.md` переписаны под новую
  архитектуру; страница настроек жёстко привязана к параметрам Стратегии.
**Проверено:** agent `ruff check`+`ruff format`+`mypy --strict` чистые; `pytest`
6 тестов (индикаторы + решение/epoch/позиция) — ок; backend ruff+mypy чисто;
frontend biome+tsc+vite build ок. **Хвост:** исполнение ордеров по сигналам
(execution lane), position monitoring/trailing/TP (spec §7), universe-скан
(§1), user-data stream для филлов, audit chain (§8), тесты индикаторов по
hand-calculated fixtures (`STRATEGY-05`).

### [2026-09-09] TESTNET-баланс = 502: это Binance-тестнет лежит, не наш код
`https://testnet.binance.vision/api/v3/time` отдавал 502 (nginx), прод — 200.
Значит 502 идёт от инфраструктуры Binance-тестнета. **Хвост:** перепроверить
после восстановления тестнета.

### [2026-09-08] Архитектурное решение: команды вместо «желаемого состояния»
Один агент, durability не критична (последнее состояние ок), общий канал команд +
снапшот `agent:runtime`, `strategy` = строка, общая ручка команд + тонкие алиасы.

> Записи v1 (портфель, риск, филлы, `grid`, `trailing_stop`, `ExecutionService`,
> старый `_run_loop`) **устарели**: этот код удалён и заменён архитектурой выше.

## ОТКРЫТО / КАНДИДАТЫ НА СЛЕДУЮЩЕЕ

- **Execution lane:** по `Signal(ENTRY/EXIT)` считать размер позиции и ставить
  ордера через `ExchangeRest` (sizing/rounding по фильтрам Binance — `OWNER-SIZING-01`).
- **Position monitoring (spec §7):** initial stop/trailing/TP по live-цене;
  приоритет ≥ entry-evaluation; инвариант `stop < fill < tp`.
- **Universe scan (spec §1):** eligible (quote/trading status/EXCEPTION_ASSETS),
  H4 slope-фильтр, top-N по 24h quote volume, динамический WS-set.
- **Филлы/позиции:** user-data stream (`client.websocket_streams.user_data`)
  или REST-реконсиляция; cooldown персистентный через рестарт (§6).
- **Audit chain (spec §8):** market state → signal → intent → risk → order → outcome.
- **Индикаторы:** зафиксировать seed-конвенцию EMA и decimal policy
  hand-calculated fixtures (`STRATEGY-05`); сейчас EMA засеян SMA — provisional.
- Проверить живой прогон, когда поднимется тестнет / на малой сумме LIVE.
- Вспомогательные сервисы (proxy/adminer/mailpit/playwright) — profile `extras`.
