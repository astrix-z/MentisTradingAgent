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
| Agent — биржа | `agent/agent/exchange/rest.py`, `agent/agent/exchange/streams.py` (тонкие адаптеры над `binance_sdk_spot`) |
| Frontend | `frontend/src/routes/_layout/strategy.tsx` (настройки+старт Стратегии), `trading.tsx` (общий контроль), `frontend/src/client/*` (сгенерировано, НЕ править руками) |
| Док SDK | `binance-spot/docs/`, `binance-spot/examples/` (документация и примеры `binance_sdk_spot`) |
| Deploy | `compose.yml` + `compose.override.yml` (проект `mentis`), `.env` |

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
   Поля страницы жёстко привязаны к параметрам Стратегии (spec §10).
3. Кнопка **Start** → backend публикует команду `start` → Redis → агент.
4. Агент валидирует `params`, создаёт Стратегию и запускает её с этими настройками.
5. **Stop** ведёт к `engine.stop()` (стратегия/потоки гасятся).

**Redis — единственный канал backend↔agent.** У каждого сервиса своя БД; никто не
пишет чужую схему.

- Backend публикует конверт в канал `agent:commands`:
  `{id, type, payload, issued_at}`. Типы: `start` `{network, strategy, symbols, params}`,
  `stop`, `balance` `{network}`.
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
- Redis-ключи/канал держать в синхроне между
  `backend/app/api/routes/trading.py` и `agent/agent/engine.py`.

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
