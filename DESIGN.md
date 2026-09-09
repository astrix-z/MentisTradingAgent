# Agent trading logic — DESIGN

Документ описывает, **как и куда** встраивается логика работы агента (торговый цикл)
поверх существующей командной модели (Redis) в `agent/`. Он задаёт границы модулей,
чтобы добавлять новую стратегию/защитника можно было новым файлом + декоратором, не
трогая движок и команды.

## Терминология

| Термин | Смысл |
|---|---|
| Decision / strategy | «что хочу сделать» — решает, когда входить/выходить; выдаёт *intent*. |
| Execution / orders | «как сделать» — единый слой работы с ордерами биржи. |
| Market data | websocket-потоки котировок отслеживаемых пар. |
| Protection / protector | оверлей защиты поверх открытых позиций (SL/trailing). |
| Portfolio | локальное состояние открытых позиций/сделок (единый источник правды). |
| Risk | глобальный guard между решением и исполнением (лимиты). |

## Ключевой принцип

Разделить **решение**, **исполнение**, **защиту** и **лимиты**. Стратегия и защитник
**никогда не трогают биржу напрямую** — только через слой исполнения. Поэтому ордера,
websocket и позиции общие для всех стратегий.

```
marketdata (websocket котировки)
   │  tick по каждому отслеживаемому символу
   ▼
Engine._run_loop ──► Стратегия (решает)            [strategies/]
        │                       │  intent
        │                       ▼
        │                Risk-гейт (лимиты)             [risk.py]
        │                       │  одобрено
        │                       ▼
        │                Execution / OrderService       [exchange/orders или методы BinanceSpot]
        │                       │  fill / ордер
        │                       ▼
        └──► Portfolio (позиции, pnl) ──► Protector (защитные ордера)   [protection/]
                                            (над любой позицией, независимо от стратегии)
```

## Структура пакета `agent/agent/`

```
agent/agent/
├── commands.py        # (сущ.) start/stop/balance; + выбор strategy/protector, switch_strategy
├── engine.py          # (сущ.) ТОЧКА ПОДКЛЮЧЕНИЯ: Engine.start_run()/._run_loop()
├── core/config.py     # (сущ.) + дефолтные пары, protector, риск-параметры
├── exchange/
│   ├── binance.py     # (сущ.) BinanceSpot: сеть + баланс; + методы ОРДЕРОВ
│   ├── streams.py     # [НОВОЕ] websocket-клиент (обёртка SpotWebSocketStreams)
│   └── __init__.py
├── strategies/        # [НОВЫЙ пакет] стратегии принятия решений
│   ├── base.py        #    абстрактный Strategy + декоратор @strategy("name")
│   ├── __init__.py    #    реестр STRATEGIES
│   └── grid.py        #    @strategy("grid")
├── protection/        # [НОВЫЙ пакет] алгоритмы защиты позиции
│   ├── base.py        #    абстрактный Protector + декоратор @protector("name")
│   ├── __init__.py    #    реестр PROTECTORS
│   └── trailing_stop.py
├── risk.py            # [НОВОЕ] guard/лимиты поверх исполнения
├── portfolio.py       # [НОВОЕ] состояние открытых позиций/сделок
└── marketdata.py      # [НОВОЕ] общий кэш котировок на символ (последняя цена/книга)
```

## Где подключаем логику

Точка подключения — `Engine` (`engine.py`). Режим **run** собирает пайплайн в
`start_run(...)` и гонит его тик-циклом `_run_loop`:

```python
async def start_run(self, *, network, strategy, protector, symbols, params):
    market = MarketData(); feed = MarketDataFeed(market)
    execution = ExecutionService(self._ensure_exchange(network))
    portfolio = Portfolio()
    risk = RiskManager(portfolio, market, ...)          # риск-гейт
    fills = FillTracker(portfolio, execution)           # источник филлов
    self._strategy  = STRATEGIES[strategy](params)
    self._protector = PROTECTORS[protector](params)
    await feed.start(network, symbols)                  # websocket
    await strategy.on_start(sctx); await protector.on_start(pctx)

async def _run_loop(self):
    while running:
        await self._market.wait_updated()               # свежий тик
        await self._fills.reconcile()                   # филлы -> Portfolio
        for symbol in self._symbols:
            await self._strategy.on_tick(sctx, symbol)  # решение
            await self._protector.on_tick(pctx, symbol) # защита
```

Слои (marketdata/portfolio/execution/risk/fills) собраны по шагам 1–6 + филлы;
текущий инкремент-статус — см. ЖУРНАЛ в `CONTEXT.md`.

Реестры (`STRATEGIES`, `PROTECTORS`) — по образцу `HANDLERS` из `commands.py`
(декоратор + dict). `start`-команда валидирует имена и кладёт их в снапшот
`agent:runtime` для resume после рестарта.

## Части

### 1. Стратегии принятия решений — `strategies/` (выбираемые)
- Базовый `Strategy` с хуками `on_start`, `on_tick(symbol, quote)`, `on_fill`, `on_stop`.
- Стратегия возвращает **intent** (сигнал: buy/sell, символ, объём, лимит), исполнение — ниже.
- Реестр `@strategy("name")`; выбор передаётся в `start {strategy}`.
- Чтобы UI мог выбирать — GET `/trading/strategies` отдаёт имена из реестра.

### 2. Работа с ордерами биржи — `exchange/` + ExecutionService (независима от стратегии)
- Методы на `BinanceSpot` (тонкие, network-aware, через `asyncio.to_thread`, т.к. REST блокирует):
  `new_order`, `delete_order`, `get_order`, `get_open_orders`, `cancel_all(symbol)`, `order_oco`.
- Над ним `ExecutionService`: единое место постановки/снятия, ретраи, защита от дублей.
- Стратегия и защитник зовут только его → новые алгоритмы переиспользуют ордерный слой.

### 3. Реальные данные через websocket — `exchange/streams.py` + `marketdata.py`
- Обёртка над `SpotWebSocketStreams` из SDK (есть `ticker`, `book_ticker`, `agg_trade`,
  `diff_book_depth`, `kline`, авто-reconnect).
- asyncio-фасад: `subscribe(symbols)`, колбэк на тик, `get_price(symbol)`.
- Сеть выбирается по `network` (ws-URL prod/testnet разные).
- `user_data`-поток (по listen-key) даёт реальные fills и изменения аккаунта → кормит Portfolio.

### 4. Логика защиты позиции — `protection/` (выбираемые алгоритмы, оверлей)
- Работает **поверх любой стратегии**: следит за открытыми позициями и ценой,
  переставляет защитные ордера (stop-loss, trailing, take-profit, тайм-выход).
- Тот же паттерн `@protector("name")`; выбор в `start {protector}`.

## Дополнительно (добавлено по собственному решению)

1. `risk.py` — риск-гейт между решением и исполнением (лимиты по позициям/notional,
   число открытых ордеров, стоп по дневному убытку, аварийный flatt). Рекомендуется сразу.
2. `portfolio.py` — локальное состояние позиций/сделок (вход, объём, средняя цена, pnl) —
   общий источник для стратегии, защитника и UI.
3. Общий кэш котировок (`marketdata.py`) — один объект «последняя цена/книга по символу».
4. Канал `agent:events` в Redis — live-события (fills, движение защитных ордеров, ошибки)
   отдельно от одноразового `agent:result`.
5. Снапшот `agent:runtime` расширяется полями `protector`, `symbols`, риск-лимиты →
   полный resume после рестарта.

## Влияние на другие слои (кратко)

- `backend/app/api/routes/trading.py`: расширить `start`-payload и статус полями
  `protector`, `symbols`; GET `/trading/strategies` и `/trading/protectors`.
- `frontend`: выбор стратегии/защитника, показ активных символов.
- `commands.py`/`engine.py`: команда `switch_strategy` (смена на лету) — пример из
  «Открыто» в CONTEXT.md.

## Порядок внедрения

1. `exchange/streams.py` (websocket котировки) + общий кэш (`marketdata.py`).
2. Ордерные методы на `BinanceSpot` + `ExecutionService`.
3. `portfolio.py`.
4. `risk.py` (включить сразу).
5. `strategies/` с первой стратегией (`grid`).
6. `protection/` с первым защитником (`trailing_stop`).
7. Провязка выбора сквозь команды/статус/UI + resume.

> TODO по мере реализации: фиксировать здесь принятые сигнатуры контекстов и событий.
