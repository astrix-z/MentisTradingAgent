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
