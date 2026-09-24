# План: реальное исполнение ордеров по сигналам стратегии

| | |
|---|---|
| Статус | **черновик на обсуждение** (v1, 2026-09-24) |
| Автор v1 | zed (Claude Code) по запросу владельца |
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

- **Q1 (sizing).** Фиксированный `position_quote` на пилоте или сразу риск от equity? Размеры?
- **Q2 (вход).** LIMIT IOC с `max_slippage_pct` (какое значение? предлагаю 0.3%) или MARKET?
- **Q3 (Stop).** Stop стратегии оставляет позиции под OCO (предложение) или закрывает их?
- **Q4 (unprotected).** Если защиту не удалось поставить за N попыток — продать по рынку или ждать оператора?
- **Q5 (stop-нога).** `STOP_LOSS` (market) там, где разрешён, или всегда `STOP_LOSS_LIMIT` + guard?
- **Q6 (UDS).** Какой тип ключа на live и testnet: HMAC или Ed25519? От этого зависит путь User Data Stream.
- **Q7 (ключ).** Есть ли у live-ключа право Spot Trading и IP whitelist?
- **Q8 (testnet).** Нужна ли тестовая команда «синтетический ENTRY» для проверки исполнения без ожидания сетапа?
- **Q9 (таймауты).** Какой таймаут для торговых REST-вызовов (предлагаю 5–10 с) против текущего 1 с по умолчанию SDK?
- **Q10 (OTOCO).** Стоит ли пожертвовать spec §7.1 (стоп от fill) ради атомарной защиты через OTOCO?

---

## 13. Обсуждение

### Правила
1. Документ — единственный источник правды. Изменения в §1–§12 вносит **автор
   предложения, которое принято** (или zed после согласования), с пометкой версии
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
5. В ClickUp-задаче — только уведомления «добавлены треды R3–R5» / «план v2», без
   дублирования содержания.

### Треды

_(пока пусто — ревьюер начинает с R1)_
