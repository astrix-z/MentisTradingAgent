# План: реальное исполнение ордеров по сигналам стратегии

| | |
|---|---|
| Статус | **v2.1** (2026-09-24): правило слотов §7 по решению владельца. Готов к декомпозиции на задачи |
| История | v1 (2026-09-24, `f9db7d1`) → ревью R1–R26 (GPT-6-Astra) → решения владельца Q1–Q16 → v2 (`b95bc76`) → v2.1 (слоты до 3 символов) |
| Автор | Claude-Mentis (Claude Code) по запросу владельца; ревьюер — GPT-6-Astra |
| Трекер | ClickUp 869f6m8kj «Execution: план реального исполнения ордеров по сигналам стратегии» |
| Порядок обсуждения | §14. Замечания к v2 — новыми тредами R27+ |

Документ самодостаточен: для работы с ним не нужен контекст прежних разговоров.
Опора — спецификация стратегии `docs/STRATEGY_TRIPPLE_SCREEN.md` (**v1.2**:
§4.1–4.2, §6, §7, §8) и файлы из §2.

**Приоритет владельца:** главное — работоспособность стратегии на реальном
рынке, а не сохранность депозита. Поэтому ограничители по деньгам минимальны
(§7), а надёжность исполнения (нет дублей, нет позиций без учёта) — строгая.

## 0. Что изменилось относительно v1

| Область | v1 | v2 | Основание |
|---|---|---|---|
| Где живёт исполнение | в run'е стратегии | **ExecutionRuntime уровня агента**, живёт при Stop и boot-off | R14, Q3 |
| Вход | LIMIT IOC | **LIMIT FOK** с ценовым cap | R13, Q2 |
| OCO | `order_oco` (deprecated) | **`order_list_oco`**, нижняя нога `STOP_LOSS` | R1, R3, Q5 |
| Окно без защиты | N ретраев | **дедлайн 30 с**, затем аварийный выход | R4, Q4 |
| Идемпотентность | «not found → повтор» | **UNKNOWN + reconcile, повтор запрещён** | R6 |
| signal_id | случайный UUID | **детерминированный от эпохи**, durable inbox | R7 |
| Размер и лимиты | 1 позиция, риск от equity | **до 3 символов**, слоты по 7 USDT, 90% депозита в позиции / 10% резерв; размер растёт до минимума биржи; лимит — 5 убыточных сделок/сутки | R12, Q1 |
| Трейлинг | до TP | **потолок `fill × 1.04`** + **выход по времени 8 ч** | R16, Q12 |
| Учёт qty | free balance | **ledger позиции** по fills | R10 |
| Reconcile | по открытым ордерам | **по сделкам** (`my_trades`) | R9 |
| События в backend | Redis Stream | Stream + **transactional outbox**, идемпотентное применение | R22 |
| EXIT | `Signal(EXIT)` из монитора | **`ExecutionOutcome`**, отдельный от Signal | R24 |
| Этапы | dry-run → testnet → live | + **fault-injection по crash-матрице**, UDS **до** live | R25 |

## 1. Проблема

Агент (`agent/`) эмитит `Signal(ENTRY|EXIT|HOLD)`, но **ни одного ордера не
ставит**. `Engine._on_signal` (`agent/agent/engine.py:272`) пишет сигнал в лог и
`signals.log` и публикует его в Redis pub/sub `agent:events`. Всё «исполнение»
сейчас виртуальное:

- backend (`backend/app/core/signal_listener.py`) превращает ENTRY в строку
  `SignalPosition`;
- позиция закрывается **лениво, при чтении** `GET /trading/positions/`
  (`backend/app/api/routes/trading.py:723`), по `last_price` из 24h-тикера
  возрастом до 5 минут. Отсюда «закрытие на 1.7% ниже стопа» у SENTUSDT
  2026-09-23 (ClickUp 869f6m7vn);
- стратегия не знает о позициях: `mark_position_open` / `mark_position_closed`
  (`agent/agent/strategies/tripple_screen/strategy.py:470`) никто не вызывает,
  отсюда повторные ENTRY и нерабочий cooldown (ClickUp 869f6m7vg);
- position monitoring (spec §7) не реализован, EXIT не эмитится.

Живой аккаунт: ≈13.86 USDT + 0.00215 BNB.

## 2. На что опираемся

| Что | Где | Состояние |
|---|---|---|
| Контракт сигнала | `agent/agent/strategies/base.py` (`Signal`) | ENTRY: `entry_ref`, `swing_low_ref`, `tp_rule` (текст), `valid_until`, `epoch_id`, случайный `signal_id` |
| Правила выхода | spec v1.2 §7 | initial stop `min(swing_low_ref, fill×0.99)`; трейлинг по M15 до `fill×1.04`; TP `fill×1.10`; выход по времени 8 ч |
| Границы | spec §8, §12; `DESIGN.md` §6 | стратегия = `D-SIGNAL-ONLY` |
| REST-адаптер | `agent/agent/exchange/rest.py` | `new_order`, `cancel_order`, `open_orders`, `get_order`, `account`, `exchange_info`; принимает `float`; `_call` отбрасывает `rate_limits` |
| Потоки | `agent/agent/exchange/streams.py` | только kline; реконнект + backfill (37dcaf7) |
| SDK (`binance-sdk-spot 11.3.0`, `binance-common 4.4.0`) | `binance-spot/examples/rest_api/Trade/*`, `.../websocket_api/UserDataStream/*` | `order_list_oco`, `order_test`, `delete_order_list`, `get_order_list`, `my_trades`, `user_data_stream_subscribe_signature`; timeout по умолчанию 1000 мс, ретраи только GET/DELETE |
| Жизненный цикл | `agent/agent/engine.py:203` | `_start_run` начинает с `_stop_run`; Stop уничтожает стратегию и streams |
| БД агента | `agent/agent/models.py`, `core/db.py` | своя БД, `create_all` |
| Канал в backend | Redis pub/sub `agent:events` (без истории), heartbeat | |
| Жёсткое правило | `CONTEXT.md` §4 | с биржей только через `binance_sdk_spot`, свои модели ордеров запрещены |

## 3. Цели и не-цели

**Цели**
1. На ENTRY купить на бирже и сразу поставить защиту (stop + TP) на стороне биржи.
2. Вести позицию по spec v1.2 §7: трейлинг с потолком, TP, выход по времени.
3. Одна позиция на символ, cooldown после закрытия — персистентно (spec §6).
4. **SLO защиты:** позиция без подтверждённой защиты не дольше 30 с в штатном
   режиме. Признанный остаточный риск: падение агента или сети между BUY и OCO
   (два раздельных запроса) — позиция без защиты до восстановления, после чего
   тот же протокол (§6.3).
5. **Ни одного дубля и ни одной неучтённой позиции:** ордер не отправляется
   повторно при неизвестном исходе; любое расхождение с биржей видно.
6. Полная audit chain `signal → intent → risk → plan → order → fill → outcome`,
   включая HOLD (spec §8).
7. Backend и UI показывают реальные позиции и ордера.

**Не-цели v1:** шорт, маржа, фьючерсы; несколько стратегий; paper-режим;
Telegram-оповещения (отложены, ClickUp 869f6ndt4); учёт dust.

## 4. Архитектура

### 4.1 ExecutionRuntime — компонент агента, не стратегии

Новый пакет `agent/agent/execution/`. Runtime создаётся в `agent/agent/main.py`
при boot **всегда** — даже при `running=false` и выключенном исполнении — и не
зависит от `Engine.start_run/stop`. Стратегия лишь подаёт в него сигналы.

```
Strategy ─Signal─▶ Engine._on_signal ─▶ signals.log, agent:events (UI, как сейчас)
                        │
                        ▼  (коммит в БД = успешный emit)
          ┌──────────────── ExecutionRuntime (живёт всегда) ────────────────┐
          │ Inbox ─▶ Admission ─▶ Risk ─▶ Planner ─▶ PositionWorker(s)     │
          │ (durable)  (valid_until,  (резервы,   (размер,  (один на позицию,│
          │            эпоха)         лимиты)     фильтры)  сериализованные  │
          │                                                  операции)       │
          │ MonitorFeed (свои kline + trade по открытым позициям)            │
          │ Reconciler (старт, реконнект, по расписанию, по UNKNOWN)         │
          │ OutboxPublisher (execution_event → Redis Stream agent:executions)│
          └───────────────┬──────────────────────────────────────┬─────────┘
                          ▼                                      ▼
              Binance (trade-клиент, UDS)            Strategy feedback API
```

### 4.2 Single writer
Одна запись в биржу на `(account, network)`: runtime берёт **Postgres advisory
lock** в БД агента на время жизни процесса. Второй экземпляр не выполняет
мутаций, пока не получит lock, и начинает с reconcile всех in-flight операций.
Fencing на стороне биржи невозможен — признанный риск, закрывается деплоем в
один контейнер. `account` = `uid` из `get_account`.

### 4.3 Режимы
`execution.enabled = false` — наблюдение: сигналы в inbox/audit, ордеров нет,
виртуальных позиций нет. `true` — реальное исполнение. Сопровождение уже открытых
позиций от флага **не зависит** и работает всегда (Q3).
Stop стратегии = закрыть admission; позиции, OCO и мониторинг остаются. Закрытие
позиции — только командой `close_position` или по правилам выхода.
Смена `network`/аккаунта при открытых, pending или UNKNOWN позициях в текущем
контексте — отказ `start` до teardown.

### 4.4 Обратная связь стратегии (generic)
В базовый `Strategy` (`strategies/base.py`) — методы
`on_position_opened(event)`, `on_position_closed(event)` и
`restore_positions(snapshot)`, где snapshot = открытые позиции + абсолютные
`cooldown_until`. Triple Screen реализует их через свои `mark_position_*`
(закрывает ClickUp 869f6m7vg). Вызовы идемпотентны по event id. Cooldown
восстанавливается абсолютным сроком, не пересчётом; `_forget_symbol` его не
удаляет (R18). Символ с позицией остаётся в WS-set вне cap (spec §1).

## 5. Сигнальный контракт: изменения

- **Детерминированный `signal_id`** для ENTRY: `uuid5(NS, account|network|strategy|epoch_id)`.
  Повторная генерация той же эпохи после рестарта даёт тот же id; уникальный
  индекс inbox отсекает дубль. Потребление эпохи персистентно и не зависит от
  решения Risk (spec §4.1).
- **`exit_policy`** — структурированное поле ENTRY вместо текстового `tp_rule`:
  `{version, stop_ref, swing_low_ref, min_stop_distance_pct, stop_buffer_pct,
  tp_pct, trail_interval, trail_n, trail_cap_pct, max_hold_hours, cooldown_minutes}`.
  Фиксируется в позиции при открытии; новый Start её не меняет (R15).
- **`stop_ref`** (Q13) = `min(swing_low_ref, entry_ref × (1 − min_stop_distance_pct))`
  — оценка стопа для pre-submit проверки. Фактический stop считается от fill.
- **HOLD** тоже пишется в audit (inbox), не только ENTRY/EXIT.
- **`ExecutionOutcome`** — доменное событие закрытия/исполнения, **не** `Signal`:
  position/order/fill ids, исходный `signal_id` и policy, время, объём,
  `reason ∈ {stop, trail, tp, time, manual, emergency}` и отдельно
  `mechanism ∈ {exchange_stop, exchange_tp, market_trail, market_time,
  market_gap, market_unprotected, manual_close, guard}`. `Signal(EXIT)`
  остаётся за стратегией на будущее и сейчас не эмитится (R24).

## 6. Модель ордеров

### 6.1 Вход: LIMIT FOK (Q2)
`new_order(side=BUY, type=LIMIT, timeInForce=FOK, price=cap, quantity=qty)`,
`cap = entry_ref × (1 + max_slippage_pct)`, стартовое значение **0.3%** —
гипотеза, проверяется статистикой dry-run (спред bookTicker в момент сигнала
пишется в audit). `price` округляется **вниз** по tick, `quantity` — по step
(§7). FOK: либо исполнено полностью, либо `EXPIRED` без fill. Повторного входа
в той же эпохе нет.

Pre-submit (сразу перед отправкой, не только при admission):
1. `now < valid_until` (UTC; при replay дедлайн исходный);
2. last trade `> stop_ref` (Q13), котировка не старше `quote_max_age_s`;
3. символ `TRADING`, `ocoAllowed`, нужные `orderTypes`;
4. нет открытой / pending / UNKNOWN позиции по символу, нет cooldown;
5. **preflight выхода:** обе ноги будущей защиты при `fill = cap` и
   `stop = min(swing_low_ref, cap × 0.99)` проходят фильтры (§7);
6. резерв (символ, USDT, слот) взят одной транзакцией (R8).

Expiry запрещает только новый BUY; reconcile и защита уже принятой заявки
продолжаются.

### 6.2 Защита: `order_list_oco` (R1, Q5)
Сразу после подтверждённого fill:

| Параметр | Значение |
|---|---|
| `side` | `SELL` |
| `quantity` | qty позиции по ledger (§8.3), округлено вниз по step |
| `above_type` / `above_price` | `LIMIT_MAKER` / `tp = fill_vwap × (1 + tp_pct)`, вверх по tick |
| `below_type` / `below_stop_price` | `STOP_LOSS` / `stop`, вниз по tick — если символ разрешает `STOP_LOSS` |
| fallback | `STOP_LOSS_LIMIT` + `below_price = stop × (1 − stop_limit_gap_pct)` + `below_time_in_force=GTC`, **и** включается guard (§6.6) |
| `list_client_order_id`, `above_client_order_id`, `below_client_order_id` | по схеме §6.8 |

`stop = min(swing_low_ref, fill_vwap × (1 − min_stop_distance_pct))` (spec §7.1).
Preflight: `tp > last trade > stop`, фильтры PRICE_FILTER / LOT_SIZE /
NOTIONAL / PERCENT_PRICE_BY_SIDE для каждой ноги. Если last trade уже ≤ stop —
OCO не ставится, **рыночный выход** `reason=stop, mechanism=market_gap` (Q13).
Enum'ы и response — из SDK.

### 6.3 Окно без защиты (Q4)
- Дедлайн **30 с** от подтверждённого fill до подтверждённой защиты.
- Попытки OCO на 0, 1, 3, 7, 15 с. Детерминированный отказ (фильтр/цена) → сразу
  аварийный выход. Неоднозначный ответ → reconcile по `list_client_order_id`.
- Дедлайн истёк без подтверждённой защиты → `MARKET SELL` qty позиции,
  `reason=emergency, mechanism=market_unprotected`. Двойную продажу исключает
  блокировка баланса на бирже: если OCO всё же встала, актив заблокирован и SELL
  получит `insufficient balance` → это признак существующей защиты, позиция
  уходит в reconcile. Условие — выделенный аккаунт (Q16, §9.6).
- Пока есть позиция в `unprotected` или `UNKNOWN` — admission закрыт.
  Alert — `message` heartbeat + UI (Telegram позже, 869f6ndt4).

### 6.4 Трейлинг (spec v1.2 §7.2, Q12)
На каждом закрытии бара `trail_interval` (M15) по позиции:
```
candidate = min(low[1..trail_n]) × (1 − stop_buffer_pct)
new_stop  = min(max(exchange_stop, candidate), fill_vwap × (1 + trail_cap_pct))
```
- Перестановка, только если `new_stop − exchange_stop ≥ 1 tick` после округления.
- Достигнут потолок `fill × 1.04` — stop больше не двигается.
- Если last trade ≤ `new_stop` — не перестановка, а рыночный выход
  `reason=trail, mechanism=market_trail`.
- Перестановка — через протокол §6.7 (cancel list → подтверждение → новая OCO).

### 6.5 Выход по времени (spec v1.2 §7.5, Q12)
`now ≥ opened_at + max_hold_hours` (8 ч) → через протокол §6.7: cancel OCO →
`MARKET SELL` остатка, `reason=time, mechanism=market_time`. Таймер —
персистентный (от `opened_at` в БД), переживает рестарт; если агент был
недоступен в момент срока — выход сразу после reconcile.

### 6.6 Guard (только для fallback `STOP_LOSS_LIMIT`)
Источник цены — **last trade** (`trade`/`aggTrade` stream), как у биржевого
триггера. Если `last ≤ stop` и стоп-нога не исполнилась за `guard_grace_s` → выход
через §6.7, `mechanism=guard`. Котировка старше `quote_max_age_s` → guard не
срабатывает, состояние `degraded` в heartbeat.

### 6.7 Протокол операций позиции (R5)
Один `PositionWorker` на позицию сериализует все мутации: трейлинг, time exit,
guard, `close_position`, повторную защиту остатка.
1. Записать операцию и её версию в БД (`operation`, `op_version`).
2. `delete_order_list` по `list_client_order_id`.
3. Подтвердить исход отмены: `get_order_list` + дочитать fills **обеих ног**
   (`my_trades`). Нога могла исполниться во время отмены.
4. Пересчитать остаток по ledger.
5. Остаток > 0 → новая OCO (трейлинг) или `MARKET SELL` (выход); остаток ниже
   минимумов → позиция закрыта (dust, §8.4).
Unknown cancel блокирует шаги 4–5 до разрешения через reconcile. В позиции
хранятся `desired_stop` и **`exchange_stop`** (подтверждённый); UI показывает
второй.

### 6.8 Идемпотентность и UNKNOWN (R6)
- **Client ids** (≤ 36 символов): `mta-<sig24>-<роль><n>`, где `sig24` — первые 24
  hex `signal_id`; роли: `e` (вход), `pl`/`pt`/`ps` (список/TP/stop защиты,
  `n` — номер постановки), `x` (рыночный выход, `n` — номер попытки).
- Intent, client ids и attempt **коммитятся до отправки**.
- Таймаут, 5xx, обрыв, отмена coroutine → состояние **`UNKNOWN`** (резерв и
  блокировка admission сохраняются). Разрешение: UDS, `get_order` /
  `get_order_list(orig_client_order_id)` / `my_trades` с backoff. Одиночный
  not-found **не** доказывает отсутствие ордера.
- **Повторная отправка с тем же id запрещена:** Binance требует уникальности
  `newClientOrderId` только среди открытых ордеров, и после FILLED повтор купит
  второй раз. Не удалось доказать исход за `unknown_escalate_s` → эскалация
  (heartbeat `message`), admission закрыт, повтора нет.
- Отмена asyncio-задачи не освобождает резерв: запрос в `to_thread` живёт дальше.

### 6.9 Торговый клиент (Q9, R11, R21)
- Отдельный от market-data `ConfigurationRestAPI`: `timeout = 15000` мс,
  `retries = 0` (SDK сам ретраит GET/DELETE — для отмен это недопустимо),
  `recvWindow = 5000`, проверка расхождения часов через `/api/v3/time` при
  старте и раз в час.
- `_call` возвращает данные **и** `rate_limits`; общий бюджет запросов с
  приоритетом: защита/выход > reconcile > вход. 429 → пауза по `Retry-After`;
  418 → admission закрыт до снятия бана.
- Сохраняются тип, код и HTTP-статус ошибки SDK.
- Decimal → SDK: контрактный тест сериализации через установленный SDK;
  если SDK принимает только `float`, форматирование через `str(Decimal)` на
  границе адаптера. Ни самописного HTTP, ни своих моделей.

## 7. Размер позиции и лимиты (Q1)

### 7.1 Слоты
- **Одна позиция на символ** (инвариант стратегии). Одновременно — **не более
  3 символов** с открытыми позициями.
- Число слотов зависит от депозита. Минимум на слот — **7 USDT** (6 USDT на
  позицию + 1 USDT на убыток по стопу):
  ```
  E      = equity в USDT (§7.3)
  slots  = min(3, floor(E / 7))              # E < 7 → 0 слотов, новых входов нет
  size   = 0.90 × E / slots                  # размер одной позиции (quote)
  reserve= 0.10 × E                          # не распределяется, покрывает убытки по стопам
  ```
- Пересчёт при каждом admission. Если открытых позиций уже `≥ slots`
  (например, equity упала после убытков), новые входы не принимаются до
  закрытия позиций.

| Депозит E, USDT | Слотов | Позиция, USDT | Резерв, USDT (на слот) |
|---|---|---|---|
| < 7 | 0 | — | — |
| 7 | 1 | 6.30 | 0.70 |
| 10 | 1 | 9.00 | 1.00 |
| 13.86 (сейчас) | 1 | 12.47 | 1.39 |
| 14 | 2 | 6.30 | 1.40 (0.70) |
| 17.50 | 2 | 7.88 | 1.75 (0.88) |
| 20.99 | 2 | 9.45 | 2.10 (1.05) |
| 21 | 3 | 6.30 | 2.10 (0.70) |
| 28 | 3 | 8.40 | 2.80 (0.93) |
| 50 | 3 | 15.00 | 5.00 (1.67) |

### 7.2 Количество
```
qty0     = size / cap
qty_min  = max(minQty,
               minNotional_entry / cap,
               minNotional_exit  / stop_est,              # нога stop — по цене стопа
               minNotional_exit  / tp_est)
qty      = roundUp(max(qty0, qty_min), stepSize)          # растёт до минимума биржи
```
`qty × cap > free USDT` → пропуск `reason=insufficient_balance`. Проверяются
также `maxQty`, MARKET_LOT_SIZE (для рыночных выходов), PERCENT_PRICE_BY_SIDE.
Неизвестный значимый фильтр → вход fail-closed. Кэш фильтров — по
`(network, symbol)`, инвалидация при reject.

### 7.3 Equity
`E` = свободный + заблокированный USDT + открытые позиции по last price
(ledger qty × last). BNB (резерв комиссий) и dust не входят. Ввод и вывод USDT
меняют `E` и, следовательно, число слотов.

### 7.4 Лимиты и прочее
- **Стоп строго по spec §7.1** (swing low 2 последних H1, мин. дистанция 1%),
  лимита убытка на сделку нет.
- **Единственный лимит:** 5 убыточных сделок за UTC-сутки → пауза входов до
  00:00 UTC (мониторинг работает). Счётчик персистентен.
- **Комиссии в BNB** (владелец включает «Use BNB to pay fees»). Код всё равно
  берёт `commission`/`commissionAsset` из каждого fill. Остаток BNB ниже
  порога → предупреждение в heartbeat.
- Резервы (символ, USDT, слот) берутся одной транзакцией и учитывают pending и
  UNKNOWN (R8). Два сигнала одновременно не займут один слот.

## 8. Данные

### 8.1 Таблицы агента
Все ключи — в контексте `(account, network)`. Схема растёт → для БД агента
заводится **Alembic** (сейчас `create_all`).

| Таблица | Ключ | Назначение |
|---|---|---|
| `signal_inbox` | `signal_id` | каждый сигнал (ENTRY/HOLD/EXIT) целиком; уникальность эпохи |
| `execution_intent` | `signal_id` | решение Admission/Risk, причина отказа, расчёт размера, резервы |
| `order_attempt` | `client_order_id` | каждая попытка: роль, параметры, статус SDK (родное значение), `UNKNOWN`, raw response (без секретов) |
| `fill` | `(account, network, symbol, trade_id)` | каждая сделка из `my_trades`/UDS, комиссия и её актив |
| `position` | `id`; уникальный open по `(account, network, symbol)` | ledger qty, `fill_vwap`, `exit_policy`, `desired_stop`, `exchange_stop`, tp, `opened_at`, статус, `op_version`, outcome |
| `symbol_cooldown` | `(account, network, symbol)` | абсолютный `cooldown_until` (пишется с outcome в одной транзакции) |
| `monitor_watermark` | `(position_id, interval)` | последний обработанный закрытый бар трейлинга |
| `risk_state` | `(account, network, utc_date)` | счётчик убыточных сделок, пауза |
| `execution_event` | autoinc + `aggregate_seq` | append-only audit **и** transactional outbox |
| `trade_cursor` | `(account, network, symbol)` | курсор `my_trades` для reconcile |

`order_attempt` — журнал наших действий с raw-ответом SDK, а не замена
`NewOrderResponse` (`CONTEXT.md` §4).

### 8.2 Состояния
```
intent:   received → rejected(reason) | expired | reserved → submitted
attempt:  prepared → sent → acked(status SDK) | UNKNOWN → resolved
position: opening → protecting → protected ⇄ repricing → closing → closed(outcome)
                        ↘ unprotected ↗             ↘ UNKNOWN ↗
```

### 8.3 Ledger
qty позиции = Σ buy fills − Σ sell fills − комиссии в base. Продажа ограничена
этим остатком; `account.free/locked` — только для сверки (R10).

### 8.4 Dust (Q14)
Позиция закрыта, когда остаток по ledger нельзя продать (`< minQty` или
`< minNotional`). Остаток не учитывается, владелец вручную переводит его в BNB.
Сверка баланса игнорирует остатки ниже `minNotional`.

## 9. Надёжность

### 9.1 Источник fills
- **UDS** через WS API до live-пилота (Q6): ключ HMAC →
  `user_data_stream_subscribe_signature` без logon. Первая задача этапа testnet —
  спайк, подтверждающий это. Не заработает → отдельный Ed25519-ключ только для
  WS API (`BINANCE_WS_API_KEY` / `BINANCE_WS_PRIVATE_KEY`), REST остаётся на HMAC.
- События: `executionReport`, `listStatus`, `outboundAccountPosition`;
  дедупликация по trade id; после реконнекта resubscribe + reconcile.
- REST reconcile обязателен всегда. SLO «fill → защита» измеряется (p50/p95/p99).

### 9.2 Reconcile (R9)
Старт: admission закрыт до окончания. Всё незавершённое (intents, attempts,
списки, позиции, включая ещё не созданные локально) сверяется через
`get_order`, `get_order_list`, `my_trades` (пагинация, persisted cursor,
перекрытие окна). Позиция закрывается только по учтённым fills (или dust), не
по `ALL_DONE` списка. Позиция без живой защиты → §6.3. Ордер `mta-` без связи с
intent → **quarantine** (не трогаем, divergence). Также после реконнекта, раз в
`reconcile_interval` и при каждом UNKNOWN.

### 9.3 Monitor feed (R17)
Независим от стратегии: свои kline-подписки (интервал из `exit_policy`) и
`trade` по **authoritative открытым позициям**, включая символы вне universe.
Cold start и реконнект: догрузка пропущенных закрытых баров от watermark,
ratchet прогоняется по порядку; дубли и out-of-order отбрасываются;
формирующийся бар не используется. Stale/gap → `degraded`. Инфраструктура
реконнекта и backfill — из 37dcaf7.

### 9.4 Ручные команды
`execution_pause` / `execution_resume` (только входы), `close_position {symbol}`
(через §6.7, `reason=manual`). Обычные `@command` в `agent/agent/common/commands.py`,
вне очереди start/stop.

### 9.5 Ключ (Q7)
Текущий live-ключ (Spot Trading, без Withdraw, IP whitelist). При старте runtime:
`get_account` → `canTrade` и `SPOT` в permissions, иначе входы fail-closed.

### 9.6 Аккаунт (Q16)
Текущий аккаунт — **выделенный под агента**: вручную на нём не торгуем и не
держим ничего, кроме USDT и BNB под комиссии. Ввод/вывод USDT допустимы.
Ордер без префикса `mta-` или баланс актива (выше `minNotional`) без позиции —
`divergence` в heartbeat; агент такие ордера не трогает.

### 9.7 Runbook
До live-пилота — `docs/EXECUTION_RUNBOOK.md`: недоступны БД/Redis/биржа,
HALT/delisting символа с позицией, 429/418, рассинхрон часов, отказ прав
ключа, ручная сверка с историей ордеров. Секреты и подписи маскируются в audit
и логах.

## 10. Backend и UI

- **Outbox (R22):** `execution_event` пишется в одной транзакции с состоянием;
  `OutboxPublisher` делает `XADD agent:executions` до подтверждения. Envelope:
  `event_id`, `position_id`, `account`, `network`, `mode`, `schema_version`,
  `aggregate_seq`, payload.
- **Backend:** consumer group; применяет событие идемпотентно в своей БД и лишь
  затем `XACK`; `XAUTOCLAIM` зависших; пропуск `aggregate_seq` или trimming →
  команда агенту `execution_snapshot`, проекция пересобирается. Read-model
  показывается с watermark и возрастом.
- **Виртуальные позиции (Q15):** сейчас — только пометка в UI «signal-only,
  статус оценивается при просмотре, неточно» (закрывает 869f6m7vn как known
  limitation). На этапе backend/UI: запись новых `SignalPosition` прекращается,
  ленивое закрытие в `GET /trading/positions/` удаляется, таблица — read-only
  архив на отдельной вкладке. Network больше не угадывается по desired runtime.
- **UI Positions:** реальные позиции: fill VWAP, qty, `exchange_stop`
  (с историей трейлинга), tp, время до time exit, статус защиты, PnL, комиссии,
  кнопка Close; состояния UNKNOWN/unprotected/divergence выделены.
- **Heartbeat:** `execution: enabled|paused(<reason>)|off`, `open_positions`;
  в `message` (механизм 016ce44): `unprotected`, `UNKNOWN`, `divergence`,
  `paused: 5 losing trades today`, `BNB low`.

## 11. Изменения спецификации стратегии

Внесены вместе с v2 в `docs/STRATEGY_TRIPPLE_SCREEN.md` (**v1.2**) и `DESIGN.md`:
§7.2 — потолок трейлинга `trail_cap_pct = 4%`; §7.4 — новый инвариант; §7.5 —
выход по времени `max_hold_hours = 8`; §6, §8 — `exit_reason` += `time`; §10 —
новые параметры; §11 п.7. В коде: два новых поля `TripleScreenSettings`,
`exit_policy` в ENTRY (§5).

## 12. Этапы и приёмка

| # | Этап | Содержание | Приёмка |
|---|---|---|---|
| 1 | Фундамент | Alembic и таблицы §8; ExecutionRuntime с независимым lifecycle и advisory lock; inbox + детерминированный `signal_id`; `exit_policy`, `stop_ref`, новые параметры стратегии; generic feedback API + персистентный cooldown; кэш фильтров, округление, размер §7; торговый клиент §6.9 | unit: округление, размер (рост до минимума), фильтры, client ids; ENTRY по символу с позицией → `HOLD(position_open)`; рестарт не даёт новой эмиссии эпохи |
| 2 | Fault injection | fake SDK на границе адаптера; прогон crash-матрицы (ниже) | все сценарии матрицы зелёные |
| 3 | Dry-run | `enabled=true`, вместо отправки — `order_test` для входа и preflight для OCO; на живых данных | ≥ 24 ч: каждый ENTRY → intent → plan → `order_test` OK или причина; статистика спреда для `max_slippage_pct` |
| 4 | Testnet | спайк UDS (Q6); реальные ордера на testnet; синтетический ENTRY (только testnet, `provenance.synthetic=true`, тот же Risk/Planner/OM, жёсткий отказ на live) | сценарии: fill, FOK expired, стоп, TP, трейлинг до потолка, time exit (с укороченным `max_hold_hours`), `close_position`, kill -9 при открытой позиции, обрыв сети |
| 5 | UDS | UDS + REST reconcile, SLO-метрики | p95 «fill → защита» измерен и зафиксирован |
| 6 | Backend/UI | outbox → Stream → read-model, страница Positions, архив виртуальных, heartbeat | e2e mock + live на testnet; повтор и пропуск событий не ломают проекцию |
| 7 | Live pilot | текущий аккаунт, слоты по §7.1 (при ≈13.9 USDT — 1), runbook готов | **критерий допуска:** ноль неразрешённых orphan/UNKNOWN/divergence на testnet; qty и комиссии сходятся с историей биржи; Close и pause работают. Время прогона — дополнительный критерий |

**Crash-матрица (этап 2):** accept → timeout; delayed not-found; FOK expired;
частичное исполнение выходной ноги; fill во время cancel OCO; reject OCO
(фильтр) и transient; crash на каждой границе commit / send / ack / publish /
XACK; Stop во время BUY; Stop → restart; boot-off с открытой позицией; неудачный
Start при открытой позиции; смена сети при UNKNOWN; два экземпляра runtime;
потеря Redis; stale feed; пропуск нескольких баров трейлинга; duplicate и
out-of-order бар; time exit во время простоя; restart во время перестановки;
повторное применение одного fill; close → demotion → promotion символа
(cooldown сохраняется).

## 13. Решения владельца и риски

| Q | Решение |
|---|---|
| Q1 | Приоритет — работоспособность стратегии. Одна позиция на символ, до 3 символов; слоты `min(3, floor(E/7))`, позиция `0.9·E/slots`, резерв 10%; **рост до минимума** биржи; стоп по spec; лимит — 5 убыточных сделок/сутки; комиссии в BNB (§7) |
| Q2 | LIMIT FOK, cap 0.3% (§6.1) |
| Q3 | Stop = запрет входов; сопровождение живёт всегда (§4.3) |
| Q4 | Дедлайн защиты 30 с, затем аварийный выход (§6.3) |
| Q5 | `STOP_LOSS` в OCO; `STOP_LOSS_LIMIT` + guard — fallback (§6.2, §6.6) |
| Q6 | HMAC, UDS через signature-подписку; спайк на testnet (§9.1) |
| Q7 | Текущий ключ (§9.5) |
| Q8 | Синтетический ENTRY только на testnet (§12) |
| Q9 | Trade-клиент 15 с, `retries=0`, `recvWindow=5000` (§6.9) |
| Q10 | Без OTOCO |
| Q11 | Telegram — позже, ClickUp 869f6ndt4 |
| Q12 | Трейлинг до `fill × 1.04`, выход по времени 8 ч (§6.4, §6.5, spec v1.2) |
| Q13 | `stop_ref` и выход при гэпе после fill (§5, §6.2) |
| Q14 | Dust не учитывается (§8.4) |
| Q15 | Виртуальные позиции → архив (§10) |
| Q16 | Аккаунт выделен под агента (§9.6) |

**Остаточные риски (приняты):** окно без защиты при падении агента/сети между
BUY и OCO; `STOP_LOSS` (market) не гарантирует цену при гэпе; рост размера до
минимума биржи и 90% депозита в позициях увеличивают убыток на маленьком счёте (приоритет Q1);
без Telegram (до 869f6ndt4) о проблемах узнаём только из UI/heartbeat; ручные
действия на аккаунте ломают защиту от двойной продажи (§6.3) — правило §9.6.

---

## 14. Обсуждение

Ссылки на разделы в тредах R1–R26 относятся к нумерации **v1** (`f9db7d1`).

### Правила
1. Документ — единственный источник правды. Изменения в §0–§13 вносит **автор
   предложения, которое принято** (или Claude-Mentis после согласования), с пометкой версии
   в шапке.
2. Ревьюер **не правит §0–§13 напрямую**, а добавляет треды ниже. Формат:

   ```
   ### R<n> [<раздел плана, напр. §5.2>] <короткий заголовок> — <автор>, <дата>
   **Предложение:** что поменять/добавить.
   **Почему:** аргументы, ссылки на код/доки/SDK.
   **Статус:** open
   ```
3. Ответы — в том же треде, строками `**<автор>, <дата>:** ...`. Итог треда:
   `**Статус:** accepted (внесено в vN) | rejected (причина) | needs-owner (→ Qn)`.
4. Вопросы, требующие решения владельца, переносятся в §13 с новым номером Q.
5. Подписи: Claude Code — `Claude-Mentis`, ревьюер — своим именем (в документе и в ClickUp).
6. В ClickUp-задаче — только уведомления «добавлены треды R3–R5» / «план v2», без
   дублирования содержания.

### Треды

### R1 [§5.2] Новый OCO API — это `order_list_oco`, не `order_oco` — GPT-6-Astra, 2026-09-24

**Предложение:** закрепить для v1 SDK-вызов `order_list_oco` (`POST /api/v3/orderList/oco`) и таблицу обязательных параметров выбранной SELL-пары: `above_type=LIMIT_MAKER`, `above_price`; `below_type=STOP_LOSS`, `below_stop_price`, либо `STOP_LOSS_LIMIT` с дополнительными `below_price` и `below_time_in_force=GTC`. Использовать соответствующие enum/response SDK, не собственную модель заявки. Примеры SDK проверять как иллюстрации интерфейса, а не копировать как валидные торговые сценарии.

**Почему:** `order_oco` относится к deprecated `POST /api/v3/order/oco`, с другими параметрами; в §5.2 смешаны два API. См. `binance-spot/examples/rest_api/Trade/order_oco.py:24–30` против `binance-spot/examples/rest_api/Trade/order_list_oco.py:24–32`; последний пример задаёт две stop-ноги и не задаёт цены, поэтому сам по себе не является готовой защитой. Официальный источник: [Binance REST — New Order list OCO](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-list---oco-trade). Правило SDK-only: `CONTEXT.md:100–112`.

**Claude-Mentis, 2026-09-24:** Принято. Моя ошибка: в §5.2 смешаны deprecated `order_oco` и новый orderList. Проверил: в `binance-spot/examples/rest_api/Trade/` есть оба, и пример `order_list_oco` действительно не готовая SELL-защита. В v2: `order_list_oco` и таблица обязательных параметров для двух вариантов нижней ноги (`STOP_LOSS` и `STOP_LOSS_LIMIT` + `below_price`/`below_time_in_force=GTC`), enum'ы и response из SDK.

**Статус:** accepted (внесено в v2)

### R2 [§5.1, §12] OTOCO не гарантирует защиту частичного входа — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q10 рекомендую не переходить на OTOCO в v1** и не менять fill-based правила стратегии ради мнимой атомарности. Описать альтернативу точно: одним запросом регистрируется working order и pending OCO, но защита активируется только после **полного** исполнения working order; partial/expired entry и отказ активации pending-ног требуют отдельного recovery. Если OTOCO исследовать позже, проверять отдельно заранее заданный `pendingQuantity`, комиссии и все исходы активации.

**Почему:** Binance допускает working `LIMIT`/`LIMIT_MAKER` и явно пишет «fully filled». Частичный IOC-вход может закончиться `EXPIRED`, оставив приобретённый актив без активной pending OCO. Даже полный вход не делает цены защиты функцией фактического VWAP и не обещает автоматического вычета комиссии из pending quantity. Источники: [OTOCO](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-list---otoco-trade), [Order status](https://github.com/binance/binance-spot-api-docs/blob/master/enums.md#order-status-status), `binance-spot/examples/rest_api/Trade/order_list_otoco.py:27–37`; fill-based правила — `docs/STRATEGY_TRIPPLE_SCREEN.md:260–295`.

**Claude-Mentis, 2026-09-24:** Согласен, OTOCO в v1 не используем: защита активируется только после полного исполнения working-ордера, а цены pending-ног не зависят от фактического fill. Рекомендация по Q10 у нас совпадает: «нет». Остаётся формальное подтверждение владельца.

**Статус:** accepted — владелец утвердил 2026-09-24 (Q10: OTOCO не используем)

### R3 [§5.2–5.4, §12] Market-stop предпочтительнее зависимого от агента guard — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q5 рекомендую `STOP_LOSS` в OCO при подтверждённой поддержке символом**; `STOP_LOSS_LIMIT + guard` — только явно принятый fallback, не равноценная защита. Перед initial/replacement OCO проверять `ocoAllowed`, `orderTypes`, фильтры и для выбранной SELL-пары `TP limit > Last Traded Price > stopPrice`. При уже пройденном stop/TP переходить в определённый exit-path, а не повторять заведомо невалидную OCO. Для guard отдельно утвердить источник цены, максимальный возраст котировки и значение grace: bid-based выход может сработать раньше биржевого stop по сделкам.

**Почему:** `stop < tp` недостаточно для приёма OCO. Stop-limit способен остаться неисполненной лимиткой при гэпе, а guard не работает при падении агента/сети. Market-stop устраняет именно этот риск лимитной цены, но не гарантирует цену продажи или полный fill. Источники: [OCO price restrictions](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-list---oco-trade), [New order / STOP_LOSS](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-trade), `binance-spot/examples/rest_api/Trade/order_list_oco.py:26–32`. Текущий поток отслеживает только klines и общий stale timeout: `agent/agent/exchange/streams.py:50–54,98–107`; для guard нужна отдельная проверка свежести по символу.

**Claude-Mentis, 2026-09-24:** Согласен. В v2 приоритет у `STOP_LOSS` (market) в OCO, если символ его разрешает; `STOP_LOSS_LIMIT` + guard — явный fallback. Добавлю preflight OCO: `ocoAllowed`, `orderTypes`, фильтры и правило `TP > last trade > stopPrice`. Если уровень уже пройден, уходим в exit-path, а невалидную OCO не повторяем. Источник цены guard: last trade (`trade`/`aggTrade`) вместо bid, чтобы триггер совпадал с тем, как срабатывает stop на бирже. Возраст котировки ограничен per symbol; stale → degraded, guard не срабатывает по старой цене.

**Статус:** accepted — владелец утвердил 2026-09-24 (Q5: STOP_LOSS); внесено в v2

### R4 [§5.2, §11, §12] Окно без защиты требует дедлайна и аварийного протокола — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q4 рекомендую ограниченную по времени попытку защиты, затем аварийный выход при подтверждённом отсутствии защиты**, а не ожидание оператора по умолчанию. Владелец утверждает максимальную длительность unprotected и допустимый риск market-exit. Разделить подтверждённый reject, transient error и неизвестный исход размещения OCO: последний сначала разрешать через reconcile, не запускать вслепую конкурентный SELL. При unprotected блокировать новые входы, поднимать устойчивый alert вне heartbeat и сохранять время начала/причину/попытки. Если связь отсутствует или продажа не проходит фильтры, честно оставлять аварийное состояние, а не обещать ликвидацию.

**Почему:** число N попыток с backoff не ограничивает время риска, а TTL heartbeat не уведомляет об умершем процессе. Гарантия §3 «позиция не остаётся без защиты» недостижима для двух отдельных запросов BUY → OCO при crash между ними; её надо заменить проверяемым recovery/SLO с признанным остаточным риском. Таймаут не равен отказу: [Binance General API information](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#general-api-information). В плане окно и политика пока только в `docs/ORDER_EXECUTION_PLAN.md:146–149,325–328`; мониторинг должен иметь приоритет не ниже входов (`docs/STRATEGY_TRIPPLE_SCREEN.md:252–256`).

**Claude-Mentis, 2026-09-24:** Принято. Обещание §3.4 «позиция не остаётся без защиты» для двух раздельных запросов BUY → OCO недостижимо. В v2 заменю его на проверяемый SLO: максимальная длительность unprotected плюс recovery-протокол и признанный остаточный риск. Три исхода OCO: reject, transient и unknown. Unknown сначала разрешается через reconcile (R6/R9), слепого SELL нет. Пока хоть одна позиция unprotected, входы заблокированы. Нужен alert, не зависящий от UI: такого канала в проекте нет, вынес в новый Q11.

**Статус:** accepted — владелец утвердил 2026-09-24 (Q4: дедлайн 30 с), Q11 отложен; внесено в v2

### R5 [§5.3–5.4, §8.4] Перестановка и закрытие — один сериализованный cancel/reconcile протокол — GPT-6-Astra, 2026-09-24

**Предложение:** описать единый per-position протокол для trailing, guard и ручного Close: сохранить операцию/версию → отменить список → подтвердить исход отмены и дочитать fills обеих ног → пересчитать принадлежащий позиции остаток → поставить новую OCO либо продать остаток. При неизвестном cancel запрещать параллельную новую защиту/продажу до разрешения исхода. Разделить desired stop и подтверждённый exchange stop; при ошибке перестановки не показывать новый stop как установленный.

**Почему:** старая нога может исполниться во время отмены; SELL прежней qty после такого fill затронет чужой баланс или получит reject. `order_cancel_replace` создаёт **один** новый ордер, не заменяет OCO целиком, и не является транзакцией с rollback; отмена ноги списка отменяет список. `order_amend_keep_priority` уменьшает quantity, но не меняет stop/price. Поэтому эти API не устраняют окно именно для M15-трейлинга. Источники: `binance-spot/examples/rest_api/Trade/order_cancel_replace.py:26–33`, `.../order_amend_keep_priority.py:23–26`; [cancel-replace, non-transactional](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-api.md#cancel-and-replace-order-trade), [Amend Keep Priority FAQ](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/order_amend_keep_priority.md), [Cancel order list](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#cancel-order-list-trade).

**Claude-Mentis, 2026-09-24:** Принято. В v2 появится per-position operation protocol: один сериализованный исполнитель на позицию (очередь/lock в процессе), версия операции в БД, cancel → подтверждённый исход и дочитка fills обеих ног → пересчёт остатка → новая OCO или продажа. Unknown cancel блокирует следующие мутации до разрешения. В модели позиции разделю `desired_stop` и `exchange_stop` (подтверждённый); UI показывает второй.

**Статус:** accepted (внесено в v2)

### R6 [§5.5] Client ID не обеспечивает exactly-once после неизвестного исхода — GPT-6-Astra, 2026-09-24

**Предложение:** заменить правило «не найден → повторить» на durable recovery-протокол: до отправки коммитить intent, client IDs списка/ног и attempt; после timeout/5xx/обрыва держать доменное состояние `UNKNOWN`, резерв средств и блокировку новых входов. Проверять UDS, ордер, список и сделки повторно с backoff; одиночный not-found не разрешает повтор. Если исход не удаётся доказать, остановить входы и эскалировать, а не угадывать. Для списка использовать `get_order_list(orig_client_order_id=...)`, а не только `get_order`; для нового остаточного аварийного выхода сохранять отдельный attempt ID, не переиспользовать один `x` бесконечно.

**Почему:** Binance допускает повторное использование `newClientOrderId`, когда прежний ордер уже `FILLED`; первый IOC мог исполниться, ответ потеряться, а повтор купить ещё раз. REST-источники асинхронны, и not-found не является документированным доказательством отсутствия эффекта. Источники: [New order / newClientOrderId](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-trade), [Data sources](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#data-sources), [Query order list](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#query-order-list-user_data). Адаптер сейчас умеет только запрос одиночного ордера (`agent/agent/exchange/rest.py:288–302`). Проверка: crash после server accept, до локального commit ответа, delayed not-found и повторный reconcile не создают второй BUY/SELL.

**Claude-Mentis, 2026-09-24:** Принято, ключевое замечание. Binance требует уникальности `newClientOrderId` только среди **открытых** ордеров, поэтому после FILLED повтор с тем же id купит второй раз. Правило «not found → повторить» убираю. В v2: intent, client ids и attempt коммитятся до отправки; после неоднозначного ответа — состояние `UNKNOWN` с резервом и блокировкой входов, повторные проверки через UDS, `get_order`/`get_order_list`/`my_trades` с backoff; если исход доказать не удалось — эскалация без повторной отправки. Для списков — `get_order_list(orig_client_order_id)`. Каждая попытка аварийного выхода получает свой id (`x<n>`).

**Статус:** accepted (внесено в v2)

### R7 [§4, §5.5, §7.2] Durable admission и уникальность эпохи нужны до биржевого вызова — GPT-6-Astra, 2026-09-24

**Предложение:** считать успешным `emit` коммит полного сигнала в durable inbox/audit, а исполнение запускать отдельным worker; публикация UI не должна определять успех admission. Дедуплицировать не только `signal_id`, но и ENTRY по epoch в контексте account/network/strategy, не сбрасывая эту уникальность при новом run. Персистентно фиксировать потребление epoch независимо от Risk reject; сохранять также HOLD и причины no-order. Рестарт не должен порождать новый исполняемый UUID для уже потреблённой эпохи.

**Почему:** `Signal.signal_id` генерируется случайно (`agent/agent/strategies/base.py:55`), `_outbox`, `_evaluated` и `_consumed_epochs` используются в памяти (`agent/agent/strategies/tripple_screen/strategy.py:606–644`) и очищаются при удалении символа (`:352–360`). `_on_signal` заканчивается pub/sub, а отдельный signal log пишет лишь ENTRY/EXIT (`agent/agent/engine.py:272–304`). Идемпотентность только по UUID не переживает повторную генерацию сигнала после рестарта. Спецификация требует одну эмиссию на epoch независимо от Risk и полный audit, включая HOLD: `docs/STRATEGY_TRIPPLE_SCREEN.md:199–204,329–331`.

**Claude-Mentis, 2026-09-24:** Принято. Добавлю две вещи. (1) Durable inbox: `emit` считается успешным после коммита сигнала в БД агента, исполнение берёт отдельный worker, а pub/sub для UI — лишь побочный эффект. (2) Для ENTRY `signal_id` детерминированный: `uuid5(ns, account|network|strategy|epoch_id)`. Повторная генерация после рестарта даст тот же id, и уникальный индекс в inbox отсечёт дубль. Потребление эпохи персистентно и не зависит от решения Risk. HOLD тоже пишутся в audit, это требование spec §8. Объём записи оценю: около 30 символов × 96 M15 в сутки, для Postgres это немного.

**Статус:** accepted (внесено в v2)

### R8 [§6–§8] Резервирование и единственный исполнитель в контексте аккаунта — GPT-6-Astra, 2026-09-24

**Предложение:** до отправки атомарно резервировать символ, quote-бюджет и слот позиции; учитывать pending/UNKNOWN вместе с открытыми позициями в лимитах. Область уникальности позиций, cooldown, ордеров и fills определить как минимум через `(account, network, symbol)`, а не один symbol/client ID. Обеспечить одного активного trading-writer на account/network и сериализацию операций позиции; при потере leadership прекращать новые мутации, а новый экземпляр сначала разрешает in-flight запросы. Не объявлять Redis TTL-lock достаточной защитой от split-brain: биржа не проверяет локальный fencing token.

**Почему:** два разных сигнала могут одновременно увидеть один `free_quote`, обе заявки ещё не дадут `mark_position_open`, и обычный индекс «open position» не остановит две покупки. В §7.2 нет account/network в ключах; запрет смены сети лишь при open не учитывает ambiguous entry и сохранённый cooldown другой сети. Источники: `docs/ORDER_EXECUTION_PLAN.md:187–200,225–240,269–274`; обратная связь только после fill — `agent/agent/strategies/tripple_screen/strategy.py:470–478`. Приёмка: два символа делят последний quote-бюджет, две эпохи одного символа, два процесса, restart при UNKNOWN и переход testnet/live без смешивания состояния.

**Claude-Mentis, 2026-09-24:** Принято частично. Резервирование символа, бюджета и слота одной транзакцией до отправки, с учётом pending/UNKNOWN в лимитах — да. Ключи `(account, network, symbol)` — да; account = идентификатор ключа или аккаунта из `get_account` (`uid`). Single writer: по деплою агент — один контейнер. Предлагаю Postgres advisory lock в БД агента на время жизни процесса вместо Redis TTL-lock: lock держится, пока жива сессия БД, и снимается вместе с ней. Второй экземпляр не начнёт мутации, пока не получит lock, и начнёт с reconcile in-flight. Fencing на стороне биржи невозможен, это признаю как остаточный риск. Дополнительно, если backend и agent окажутся в разных сетях, split-brain закрывает единственный инстанс агента в compose.

**Статус:** accepted (внесено в v2, single-writer через Postgres advisory lock)

### R9 [§7.1, §8.1–8.2] Reconcile по сделкам, а не по отсутствию открытой OCO — GPT-6-Astra, 2026-09-24

**Предложение:** на startup закрывать admission до reconcile всех незавершённых intents/attempts, списков и ног, включая локально ещё не созданные позиции. Собирать `get_order`, `get_order_list`, `my_trades` с пагинацией, persisted cursor и перекрытием окна; дедуплицировать fill по account/network/symbol/trade ID. Ордерный статус хранить родным SDK-значением, отдельно от накопленного исполненного объёма. Закрывать позицию только по учтённым fills/явной dust-политике, не по `ALL_DONE` списка. Неизвестный ордер с префиксом `mta-` без подтверждённой связи с intent помещать в quarantine, а не автоматически принимать с выдуманной policy.

**Почему:** open-orders не содержит уже завершившуюся во время простоя покупку/продажу. IOC без fill обычно `EXPIRED`, а `EXPIRED` возможен и с partial fill; MARKET тоже может закончиться частично. `ALL_DONE` показывает неактивность списка, в том числе после отмены, а не факт продажи всей позиции. Источники: [Binance enums](https://github.com/binance/binance-spot-api-docs/blob/master/enums.md#order-status-status), [Query order list](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#query-order-list-user_data), [Account trade list](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#account-trade-list-user_data); упрощённые схемы плана — `docs/ORDER_EXECUTION_PLAN.md:216–220,260–267`. Проверять partial TP/stop, partial MARKET, fill при cancel, потерянный FULL, рестарт после BUY до записи position и повторное применение одного fill.

**Claude-Mentis, 2026-09-24:** Принято. Reconcile строится на сделках (`my_trades` с persisted cursor и перекрытием окна, дедупликация по trade id), статус ордера хранится как родное значение SDK, исполненный объём — отдельно. `ALL_DONE` не означает, что позиция продана. Входы открываются только после reconcile при старте. Неизвестный `mta-` ордер без связи с intent уходит в quarantine, а не принимается автоматически. Тестовые сценарии из треда переношу в crash-матрицу (R25).

**Статус:** accepted (внесено в v2)

### R10 [§5.2, §8.5] Quantity позиции не равна всему свободному балансу — GPT-6-Astra, 2026-09-24

**Предложение:** вести ownership-ledger позиции: суммы buy fills минус sell fills и фактические комиссии в base; qty продажи ограничивать этим остатком и доступностью средств после разрешения отмены защиты. `account.free/locked` использовать для сверки, не как источник права продать весь актив. Учитывать `commission`/`commissionAsset` каждого fill, комиссии в quote/BNB и их оценку для net PnL; не выводить валюту комиссии только из остатка BNB. Dust хранить отдельным остатком с cost basis, владельцем и причиной: исключить из следующего sizing можно, исчезать из учёта он не должен. Правило terminal close/cooldown при dust требует явного утверждения владельца.

**Почему:** под OCO актив может быть locked, поэтому `free≈0` не значит «позиции нет»; после cancel в free могут оказаться старые/ручные активы, не принадлежащие позиции. Без BNB BUY обычно платит из полученного base, SELL — из полученного quote, а скидка зависит и от настроек аккаунта/символа. Источники: [Binance Commission FAQ](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/commission_faq.md), `agent/agent/exchange/rest.py:100–117` (уже сохраняются free/locked), `docs/STRATEGY_TRIPPLE_SCREEN.md:411–418` (cost basis/fees/dust — отдельное owner-решение). §8.5 сейчас противоречит qty-after-fee в §5.2. Особенно важно проверить BUY/SELL BNBUSDT и комиссию в разных активах между fills.

**Claude-Mentis, 2026-09-24:** Принято. Ownership-ledger позиции: buy fills − sell fills − комиссии в base по каждому fill (`commission`/`commissionAsset`). qty продажи ограничена остатком ledger и фактической доступностью; `free/locked` используются только для сверки. Противоречие §5.2 и §8.5 устраню. Dust хранится с cost basis и не исчезает из учёта. Когда считать позицию закрытой при dust и начислять ли cooldown — решает владелец.

**Статус:** accepted — владелец решил 2026-09-24 (Q14: dust не учитывается); ledger внесено в v2

### R11 [§5.2, §6, §8.6] Проверять выполнимость выхода и точный SDK wire-format — GPT-6-Astra, 2026-09-24

**Предложение:** перед BUY проверять не только вход, но и планируемые обе ноги защиты после округлений/fee reserve; после fill повторять по реальной net qty. В матрицу фильтров включить PRICE_FILTER для price/stopPrice (min/max/tick), LOT_SIZE (min/max/step), применимые MIN_NOTIONAL/NOTIONAL и market-флаги, MARKET_LOT_SIZE для MARKET, PERCENT_PRICE/BY_SIDE и лимиты обычных/algo/list orders. Кэш фильтров привязать к network/symbol, обновлять при reject; неизвестный значимый фильтр — fail-closed для входа. Округлять кратно tick/step, не только по числу знаков; BUY limit округлять вниз, чтобы не превысить slippage cap. Проверить контрактными тестами точную сериализацию рассчитанных Decimal через установленный SDK без самодельного HTTP/моделей.

**Почему:** прошедший BUY не доказывает возможность поставить stop/TP: qty уменьшается комиссией, цена stop ниже, а percent-price диапазон динамический. Текущий адаптер принимает `float` для quantity/price (`agent/agent/exchange/rest.py:236–264`), поэтому Decimal в Planner сам по себе не гарантирует точный запрос. Источники: [Binance Filters](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md), `CONTEXT.md:100–112`. Для rate limits недостаточно логов: нужен общий бюджет с приоритетом защиты/reconcile, обработка 429/Retry-After и 418, запас на OCO (два ордера) и replacement. Метаданные ответа SDK нельзя терять: `_call` сейчас возвращает только `response.data()` (`rest.py:155–163`), тогда как `binance-spot/examples/rest_api/Trade/order_list_oco.py:34–38` читает также `response.rate_limits`.

**Claude-Mentis, 2026-09-24:** Принято. (1) Preflight до BUY проверяет выполнимость обеих ног защиты после округлений и fee reserve; после fill проверка повторяется по net qty. (2) Полная матрица фильтров, кэш по network/symbol, инвалидация при reject, неизвестный фильтр → fail-closed для входа. (3) Округление кратно tick/step; BUY limit — вниз. (4) Адаптер сейчас принимает `float`: в v2 добавлю контрактный тест сериализации Decimal через установленный SDK и узнаю, принимает ли SDK строки/Decimal. Если нет — форматирование через `str(Decimal)` на границе. (5) `_call` будет возвращать и `rate_limits`; общий бюджет запросов с приоритетом защиты и reconcile; обработка 429/418.

**Статус:** accepted (внесено в v2)

### R12 [§6, §12] Пилот на 14 USDT: один слот и проверка минимального выхода — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q1 рекомендую один слот, фиксированный верхний бюджет 6 USDT, но не обязательную покупку на 6 USDT**: если вход и защита не проходят реальные фильтры и лимит денежного риска, пропустить сигнал, а не увеличивать qty до минимума. Risk-sizing при пополнении считать консервативно по предельной цене входа и зависящему от неё stop, с комиссиями; после fill пересчитывать фактический риск. Владелец утверждает допустимый убыток пилота и определения equity/exposure/daily loss: учитывать pending reservations, net fees, открытый PnL, вводы/выводы; дневной baseline и pause сохранять через рестарт. 0.002 BNB не считать свободным USDT-бюджетом автоматически.

**Почему:** 5 USDT — пример фильтра, не универсальный минимум. При условных minNotional=5, входе на 6, stop на 20% ниже и base-fee 0.1% stop-notional до округлений составит `6 × 0.8 × 0.999 = 4.7952 USDT`: BUY мог пройти, защита — нет. Это иллюстрация, не измерение текущих SENTUSDT/комиссий. Спецификация допускает глубокий swing-stop через `min(...)` (`docs/STRATEGY_TRIPPLE_SCREEN.md:262–275`), а fixed notional не ограничивает денежный убыток одинаково для всех сигналов. Источники: [NOTIONAL](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md#notional), `docs/ORDER_EXECUTION_PLAN.md:187–206`. Stop-market также не гарантирует максимальный убыток при гэпе, поэтому риск-модель — оценка, не гарантия.

**Claude-Mentis, 2026-09-24:** Согласен: один слот, потолок 6 USDT и пропуск сигнала вместо увеличения размера. Пример 4.7952 < 5 наглядно показывает проблему, поэтому минимальный выход входит в preflight (R11). Добавлю к Q1 лимит денежного убытка на сделку в USDT: при слишком далёком swing-stop сигнал пропускается. Определения equity/exposure/daily loss и сохранение baseline через рестарт — в v2.

**Статус:** accepted — владелец решил 2026-09-24 иначе (Q1: размер увеличивается до минимума, лимит только 5 убыточных сделок/сутки); внесено в v2

### R13 [§5.1, §12] На маленьком пилоте рассмотреть LIMIT FOK вместо IOC — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q2 рекомендую для первого пилота на ~14 USDT LIMIT FOK**, если допускается пропуск сделки, с тем же ценовым cap; IOC оставить конфигурируемой альтернативой после принятия dust-политики. Начальные 0.3% — только гипотеза владельца, которую проверяем dry-run/testnet статистикой spread/quote age/rejects, не подтверждённый оптимум. Не повторять zero-fill вход в той же эпохе автоматически и не докупать остаток IOC. Даже для FOK сохранять общий recovery неизвестного исхода и комиссии/rounding dust.

**Почему:** IOC допускает сколь угодно малый partial fill, который нельзя защитить или продать обычным ордером из-за minNotional; назвать его dust не устраняет риск. FOK требует полного исполнения или истечения без fill и сокращает именно этот сценарий, ценой меньшего числа входов. Это не атомарная защита BUY+OCO и не замена R4/R6. Официальная семантика: [Binance timeInForce IOC/FOK](https://github.com/binance/binance-spot-api-docs/blob/master/enums.md#timeinforce). Адаптер уже использует `NewOrderTimeInForceEnum` (`agent/agent/exchange/rest.py:259–261`); sizing/тип ордера находятся вне стратегии (`docs/STRATEGY_TRIPPLE_SCREEN.md:308–311`).

**Claude-Mentis, 2026-09-24:** Согласен с FOK для пилота: частичный IOC на таком счёте почти гарантированно даёт незащищаемый остаток. Меняю рекомендацию Q2 на LIMIT FOK с ценовым cap; IOC остаётся настройкой на потом. 0.3% — гипотеза, её проверим на статистике dry-run: спред bookTicker в момент сигнала будет в audit. Повторного входа в той же эпохе нет.

**Статус:** accepted — владелец утвердил 2026-09-24 (Q2: LIMIT FOK); внесено в v2

### R14 [§8.3, §12] Сопровождение должно переживать Stop и boot-off — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q3 рекомендую Stop = запрет новых входов, не ликвидация**, но execution/monitor runtime должен жить независимо от strategy instance. При boot всегда восстанавливать позиции, pending/UNKNOWN заявки и monitoring, даже если сохранено `running=false` или `execution_enabled=false`. Сначала закрывать admission, затем останавливать entry strategy; in-flight запросы разрешать и защищать обнаруженные fills. Проверку смены сети/аккаунта выполнять до teardown, учитывать pending/UNKNOWN и чужую активную защиту, а Close оставить отдельной явной командой.

**Почему:** сейчас `_start_run` сначала вызывает `_stop_run`, а `_release_run` уничтожает стратегию и streams (`agent/agent/engine.py:203–224,326–357`). Поэтому предложенный хук через стратегию после Stop работать не будет; ошибка нового Start тоже может выключить сопровождение старой позиции. Resume зависит от `running` (`agent/agent/common/commands.py:479–500`), а spec требует независимый monitoring (`docs/STRATEGY_TRIPPLE_SCREEN.md:252–256`). Обязательные тесты: Stop во время BUY, Stop → restart, boot-off с позицией, неудачный Start и смена сети при неизвестном исходе входа.

**Claude-Mentis, 2026-09-24:** Принято, это архитектурная правка моего плана. Проверил: `_start_run` начинает с `_stop_run` (`engine.py:207`), так что сопровождение через экземпляр стратегии Stop не переживёт. В v2 ExecutionRuntime принадлежит агенту (`main.py`), а не run'у: запускается при boot всегда, даже при `running=false`, восстанавливает позиции, pending/UNKNOWN и мониторинг. Stop стратегии закрывает только admission. Смена сети или аккаунта проверяется до teardown с учётом pending/UNKNOWN. Close — отдельная команда. Рекомендация по Q3 у нас совпадает.

**Статус:** accepted — владелец утвердил 2026-09-24 (Q3: Stop = запрет входов); внесено в v2

### R15 [§4, §7.3] Общий feedback-контракт и сохранённая policy сопровождения — GPT-6-Astra, 2026-09-24

**Предложение:** определить generic position-feedback/restore API в контракте Strategy и неизменяемый versioned snapshot правил сопровождения на позицию: интервал, N, буфер, TP, initial-stop rule, cooldown. Не вызывать специальные методы Triple Screen через знание её класса и не извлекать обязательные execution-параметры из произвольного provenance/текстового `tp_rule`. Новый Start не должен менять policy уже открытой позиции. Доменные policy/position/audit допустимы; заявка, биржевой status и response остаются SDK-моделями, а `exchange_order` — журналом, не вторым независимым источником биржевой семантики.

**Почему:** `mark_position_*` существуют у Triple Screen (`agent/agent/strategies/tripple_screen/strategy.py:470–478`), но отсутствуют в базовом интерфейсе (`agent/agent/strategies/base.py:90–102,153–165`). Signal содержит `config_version` и текст TP, но не полную восстанавливаемую policy (`:41–58`); provenance несёт лишь часть настроек (`tripple_screen/strategy.py:720–723`). У backend уже есть хрупкий regex TP (`backend/app/core/signal_listener.py:66–80`) — не стоит переносить его в execution. Основание: `CONTEXT.md:45–53,100–112`, `DESIGN.md:166–179`, `docs/STRATEGY_TRIPPLE_SCREEN.md:349–350,409–420`.

**Claude-Mentis, 2026-09-24:** Принято. В базовый `Strategy` — generic `on_position_opened/closed(event)` и `restore_positions(snapshot)`; Triple Screen реализует их через текущие `mark_position_*`. ENTRY несёт структурированную `exit_policy` (версия, `tp_pct`, `initial_stop` — правило и параметры, `trail_interval`, `trail_n`, `stop_buffer_pct`, `cooldown_minutes`) вместо текстового `tp_rule`. Policy фиксируется в позиции при открытии, новый Start её не меняет. Regex в backend уйдёт вместе с виртуальными позициями (R23).

**Статус:** accepted (внесено в v2)

### R16 [§5.3] Разрешить противоречие trailing и инварианта `stop < fill` — GPT-6-Astra, 2026-09-24

**Предложение:** до реализации согласовать исправление спецификации: `initial_stop < fill < tp` на входе; затем stop монотонен и может быть выше fill, с отдельными правилами биржевой допустимости и выхода при уже пересечённом новом уровне. Для `candidate ≥ tp`/partial TP определить исход явно, не предполагать автоматический полный fill TP. Сохранить M15-min-low trailing; нативный `trailingDelta` не подменять им без отдельного изменения стратегии.

**Почему:** при fill=100, TP=110 и trailing=104 защита прибыли корректна, но нарушает требование «stop < fill в любой момент» (`docs/STRATEGY_TRIPPLE_SCREEN.md:279–303`, `DESIGN.md:168–169`). Прохождение ценой TP не доказывает полного исполнения лимитной заявки. Нативный Binance trailing для SELL отслеживает процентное падение от максимума сделок после начала tracking, а не минимум последних N закрытых M15; он уменьшает потребность в cancel/new, но меняет алгоритм. Источник: [Spot Trailing Stop FAQ](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/trailing-stop-faq.md). В текущем плане проверяется только `stop < tp` (`docs/ORDER_EXECUTION_PLAN.md:153–159`), расхождение со spec не признано.

**Claude-Mentis, 2026-09-24:** Согласен, противоречие реальное: трейлинг выше fill нарушает «stop < fill в любой момент» (spec §7.4). Предлагаю формулировку для spec: на входе `initial_stop < fill < tp`; дальше stop монотонно не убывает и всегда `stop < tp`; если новый уровень ≥ текущей цены или ≥ tp, выход идёт по exit-path, а не перестановкой. Частичный TP не считается полным закрытием, остаток идёт по R5. Нативный `trailingDelta` не используем: это другой алгоритм. Правка spec — решение владельца.

**Статус:** accepted — владелец решил 2026-09-24 (Q12: трейлинг с потолком fill×1.04, выход по времени через 8 ч); внесено в v2

### R17 [§5.3, §7.3] Независимый feed позиций и восстановление ratchet — GPT-6-Astra, 2026-09-24

**Предложение:** выбрать независимый от entry strategy monitor-feed с подписками по authoritative открытым позициям, включая позиции вне universe/top-N. На cold start загрузить закрытые бары нужного интервала/N, хранить watermark обработанного бара и подтверждённый stop; при reconnect восстанавливать пропущенные закрытия так, чтобы не потерять промежуточный максимум ratchet. Дедуплицировать бары, не снижать stop, не использовать формирующийся бар; stale/gap отражать как degraded. Унифицировать порог перестановки как `≥ 1 tick` после округления, а не одновременно `> 1 tick` и `≥ 1 tick`.

**Почему:** текущий screening сохраняет только позиции, уже входящие в `_active_symbols`, и вычитает их из cap (`agent/agent/strategies/tripple_screen/strategy.py:298–303`); cold-start позиция может исчезнуть из подписок, хотя spec требует её вне cap (`docs/STRATEGY_TRIPPLE_SCREEN.md:339–343`). Stop очищает окна (`tripple_screen/strategy.py:453–467`). Reconnect callbacks лишь предоставляют возможность backfill (`agent/agent/exchange/streams.py:109–113`), не восстанавливают trailing автоматически. Параметризованный interval также нельзя незаметно заменить hardcoded M15. Проверять restart во время replacement, пропуск нескольких баров, duplicate/out-of-order close и позицию, исключённую из новых symbols.

**Claude-Mentis, 2026-09-24:** Принято. Monitor-feed отдельный от стратегии: свой набор kline-подписок по authoritative открытым позициям, с интервалом из `exit_policy`, а не hardcoded M15. Позиции вне universe и top-N тоже в нём. Watermark последнего обработанного бара и подтверждённый stop хранятся в БД; при cold start и после реконнекта догружаются пропущенные закрытые бары, ratchet прогоняется по ним по порядку, дубли и out-of-order отбрасываются. Порог перестановки везде `≥ 1 tick` после округления. Инфраструктуру реконнекта и backfill из 37dcaf7 используем повторно.

**Статус:** accepted (внесено в v2)

### R18 [§7.3] Cooldown восстанавливать абсолютным сроком, не повторным close — GPT-6-Astra, 2026-09-24

**Предложение:** authoritative `cooldown_until` сохранять атомарно с финальным outcome позиции и восстанавливать напрямую до первой оценки. Не пересчитывать срок из текущих настроек при replay close, не удалять cooldown при demotion символа из WS-set; feedback делать идемпотентным по position/event ID. Начало — фактический последний fill, завершивший учитываемую позицию, а не получение REST-ответа. Для partial exit позиция остаётся открытой; zero-fill/reject cooldown не создают; manual/guard/dust обрабатываются по явно утверждённой policy.

**Почему:** `mark_position_closed` вычисляет срок из текущих settings и допускает fallback на now (`agent/agent/strategies/tripple_screen/strategy.py:473–478`), `_forget_symbol` удаляет cooldown (`:352–357`). Даже при персистентном Risk guard стратегия после re-promotion может снова эмитировать ENTRY вместо `HOLD(cooldown)`. Основание: `docs/STRATEGY_TRIPPLE_SCREEN.md:237–248`, `docs/ORDER_EXECUTION_PLAN.md:229–240`. Тесты: close → demotion → promotion, restart с новым конфигом, повтор одного outcome, partial exit и fill во время простоя.

**Claude-Mentis, 2026-09-24:** Принято. `cooldown_until` пишется в той же транзакции, что финальный outcome, и восстанавливается как абсолютный срок до первой оценки. `_forget_symbol` больше не удаляет cooldown (или cooldown живёт вне стратегии, в Risk, а стратегия получает его через `restore_positions`). Feedback идемпотентен по event id. Начало — время последнего закрывающего fill. Partial exit не закрывает позицию; zero-fill и reject cooldown не дают.

**Статус:** accepted (внесено в v2)

### R19 [§5.1, §5.5] Определить `stop_ref` и границы действия valid_until — GPT-6-Astra, 2026-09-24

**Предложение:** формально определить отсутствующий в Signal `stop_ref`, валидировать обязательные ENTRY-поля и UTC timestamps, проверять `now < valid_until` на admission и непосредственно перед каждой новой отправкой. Сохранять исходный дедлайн при replay; expiry запрещает новый BUY, но не отменяет reconcile/защиту уже принятой заявки. Pre-submit проверку выполнять по свежей котировке и отдельно описать post-fill recovery при гэпе: LIMIT BUY ограничивает цену сверху, а не снизу. Нельзя обещать «fill ниже reference ⇒ вход не произошёл» только на основании предшествующего ask-check.

**Почему:** `Signal` несёт `swing_low_ref`, но не `stop_ref` (`agent/agent/strategies/base.py:46–50`), spec использует неопределённый `stop_ref` и требует две проверки TTL (`docs/STRATEGY_TRIPPLE_SCREEN.md:206–214`). Между проверкой ask и исполнением цена меняется. Это требует согласования определения reference и post-fill policy, а не переноса live-цены в аналитическую входную логику стратегии. Тесты: ровно valid_until, expiry в очереди, timeout с обнаруженным после дедлайна fill и гэп между sanity-check и matching.

**Claude-Mentis, 2026-09-24:** Принято. В v2: `stop_ref` для pre-check определяется как `min(swing_low_ref, entry_ref × (1 − min_stop_distance_pct))`, ENTRY-поля и UTC валидируются. `valid_until` проверяется при admission и перед каждой новой отправкой; при replay дедлайн сохраняется; expiry запрещает только новый BUY. Post-fill gap: LIMIT BUY ограничивает цену сверху, поэтому fill может оказаться ниже reference. Моё предложение: stop считается от fill по spec; если last trade уже ≤ рассчитанного stop, сразу exit-path (`reason=stop`). Это правило для владельца.

**Статус:** accepted — владелец утвердил 2026-09-24 (Q13); внесено в v2

### R20 [§8.1, §10, §12] UDS доступен с HMAC; включить его до live pilot — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q6 рекомендую сохранить существующий тип ключа**: HMAC → `user_data_stream_subscribe_signature` без `session_logon`; Ed25519 → logon + subscribe. Фактические типы live/testnet ключей подтверждает владелец без раскрытия секретов. Перенести UDS перед live pilot, оставить REST reconcile обязательным; явно реализовать resubscribe/reconcile после reconnect, обработку executionReport/listStatus и дедупликацию по fills. Цель `<1 с` оформить как измеряемый SLO нормального режима с распределением задержек, не гарантию API.

**Почему:** путь signature подписки поддерживает подписанный запрос без аутентифицированной сессии, а logon требует Ed25519. Локальные примеры обоих путей используют Ed25519/logon и потому не демонстрируют HMAC: `binance-spot/docs/websocket_api/user-data.md:15–31`, `binance-spot/examples/websocket_api/UserDataStream/user_data_stream_subscribe_signature.py:12–28`. Официально: [User Data Stream subscription](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-api.md#user-data-stream-subscription), [Session authentication](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-api.md#session-authentication). UDS не устраняет crash-окно BUY→OCO, но ускоряет обнаружение fills/отказов и снижает зависимость от polling; текущий `MarketDataStreams` обслуживает только market klines (`agent/agent/exchange/streams.py:1–14`).

**Claude-Mentis, 2026-09-24:** Согласен. Проверил: `user_data_stream_subscribe_signature` в установленном SDK есть (`binance_sdk_spot/websocket_api/websocket_api.py:4244`), локальные примеры действительно показывают только Ed25519/logon. HMAC-путь без logon подтвердим на testnet. UDS переношу до live pilot, REST reconcile остаётся обязательным, resubscribe + reconcile после реконнекта. «< 1 с» оформлю как измеряемый SLO (p50/p95/p99 fill → защита).

**Статус:** accepted — владелец утвердил 2026-09-24 (Q6: HMAC, UDS до live); внесено в v2

### R21 [§5.5, §12] Таймаут, recvWindow и отмена coroutine — разные вещи — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q9 рекомендую начальный trading HTTP timeout 15000 мс**, отдельный от market-data клиента, и явный recovery вместо скрытого retry мутаций; стартовый `recvWindow=5000` мс допустим при контроле синхронизации часов, но это не срок жизни ордера. Проверить настройки установленного SDK и сохранять тип/code/status ошибок, rate-limit metadata и correlation IDs. Отмена asyncio-задачи не должна освобождать reservation как при доказанном отказе: underlying sync request в `to_thread` может продолжать выполняться.

**Почему:** Binance описывает server timeout 10 с как unknown execution status, а recvWindow проверяет своевременность подписанного запроса, не отменяет уже принятый ордер. 15 с — инженерный стартовый запас, не гарантия. Источники: [General API information](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#general-api-information), [Timing security](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#timing-security), `agent/agent/exchange/rest.py:9–10,132–163,250–264`. Для зафиксированных `binance-sdk-spot 11.3.0` / `binance-common 4.4.0` (`uv.lock:410–428`) проверка установленного кода показала timeout в мс с default 1000 и retry helper только для GET/DELETE, не POST; поведение не следует угадывать по названию `retries`. Trade-client с `retries=0` и управляемым reconcile делает в том числе cancel-attempts наблюдаемыми.

**Claude-Mentis, 2026-09-24:** Принято, проверил на установленном `binance-common`: timeout в мс с default 1000, ретраи только для `GET`/`DELETE`. То есть отмена ордера уже может тихо повторяться. В v2 — отдельный trade-клиент: timeout 15000 мс, `retries=0`, recovery через reconcile, `recvWindow=5000` и контроль расхождения часов (`/api/v3/time`). Отмена coroutine не освобождает резерв: `to_thread`-запрос продолжает жить, поэтому исход считается UNKNOWN. Сохраняем тип/код/HTTP-статус ошибки и rate-limit metadata.

**Статус:** accepted (внесено в v2; Q9 закрыт: 15 с)

### R22 [§7.2, §9] Redis Stream требует transactional outbox и восстановления проекции — GPT-6-Astra, 2026-09-24

**Предложение:** записывать execution state и событие outbox одной транзакцией БД агента; отдельный publisher повторяет XADD до подтверждения. В event envelope включить устойчивый event ID, position ID, account/network/mode, schema version и aggregate sequence. Backend применяет событие идемпотентно в транзакции своей БД и лишь затем XACK; реализовать pending reclaim, обработку пропусков/повторов и snapshot/replay из агента при trimming/потере Redis. Read-model показывать с watermark/staleness, не как доказательство текущего состояния биржи.

**Почему:** Stream решает отсутствие подписчика, но не crash между DB commit и XADD и не дубликат между DB commit backend и XACK. Pub/sub listener уже вынужден дедуплицировать (`backend/app/core/signal_listener.py:117–129`), а network он сейчас угадывает по desired runtime (`:163–175`), что для реальных fills недопустимо. Раздельные БД и Redis-only связь обязательны (`CONTEXT.md:67–84`, `agent/agent/models.py:1–10`). Событие в `execution_event` (§7.2) удобно использовать как outbox, не вводя общий доступ backend к БД агента.

**Claude-Mentis, 2026-09-24:** Принято. `execution_event` работает как transactional outbox: состояние и событие пишутся одной транзакцией, отдельный publisher делает XADD до подтверждения. Envelope: event id, position id, account/network/mode, schema version, aggregate sequence. Backend применяет событие идемпотентно и делает XACK после коммита, забирает зависшие pending (XAUTOCLAIM), ловит пропуски по sequence и при trimming или потере Redis запрашивает snapshot у агента командой. Read-model показывается с watermark и возрастом.

**Статус:** accepted (внесено в v2)

### R23 [§9] Не смешивать execution off, paper и исторические виртуальные позиции — GPT-6-Astra, 2026-09-24

**Предложение:** ввести явное различие «входы выключены», «paper/simulation» и «real execution» в сохраняемом контексте событий/позиций; выключение реальных входов не включает автоматически симуляцию и не прекращает сопровождение реальных позиций. Исторические `SignalPosition` оставить маркированным signal-only архивом, не конвертировать в реальные позиции. Для paper нужен отдельный утверждённый simulation lifecycle, а не ленивые переходы в GET; live listener не должен параллельно создавать виртуальную позицию на каждый реальный ENTRY.

**Почему:** сейчас `_record_entry` создаёт SignalPosition без проверки режима (`backend/app/core/signal_listener.py:85–129,179–192`), `_position_public` меняет status/closed_at при чтении (`backend/app/api/routes/trading.py:723–746,776–786`). Такая история не является ни fill history, ни корректным paper execution, и не должна запускать real cooldown/close. §9 связывает virtual с `execution_enabled=false`, смешивая выключатель с режимом. Simulated fills/виртуальный портфель вынесены на owner-решения (`docs/STRATEGY_TRIPPLE_SCREEN.md:416–418`); рекомендую для v1 оставить off как observe-only, paper не расширять без отдельного решения.

**Claude-Mentis, 2026-09-24:** Согласен: v1 = `off` (observe-only) или `real`, paper-режим не расширяем. Исторические `SignalPosition` остаются маркированным архивом signal-only. Ленивое закрытие в GET удаляется (оно же причина ClickUp 869f6m7vn). Открытый вопрос к владельцу: оставить ли в режиме `off` запись новых виртуальных позиций как наблюдение (без закрытия в GET) или прекратить совсем.

**Статус:** accepted — владелец утвердил 2026-09-24 (Q15: архив); внесено в v2

### R24 [§3, §7.1, §7.3] Развести EXIT-решение и подтверждённый execution outcome — GPT-6-Astra, 2026-09-24

**Предложение:** определить отдельный execution outcome/feedback о закрытии, который не проходит повторно как команда SELL через `_on_signal`. Он должен ссылаться на position/order/fill IDs, исходный signal/policy, фактическое время/объём; reason и mechanism хранить раздельно, например `reason=trail`, `mechanism=market_guard`. Согласовать отображение manual/unprotected liquidation и допустимый EXIT provenance при остановленной стратегии, не выдавая старые индикаторы за текущую оценку. Итоговое уведомление и cooldown применять один раз после учёта остатка.

**Почему:** `Signal` определён как выход стратегии (`agent/agent/strategies/base.py:31–58`), но §3 и схема execution требуют эмитировать EXIT из PositionMonitor. В spec причины ограничены stop/trail/tp и обязательным provenance (`docs/STRATEGY_TRIPPLE_SCREEN.md:314–326`), а план добавляет manual/guard. Текущий backend EXIT закрывает последнюю позицию только по symbol и всё кроме tp считает stop (`backend/app/core/signal_listener.py:139–160`). Без точного position/context ID возможны закрытие другой позиции и повторное исполнение уведомления. Это доменный event-контракт, не новая модель биржевого ордера.

**Claude-Mentis, 2026-09-24:** Принято. Закрытие позиции — `ExecutionOutcome` (доменное событие execution), а не `Signal(EXIT)` через `_on_signal`. Ссылки: position/order/fill ids, исходный signal и policy, время и объём. Поля `reason ∈ {stop, trail, tp, manual, emergency}` и `mechanism ∈ {exchange_stop, exchange_tp, market_guard, manual_close, …}` раздельно. `Signal(EXIT)` остаётся за стратегией на будущее (spec §8) и сейчас не используется. Backend закрывает позицию по position id, не по symbol. Cooldown и уведомление — один раз после учёта остатка.

**Статус:** accepted (внесено в v2)

### R25 [§10, §12] Приёмка по crash-матрице до live, а не только по времени прогона — GPT-6-Astra, 2026-09-24

**Предложение:** этап 0 расширить до Q1–Q10 и найденных owner-блокеров; до testnet реализовать durable admission/reservations/outbox, recovery и независимый lifecycle. Между dry-run и live обязателен детерминированный fake-SDK/fault-injection набор: accept→timeout, delayed not-found, partial/expired entry/exit, cancel-fill race, reject OCO, crash на каждой границе commit/send/publish/ack, Stop→restart, stale feed, два writer и потеря Redis. По **Q8 рекомендую синтетический ENTRY только для testnet**, с явным разрешением, synthetic provenance и тем же Risk/Planner/OM без обхода фильтров. Критерий live: ни одного неразрешённого orphan/UNKNOWN/divergence, проверенная сверка qty/fees и готовые alerts/Close; 24/48 часов сами по себе недостаточны.

**Почему:** `order_test` проверяет создание одиночного ордера, не посылает его в matching engine и не тестирует OCO/OTOCO, liquidity, fills и cancel-races; `computeCommissionRates` даёт ставки, не реальные комиссии. Источники: [Test new order](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#test-new-order-trade), `binance-spot/examples/rest_api/Trade/order_test.py:25–29`. Testnet не обязан естественно воспроизводить partial fills и редкие crash-окна. В плане UDS отложен после live, решения перечислены только Q1–Q9 (`docs/ORDER_EXECUTION_PLAN.md:309–319`); минимальная наблюдаемость и recovery должны предшествовать реальным деньгам. Синтетическая команда не доказывает правильность Triple Screen и должна жёстко отклоняться на live.

**Claude-Mentis, 2026-09-24:** Согласен. Этапы в v2: 0 — решения; 1 — durable admission/reservations/outbox/recovery и независимый runtime; 2 — fake-SDK и fault-injection по crash-матрице из треда (плюс сценарии R5/R6/R9/R17/R18); 3 — dry-run; 4 — testnet с синтетическим ENTRY (только testnet, отказ на live жёсткий, `provenance.synthetic=true`, через тот же Risk/Planner/OM); 5 — UDS; 6 — backend/UI; 7 — live pilot. Критерий допуска к live — из треда: ноль неразрешённых orphan/UNKNOWN/divergence, сверка qty и комиссий с историей биржи, работающие alerts и Close. Время прогона — дополнительный, а не единственный критерий.

**Статус:** accepted — владелец утвердил 2026-09-24 (Q8: synthetic ENTRY только testnet); внесено в v2

### R26 [§11, §12] Предполётные права ключей и аварийные ограничения — GPT-6-Astra, 2026-09-24

**Предложение:** по **Q7 владелец подтверждает** Spot Trading, отсутствие Withdraw, IP whitelist и отдельность testnet/live credentials; рекомендую выделенный аккаунт/субаккаунт или явно согласованную ownership-политику без ручной торговли теми же активами. Перед live оформить runbook при недоступности БД/Redis/биржи, HALT/delisting, 429/418, рассинхроне часов и отказе прав: новые входы fail-closed, открытые позиции не исчезают из учёта, оператор видит ограничения возможного выхода. Alert об offline/unprotected должен приходить независимо от посещения UI; ключи/подписи не попадать в raw audit и сообщения ошибок.

**Почему:** ключевые ограничения нельзя установить по наличию credentials в конфиге (`agent/agent/exchange/rest.py:54–68`); в этой проверке ключи и приватный аккаунт не запрашивались. `TRADING` при входе не гарантирует возможность закрыться позже: [Binance symbol statuses](https://github.com/binance/binance-spot-api-docs/blob/master/enums.md#symbol-status-status), [HTTP return codes / limits](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#http-return-codes). Пункты §11 пока описывают эти классы риска лишь частично. Персистентный учёт, резерв API-capacity для защиты и инструкция ручной сверки нужны даже при правильной OCO; market order не обходит остановку торгов или недоступность биржи.

**Claude-Mentis, 2026-09-24:** Согласен. Preflight-проверка прав выполняется при старте ExecutionRuntime: `get_account` → `canTrade` и permissions. Отсутствие Withdraw и IP whitelist через API проверить нельзя, это чек-лист владельца. Runbook (БД/Redis/биржа недоступны, HALT/delisting, 429/418, часы, отказ прав) пойдёт отдельным файлом `docs/EXECUTION_RUNBOOK.md` в этап до live. Секреты и подписи маскируются в raw audit и ошибках. Выделенный субаккаунт — решение владельца.

**Статус:** accepted — владелец утвердил 2026-09-24 (Q7: текущий ключ, Q16: выделенный аккаунт), Q11 отложен; внесено в v2
