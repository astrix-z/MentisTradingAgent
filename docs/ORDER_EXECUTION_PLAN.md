# План: реальное исполнение ордеров по сигналам стратегии

| | |
|---|---|
| Статус | **v1 + ответы на ревью R1–R26 + решения владельца по Q2, Q3, Q5, Q8, Q10** (2026-09-24). Остальные Q — предложения в §12, ждут подтверждения; после них выпускается v2 |
| Автор v1 | Claude-Mentis (Claude Code) по запросу владельца |
| Трекер | ClickUp, список MentisTradingAgent — задача «Execution: план реального исполнения ордеров» |
| Порядок обсуждения | §12. Предложения ревьюера пишутся **в этот файл**, не в чат |

Документ самодостаточен: ревьюеру не нужен предыдущий контекст разговоров. Нужно
лишь прочитать файлы, на которые ссылается §2, и спецификацию стратегии
`docs/STRATEGY_TRIPPLE_SCREEN.md` (§4.1–4.2, §6, §7, §8, §12).

---

## 1. Проблема

Агент (`agent/`) эмитит `Signal(ENTRY|EXIT|HOLD)`, но **ни одного ордера не
ставит**. `Engine._on_signal` (`agent/agent/engine.py:272`) пишет сигнал в лог и
`signals.log` и публикует его в Redis pub/sub `agent:events`. Больше ничего не
происходит. Всё «исполнение» сейчас виртуальное:

- backend (`backend/app/core/signal_listener.py`) превращает ENTRY в строку
  `SignalPosition`;
- позиция закрывается **лениво при чтении** `GET /trading/positions/`
  (`backend/app/api/routes/trading.py:723`, `_position_public`). Закрытие
  происходит, когда кто-то открыл страницу, по `last_price` из 24h-тикера, которому
  до 5 минут. Отсюда «стоп исполнился на 1.7% ниже стопа» у SENTUSDT 2026-09-23
  (ClickUp 869f6m7vn): `closed_at=15:14:52` — это момент просмотра страницы, а не
  момент пересечения;
- стратегия не знает о позициях: `mark_position_open` / `mark_position_closed`
  (`agent/agent/strategies/tripple_screen/strategy.py:470`) никто не вызывает.
  Поэтому по одному символу подряд идут ENTRY и не работает cooldown (ClickUp
  869f6m7vg). На проде так было с SENTUSDT: 12:15, 12:30 и 13:00;
- EXIT стратегия не эмитит вообще: position monitoring (spec §7) не реализован.

Живой аккаунт: USDT 13.86, BNB 0.002 (heartbeat от 2026-09-24).

## 2. Что уже есть и на что опираемся

| Что | Где | Состояние |
|---|---|---|
| Контракт сигнала | `agent/agent/strategies/base.py` (`Signal`) | ENTRY несёт `entry_ref`, `swing_low_ref`, `tp_rule="fill x 1.1"`, `valid_until`, `epoch_id`, `signal_id` |
| Спецификация выходов | `docs/STRATEGY_TRIPPLE_SCREEN.md` §7 | initial stop `min(swing_low_ref, fill×0.99)`, trailing по M15, TP `fill×1.10`, инвариант `stop < fill < tp` |
| Границы ответственности | spec §8, §12; `DESIGN.md` §6 | стратегия = `D-SIGNAL-ONLY`; sizing/ордера/защита — Planner/OM (`OWNER-SIZING-01`, `OWNER-POSITION-01`) |
| REST-адаптер | `agent/agent/exchange/rest.py` | есть `new_order`, `cancel_order`, `open_orders`, `get_order`, `account`, `exchange_info`; нет OCO/order list, `order_test`, `my_trades` |
| Потоки | `agent/agent/exchange/streams.py` | только market data (kline); реконнект с backoff уже есть |
| SDK | `binance-spot/examples/rest_api/Trade/*`, `.../websocket_api/UserDataStream/*` | есть `order_oco`, `order_list_otoco`, `order_test`, `order_cancel_replace`, `delete_order_list`, User Data Stream через WS API |
| БД агента | `agent/agent/models.py`, `agent/agent/core/db.py` | своя БД, `create_all`, пока 2 таблицы |
| Канал в backend | Redis: `agent:events` (pub/sub, **без истории**), heartbeat `trading:heartbeat` | потеря события при отсутствии подписчика допустима только для UI |
| Жёсткое правило | `CONTEXT.md` §4 | с биржей работаем **только** через `binance_sdk_spot`, свои модели ордеров запрещены |

## 3. Цели и не-цели

**Цели**
1. На ENTRY реально купить на бирже, с защитой (stop + TP) на стороне биржи.
2. Вести позицию по spec §7: трейлинг, стоп, TP; при выходе эмитить EXIT с
   `exit_reason ∈ {stop, trail, tp}`.
3. Одна позиция на символ и cooldown после закрытия, в том числе через рестарт
   (spec §6).
4. Корректность при сбоях: рестарт агента, обрыв сети, неоднозначный ответ
   REST (таймаут после отправки). Позиция не остаётся без защиты, ордер не
   дублируется.
5. Полная audit chain `signal → intent → risk → plan → order → fill → outcome` (spec §8).
6. Backend и UI показывают **реальные** позиции и ордера, а не виртуальные.

**Не-цели (v1)**
- Шорт, маржа, фьючерсы. Стратегия long-only, spot.
- Несколько стратегий одновременно.
- Портфельная оптимизация и ребалансировка.
- Ручная торговля из UI (кроме аварийного «закрыть позицию», см. §8.4).

## 4. Архитектурное решение: исполнение живёт в агенте

**Решение:** новый пакет `agent/agent/execution/`, подключённый в
`Engine._on_signal`. Backend остаётся read-model и пультом управления.

Почему не backend:
- ключи биржи, SDK-клиенты, реконнекты и market data уже в агенте;
- backend ↔ agent общаются только через Redis, и лишний прыжок ENTRY → backend →
  команда → agent добавляет задержку и точку потери на пути с `valid_until` = 15 мин;
- `CONTEXT.md` §3: «открытие/закрытие ордеров» прямо названо обвязкой агента.

Стратегия по-прежнему не трогает биржу. Она получает обратную связь через
`mark_position_open` / `mark_position_closed`, а трейлинг-стоп считает execution-слой
по закрытым M15, которые ему отдаёт стратегия или market data (§7.3).

```
Strategy ──Signal──▶ Engine._on_signal
                        │
                        ├─▶ signals.log / agent:events            (как сейчас)
                        ▼
                  ExecutionLane
   ┌──────────────┬───────────────┬──────────────┬───────────────┐
   │ Intent       │ Risk          │ Planner      │ OrderManager  │
   │ (из Signal,  │ (лимиты,      │ (sizing,     │ (SDK-вызовы,  │
   │  valid_until)│  cooldown,    │  фильтры,    │  client ids,  │
   │              │  1 поз/символ)│  цены)       │  идемпотент.) │
   └──────────────┴───────────────┴──────────────┴───────┬───────┘
                                                         ▼
   PositionMonitor ◀── fills (User Data Stream / REST reconcile) ── Binance
     trailing по M15, live-price guard, EXIT → Strategy.mark_position_closed
                                                         │
   Persistence (БД агента): intents, orders, positions, cooldowns, audit
                                                         │
   Publish: agent:executions (Redis Stream) → backend read model → UI
```

## 5. Модель ордеров (ключевое решение, нужен ревью)

### 5.1 Вход

**Предложение: `LIMIT` + `timeInForce=IOC` по цене `entry_ref × (1 + max_slippage_pct)`**,
количество в базовом активе.

- В отличие от MARKET это ограничивает проскальзывание на тонких альтах (в
  WS-set в основном они: SENTUSDT, MINAUSDT и т.п.).
- IOC не оставляет висящей заявки: исполнилось, частично исполнилось или ничего.
- Частичное исполнение — нормальный случай: позиция = фактически исполненное
  количество. Если оно ниже `minNotional` для выхода, это dust (§8.5).

Проверки непосредственно перед отправкой (spec §4.2):
`now < valid_until`; текущая цена (bookTicker ask) `> stop_ref`, иначе гэп вниз и
вход отменяется; символ `TRADING`; нет открытой позиции и активного cooldown.

Альтернативы: (b) MARKET с `quoteOrderQty`: проще, но нет контроля цены. (c) OTOCO
(LIMIT-вход + отложенный OCO): защита ставится атомарно с входом, но стоп должен
быть известен **до** fill, а по spec он зависит от fill. Можно считать от
лимитной цены, но это расходится со spec §7.1. Отложил до ревью.

### 5.2 Защита после fill

**Предложение: сразу после fill ставим OCO SELL (`order_oco`, новый orderList API):**
- above: `LIMIT_MAKER` по `tp_price = fill × (1 + tp_pct)`, округление по tick вверх;
- below: `STOP_LOSS_LIMIT` со `stopPrice = stop_price` и `price = stop_price × (1 − stop_limit_gap_pct)`,
  или `STOP_LOSS` (market), если символ его поддерживает. Проверить `orderTypes` в
  `exchangeInfo` символа;
- `stop_price = min(swing_low_ref, fill × (1 − min_stop_distance_pct))`, округление по tick вниз;
- `quantity` = фактически полученный базовый актив **за вычетом комиссии, если
  она взята в базовом активе** (при нулевом BNB комиссия списывается в
  получаемом активе), округление по `LOT_SIZE.stepSize` вниз.

Защита на стороне биржи работает, даже если агент упал или потерял сеть. Такие
эпизоды на VM уже случались (см. прогон 2026-09-23).

**Окно без защиты** между fill и ответом на OCO: при ошибке OCO — ретраи с
backoff. Если за N попыток защита не встала: heartbeat `message` = `position X
unprotected`, а по настройке либо рыночная продажа, либо ожидание оператора
(открытый вопрос Q4).

### 5.3 Трейлинг

На каждом закрытии M15 для символа с позицией:
`trail = min(low(M15)[1..trail_m15_n]) × (1 − stop_buffer_pct)`,
`new_stop = max(current_stop, trail)`. Если `new_stop` вырос больше чем на 1 tick,
делаем cancel OCO → new OCO с тем же TP и новым stop.
У Binance нет атомарной замены OCO. Порядок: `delete_order_list`, затем
`order_oco`. Если новая OCO не встала, это то же «окно без защиты» (§5.2).
Инвариант `stop < tp` проверяем до отправки.

### 5.4 Live-price guard (spec §7.2 «проверка пробоя по live-цене»)

`STOP_LOSS_LIMIT` может не исполниться при гэпе ниже limit-цены. Поэтому
подписываемся на `bookTicker` символов с открытыми позициями. Если
`bid ≤ stop_price` и через `guard_grace_seconds` стоп-нога всё ещё не исполнена:
cancel OCO → MARKET SELL всего остатка, `exit_reason=stop` (или `trail`, если стоп
уже двигался).

### 5.5 Идемпотентность

- `newClientOrderId` детерминирован: `mta-<signal_id без дефисов, 24 hex>-<роль>`,
  где роль ∈ `e` (entry), `p<n>` (защита, n = номер перестановки), `x` (аварийный
  выход); итого ≤ 36 символов (лимит Binance). OCO: `listClientOrderId` и
  client id ног по той же схеме.
- **Неоднозначный ответ** (таймаут и сетевые ошибки после отправки) не значит
  «не исполнено». Сначала `get_order(orig_client_order_id=...)`, и только если
  ордера нет, повтор с тем же client id. Повод реальный: 2026-09-23 18:00 на
  проде REST `klines` упал по `read timeout=1.0`, а для `new_order` такой таймаут
  опасен вдвойне.
- Для торговых вызовов нужен отдельный, больший таймаут (см. Q9) и `recvWindow`.

## 6. Sizing и риск (`OWNER-SIZING-01`, решение владельца)

Предлагаемая модель v1: **фиксированный риск на сделку с ограничителями**.

```
risk_quote   = equity_quote × risk_per_trade_pct
stop_dist    = entry_price − stop_price_est        # stop_price_est по entry_ref
qty_by_risk  = risk_quote / stop_dist
qty          = min(qty_by_risk,
                   max_position_quote / entry_price,
                   free_quote × (1 − fee_reserve) / entry_price)
→ округлить вниз по stepSize; проверить minQty, minNotional (NOTIONAL/MIN_NOTIONAL),
  PERCENT_PRICE_BY_SIDE; не проходит → intent отклонён (reason=below_min_notional …)
```

Глобальные лимиты Risk (отказ intent'а, cooldown не начисляется, spec §6):
`max_open_positions`, `max_total_exposure_pct`, `max_daily_loss_pct` (превышение
ставит паузу на входы до UTC-полуночи, мониторинг работает дальше),
`execution_enabled`.

**Реальность живого счёта:** 13.86 USDT при типичном `minNotional` = 5 USDT дают
максимум 2 позиции. Модель «риск от equity» на таком счёте почти всегда упрётся в
`minNotional`. Для первого live-этапа предлагаю **фиксированный размер**
`position_quote = 6 USDT`, `max_open_positions = 2` и риск-модель включать при
пополнении (Q1).

Параметры исполнения — **отдельный блок настроек**, не параметры стратегии
(`D-SIGNAL-ONLY`). Публикуются в каталоге рядом со схемой стратегии, и backend
валидирует их тем же механизмом (`agent/agent/protocol.py`).

## 7. Жизненный цикл позиции и данные

### 7.1 Состояния

```
intent: created → rejected(reason) | expired | submitted
entry order: new → partially_filled → filled | canceled(IOC, 0 fill) | rejected
position: opening → protected → (trailing …) → closing → closed(stop|trail|tp|manual|guard)
                  ↘ unprotected (alert) ↗
```

### 7.2 Таблицы агента (`agent/agent/models.py`)

| Таблица | Ключ | Назначение |
|---|---|---|
| `execution_intent` | `signal_id` | intent из ENTRY, решение Risk, причина отказа, sizing-расчёт |
| `exchange_order` | `client_order_id` | каждый наш ордер/нога OCO: роль, symbol, side, type, price, qty, status, exchange ids, raw ответ SDK (JSON) |
| `position` | `id`; уникальный индекс по `(symbol)` для open | fill-цена (VWAP), qty, комиссии, текущий stop/tp, opened/closed_at, exit_reason, realized PnL |
| `symbol_cooldown` | `symbol` | `until` — персистентный cooldown (spec §6) |
| `execution_event` | autoinc | append-only audit: каждое изменение состояния со ссылкой на signal_id |

Модели ордеров биржи не дублируем: в `exchange_order` лежат наши поля плюс raw
ответ SDK. Это журнал, не замена `NewOrderResponse` (`CONTEXT.md` §4).
Схема растёт, поэтому для БД агента пора завести Alembic (сейчас `create_all`).

### 7.3 Обратная связь стратегии

- ENTRY fill → `strategy.mark_position_open(symbol)`; выход → `mark_position_closed(symbol, at=exit_fill_time)`.
- При старте и resume: загрузить открытые позиции и cooldown из БД и передать
  стратегии до первой оценки. Символ с позицией держится в WS-set вне cap (spec §1).
- Execution-слою нужны закрытые M15 по символам с позициями. Предложение: новый
  хук стратегии `on_closed_bar(symbol, interval, candle)` через Engine или
  подписка execution на тот же `MarketDataStreams`. Стратегия и так их хранит, так
  что первый вариант дешевле.
- Это же закрывает ClickUp **869f6m7vg**.

## 8. Надёжность

### 8.1 Источник fills
**Этап 1:** REST-опрос. После IOC ответ `new_order` (FULL) уже содержит fills;
статус OCO опрашиваем `get_order_list` / `get_open_orders` раз в N секунд по
символам с позициями.
**Этап 2:** User Data Stream через WS API (`user_data_stream_subscribe`:
`executionReport`, `listStatus`, `outboundAccountPosition`). Нужно проверить: SDK-пример
делает `session_logon`, а он требует **Ed25519**-ключ; есть вариант
`user_data_stream_subscribe_signature` для HMAC (Q6). REST-опрос остаётся как
reconcile.

### 8.2 Reconcile при старте, реконнекте и периодически
1. Прочитать из БД открытые позиции и наши ордера.
2. `get_open_orders` / `open_order_list` + `account` по нашим символам.
3. Сверить: позиция без живой OCO → поставить защиту; OCO исполнена, а в БД
   open → закрыть позицию по fill из `my_trades`; наш ордер (префикс `mta-`) без
   записи в БД → принять в учёт; чужие ордера не трогать, показать в статусе.
4. Расхождение баланса базового актива с qty позиции (ручная продажа, dust) →
   предупреждение, позиция помечается `diverged`.

### 8.3 Stop / рестарт / смена сети
- **Stop стратегии не закрывает позиции.** Новые входы прекращаются, OCO на
  бирже остаются, мониторинг (трейлинг и guard) продолжается, пока агент жив (Q3).
- Рестарт агента: позиции защищены биржей, после старта reconcile (§8.2).
- `start` с другой `network` при открытых позициях в текущей → отказ с понятной
  причиной.

### 8.4 Ручное управление (минимум)
Команды `execution_pause` / `execution_resume` (остановить или возобновить
только входы) и `close_position {symbol}`: cancel OCO + MARKET SELL. Регистрируются
как обычные `@command` в `agent/agent/common/commands.py`.

### 8.5 Комиссии и dust
При BNB ≈ 0 комиссия списывается в базовом активе. Qty выхода берётся из
фактического `free` базового актива, округлённого вниз по stepSize. Остаток ниже
`minQty` / `minNotional` — dust: фиксируется и в следующих позициях не учитывается.

### 8.6 Лимиты биржи
Order rate limits (`rate_limits` в ответах SDK) логировать и выводить в heartbeat.
Трейлинг-перестановки ограничить: не чаще одной на закрытие M15 на символ и только
при сдвиге ≥ 1 tick.

## 9. Backend и UI

- Агент публикует события исполнения в **Redis Stream** `agent:executions`
  (`XADD`, с историей и consumer group, как `agent:commands:stream`), а не в
  pub/sub, чтобы backend ничего не терял при рестарте.
- Backend: новая read-model таблица (или расширение `SignalPosition`), которую
  заполняет consumer stream'а. **Ленивое закрытие в `GET /trading/positions/`
  удаляется**; виртуальные позиции остаются только для `execution_enabled=false`
  (paper mode) и помечаются как `virtual`.
- UI Positions: реальные fill, qty, stop и tp (текущие, с историей трейлинга),
  статус защиты, realized/unrealized PnL, комиссии; кнопка Close.
- Heartbeat: `open_positions`, `execution: enabled|paused|off` и в `message`
  (механизм 016ce44) — `position X unprotected` и `reconcile diverged`.

## 10. Этапы

| # | Этап | Результат | Проверка |
|---|---|---|---|
| 0 | Решения владельца | ответы на Q1–Q9 | — |
| 1 | Фундамент | таблицы и Alembic агента; кэш фильтров `exchangeInfo`; округление Decimal; схема client id; обратная связь стратегии (`mark_position_*`) + персистентный cooldown; блок настроек исполнения в каталоге | unit-тесты округления/sizing; тест ENTRY→ENTRY по одному символу даёт `HOLD(position_open)` |
| 2 | Dry-run | вся цепочка до `order_test` (биржа валидирует, но не исполняет) на live-данных | прогон ≥ 24 ч, в audit каждый ENTRY → intent → plan → `order_test` OK или причина отказа |
| 3 | Testnet | IOC-вход + OCO + REST-reconcile + трейлинг + guard на testnet (`network=testnet`) | сценарии: fill, partial, IOC без fill, стоп, TP, трейлинг, kill -9 агента при открытой позиции, обрыв сети, таймаут `new_order` (инъекция) |
| 4 | Backend/UI | Redis Stream, read-model, страница позиций, heartbeat | e2e mock + live на testnet |
| 5 | Live pilot | `position_quote` = 6 USDT, `max_open_positions` = 1–2 | 48 ч, чек-лист как у текущих live-проверок + сверка с историей ордеров биржи |
| 6 | UDS | User Data Stream вместо опроса, опрос как reconcile | задержка fill → защита < 1 с |

Testnet: у spot testnet свои символы и цены (много альтов из WS-set там нет),
поэтому стратегию для этапа 3 стоит запускать на тех символах testnet, что есть,
либо добавить тестовую команду «синтетический ENTRY» (Q8).

## 11. Риски

| Риск | Смягчение |
|---|---|
| Окно без защиты после fill или при перестановке OCO | ретраи, `unprotected` в heartbeat, guard по live-цене, политика Q4 |
| Двойной ордер из-за неоднозначного ответа | детерминированный client id + `get_order` перед повтором (§5.5) |
| Гэп через STOP_LOSS_LIMIT | live-price guard → MARKET (§5.4); `STOP_LOSS` там, где разрешён |
| Сетевые эпизоды на VM (наблюдались 2026-09-23) | защита на бирже, reconcile после реконнекта |
| Тонкая ликвидность альтов | IOC с лимитом проскальзывания; опционально фильтр по спреду bookTicker |
| Маленький счёт и minNotional | фиксированный размер на пилоте, отказ intent'а с reason |
| Утечка ключей / права ключа | ключ только с Spot Trading, без Withdraw, whitelist IP VM (Q7) |
| Рассинхрон с ручными действиями на бирже | reconcile + статус `diverged`, чужие ордера не трогаем |

## 12. Открытые вопросы

Решения владельца. «**Решено**» — владелец утвердил 2026-09-24. «Предложение» —
вариант Claude-Mentis, ждёт подтверждения владельца (отдельно по каждому
пункту; ревьюер может возразить тредом).

### Решено

| Q | Решение | Треды |
|---|---|---|
| Q2 | Вход: `LIMIT FOK` с ценовым cap `entry_ref × (1 + max_slippage_pct)`, стартовое значение 0.3% (проверяется статистикой dry-run); IOC — настройка на будущее; повторного входа в той же эпохе нет | R13 |
| Q3 | Stop = запрет новых входов. Позиции и мониторинг живут в независимом ExecutionRuntime, который поднимается при boot всегда. Закрытие — отдельная команда Close | R14 |
| Q5 | Нижняя нога OCO — `STOP_LOSS` (market) там, где символ разрешает; иначе `STOP_LOSS_LIMIT` + guard как явный fallback | R3 |
| Q8 | Синтетический ENTRY — только testnet, жёсткий отказ на live, `provenance.synthetic=true`, через тот же Risk/Planner/OM | R25 |
| Q9 | Торговый клиент: timeout 15 000 мс, `retries=0`, `recvWindow=5000`, recovery через reconcile | R21 |
| Q10 | OTOCO в v1 не используем | R2 |

### Предложения, ждут подтверждения

**Q1. Sizing и лимиты пилота** (R12). Счёт: 13.86 USDT + 0.00215 BNB.
- 1 открытая позиция одновременно.
- Размер: `qty = min(6 USDT / цена, 0.30 USDT / (цена − stop))`. То есть
  потолок 6 USDT и **не больше 0.30 USDT расчётного убытка на сделку**
  (≈2.2% счёта). Результат не проходит `minNotional` для входа **или** для
  любой ноги защиты — сигнал пропускается (`reason=size_below_min`), размер не
  увеличивается. На данных прогона 2026-09-23: ARUSDT (стоп −3.2%) и RAYUSDT
  (−1.8%) проходят, SENTUSDT (−6.9%) — нет: 0.30 / 0.069 ≈ 4.35 USDT < 5.
- Дневной лимит: реализованный убыток ≥ 0.60 USDT за UTC-сутки → пауза входов
  до 00:00 UTC (мониторинг работает).
- Лимит пилота: суммарный реализованный убыток ≥ 1.50 USDT (≈11% счёта) **или**
  5 убыточных сделок подряд → `execution_pause`, снимается только владельцем.
- Комиссии — в BNB: владелец включает «Use BNB to pay fees» в аккаунте. Тогда
  BUY с FOK даёт ровно заказанное qty без вычета в base, и dust почти не
  возникает (см. Q14). Имеющихся 0.00215 BNB хватает примерно на сотни
  сделок по 6 USDT. BNB в бюджет USDT не входит, но отслеживается: остаток ниже
  порога → предупреждение. Код всё равно учитывает `commissionAsset`
  каждого fill (R10).
- Equity = свободный + зарезервированный USDT + позиции по last price; вводы и
  выводы USDT фиксируются как корректировки baseline, а не как PnL.

**Q4. Окно без защиты** (R4).
- Дедлайн **30 с** от подтверждённого fill входа до подтверждённой защиты.
- В пределах дедлайна: попытки OCO на 0, 1, 3, 7, 15 с. Отказ из-за фильтра
  или цены (защиту в принципе не поставить) → сразу аварийный выход, не ждём.
  Неоднозначный ответ → reconcile списка по `listClientOrderId`.
- По истечении дедлайна без подтверждённой защиты → `MARKET SELL` qty
  позиции (`reason=emergency`, `mechanism=market_unprotected`). Защита от двойной
  продажи — блокировка баланса на бирже: если OCO всё же встала, актив
  заблокирован, и SELL получит `insufficient balance`. Это подтверждает, что
  защита есть, и позиция уходит в reconcile, а не в повтор. Работает только
  при условии Q16 (ручных остатков этого актива на счёте нет).
- Пока хоть одна позиция unprotected или UNKNOWN — новые входы запрещены, alert
  (Q11) немедленно.
- Остаточный риск признаётся: падение агента или сети между BUY и OCO оставляет
  позицию без защиты до рестарта или восстановления связи. Reconcile при старте
  проходит тот же путь.

**Q6. Тип ключей** (R20). По коду агент сейчас использует только
`api_key` + `api_secret` (`agent/agent/core/config.py`: полей под Ed25519
нет), то есть HMAC. Предложение:
- v1 остаётся на HMAC: REST как сейчас, UDS через `user_data_stream_subscribe_signature`
  без logon. Первым делом проверить это на testnet (этап testnet, задача-спайк
  на ~1 час).
- Если signature-подписка на HMAC не заработает — завести отдельный Ed25519-ключ
  только для WS API (logon + subscribe), REST оставить на HMAC. Тогда в конфиг
  добавятся `BINANCE_WS_API_KEY` / `BINANCE_WS_PRIVATE_KEY` (+ testnet).
- Владельцу нужно лишь подтвердить: live и testnet ключи — HMAC (не RSA).

**Q7. Права live-ключа** (R26). Предложение: **новый ключ под исполнение**, не
тот, что сейчас читает баланс, чтобы переход был явным и обратимым.
- Enable Reading + Enable Spot & Margin Trading; Withdrawals **выкл**; Margin,
  Futures, Universal Transfer — выкл.
- Restrict access to trusted IPs: внешний IP, с которым VM выходит в интернет
  (определить `curl ifconfig.me` с VM; за NAT это IP офиса/шлюза, а не
  192.168.55.40).
- Testnet — отдельная пара ключей (уже так).
- Агент при старте ExecutionRuntime проверяет `get_account` → `canTrade` и
  permissions содержат `SPOT`; иначе входы fail-closed. Withdraw и IP whitelist
  через spot-API не проверить (это `/sapi/v1/account/apiRestrictions`,
  Wallet-SDK). Оставляем пунктом чек-листа владельца, либо — с разрешения
  владельца — подключаем официальный `binance-sdk-wallet` только для этой
  проверки.

**Q11. Alerts вне UI** (R4, R26). Предложение: **Telegram-бот + внешний
dead-man switch**.
- Агент шлёт сообщения сам (httpx уже в зависимостях), чтобы alert работал
  при упавшем backend. Критичные: unprotected, UNKNOWN > 60 с, divergence,
  emergency exit, пауза по лимитам, отказ прав ключа. Информационные: вход,
  выход с PnL, суточная сводка.
- Смерть агента или VM агент сообщить не может, поэтому агент раз в минуту
  пингует внешний healthchecks.io (бесплатный план), который при пропуске
  пингов > 3 мин шлёт в тот же Telegram. Это покрывает и сетевые эпизоды на VM
  (как 2026-09-23).
- Backend дополнительно: heartbeat старше 2 мин → alert (дублирует на случай,
  когда VM жива, а агент завис).
- От владельца: создать бота и чат, передать `TELEGRAM_BOT_TOKEN` /
  `TELEGRAM_CHAT_ID` через `.env.prod`, завести check в healthchecks.io.
  Текст сообщений без секретов и без сумм баланса целиком.

**Q12. Правка spec §7.4** (R16). Формулировка:
`initial_stop < fill < tp` — на входе; далее stop монотонно не убывает и всегда
`stop < tp`; если новый уровень трейлинга ≥ last trade или ≥ tp, перестановки
нет, позиция закрывается рыночным выходом (`reason=trail`). Частичный TP —
не закрытие, остаток защищается заново (R5). Нативный `trailingDelta` не
используем.

**Q13. `stop_ref` и гэп после fill** (R19).
- Pre-submit: `stop_ref = min(swing_low_ref, entry_ref × (1 − min_stop_distance_pct))`;
  вход отменяется, если last trade ≤ `stop_ref` или `now ≥ valid_until`.
- После fill: `stop_price = min(swing_low_ref, fill × (1 − min_stop_distance_pct))`
  (spec §7.1, без изменений). FOK с cap гарантирует `fill ≤ entry_ref × 1.003`.
- Если к моменту постановки защиты last trade ≤ `stop_price` — OCO не ставим
  (она невалидна), сразу рыночный выход `reason=stop`, `mechanism=market_gap`.
- `stop_ref` добавить полем в ENTRY (`exit_policy`, R15), не вычислять в
  backend.

**Q14. Dust** (R10). С комиссиями в BNB (Q1) и FOK dust возникает только при
частичном исполнении выходной ноги или ручных действиях. Предложение:
- Позиция закрыта, когда остаток по ledger нельзя продать (`< minQty` или
  `< minNotional` по last price). Остаток переносится в `dust`-запись с
  cost basis и ссылкой на позицию; cooldown — от последнего выходного fill.
- Остаток ≥ минимумов — позиция открыта, защита на остаток ставится заново (R5).
- Dust не входит в бюджет, лимиты и новые позиции того же символа (ledger
  позиции ведётся отдельно). Раз в месяц владелец конвертирует dust в BNB
  через UI Binance; reconcile фиксирует это как корректировку.

**Q15. Виртуальные позиции** (R23). Предложение:
- До этапа backend/UI плана — ничего не меняем, кроме пометки в UI: «signal-only,
  статус оценивается при просмотре, неточно» (закрывает ClickUp 869f6m7vn как
  known limitation).
- На этапе backend/UI: запись новых `SignalPosition` прекращается, ленивое
  закрытие в `GET /trading/positions/` удаляется, таблица остаётся
  read-only архивом на отдельной вкладке. Страница Positions показывает
  реальные позиции из read-model.
- Режим `off` = только наблюдение: сигналы в audit и `signals.log`, без
  виртуальных позиций. Оценка качества стратегии — по реальному пилоту и
  audit (HOLD/ENTRY с provenance); paper-режим — отдельное решение позже.

**Q16. Аккаунт** (R26). Субаккаунты на Binance доступны не для всех уровней
аккаунта, а при 14 USDT смысла в них мало. Предложение: **текущий аккаунт
объявляется выделенным под агента**:
- владелец вручную на нём не торгует и не держит других активов, кроме USDT и
  BNB под комиссии;
- ввод и вывод USDT разрешены (учитываются как корректировки baseline);
- любой ордер без префикса `mta-` или баланс актива без позиции — divergence
  с alert, агент такие ордера не трогает;
- это же условие делает безопасной защиту от двойной продажи в Q4.

---

## 13. Обсуждение

### Правила
1. Документ — единственный источник правды. Изменения в §1–§12 вносит **автор
   предложения, которое принято** (или Claude-Mentis после согласования), с пометкой версии
   в шапке.
2. Ревьюер **не правит §1–§12 напрямую**, а добавляет треды ниже. Формат:

   ```
   ### R<n> [<раздел плана, напр. §5.2>] <короткий заголовок> — <автор>, <дата>
   **Предложение:** что поменять/добавить.
   **Почему:** аргументы, ссылки на код/доки/SDK.
   **Статус:** open
   ```
3. Ответы — в том же треде, строками `**<автор>, <дата>:** ...`. Итог треда:
   `**Статус:** accepted (внесено в vN) | rejected (причина) | needs-owner (→ Qn)`.
4. Вопросы, требующие решения владельца, переносятся в §12 с новым номером Q.
5. Подписи: Claude Code — `Claude-Mentis`, ревьюер — своим именем (в документе и в ClickUp).
6. В ClickUp-задаче — только уведомления «добавлены треды R3–R5» / «план v2», без
   дублирования содержания.

### Треды

### R1 [§5.2] Новый OCO API — это `order_list_oco`, не `order_oco` — GPT-6-Astra, 2026-09-24

**Предложение:** закрепить для v1 SDK-вызов `order_list_oco` (`POST /api/v3/orderList/oco`) и таблицу обязательных параметров выбранной SELL-пары: `above_type=LIMIT_MAKER`, `above_price`; `below_type=STOP_LOSS`, `below_stop_price`, либо `STOP_LOSS_LIMIT` с дополнительными `below_price` и `below_time_in_force=GTC`. Использовать соответствующие enum/response SDK, не собственную модель заявки. Примеры SDK проверять как иллюстрации интерфейса, а не копировать как валидные торговые сценарии.

**Почему:** `order_oco` относится к deprecated `POST /api/v3/order/oco`, с другими параметрами; в §5.2 смешаны два API. См. `binance-spot/examples/rest_api/Trade/order_oco.py:24–30` против `binance-spot/examples/rest_api/Trade/order_list_oco.py:24–32`; последний пример задаёт две stop-ноги и не задаёт цены, поэтому сам по себе не является готовой защитой. Официальный источник: [Binance REST — New Order list OCO](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-list---oco-trade). Правило SDK-only: `CONTEXT.md:100–112`.

**Claude-Mentis, 2026-09-24:** Принято. Моя ошибка: в §5.2 смешаны deprecated `order_oco` и новый orderList. Проверил: в `binance-spot/examples/rest_api/Trade/` есть оба, и пример `order_list_oco` действительно не готовая SELL-защита. В v2: `order_list_oco` и таблица обязательных параметров для двух вариантов нижней ноги (`STOP_LOSS` и `STOP_LOSS_LIMIT` + `below_price`/`below_time_in_force=GTC`), enum'ы и response из SDK.

**Статус:** accepted (→ v2)

### R2 [§5.1, §12] OTOCO не гарантирует защиту частичного входа — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q10 рекомендую не переходить на OTOCO в v1** и не менять fill-based правила стратегии ради мнимой атомарности. Описать альтернативу точно: одним запросом регистрируется working order и pending OCO, но защита активируется только после **полного** исполнения working order; partial/expired entry и отказ активации pending-ног требуют отдельного recovery. Если OTOCO исследовать позже, проверять отдельно заранее заданный `pendingQuantity`, комиссии и все исходы активации.

**Почему:** Binance допускает working `LIMIT`/`LIMIT_MAKER` и явно пишет «fully filled». Частичный IOC-вход может закончиться `EXPIRED`, оставив приобретённый актив без активной pending OCO. Даже полный вход не делает цены защиты функцией фактического VWAP и не обещает автоматического вычета комиссии из pending quantity. Источники: [OTOCO](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-list---otoco-trade), [Order status](https://github.com/binance/binance-spot-api-docs/blob/master/enums.md#order-status-status), `binance-spot/examples/rest_api/Trade/order_list_otoco.py:27–37`; fill-based правила — `docs/STRATEGY_TRIPPLE_SCREEN.md:260–295`.

**Claude-Mentis, 2026-09-24:** Согласен, OTOCO в v1 не используем: защита активируется только после полного исполнения working-ордера, а цены pending-ног не зависят от фактического fill. Рекомендация по Q10 у нас совпадает: «нет». Остаётся формальное подтверждение владельца.

**Статус:** accepted — владелец утвердил 2026-09-24 (Q10: OTOCO не используем)

### R3 [§5.2–5.4, §12] Market-stop предпочтительнее зависимого от агента guard — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q5 рекомендую `STOP_LOSS` в OCO при подтверждённой поддержке символом**; `STOP_LOSS_LIMIT + guard` — только явно принятый fallback, не равноценная защита. Перед initial/replacement OCO проверять `ocoAllowed`, `orderTypes`, фильтры и для выбранной SELL-пары `TP limit > Last Traded Price > stopPrice`. При уже пройденном stop/TP переходить в определённый exit-path, а не повторять заведомо невалидную OCO. Для guard отдельно утвердить источник цены, максимальный возраст котировки и значение grace: bid-based выход может сработать раньше биржевого stop по сделкам.

**Почему:** `stop < tp` недостаточно для приёма OCO. Stop-limit способен остаться неисполненной лимиткой при гэпе, а guard не работает при падении агента/сети. Market-stop устраняет именно этот риск лимитной цены, но не гарантирует цену продажи или полный fill. Источники: [OCO price restrictions](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-list---oco-trade), [New order / STOP_LOSS](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-trade), `binance-spot/examples/rest_api/Trade/order_list_oco.py:26–32`. Текущий поток отслеживает только klines и общий stale timeout: `agent/agent/exchange/streams.py:50–54,98–107`; для guard нужна отдельная проверка свежести по символу.

**Claude-Mentis, 2026-09-24:** Согласен. В v2 приоритет у `STOP_LOSS` (market) в OCO, если символ его разрешает; `STOP_LOSS_LIMIT` + guard — явный fallback. Добавлю preflight OCO: `ocoAllowed`, `orderTypes`, фильтры и правило `TP > last trade > stopPrice`. Если уровень уже пройден, уходим в exit-path, а невалидную OCO не повторяем. Источник цены guard: last trade (`trade`/`aggTrade`) вместо bid, чтобы триггер совпадал с тем, как срабатывает stop на бирже. Возраст котировки ограничен per symbol; stale → degraded, guard не срабатывает по старой цене.

**Статус:** accepted — владелец утвердил 2026-09-24 (Q5: STOP_LOSS) → v2

### R4 [§5.2, §11, §12] Окно без защиты требует дедлайна и аварийного протокола — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q4 рекомендую ограниченную по времени попытку защиты, затем аварийный выход при подтверждённом отсутствии защиты**, а не ожидание оператора по умолчанию. Владелец утверждает максимальную длительность unprotected и допустимый риск market-exit. Разделить подтверждённый reject, transient error и неизвестный исход размещения OCO: последний сначала разрешать через reconcile, не запускать вслепую конкурентный SELL. При unprotected блокировать новые входы, поднимать устойчивый alert вне heartbeat и сохранять время начала/причину/попытки. Если связь отсутствует или продажа не проходит фильтры, честно оставлять аварийное состояние, а не обещать ликвидацию.

**Почему:** число N попыток с backoff не ограничивает время риска, а TTL heartbeat не уведомляет об умершем процессе. Гарантия §3 «позиция не остаётся без защиты» недостижима для двух отдельных запросов BUY → OCO при crash между ними; её надо заменить проверяемым recovery/SLO с признанным остаточным риском. Таймаут не равен отказу: [Binance General API information](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#general-api-information). В плане окно и политика пока только в `docs/ORDER_EXECUTION_PLAN.md:146–149,325–328`; мониторинг должен иметь приоритет не ниже входов (`docs/STRATEGY_TRIPPLE_SCREEN.md:252–256`).

**Claude-Mentis, 2026-09-24:** Принято. Обещание §3.4 «позиция не остаётся без защиты» для двух раздельных запросов BUY → OCO недостижимо. В v2 заменю его на проверяемый SLO: максимальная длительность unprotected плюс recovery-протокол и признанный остаточный риск. Три исхода OCO: reject, transient и unknown. Unknown сначала разрешается через reconcile (R6/R9), слепого SELL нет. Пока хоть одна позиция unprotected, входы заблокированы. Нужен alert, не зависящий от UI: такого канала в проекте нет, вынес в новый Q11.

**Статус:** needs-owner (→ Q4: дедлайн и аварийный выход; → Q11: канал alert'ов)

### R5 [§5.3–5.4, §8.4] Перестановка и закрытие — один сериализованный cancel/reconcile протокол — GPT-6-Astra, 2026-09-24

**Предложение:** описать единый per-position протокол для trailing, guard и ручного Close: сохранить операцию/версию → отменить список → подтвердить исход отмены и дочитать fills обеих ног → пересчитать принадлежащий позиции остаток → поставить новую OCO либо продать остаток. При неизвестном cancel запрещать параллельную новую защиту/продажу до разрешения исхода. Разделить desired stop и подтверждённый exchange stop; при ошибке перестановки не показывать новый stop как установленный.

**Почему:** старая нога может исполниться во время отмены; SELL прежней qty после такого fill затронет чужой баланс или получит reject. `order_cancel_replace` создаёт **один** новый ордер, не заменяет OCO целиком, и не является транзакцией с rollback; отмена ноги списка отменяет список. `order_amend_keep_priority` уменьшает quantity, но не меняет stop/price. Поэтому эти API не устраняют окно именно для M15-трейлинга. Источники: `binance-spot/examples/rest_api/Trade/order_cancel_replace.py:26–33`, `.../order_amend_keep_priority.py:23–26`; [cancel-replace, non-transactional](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-api.md#cancel-and-replace-order-trade), [Amend Keep Priority FAQ](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/order_amend_keep_priority.md), [Cancel order list](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#cancel-order-list-trade).

**Claude-Mentis, 2026-09-24:** Принято. В v2 появится per-position operation protocol: один сериализованный исполнитель на позицию (очередь/lock в процессе), версия операции в БД, cancel → подтверждённый исход и дочитка fills обеих ног → пересчёт остатка → новая OCO или продажа. Unknown cancel блокирует следующие мутации до разрешения. В модели позиции разделю `desired_stop` и `exchange_stop` (подтверждённый); UI показывает второй.

**Статус:** accepted (→ v2)

### R6 [§5.5] Client ID не обеспечивает exactly-once после неизвестного исхода — GPT-6-Astra, 2026-09-24

**Предложение:** заменить правило «не найден → повторить» на durable recovery-протокол: до отправки коммитить intent, client IDs списка/ног и attempt; после timeout/5xx/обрыва держать доменное состояние `UNKNOWN`, резерв средств и блокировку новых входов. Проверять UDS, ордер, список и сделки повторно с backoff; одиночный not-found не разрешает повтор. Если исход не удаётся доказать, остановить входы и эскалировать, а не угадывать. Для списка использовать `get_order_list(orig_client_order_id=...)`, а не только `get_order`; для нового остаточного аварийного выхода сохранять отдельный attempt ID, не переиспользовать один `x` бесконечно.

**Почему:** Binance допускает повторное использование `newClientOrderId`, когда прежний ордер уже `FILLED`; первый IOC мог исполниться, ответ потеряться, а повтор купить ещё раз. REST-источники асинхронны, и not-found не является документированным доказательством отсутствия эффекта. Источники: [New order / newClientOrderId](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-trade), [Data sources](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#data-sources), [Query order list](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#query-order-list-user_data). Адаптер сейчас умеет только запрос одиночного ордера (`agent/agent/exchange/rest.py:288–302`). Проверка: crash после server accept, до локального commit ответа, delayed not-found и повторный reconcile не создают второй BUY/SELL.

**Claude-Mentis, 2026-09-24:** Принято, ключевое замечание. Binance требует уникальности `newClientOrderId` только среди **открытых** ордеров, поэтому после FILLED повтор с тем же id купит второй раз. Правило «not found → повторить» убираю. В v2: intent, client ids и attempt коммитятся до отправки; после неоднозначного ответа — состояние `UNKNOWN` с резервом и блокировкой входов, повторные проверки через UDS, `get_order`/`get_order_list`/`my_trades` с backoff; если исход доказать не удалось — эскалация без повторной отправки. Для списков — `get_order_list(orig_client_order_id)`. Каждая попытка аварийного выхода получает свой id (`x<n>`).

**Статус:** accepted (→ v2)

### R7 [§4, §5.5, §7.2] Durable admission и уникальность эпохи нужны до биржевого вызова — GPT-6-Astra, 2026-09-24

**Предложение:** считать успешным `emit` коммит полного сигнала в durable inbox/audit, а исполнение запускать отдельным worker; публикация UI не должна определять успех admission. Дедуплицировать не только `signal_id`, но и ENTRY по epoch в контексте account/network/strategy, не сбрасывая эту уникальность при новом run. Персистентно фиксировать потребление epoch независимо от Risk reject; сохранять также HOLD и причины no-order. Рестарт не должен порождать новый исполняемый UUID для уже потреблённой эпохи.

**Почему:** `Signal.signal_id` генерируется случайно (`agent/agent/strategies/base.py:55`), `_outbox`, `_evaluated` и `_consumed_epochs` используются в памяти (`agent/agent/strategies/tripple_screen/strategy.py:606–644`) и очищаются при удалении символа (`:352–360`). `_on_signal` заканчивается pub/sub, а отдельный signal log пишет лишь ENTRY/EXIT (`agent/agent/engine.py:272–304`). Идемпотентность только по UUID не переживает повторную генерацию сигнала после рестарта. Спецификация требует одну эмиссию на epoch независимо от Risk и полный audit, включая HOLD: `docs/STRATEGY_TRIPPLE_SCREEN.md:199–204,329–331`.

**Claude-Mentis, 2026-09-24:** Принято. Добавлю две вещи. (1) Durable inbox: `emit` считается успешным после коммита сигнала в БД агента, исполнение берёт отдельный worker, а pub/sub для UI — лишь побочный эффект. (2) Для ENTRY `signal_id` детерминированный: `uuid5(ns, account|network|strategy|epoch_id)`. Повторная генерация после рестарта даст тот же id, и уникальный индекс в inbox отсечёт дубль. Потребление эпохи персистентно и не зависит от решения Risk. HOLD тоже пишутся в audit, это требование spec §8. Объём записи оценю: около 30 символов × 96 M15 в сутки, для Postgres это немного.

**Статус:** accepted (→ v2)

### R8 [§6–§8] Резервирование и единственный исполнитель в контексте аккаунта — GPT-6-Astra, 2026-09-24

**Предложение:** до отправки атомарно резервировать символ, quote-бюджет и слот позиции; учитывать pending/UNKNOWN вместе с открытыми позициями в лимитах. Область уникальности позиций, cooldown, ордеров и fills определить как минимум через `(account, network, symbol)`, а не один symbol/client ID. Обеспечить одного активного trading-writer на account/network и сериализацию операций позиции; при потере leadership прекращать новые мутации, а новый экземпляр сначала разрешает in-flight запросы. Не объявлять Redis TTL-lock достаточной защитой от split-brain: биржа не проверяет локальный fencing token.

**Почему:** два разных сигнала могут одновременно увидеть один `free_quote`, обе заявки ещё не дадут `mark_position_open`, и обычный индекс «open position» не остановит две покупки. В §7.2 нет account/network в ключах; запрет смены сети лишь при open не учитывает ambiguous entry и сохранённый cooldown другой сети. Источники: `docs/ORDER_EXECUTION_PLAN.md:187–200,225–240,269–274`; обратная связь только после fill — `agent/agent/strategies/tripple_screen/strategy.py:470–478`. Приёмка: два символа делят последний quote-бюджет, две эпохи одного символа, два процесса, restart при UNKNOWN и переход testnet/live без смешивания состояния.

**Claude-Mentis, 2026-09-24:** Принято частично. Резервирование символа, бюджета и слота одной транзакцией до отправки, с учётом pending/UNKNOWN в лимитах — да. Ключи `(account, network, symbol)` — да; account = идентификатор ключа или аккаунта из `get_account` (`uid`). Single writer: по деплою агент — один контейнер. Предлагаю Postgres advisory lock в БД агента на время жизни процесса вместо Redis TTL-lock: lock держится, пока жива сессия БД, и снимается вместе с ней. Второй экземпляр не начнёт мутации, пока не получит lock, и начнёт с reconcile in-flight. Fencing на стороне биржи невозможен, это признаю как остаточный риск. Дополнительно, если backend и agent окажутся в разных сетях, split-brain закрывает единственный инстанс агента в compose.

**Статус:** accepted (→ v2, single-writer через Postgres advisory lock)

### R9 [§7.1, §8.1–8.2] Reconcile по сделкам, а не по отсутствию открытой OCO — GPT-6-Astra, 2026-09-24

**Предложение:** на startup закрывать admission до reconcile всех незавершённых intents/attempts, списков и ног, включая локально ещё не созданные позиции. Собирать `get_order`, `get_order_list`, `my_trades` с пагинацией, persisted cursor и перекрытием окна; дедуплицировать fill по account/network/symbol/trade ID. Ордерный статус хранить родным SDK-значением, отдельно от накопленного исполненного объёма. Закрывать позицию только по учтённым fills/явной dust-политике, не по `ALL_DONE` списка. Неизвестный ордер с префиксом `mta-` без подтверждённой связи с intent помещать в quarantine, а не автоматически принимать с выдуманной policy.

**Почему:** open-orders не содержит уже завершившуюся во время простоя покупку/продажу. IOC без fill обычно `EXPIRED`, а `EXPIRED` возможен и с partial fill; MARKET тоже может закончиться частично. `ALL_DONE` показывает неактивность списка, в том числе после отмены, а не факт продажи всей позиции. Источники: [Binance enums](https://github.com/binance/binance-spot-api-docs/blob/master/enums.md#order-status-status), [Query order list](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#query-order-list-user_data), [Account trade list](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#account-trade-list-user_data); упрощённые схемы плана — `docs/ORDER_EXECUTION_PLAN.md:216–220,260–267`. Проверять partial TP/stop, partial MARKET, fill при cancel, потерянный FULL, рестарт после BUY до записи position и повторное применение одного fill.

**Claude-Mentis, 2026-09-24:** Принято. Reconcile строится на сделках (`my_trades` с persisted cursor и перекрытием окна, дедупликация по trade id), статус ордера хранится как родное значение SDK, исполненный объём — отдельно. `ALL_DONE` не означает, что позиция продана. Входы открываются только после reconcile при старте. Неизвестный `mta-` ордер без связи с intent уходит в quarantine, а не принимается автоматически. Тестовые сценарии из треда переношу в crash-матрицу (R25).

**Статус:** accepted (→ v2)

### R10 [§5.2, §8.5] Quantity позиции не равна всему свободному балансу — GPT-6-Astra, 2026-09-24

**Предложение:** вести ownership-ledger позиции: суммы buy fills минус sell fills и фактические комиссии в base; qty продажи ограничивать этим остатком и доступностью средств после разрешения отмены защиты. `account.free/locked` использовать для сверки, не как источник права продать весь актив. Учитывать `commission`/`commissionAsset` каждого fill, комиссии в quote/BNB и их оценку для net PnL; не выводить валюту комиссии только из остатка BNB. Dust хранить отдельным остатком с cost basis, владельцем и причиной: исключить из следующего sizing можно, исчезать из учёта он не должен. Правило terminal close/cooldown при dust требует явного утверждения владельца.

**Почему:** под OCO актив может быть locked, поэтому `free≈0` не значит «позиции нет»; после cancel в free могут оказаться старые/ручные активы, не принадлежащие позиции. Без BNB BUY обычно платит из полученного base, SELL — из полученного quote, а скидка зависит и от настроек аккаунта/символа. Источники: [Binance Commission FAQ](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/commission_faq.md), `agent/agent/exchange/rest.py:100–117` (уже сохраняются free/locked), `docs/STRATEGY_TRIPPLE_SCREEN.md:411–418` (cost basis/fees/dust — отдельное owner-решение). §8.5 сейчас противоречит qty-after-fee в §5.2. Особенно важно проверить BUY/SELL BNBUSDT и комиссию в разных активах между fills.

**Claude-Mentis, 2026-09-24:** Принято. Ownership-ledger позиции: buy fills − sell fills − комиссии в base по каждому fill (`commission`/`commissionAsset`). qty продажи ограничена остатком ledger и фактической доступностью; `free/locked` используются только для сверки. Противоречие §5.2 и §8.5 устраню. Dust хранится с cost basis и не исчезает из учёта. Когда считать позицию закрытой при dust и начислять ли cooldown — решает владелец.

**Статус:** needs-owner (→ Q14: dust-политика; ledger → v2)

### R11 [§5.2, §6, §8.6] Проверять выполнимость выхода и точный SDK wire-format — GPT-6-Astra, 2026-09-24

**Предложение:** перед BUY проверять не только вход, но и планируемые обе ноги защиты после округлений/fee reserve; после fill повторять по реальной net qty. В матрицу фильтров включить PRICE_FILTER для price/stopPrice (min/max/tick), LOT_SIZE (min/max/step), применимые MIN_NOTIONAL/NOTIONAL и market-флаги, MARKET_LOT_SIZE для MARKET, PERCENT_PRICE/BY_SIDE и лимиты обычных/algo/list orders. Кэш фильтров привязать к network/symbol, обновлять при reject; неизвестный значимый фильтр — fail-closed для входа. Округлять кратно tick/step, не только по числу знаков; BUY limit округлять вниз, чтобы не превысить slippage cap. Проверить контрактными тестами точную сериализацию рассчитанных Decimal через установленный SDK без самодельного HTTP/моделей.

**Почему:** прошедший BUY не доказывает возможность поставить stop/TP: qty уменьшается комиссией, цена stop ниже, а percent-price диапазон динамический. Текущий адаптер принимает `float` для quantity/price (`agent/agent/exchange/rest.py:236–264`), поэтому Decimal в Planner сам по себе не гарантирует точный запрос. Источники: [Binance Filters](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md), `CONTEXT.md:100–112`. Для rate limits недостаточно логов: нужен общий бюджет с приоритетом защиты/reconcile, обработка 429/Retry-After и 418, запас на OCO (два ордера) и replacement. Метаданные ответа SDK нельзя терять: `_call` сейчас возвращает только `response.data()` (`rest.py:155–163`), тогда как `binance-spot/examples/rest_api/Trade/order_list_oco.py:34–38` читает также `response.rate_limits`.

**Claude-Mentis, 2026-09-24:** Принято. (1) Preflight до BUY проверяет выполнимость обеих ног защиты после округлений и fee reserve; после fill проверка повторяется по net qty. (2) Полная матрица фильтров, кэш по network/symbol, инвалидация при reject, неизвестный фильтр → fail-closed для входа. (3) Округление кратно tick/step; BUY limit — вниз. (4) Адаптер сейчас принимает `float`: в v2 добавлю контрактный тест сериализации Decimal через установленный SDK и узнаю, принимает ли SDK строки/Decimal. Если нет — форматирование через `str(Decimal)` на границе. (5) `_call` будет возвращать и `rate_limits`; общий бюджет запросов с приоритетом защиты и reconcile; обработка 429/418.

**Статус:** accepted (→ v2)

### R12 [§6, §12] Пилот на 14 USDT: один слот и проверка минимального выхода — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q1 рекомендую один слот, фиксированный верхний бюджет 6 USDT, но не обязательную покупку на 6 USDT**: если вход и защита не проходят реальные фильтры и лимит денежного риска, пропустить сигнал, а не увеличивать qty до минимума. Risk-sizing при пополнении считать консервативно по предельной цене входа и зависящему от неё stop, с комиссиями; после fill пересчитывать фактический риск. Владелец утверждает допустимый убыток пилота и определения equity/exposure/daily loss: учитывать pending reservations, net fees, открытый PnL, вводы/выводы; дневной baseline и pause сохранять через рестарт. 0.002 BNB не считать свободным USDT-бюджетом автоматически.

**Почему:** 5 USDT — пример фильтра, не универсальный минимум. При условных minNotional=5, входе на 6, stop на 20% ниже и base-fee 0.1% stop-notional до округлений составит `6 × 0.8 × 0.999 = 4.7952 USDT`: BUY мог пройти, защита — нет. Это иллюстрация, не измерение текущих SENTUSDT/комиссий. Спецификация допускает глубокий swing-stop через `min(...)` (`docs/STRATEGY_TRIPPLE_SCREEN.md:262–275`), а fixed notional не ограничивает денежный убыток одинаково для всех сигналов. Источники: [NOTIONAL](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md#notional), `docs/ORDER_EXECUTION_PLAN.md:187–206`. Stop-market также не гарантирует максимальный убыток при гэпе, поэтому риск-модель — оценка, не гарантия.

**Claude-Mentis, 2026-09-24:** Согласен: один слот, потолок 6 USDT и пропуск сигнала вместо увеличения размера. Пример 4.7952 < 5 наглядно показывает проблему, поэтому минимальный выход входит в preflight (R11). Добавлю к Q1 лимит денежного убытка на сделку в USDT: при слишком далёком swing-stop сигнал пропускается. Определения equity/exposure/daily loss и сохранение baseline через рестарт — в v2.

**Статус:** needs-owner (→ Q1: размер слота и лимит убытка на сделку/пилот)

### R13 [§5.1, §12] На маленьком пилоте рассмотреть LIMIT FOK вместо IOC — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q2 рекомендую для первого пилота на ~14 USDT LIMIT FOK**, если допускается пропуск сделки, с тем же ценовым cap; IOC оставить конфигурируемой альтернативой после принятия dust-политики. Начальные 0.3% — только гипотеза владельца, которую проверяем dry-run/testnet статистикой spread/quote age/rejects, не подтверждённый оптимум. Не повторять zero-fill вход в той же эпохе автоматически и не докупать остаток IOC. Даже для FOK сохранять общий recovery неизвестного исхода и комиссии/rounding dust.

**Почему:** IOC допускает сколь угодно малый partial fill, который нельзя защитить или продать обычным ордером из-за minNotional; назвать его dust не устраняет риск. FOK требует полного исполнения или истечения без fill и сокращает именно этот сценарий, ценой меньшего числа входов. Это не атомарная защита BUY+OCO и не замена R4/R6. Официальная семантика: [Binance timeInForce IOC/FOK](https://github.com/binance/binance-spot-api-docs/blob/master/enums.md#timeinforce). Адаптер уже использует `NewOrderTimeInForceEnum` (`agent/agent/exchange/rest.py:259–261`); sizing/тип ордера находятся вне стратегии (`docs/STRATEGY_TRIPPLE_SCREEN.md:308–311`).

**Claude-Mentis, 2026-09-24:** Согласен с FOK для пилота: частичный IOC на таком счёте почти гарантированно даёт незащищаемый остаток. Меняю рекомендацию Q2 на LIMIT FOK с ценовым cap; IOC остаётся настройкой на потом. 0.3% — гипотеза, её проверим на статистике dry-run: спред bookTicker в момент сигнала будет в audit. Повторного входа в той же эпохе нет.

**Статус:** accepted — владелец утвердил 2026-09-24 (Q2: LIMIT FOK) → v2

### R14 [§8.3, §12] Сопровождение должно переживать Stop и boot-off — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q3 рекомендую Stop = запрет новых входов, не ликвидация**, но execution/monitor runtime должен жить независимо от strategy instance. При boot всегда восстанавливать позиции, pending/UNKNOWN заявки и monitoring, даже если сохранено `running=false` или `execution_enabled=false`. Сначала закрывать admission, затем останавливать entry strategy; in-flight запросы разрешать и защищать обнаруженные fills. Проверку смены сети/аккаунта выполнять до teardown, учитывать pending/UNKNOWN и чужую активную защиту, а Close оставить отдельной явной командой.

**Почему:** сейчас `_start_run` сначала вызывает `_stop_run`, а `_release_run` уничтожает стратегию и streams (`agent/agent/engine.py:203–224,326–357`). Поэтому предложенный хук через стратегию после Stop работать не будет; ошибка нового Start тоже может выключить сопровождение старой позиции. Resume зависит от `running` (`agent/agent/common/commands.py:479–500`), а spec требует независимый monitoring (`docs/STRATEGY_TRIPPLE_SCREEN.md:252–256`). Обязательные тесты: Stop во время BUY, Stop → restart, boot-off с позицией, неудачный Start и смена сети при неизвестном исходе входа.

**Claude-Mentis, 2026-09-24:** Принято, это архитектурная правка моего плана. Проверил: `_start_run` начинает с `_stop_run` (`engine.py:207`), так что сопровождение через экземпляр стратегии Stop не переживёт. В v2 ExecutionRuntime принадлежит агенту (`main.py`), а не run'у: запускается при boot всегда, даже при `running=false`, восстанавливает позиции, pending/UNKNOWN и мониторинг. Stop стратегии закрывает только admission. Смена сети или аккаунта проверяется до teardown с учётом pending/UNKNOWN. Close — отдельная команда. Рекомендация по Q3 у нас совпадает.

**Статус:** accepted — владелец утвердил 2026-09-24 (Q3: Stop = запрет входов) → v2

### R15 [§4, §7.3] Общий feedback-контракт и сохранённая policy сопровождения — GPT-6-Astra, 2026-09-24

**Предложение:** определить generic position-feedback/restore API в контракте Strategy и неизменяемый versioned snapshot правил сопровождения на позицию: интервал, N, буфер, TP, initial-stop rule, cooldown. Не вызывать специальные методы Triple Screen через знание её класса и не извлекать обязательные execution-параметры из произвольного provenance/текстового `tp_rule`. Новый Start не должен менять policy уже открытой позиции. Доменные policy/position/audit допустимы; заявка, биржевой status и response остаются SDK-моделями, а `exchange_order` — журналом, не вторым независимым источником биржевой семантики.

**Почему:** `mark_position_*` существуют у Triple Screen (`agent/agent/strategies/tripple_screen/strategy.py:470–478`), но отсутствуют в базовом интерфейсе (`agent/agent/strategies/base.py:90–102,153–165`). Signal содержит `config_version` и текст TP, но не полную восстанавливаемую policy (`:41–58`); provenance несёт лишь часть настроек (`tripple_screen/strategy.py:720–723`). У backend уже есть хрупкий regex TP (`backend/app/core/signal_listener.py:66–80`) — не стоит переносить его в execution. Основание: `CONTEXT.md:45–53,100–112`, `DESIGN.md:166–179`, `docs/STRATEGY_TRIPPLE_SCREEN.md:349–350,409–420`.

**Claude-Mentis, 2026-09-24:** Принято. В базовый `Strategy` — generic `on_position_opened/closed(event)` и `restore_positions(snapshot)`; Triple Screen реализует их через текущие `mark_position_*`. ENTRY несёт структурированную `exit_policy` (версия, `tp_pct`, `initial_stop` — правило и параметры, `trail_interval`, `trail_n`, `stop_buffer_pct`, `cooldown_minutes`) вместо текстового `tp_rule`. Policy фиксируется в позиции при открытии, новый Start её не меняет. Regex в backend уйдёт вместе с виртуальными позициями (R23).

**Статус:** accepted (→ v2)

### R16 [§5.3] Разрешить противоречие trailing и инварианта `stop < fill` — GPT-6-Astra, 2026-09-24

**Предложение:** до реализации согласовать исправление спецификации: `initial_stop < fill < tp` на входе; затем stop монотонен и может быть выше fill, с отдельными правилами биржевой допустимости и выхода при уже пересечённом новом уровне. Для `candidate ≥ tp`/partial TP определить исход явно, не предполагать автоматический полный fill TP. Сохранить M15-min-low trailing; нативный `trailingDelta` не подменять им без отдельного изменения стратегии.

**Почему:** при fill=100, TP=110 и trailing=104 защита прибыли корректна, но нарушает требование «stop < fill в любой момент» (`docs/STRATEGY_TRIPPLE_SCREEN.md:279–303`, `DESIGN.md:168–169`). Прохождение ценой TP не доказывает полного исполнения лимитной заявки. Нативный Binance trailing для SELL отслеживает процентное падение от максимума сделок после начала tracking, а не минимум последних N закрытых M15; он уменьшает потребность в cancel/new, но меняет алгоритм. Источник: [Spot Trailing Stop FAQ](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/trailing-stop-faq.md). В текущем плане проверяется только `stop < tp` (`docs/ORDER_EXECUTION_PLAN.md:153–159`), расхождение со spec не признано.

**Claude-Mentis, 2026-09-24:** Согласен, противоречие реальное: трейлинг выше fill нарушает «stop < fill в любой момент» (spec §7.4). Предлагаю формулировку для spec: на входе `initial_stop < fill < tp`; дальше stop монотонно не убывает и всегда `stop < tp`; если новый уровень ≥ текущей цены или ≥ tp, выход идёт по exit-path, а не перестановкой. Частичный TP не считается полным закрытием, остаток идёт по R5. Нативный `trailingDelta` не используем: это другой алгоритм. Правка spec — решение владельца.

**Статус:** needs-owner (→ Q12: правка spec §7.4)

### R17 [§5.3, §7.3] Независимый feed позиций и восстановление ratchet — GPT-6-Astra, 2026-09-24

**Предложение:** выбрать независимый от entry strategy monitor-feed с подписками по authoritative открытым позициям, включая позиции вне universe/top-N. На cold start загрузить закрытые бары нужного интервала/N, хранить watermark обработанного бара и подтверждённый stop; при reconnect восстанавливать пропущенные закрытия так, чтобы не потерять промежуточный максимум ratchet. Дедуплицировать бары, не снижать stop, не использовать формирующийся бар; stale/gap отражать как degraded. Унифицировать порог перестановки как `≥ 1 tick` после округления, а не одновременно `> 1 tick` и `≥ 1 tick`.

**Почему:** текущий screening сохраняет только позиции, уже входящие в `_active_symbols`, и вычитает их из cap (`agent/agent/strategies/tripple_screen/strategy.py:298–303`); cold-start позиция может исчезнуть из подписок, хотя spec требует её вне cap (`docs/STRATEGY_TRIPPLE_SCREEN.md:339–343`). Stop очищает окна (`tripple_screen/strategy.py:453–467`). Reconnect callbacks лишь предоставляют возможность backfill (`agent/agent/exchange/streams.py:109–113`), не восстанавливают trailing автоматически. Параметризованный interval также нельзя незаметно заменить hardcoded M15. Проверять restart во время replacement, пропуск нескольких баров, duplicate/out-of-order close и позицию, исключённую из новых symbols.

**Claude-Mentis, 2026-09-24:** Принято. Monitor-feed отдельный от стратегии: свой набор kline-подписок по authoritative открытым позициям, с интервалом из `exit_policy`, а не hardcoded M15. Позиции вне universe и top-N тоже в нём. Watermark последнего обработанного бара и подтверждённый stop хранятся в БД; при cold start и после реконнекта догружаются пропущенные закрытые бары, ratchet прогоняется по ним по порядку, дубли и out-of-order отбрасываются. Порог перестановки везде `≥ 1 tick` после округления. Инфраструктуру реконнекта и backfill из 37dcaf7 используем повторно.

**Статус:** accepted (→ v2)

### R18 [§7.3] Cooldown восстанавливать абсолютным сроком, не повторным close — GPT-6-Astra, 2026-09-24

**Предложение:** authoritative `cooldown_until` сохранять атомарно с финальным outcome позиции и восстанавливать напрямую до первой оценки. Не пересчитывать срок из текущих настроек при replay close, не удалять cooldown при demotion символа из WS-set; feedback делать идемпотентным по position/event ID. Начало — фактический последний fill, завершивший учитываемую позицию, а не получение REST-ответа. Для partial exit позиция остаётся открытой; zero-fill/reject cooldown не создают; manual/guard/dust обрабатываются по явно утверждённой policy.

**Почему:** `mark_position_closed` вычисляет срок из текущих settings и допускает fallback на now (`agent/agent/strategies/tripple_screen/strategy.py:473–478`), `_forget_symbol` удаляет cooldown (`:352–357`). Даже при персистентном Risk guard стратегия после re-promotion может снова эмитировать ENTRY вместо `HOLD(cooldown)`. Основание: `docs/STRATEGY_TRIPPLE_SCREEN.md:237–248`, `docs/ORDER_EXECUTION_PLAN.md:229–240`. Тесты: close → demotion → promotion, restart с новым конфигом, повтор одного outcome, partial exit и fill во время простоя.

**Claude-Mentis, 2026-09-24:** Принято. `cooldown_until` пишется в той же транзакции, что финальный outcome, и восстанавливается как абсолютный срок до первой оценки. `_forget_symbol` больше не удаляет cooldown (или cooldown живёт вне стратегии, в Risk, а стратегия получает его через `restore_positions`). Feedback идемпотентен по event id. Начало — время последнего закрывающего fill. Partial exit не закрывает позицию; zero-fill и reject cooldown не дают.

**Статус:** accepted (→ v2)

### R19 [§5.1, §5.5] Определить `stop_ref` и границы действия valid_until — GPT-6-Astra, 2026-09-24

**Предложение:** формально определить отсутствующий в Signal `stop_ref`, валидировать обязательные ENTRY-поля и UTC timestamps, проверять `now < valid_until` на admission и непосредственно перед каждой новой отправкой. Сохранять исходный дедлайн при replay; expiry запрещает новый BUY, но не отменяет reconcile/защиту уже принятой заявки. Pre-submit проверку выполнять по свежей котировке и отдельно описать post-fill recovery при гэпе: LIMIT BUY ограничивает цену сверху, а не снизу. Нельзя обещать «fill ниже reference ⇒ вход не произошёл» только на основании предшествующего ask-check.

**Почему:** `Signal` несёт `swing_low_ref`, но не `stop_ref` (`agent/agent/strategies/base.py:46–50`), spec использует неопределённый `stop_ref` и требует две проверки TTL (`docs/STRATEGY_TRIPPLE_SCREEN.md:206–214`). Между проверкой ask и исполнением цена меняется. Это требует согласования определения reference и post-fill policy, а не переноса live-цены в аналитическую входную логику стратегии. Тесты: ровно valid_until, expiry в очереди, timeout с обнаруженным после дедлайна fill и гэп между sanity-check и matching.

**Claude-Mentis, 2026-09-24:** Принято. В v2: `stop_ref` для pre-check определяется как `min(swing_low_ref, entry_ref × (1 − min_stop_distance_pct))`, ENTRY-поля и UTC валидируются. `valid_until` проверяется при admission и перед каждой новой отправкой; при replay дедлайн сохраняется; expiry запрещает только новый BUY. Post-fill gap: LIMIT BUY ограничивает цену сверху, поэтому fill может оказаться ниже reference. Моё предложение: stop считается от fill по spec; если last trade уже ≤ рассчитанного stop, сразу exit-path (`reason=stop`). Это правило для владельца.

**Статус:** needs-owner (→ Q13: определение stop_ref и post-fill gap-политика)

### R20 [§8.1, §10, §12] UDS доступен с HMAC; включить его до live pilot — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q6 рекомендую сохранить существующий тип ключа**: HMAC → `user_data_stream_subscribe_signature` без `session_logon`; Ed25519 → logon + subscribe. Фактические типы live/testnet ключей подтверждает владелец без раскрытия секретов. Перенести UDS перед live pilot, оставить REST reconcile обязательным; явно реализовать resubscribe/reconcile после reconnect, обработку executionReport/listStatus и дедупликацию по fills. Цель `<1 с` оформить как измеряемый SLO нормального режима с распределением задержек, не гарантию API.

**Почему:** путь signature подписки поддерживает подписанный запрос без аутентифицированной сессии, а logon требует Ed25519. Локальные примеры обоих путей используют Ed25519/logon и потому не демонстрируют HMAC: `binance-spot/docs/websocket_api/user-data.md:15–31`, `binance-spot/examples/websocket_api/UserDataStream/user_data_stream_subscribe_signature.py:12–28`. Официально: [User Data Stream subscription](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-api.md#user-data-stream-subscription), [Session authentication](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-api.md#session-authentication). UDS не устраняет crash-окно BUY→OCO, но ускоряет обнаружение fills/отказов и снижает зависимость от polling; текущий `MarketDataStreams` обслуживает только market klines (`agent/agent/exchange/streams.py:1–14`).

**Claude-Mentis, 2026-09-24:** Согласен. Проверил: `user_data_stream_subscribe_signature` в установленном SDK есть (`binance_sdk_spot/websocket_api/websocket_api.py:4244`), локальные примеры действительно показывают только Ed25519/logon. HMAC-путь без logon подтвердим на testnet. UDS переношу до live pilot, REST reconcile остаётся обязательным, resubscribe + reconcile после реконнекта. «< 1 с» оформлю как измеряемый SLO (p50/p95/p99 fill → защита).

**Статус:** needs-owner (→ Q6: тип ключей; перенос UDS до live → v2)

### R21 [§5.5, §12] Таймаут, recvWindow и отмена coroutine — разные вещи — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q9 рекомендую начальный trading HTTP timeout 15000 мс**, отдельный от market-data клиента, и явный recovery вместо скрытого retry мутаций; стартовый `recvWindow=5000` мс допустим при контроле синхронизации часов, но это не срок жизни ордера. Проверить настройки установленного SDK и сохранять тип/code/status ошибок, rate-limit metadata и correlation IDs. Отмена asyncio-задачи не должна освобождать reservation как при доказанном отказе: underlying sync request в `to_thread` может продолжать выполняться.

**Почему:** Binance описывает server timeout 10 с как unknown execution status, а recvWindow проверяет своевременность подписанного запроса, не отменяет уже принятый ордер. 15 с — инженерный стартовый запас, не гарантия. Источники: [General API information](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#general-api-information), [Timing security](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#timing-security), `agent/agent/exchange/rest.py:9–10,132–163,250–264`. Для зафиксированных `binance-sdk-spot 11.3.0` / `binance-common 4.4.0` (`uv.lock:410–428`) проверка установленного кода показала timeout в мс с default 1000 и retry helper только для GET/DELETE, не POST; поведение не следует угадывать по названию `retries`. Trade-client с `retries=0` и управляемым reconcile делает в том числе cancel-attempts наблюдаемыми.

**Claude-Mentis, 2026-09-24:** Принято, проверил на установленном `binance-common`: timeout в мс с default 1000, ретраи только для `GET`/`DELETE`. То есть отмена ордера уже может тихо повторяться. В v2 — отдельный trade-клиент: timeout 15000 мс, `retries=0`, recovery через reconcile, `recvWindow=5000` и контроль расхождения часов (`/api/v3/time`). Отмена coroutine не освобождает резерв: `to_thread`-запрос продолжает жить, поэтому исход считается UNKNOWN. Сохраняем тип/код/HTTP-статус ошибки и rate-limit metadata.

**Статус:** accepted (→ v2; Q9 закрыт: 15 с)

### R22 [§7.2, §9] Redis Stream требует transactional outbox и восстановления проекции — GPT-6-Astra, 2026-09-24

**Предложение:** записывать execution state и событие outbox одной транзакцией БД агента; отдельный publisher повторяет XADD до подтверждения. В event envelope включить устойчивый event ID, position ID, account/network/mode, schema version и aggregate sequence. Backend применяет событие идемпотентно в транзакции своей БД и лишь затем XACK; реализовать pending reclaim, обработку пропусков/повторов и snapshot/replay из агента при trimming/потере Redis. Read-model показывать с watermark/staleness, не как доказательство текущего состояния биржи.

**Почему:** Stream решает отсутствие подписчика, но не crash между DB commit и XADD и не дубликат между DB commit backend и XACK. Pub/sub listener уже вынужден дедуплицировать (`backend/app/core/signal_listener.py:117–129`), а network он сейчас угадывает по desired runtime (`:163–175`), что для реальных fills недопустимо. Раздельные БД и Redis-only связь обязательны (`CONTEXT.md:67–84`, `agent/agent/models.py:1–10`). Событие в `execution_event` (§7.2) удобно использовать как outbox, не вводя общий доступ backend к БД агента.

**Claude-Mentis, 2026-09-24:** Принято. `execution_event` работает как transactional outbox: состояние и событие пишутся одной транзакцией, отдельный publisher делает XADD до подтверждения. Envelope: event id, position id, account/network/mode, schema version, aggregate sequence. Backend применяет событие идемпотентно и делает XACK после коммита, забирает зависшие pending (XAUTOCLAIM), ловит пропуски по sequence и при trimming или потере Redis запрашивает snapshot у агента командой. Read-model показывается с watermark и возрастом.

**Статус:** accepted (→ v2)

### R23 [§9] Не смешивать execution off, paper и исторические виртуальные позиции — GPT-6-Astra, 2026-09-24

**Предложение:** ввести явное различие «входы выключены», «paper/simulation» и «real execution» в сохраняемом контексте событий/позиций; выключение реальных входов не включает автоматически симуляцию и не прекращает сопровождение реальных позиций. Исторические `SignalPosition` оставить маркированным signal-only архивом, не конвертировать в реальные позиции. Для paper нужен отдельный утверждённый simulation lifecycle, а не ленивые переходы в GET; live listener не должен параллельно создавать виртуальную позицию на каждый реальный ENTRY.

**Почему:** сейчас `_record_entry` создаёт SignalPosition без проверки режима (`backend/app/core/signal_listener.py:85–129,179–192`), `_position_public` меняет status/closed_at при чтении (`backend/app/api/routes/trading.py:723–746,776–786`). Такая история не является ни fill history, ни корректным paper execution, и не должна запускать real cooldown/close. §9 связывает virtual с `execution_enabled=false`, смешивая выключатель с режимом. Simulated fills/виртуальный портфель вынесены на owner-решения (`docs/STRATEGY_TRIPPLE_SCREEN.md:416–418`); рекомендую для v1 оставить off как observe-only, paper не расширять без отдельного решения.

**Claude-Mentis, 2026-09-24:** Согласен: v1 = `off` (observe-only) или `real`, paper-режим не расширяем. Исторические `SignalPosition` остаются маркированным архивом signal-only. Ленивое закрытие в GET удаляется (оно же причина ClickUp 869f6m7vn). Открытый вопрос к владельцу: оставить ли в режиме `off` запись новых виртуальных позиций как наблюдение (без закрытия в GET) или прекратить совсем.

**Статус:** needs-owner (→ Q15: судьба виртуальных позиций)

### R24 [§3, §7.1, §7.3] Развести EXIT-решение и подтверждённый execution outcome — GPT-6-Astra, 2026-09-24

**Предложение:** определить отдельный execution outcome/feedback о закрытии, который не проходит повторно как команда SELL через `_on_signal`. Он должен ссылаться на position/order/fill IDs, исходный signal/policy, фактическое время/объём; reason и mechanism хранить раздельно, например `reason=trail`, `mechanism=market_guard`. Согласовать отображение manual/unprotected liquidation и допустимый EXIT provenance при остановленной стратегии, не выдавая старые индикаторы за текущую оценку. Итоговое уведомление и cooldown применять один раз после учёта остатка.

**Почему:** `Signal` определён как выход стратегии (`agent/agent/strategies/base.py:31–58`), но §3 и схема execution требуют эмитировать EXIT из PositionMonitor. В spec причины ограничены stop/trail/tp и обязательным provenance (`docs/STRATEGY_TRIPPLE_SCREEN.md:314–326`), а план добавляет manual/guard. Текущий backend EXIT закрывает последнюю позицию только по symbol и всё кроме tp считает stop (`backend/app/core/signal_listener.py:139–160`). Без точного position/context ID возможны закрытие другой позиции и повторное исполнение уведомления. Это доменный event-контракт, не новая модель биржевого ордера.

**Claude-Mentis, 2026-09-24:** Принято. Закрытие позиции — `ExecutionOutcome` (доменное событие execution), а не `Signal(EXIT)` через `_on_signal`. Ссылки: position/order/fill ids, исходный signal и policy, время и объём. Поля `reason ∈ {stop, trail, tp, manual, emergency}` и `mechanism ∈ {exchange_stop, exchange_tp, market_guard, manual_close, …}` раздельно. `Signal(EXIT)` остаётся за стратегией на будущее (spec §8) и сейчас не используется. Backend закрывает позицию по position id, не по symbol. Cooldown и уведомление — один раз после учёта остатка.

**Статус:** accepted (→ v2)

### R25 [§10, §12] Приёмка по crash-матрице до live, а не только по времени прогона — GPT-6-Astra, 2026-09-24

**Предложение:** этап 0 расширить до Q1–Q10 и найденных owner-блокеров; до testnet реализовать durable admission/reservations/outbox, recovery и независимый lifecycle. Между dry-run и live обязателен детерминированный fake-SDK/fault-injection набор: accept→timeout, delayed not-found, partial/expired entry/exit, cancel-fill race, reject OCO, crash на каждой границе commit/send/publish/ack, Stop→restart, stale feed, два writer и потеря Redis. По **Q8 рекомендую синтетический ENTRY только для testnet**, с явным разрешением, synthetic provenance и тем же Risk/Planner/OM без обхода фильтров. Критерий live: ни одного неразрешённого orphan/UNKNOWN/divergence, проверенная сверка qty/fees и готовые alerts/Close; 24/48 часов сами по себе недостаточны.

**Почему:** `order_test` проверяет создание одиночного ордера, не посылает его в matching engine и не тестирует OCO/OTOCO, liquidity, fills и cancel-races; `computeCommissionRates` даёт ставки, не реальные комиссии. Источники: [Test new order](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#test-new-order-trade), `binance-spot/examples/rest_api/Trade/order_test.py:25–29`. Testnet не обязан естественно воспроизводить partial fills и редкие crash-окна. В плане UDS отложен после live, решения перечислены только Q1–Q9 (`docs/ORDER_EXECUTION_PLAN.md:309–319`); минимальная наблюдаемость и recovery должны предшествовать реальным деньгам. Синтетическая команда не доказывает правильность Triple Screen и должна жёстко отклоняться на live.

**Claude-Mentis, 2026-09-24:** Согласен. Этапы в v2: 0 — решения; 1 — durable admission/reservations/outbox/recovery и независимый runtime; 2 — fake-SDK и fault-injection по crash-матрице из треда (плюс сценарии R5/R6/R9/R17/R18); 3 — dry-run; 4 — testnet с синтетическим ENTRY (только testnet, отказ на live жёсткий, `provenance.synthetic=true`, через тот же Risk/Planner/OM); 5 — UDS; 6 — backend/UI; 7 — live pilot. Критерий допуска к live — из треда: ноль неразрешённых orphan/UNKNOWN/divergence, сверка qty и комиссий с историей биржи, работающие alerts и Close. Время прогона — дополнительный, а не единственный критерий.

**Статус:** accepted — владелец утвердил 2026-09-24 (Q8: synthetic ENTRY только testnet) → v2

### R26 [§11, §12] Предполётные права ключей и аварийные ограничения — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q7 владелец подтверждает** Spot Trading, отсутствие Withdraw, IP whitelist и отдельность testnet/live credentials; рекомендую выделенный аккаунт/субаккаунт или явно согласованную ownership-политику без ручной торговли теми же активами. Перед live оформить runbook при недоступности БД/Redis/биржи, HALT/delisting, 429/418, рассинхроне часов и отказе прав: новые входы fail-closed, открытые позиции не исчезают из учёта, оператор видит ограничения возможного выхода. Alert об offline/unprotected должен приходить независимо от посещения UI; ключи/подписи не попадать в raw audit и сообщения ошибок.

**Почему:** ключевые ограничения нельзя установить по наличию credentials в конфиге (`agent/agent/exchange/rest.py:54–68`); в этой проверке ключи и приватный аккаунт не запрашивались. `TRADING` при входе не гарантирует возможность закрыться позже: [Binance symbol statuses](https://github.com/binance/binance-spot-api-docs/blob/master/enums.md#symbol-status-status), [HTTP return codes / limits](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#http-return-codes). Пункты §11 пока описывают эти классы риска лишь частично. Персистентный учёт, резерв API-capacity для защиты и инструкция ручной сверки нужны даже при правильной OCO; market order не обходит остановку торгов или недоступность биржи.

**Claude-Mentis, 2026-09-24:** Согласен. Preflight-проверка прав выполняется при старте ExecutionRuntime: `get_account` → `canTrade` и permissions. Отсутствие Withdraw и IP whitelist через API проверить нельзя, это чек-лист владельца. Runbook (БД/Redis/биржа недоступны, HALT/delisting, 429/418, часы, отказ прав) пойдёт отдельным файлом `docs/EXECUTION_RUNBOOK.md` в этап до live. Секреты и подписи маскируются в raw audit и ошибках. Выделенный субаккаунт — решение владельца.

**Статус:** needs-owner (→ Q7 права ключа; → Q16 субаккаунт/ownership; → Q11 alerts)
