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
- Стек проверок: `ruff` + `mypy --strict` (оба python-пакета); `biome` + `tsc` (frontend).

## 2. Карта «куда смотреть»

| Слой | Точки входа (главное) |
|---|---|
| Backend API | `backend/app/api/routes/trading.py` (командный контур), `backend/app/api/main.py` (регистрация роутеров), `backend/app/core/redis_client.py`, `backend/app/main.py` (lifespan), `backend/app/core/config.py` |
| Agent | `agent/agent/main.py` (подписка/диспетчер), `agent/agent/commands.py` (реестр `@command`), `agent/agent/engine.py` (движок, heartbeat), `agent/agent/exchange/binance.py` (обёртка `binance-sdk-spot`), `agent/agent/core/config.py` |
| Frontend | `frontend/src/routes/_layout/trading.tsx` (страница Trading), `frontend/src/client/*` (сгенерировано, НЕ править руками) |
| Deploy | `compose.yml` + `compose.override.yml` (имя проекта `mentis`, образы `mentis-backend`/`mentis-agent`), `.env` |

## 3. Ключевая архитектура (не «желаемое состояние», а КОМАНДЫ)

UI и агент не общаются напрямую. Единственный канал backend↔agent — **Redis**.
У каждого сервиса своя БД; никто не пишет и не мигрирует чужую схему.

- Backend публикует конверт команды в канал `agent:commands`:
  `{id, type, payload, issued_at}`. Типы: `start` `{network,strategy}`, `stop`, `balance` `{network}`.
- Агент слушает канал и вызывает обработчик по типу (реестр + декоратор `@command`).
  **Новая команда = новый обработчик в commands.py**, цикл подписки не трогается.
- `start`/`stop` пишут снапшот `agent:runtime` (для resume после рестарта + статус UI);
  `balance` снапшот не трогает.
- Результат команды агент пишет в `agent:result` (`CommandContext.report`),
  backend отдаёт через `GET /trading/result`.
- Живость: heartbeat `trading:heartbeat` (TTL 30 c). `GET /trading/status` =
  намерение (снапшот) + факт (heartbeat) + balances.
- Redis-ключи/канал должны совпадать между
  `backend/app/api/routes/trading.py` и `agent/agent/engine.py` (+ `commands.py`).

## 4. Обвязка binance (важно)

`binance-sdk-spot` (модуль `binance_sdk_spot`). REST-методы **синхронные/блокирующие**
→ вызывать только через `asyncio.to_thread`. Сеть выбирается по `network`
(testnet/live) + кредам из config (`BINANCE_*` live, `BINANCE_TESTNET_*`).
Ответы: `resp.data()` возвращает модель (метод), `.balances[]` → поля asset/free/locked (строки).

## 5. Генерация клиента и сборка UI (делать при изменении моделей backend)

- Собрать схему: из `backend/` (нужен существующий каталог `backend/app/frontend`)
  `uv run python -c "import json;from app.main import app;json.dump(app.openapi(),open('../frontend/openapi.json','w'))"`
  (задать env: FASTAPI_ENV=development, SECRET_KEY, DATABASE_URL, FIRST_SUPERUSER, FIRST_SUPERUSER_PASSWORD).
- Из `frontend/`: `bun node_modules/@hey-api/openapi-ts/bin/run.js` (путь от корня репо).
- Сборка UI в `backend/app/frontend` (которое раздаёт backend): `bun node_modules/vite/bin/vite.js build`.
- **НЕ использовать `bun run build` / `bunx` напрямую** — в этой среде зависают.

## 6. Запуск / просмотр (локально на хосте)

- **Поднять весь рабочий стек одной командой:** `docker compose up -d --build`
  (поднимает `db` + `redis` + `backend` + `agent`). Миграции и суперюзер применяются
  автоматически при старте backend через entrypoint `backend/scripts/entrypoint.sh`
  (idempotent `prestart.sh`), отдельный прогон не нужен.
- Вспомогательные сервисы (Traefik proxy, adminer, mailpit, playwright) — за profile
  `extras`, чтобы не конфликтовать с занятым портом 80 и не тянуться в дефолтном аппе:
  `docker compose --profile extras up -d`.
- Приложение: **http://127.0.0.1:8000/** (НЕ `:80` — порт 80 занят системным httpd AlmaLinux;
  Traefik/proxy по умолчанию не поднимается, это норма).
- Креды для входа/Баланс-ключи — в `.env` (не хардкодить в этом файле).
  См. `FIRST_SUPERUSER`/`FIRST_SUPERUSER_PASSWORD`, `BINANCE_*`, `BINANCE_TESTNET_*`.

## 7. Быстрые проверки (среда)

- backend/agent: `cd backend && uv run ruff check app && uv run mypy app`; аналогично `agent`.
- frontend: `bun node_modules/@biomejs/biome/bin/biome check <file>`;
  `bun node_modules/typescript/bin/tsc -p tsconfig.build.json`;
  сборка = vite (см. п.5).

---

## ЖУРНАЛ (последняя задача сверху)

### [2026-09-09] `docker compose up -d --build` поднимает весь рабочий стек сам
Сделано, чтобы полная команда сразу давала рабочее приложение без отдельного шага миграций
и без конфликтов портов/сервисов.
- **Автозапуск миграций:** `backend/scripts/entrypoint.sh` (новый) → ставится ENTRYPOINT в
  `backend/Dockerfile`; на старте гоняет `prestart.sh` (alembic upgrade + initial_data,
  идемпотентно) и затем exec реальной команды (fastapi run/dev из CMD/override).
- **Профиль `extras`:** proxy(80), adminer, mailpit, playwright убраны из дефолтного аппа —
  `compose.yml` (profiles на proxy/adminer), `compose.override.yml` (profiles на
  mailpit/playwright). Дефолт `up` = db, redis, backend, agent. Профиль включается
  `docker compose --profile extras up -d`.
**Проверено (на хосте):** `docker compose config -q` ок; `docker compose up -d --build`
собрал образы (~18s, кэш) и поднял db/redis/backend/agent; backend `healthy` (значит миграции
и health-эндпоинт работают). **Файлы:** `compose.yml`, `compose.override.yml`,
`backend/Dockerfile`, `backend/scripts/entrypoint.sh` (новый), `CONTEXT.md` §6/ОТКРЫТО.
**Хвост:** расширенный стек — через `--profile extras`; проверить UI/ран торговли вживую,
когда поднимется тестнет.

### [2026-09-09] Frontend Trading: выбор strategy/protector/symbols + показ в Status
Клиент перегенерён (openapi -> client): `StartPayload` c protector/symbols/params,
`TradingStatus` c protector/symbols, `TradingService.listStrategies()/listProtectors()`.
`frontend/src/routes/_layout/trading.tsx`: списки стратегий/защитников загружаются и даются
как подсказки (datalist) + поля Protector и Symbols (CSV); start шлёт
`{network,strategy,protector,symbols}`; валидация (нужны strategy/protector/>=1 символ);
Status показывает Protector и Symbols; при изменении полей во время работы ран рестартится
(blur). Собирается статика в `backend/app/frontend`.
**Проверено:** biome check (ок), tsc -p tsconfig.build.json (ок), vite build (ок, ~1.2s).
**Хвост:** пересобрать/перезапустить контейнеры backend+agent (новые поля команд/клиент) и
hard refresh UI; реальный ран требует тестнет/кредактив (тестнет лежит).

### [2026-09-09] Доки приведены к реализации + backend: start-пейлоад/снапшот/статус/GET-списки
**Доки:** `DESIGN.md` (диаграмма/дерево/«где подключаем» — теперь `start_run`/`_run_loop`,
слои по шагам), `agent/README.md` (структура актуальна), `CONTEXT.md` ОТКРЫТО/упоминания
`_trading_loop` убраны.
**Backend** (`backend/app/api/routes/trading.py`): `StartPayload` расширен (`protector`,
`symbols`, `params`); `RuntimeSnapshot` и `TradingStatus` + `protector`/`symbols`;
`_start_command_payload`/`_start_payload` шлют полный пейлоад; снапшот на start пишет
protector/symbols/params (на stop чистит); `get_status` отдаёт protector/symbols; новые
`GET /trading/strategies` и `GET /trading/protectors` из `KNOWN_STRATEGIES`/`KNOWN_PROTECTORS`
(коммент держать в синхроне с @strategy/@protector агента).
**Проверено:** backend ruff+mypy чистые (21 файл); agent ruff+mypy чистые.
**Хвост:** frontend-клиент не перегенерён (новые поля/ручки); UI-шаг: перегенерация клиента,
выбор стратегии/защитника/символов, показать их в Status.

### [2026-09-09] Команды: `start` ведёт на `start_run` + resume по снапшоту
`commands.py`: `_start` читает опциональные `protector`/`symbols`/`params`; если есть
`symbols` — зовёт `ctx.engine.start_run(...)` (ValueError ловится и репортится ok=False),
иначе legacy `engine.start` (балансовый режим для backends, не шлющих полный ран).
Добавлены `_default_protector()` (trailing_stop, если зарегистрирован) и `_as_params()`.
`resume_from_runtime`: если снапшот несёт `symbols` и strategy/protector зарегистрированы —
`start_run`, иначе legacy `start`. Backend сейчас шлёт только network/strategy, поэтому run-путь
пока не триггерится реальными командами — но готов.
**Проверено:** ruff+mypy strict чистые (23 файла). Офлайн-тест dispatch: без symbols ->
engine.start; с symbols -> start_run(network,strategy,default protector,symbols,params) и
ok=True c symbols; неизвестная стратегия -> ok=False message. **Хвост:** расширить backend
(start-payload + снапшот полями protector/symbols/params) и UI — тогда start реально запускает
ран. ОТКРЫТО/ДОКИ всё ещё упоминают старый `_trading_loop`/README — поправить доки.

### [2026-09-09] Движок: собран тик-цикл торгового пайплайна (start_run/_run_loop)
`engine.py` переписан: добавлен режим **run** (`start_run(network,strategy,protector,
symbols,params)`): композирует MarketData+MarketDataFeed(ws), ExecutionService(exchange),
Portfolio, RiskManager(orders_provider=open_orders, лимиты из params["risk_limits"]),
FillTracker, создаёт strategy/protector из `STRATEGIES`/`PROTECTORS` + их контексты (с fills),
стартует фид, on_start обоих, ре-консиляция филлов, публикует heartbeat. `_run_loop`:
ждёт `market.wait_updated()` -> reconcile всех филлов -> на каждый символ `_tick()`
(reconcile + strategy.on_tick + protector.on_tick); каждые 10 тиков тихий refresh баланса.
`_tick`/`_reconcile_all` вынесены (тестируемо). `stop()`/`_teardown_run()` гасят
strategy.on_stop/protector.on_stop/feed.stop и чистят поля. Legacy `start(network,strategy)`
(балансовый режим `_balance_loop`) сохранён для текущих команд/resume; `start_run` пока никто
не зовёт. Heartbeat/баланс/балансовая команда не тронуты.
**Проверено:** ruff+mypy strict чистые (23 файла). Офлайн-тест `_tick`: рестующий BUY
филлится -> портфель пополнен, стратегия и защитник вызваны на символ, повторный тик дубля
не даёт. **Хвост:** расширить команды/`start_run`-вызов полями strategy/protector/symbols/
params + resume по снапшоту (и backend/UI) — следующий шаг, тогда run реально запускается.

### [2026-09-09] Инкремент: контексты авто-следят за рестующими ордерами (fills)
`StrategyContext` и `ProtectionContext` получили опциональный `fills: FillTracker`;
после постановки рестующего ордера (buy_limit/sell_limit LIMIT, place_stop_sell
STOP_LOSS_LIMIT) контекст сам зовёт `FillTracker.watch(...)` — стратегии/защитник
«бесшовно» кормят Portfolio, когда их ордер исполнится.
**Файлы:** `strategies/base.py`, `protection/base.py` (параметр fills + `_watch_resting`).
**Проверено:** ruff+mypy strict чистые. Офлайн-тест: strategy buy_limit -> watch_count=1 ->
филл через reconcile попадает в Portfolio (qty/avg), ордер уходит из watch; protection
place_stop_sell -> watch — ок. **Хвост:** никто ещё не зовёт `reconcile` по тику (это
движок/`_trading_loop`).

### [2026-09-09] Фундамент: FillTracker (источник филлов по своим ордерам)
**Файл:** `agent/agent/fills.py` (новый). `Fill(symbol,side,qty,price)` +
`FillTracker(portfolio, execution)`: следит за REST-ордерами, которые поставили МЫ, и
детектит филлы по `executed_qty`. `watch(order_id,symbol,side,price)` — взять рестующий
ордер под наблюдение; `reconcile(symbol?)` — каждый тик движка: `get_order` по каждому
наблюдаемому ордеру, дельта executed_qty (>EPS) -> `Portfolio.record_fill` + вернуть
`Fill`; убрать из наблюдения, когда ордер ушёл из open (FILLED/CANCELED/EXPIRED). Частичные
филлы тоже ловятся. Market-ордера исполняются сразу и сюда не идут (движок применит их
напрямую с текущей ценой).
**Почему так (а не user_data websocket):** listen-key REST в SDK нет, user-data поток
требует ключ, который SDK сам не заводит, тестнет лежит → путь не проверить; в этом боте все
сделки инициируем только мы, поэтому филлы выводятся из жизни наших ордеров детерминированно.
**Проверено:** ruff+mypy strict чистые (23 файла). Офлайн-тест: частичный->полный филл BUY
в Portfolio (qty/avg), отмена с частичным исполнением ловится, отмена без исполнения — нет,
уход из наблюдения по FILLED/CANCELED — ок.
**Хвост:** никто ещё не зовёт `watch`/`reconcile` — следующий инкремент: либо контексты
(стратегия/защитник) авто-ставят на watch свои рестующие ордера + движок зовёт reconcile по
тику, либо напрямую встроить в Engine. user_data-ws остаётся будущей оптимизацией.

### [2026-09-09] Шаг 6: protection/ (реестр + первый алгоритм trailing_stop)
По DESIGN.md шаг 6. **Файлы:** `agent/agent/exchange/execution.py` (+ `place_tagged`
— низкоуровневый заказ ордера с идемпотентным client-id для произвольных типов);
`agent/agent/protection/base.py` (новый): `ProtectionContext` (execution+portfolio+market+
risk+config; `position()/open_positions()/current_price()`, `place_stop_sell` SELL stop-limit
через риск-гейт — SELL всегда разрешён, `cancel` best-effort), `Protector` (хуки
on_start/on_tick(symbol)/on_fill/on_stop), реестр `PROTECTORS` + `@protector(name)` +
`available_protectors()`. `agent/agent/protection/trailing_stop.py` (новый): `@protector(
"trailing_stop")` — трал стоп за лучшей ценой (ratchet вверх), замена при сужении/изменении
qty, снятие при закрытии позы. `agent/agent/protection/__init__.py`.
**Проверено:** ruff+mypy strict чистые (22 файла). Офлайн-тест: реестр, первичная постановка
стопа 1 BTC, замена при росте цены (stop 49490->49980), без замены при откате выше стопа,
снятие при закрытии позы — ок. **Хвост:** защитник ещё не вызывается движком. Следующий шаг
— интеграция в Engine/`_trading_loop`: фид -> portfolio -> risk -> strategy + protector
(и расширение start-команды полями strategy/protector/symbols).

### [2026-09-09] Шаг 5: strategies/ (реестр + первая стратегия grid)
По DESIGN.md шаг 5. **Файлы:** `agent/agent/strategies/base.py` (новый): `StrategyContext`
(execution+portfolio+market+risk+config; buy_limit/sell_limit идут ЧЕРЕЗ риск-гейт, cancel
best-effort), `Strategy` (хуки on_start/on_tick(symbol)/on_fill/on_stop), реестр
`STRATEGIES` + `@strategy(name)` + `available_strategies()`. `agent/agent/strategies/grid.py`
(новый): `@strategy("grid")` — лестница лимиток вокруг якоря (spacing/levels/notional_per_level),
ре-якорь при уходе цены >=1 ячейка; все ордера через risk+execution.
`agent/agent/strategies/__init__.py` (реестр + авто-импорт grid).
**Проверено:** ruff+mypy strict чистые (19 файлов). Офлайн-тест: реестр, на старте ставится
levels*2 ордеров (buy ниже/sell выше якоря по ценам), ре-якорь при сдвиге цены (cancel 4 +
place 4), stop гасит лэддер — ок. **Хвост:** engine ещё не запускает strategy (соберём на шаге
торгового цикла). Следующий шаг — protection/ (первый алгоритм trailing_stop).

### [2026-09-09] Шаг 4: risk.py (риск-гейт между решением и исполнением)
По DESIGN.md шаг 4. **Файл:** `agent/agent/risk.py` (новый). `RiskLimits` (max_symbols,
max_position_notional, max_total_notional, max_open_orders, max_daily_loss), `RiskDecision`
(ok/reject), `RiskError(ExchangeError)`, `RiskManager(portfolio, market, limits, orders_provider)`. 
`can_place(order, quote_price)` / `assert_can_place` (raise). Правила: SELL (снижение риска)
всегда разрешён в рамках лимита ордеров (защитник всегда может срезать позу); BUY сверяет
per-symbol notional, общий notional (сумма по позициям через market mid), число символов,
дневной убыток (session_realized = realized - baseline на старте). Без reference price BUY
отклоняется (нельзя оценить notional). `snapshot()` для логов/UI. Счёт open-orders — опц.
провайдер (если нет — пропускается).
**Проверено:** ruff+mypy strict чистые; офлайн-тест: per-symbol/общий notional, max_symbols,
SELL всегда разрешён, дневной лимит убытка, assert_can_place raise — ок.
**Хвост:** risk не связан с движком/ExecutionService (сделаем на шаге сборки движка); убрать
встроенную валидацию qty/price из ExecutionService, чтобы не дублировалась, или оставить.
Следующий шаг — `strategies/` (реестр + первая стратегия `grid`).

### [2026-09-09] Шаг 3: portfolio.py (состояние позиций + pnl)
По DESIGN.md шаг 3. **Файл:** `agent/agent/portfolio.py` (новый). `Position` (symbol,
quantity, avg_price|None, realized_pnl, время) + `Portfolio` (по символу, только лонг в base).
`record_fill` (BUY/SELL): BUY растёт qty + скользящая средняя цена входа; SELL уменьшает
и реализует pnl; заявка с «битым» qty/price игнорируется; перепродажа капится по позе.
`reconcile_quantity` — якорение с биржи на boot/restart (цена входа неизвестна → avg None).
`set_entry_price` — явный якорь avg из истории сделок. `unrealized_pnl(price)` при avg None = 0.
**Проверено:** ruff+mypy strict чистые; офлайн-тесты pnl/avg/unanchored/reconcile/anchor — ок.
**Хвост:** реальные fills на PORTFOLIO придут из user_data-стрима ордеров (ещё не подключён).
Следующий шаг — `risk.py` (риск-гейт между решением и исполнением).

### [2026-09-09] Шаг 2: слой ордеров (BinanceSpot + ExecutionService)
По DESIGN.md шаг 2. **Файлы:** `agent/agent/exchange/order_types.py` (новый, доменные
`NewOrder`/`OrderResult`/`OrderStatus`/`CancelResult`, сетевые значения-строки);
`agent/agent/exchange/binance.py` (+ методы ордеров: `place_order`, `cancel_order`,
`get_order`, `open_orders` — тонкие, network-scoped, через `asyncio.to_thread`,
маппинг enum + общий парсер в `OrderStatus`); `agent/agent/exchange/execution.py`
(новый, `ExecutionService`: единый интерфейс исполнения для стратегий/защитника —
`market`/`limit`/`cancel`/`cancel_all`/`open_orders`/`is_order_open`, идемпотентные
client-order-id `run-tag-uuid`); `exchange/__init__.py` (экспорт нового слоя).
**Проверено:** ruff+mypy strict чистые; офлайн-тест `ExecutionService` на заглушке биржи
(market/limit/валидация qty&price/cancel_all/unique client id/is_order_open) — ок;
импорт пакета ок. **Хвост:** реальную постановку ордера не тестировали (LIVE = реальные
сделки, TESTNET лежит/502). Следующий шаг — `portfolio.py` (состояние позиций).

### [2026-09-09] DESIGN.md + шаг 1: websocket-котировки (market data)
Оформлен `DESIGN.md` (архитектура торговой логики агента: стратегии/защитники как
реестры-декораторы, ордера через общий Execution-слой, risk/portfolio/marketdata;
порядок внедрения из 7 шагов). Реализован **шаг 1**: websocket-фид котировок.
**Файлы:** `DESIGN.md` (новый); `agent/agent/marketdata.py` (новый, кэш Quote/MarketData);
`agent/agent/exchange/streams.py` (новый, `MarketDataFeed` над `SpotWebSocketStreams`,
book_ticker, авто-reconnect SDK, сеть по network); `agent/agent/exchange/__init__.py`
(экспорт `MarketDataFeed`).
**Проверено:** ruff+ mypy (strict) чистые; offline-тест кэша/парсинга тика; живой фид на
прод: старт + BTCUSDT book_ticker дал реальную котировку (bid/ask) за секунды.
**Хвост:** следующий шаг по DESIGN — ордерные методы на `BinanceSpot` + `ExecutionService`.

### [2026-09-09] TESTNET-баланс = 502: это Binance-тестнет лежит, не наш код
**Симптом:** баланс TESTNET падает с «failed to fetch account balance: Server error: 502»,
LIVE — ок. Гипотеза пользователя подтверждена: `https://testnet.binance.vision/api/v3/time`
(публичный, без авторизации) прямо сейчас отдаёт 502 Bad Gateway (nginx);
`https://api.binance.com/api/v3/time` — 200 OK. Значит 502 идёт от инфраструктуры
Binance-тестнета (лежит/нестабилен), а не от конфигов/ключей/кода.
Зелёный тост «Balance requested» — не баг: он срабатывает на HTTP 202 (команда принята
в очередь), а реальный исход показывает Command Result (там корректно видна ошибка).
**Изменения:** нет (править нечего). **Проверено:** fetch /api/v3/time тестнета (502)
и прода (200). **Хвост:** перепроверить после восстановления тестнета; при желании можно
смягчить тост (напр. нейтральный «requested»), но это косметика.

### [2026-09-09] Trading: убран дубль баланса в Status (остался только Command Result)
**Проблема:** после команды `balance` одни и те же суммы показывались дважды — в блоке
Status (`status.balances` из heartbeat агента) и в Command Result (`commandResult.data`).
При смене сети TESTNET/LIVE баланс в блоке Status «зависал»: `Engine.fetch_account_balance`
кэширует в heartbeat только когда запрошенная сеть == сети запущенного движка, поэтому
старые суммы оставались.
**Решение:** карточка Status — про живость/состояние агента; вывод баланса убрали оттуда.
Единственное место показа результата `balance` — карточка Command Result (в неё уже
приходит свежий результат с выбранной сетью, т.к. `Check balance` шлёт текущий `network`).
**Файлы:** `frontend/src/routes/_layout/trading.tsx` (убран блок `status?.balances`),
пересобрана статика в `backend/app/frontend`.
**Проверено:** biome check (чисто), tsc -p tsconfig.build.json (ок), vite build (ок, ~1.3s).
**Хвост:** live-проверка в браузере (hard refresh). Если захотим живой мониторинг баланса
запущенного движка в Status — это отдельный блок, но его надо согласовать, чтобы не было
дубля с Command Result.

### [2026-09-09] LIVE-баланс не работал → сделан network-aware запрос баланса
**Проблема:** переключение сети LIVE в UI не влияло на команду `balance` (сеть была
параметром только `start`), поэтому запрос шёл на testnet; ошибки биржи молча
превращались в пустой результат → страница «не обновлялась».
**Решение:** команда `balance` теперь несёт целевую сеть; движок умеет запрашивать
баланс по произвольной сети; ошибки пробрасываются и показываются в UI.
**Файлы:** `agent/agent/engine.py` (`fetch_account_balance(network=...)`, свойство
`network`, `_connected_network`), `agent/agent/commands.py` (`_balance` + `ExchangeError`),
`backend/app/api/routes/trading.py` (`NetworkRequest`, `POST /trading/balance` c телом,
передача payload в generic-ручке), frontend `trading.tsx` + перегенерация клиента.
**Проверено:** ruff+mypy (обоих), biome+tsc+vite build; по API live-баланс вернул
`ok=true`, активы `BNB`, `USDT`. Образы `mentis-backend`/`mentis-agent` пересобраны.
**Хвост:** обновить живую страницу в браузере (hard refresh) у пользователя.

### [2026-09-08] Реализован командный контур (start/stop) + результат команды
Backend-эндпоинты команд, движок по network+strategy, диспетчер команд, heartbeat,
снапшот, `GET /trading/status` и `GET /trading/result`, страница Trading.
**Файлы:** `backend/app/api/routes/trading.py`, `backend/app/api/main.py`,
`backend/app/core/redis_client.py`, `backend/app/main.py` (lifespan);
`agent/agent/{main,commands,engine}.py`, `agent/agent/exchange/binance.py`;
frontend `trading.tsx`, `switch.tsx`, regenerated client.

### [2026-09-08] Архитектурное решение: команды вместо «желаемого состояния»
Согласовано: один агент, durability не критична (последнее состояние ок), статус/результаты
через БД по месту, общий канал команд + снапшот, `strategy` = строка, общая ручка
команд + тонкие алиасы, фронт — отдельным шагом.

## ОТКРЫТО / КАНДИДАТЫ НА СЛЕДУЮЩЕЕ
- Расширить backend/UI, чтобы `start` реально запускал ран: payload+снапшот с полями
  `protector`/`symbols`/`params`, показ в статусе, GET `/trading/strategies` и
  `/trading/protectors`; UI-выбор стратегии/защитника/символов.
- Показать, как добавить новую команду насквозь (пример: `sell_all`, `switch_strategy`).
- Довести до продакшн-качества стратегию `grid` (логика филлов) и защитники; реальный
  сетевой прогон, когда поднимется тестнет / на маленькой сумме LIVE.
- Traefik/proxy, adminer, mailpit, playwright убраны из дефолтного аппа за profile
  `extras` (порт 80 хоста занят httpd; дефолт = db/redis/backend/agent на `:8000`).
  Если понадобится proxy (нужен свободный 80): `docker compose --profile extras up -d`.
