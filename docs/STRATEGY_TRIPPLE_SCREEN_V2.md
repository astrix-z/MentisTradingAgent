# Стратегия «Три Экрана v2» (Triple Screen v2) — Strategy Spec

- **Реестр:** `tripple_screen_v2`, в UI — «Triple Screen v2»
- **Код:** `agent/agent/strategies/tripple_screen_v2/`
- **Основа:** [Triple Screen v1](STRATEGY_TRIPPLE_SCREEN.md). Ниже описаны только
  отличия; всё, что здесь не упомянуто, совпадает с v1 (§1.2 pipeline,
  §3.1 Tide, §4.1–4.2 эпохи и валидность, §5 закрытые свечи и cutoff,
  §6 cooldown, §7 выходы, §8 сигнальный контракт).
- **Продукт:** Binance Spot, **long-only**. v1 продолжает работать без изменений.

## 1. Первоначальный отбор и экран 1 (Tide, H4) — как в v1

Это stage-1 screen (EMA13 и EMA34 растут, ранжирование по спреду, cap
`ws_top_n`) и Tide-выстраивание EMA 5/13/34 с ценой из v1 §3.1.

Добавлено одно правило: символ без таблицы `prcntl_<symbol>_<ripple_interval>`
в Binance DB исключается до tide-проверки. Его REST-окно не загружается.
В Indicators Monitor видна причина:
`no 15m percentile table (prcntl_<symbol>_15m)`.

## 2. Экран 2 — Wave (H1): выстраивание EMA

На последнем закрытом wave-баре (cutoff как в v1 §5 — бар, закрытый к
**открытию** триггерной ряби):

```
EMA5 > EMA13 > EMA34          (ema_wave_periods, default [5, 13, 34])
```

Stochastic и Force Index из v1 §3.2–3.3 не используются. Их параметров в
настройках v2 нет.

## 3. Экран 3 — Ripple (M15): изменение EMA выше персентиля

На триггерном (последнем закрытом) ripple-баре для каждого периода из
`ema_ripple_periods` (default `[5, 13]`):

```
ΔEMA% = (EMA[t] − EMA[t−1]) / EMA[t−1] × 100
ΔEMA5%  > P95(ma_5_delta)
ΔEMA13% > P95(ma_13_delta)
```

- `P95` берётся из строки `index = '95%'` таблицы
  `prcntl_<symbol>_<ripple_interval>` (имя в нижнем регистре, например
  `prcntl_btcusdt_15m`). Строка задаётся параметром `ripple_percentile`
  (`"50"` … `"95"`, default `"95"`).
- Формула ΔEMA% та же, по которой Binance DB считает `ma_N_delta` в
  `klines_<symbol>_<interval>_ema`, то есть сравниваются величины одной
  природы. EMA агент считает сам по своему окну закрытых свечей (SMA-seed, как
  в v1). Значение не округляется.
- Сравнение строгое: равенство порогу условие не проходит.
- Пробой v1 §3.4 (close выше high предыдущего бара) **не используется**.
- Допустимые периоды ограничены колонками таблиц: 5, 8, 13, 21, 34, 55, 89, 144.

Пороги читаются для отобранных символов при каждом stage-1 screen: на старте
и на каждом закрытии tide-бара. Если чтение не удалось, это ошибка цикла:
на старте старт прерывается, при rescreen предыдущие пороги остаются, а
ошибка видна в `health_warning`.

## 4. Решение

ENTRY выдаётся, когда Tide bullish, Wave выстроена и Ripple проходит, а
блокировки v1 не действуют. Если Tide не bullish, сигнала нет (как в v1).
Иначе выдаётся HOLD с первой сработавшей причиной:

| reason              | условие                                               |
|---------------------|-------------------------------------------------------|
| `no_wave_alignment` | нет `EMA fast > EMA mid > EMA slow` на wave           |
| `no_percentiles`    | у символа нет порогов (нет таблицы или строки)        |
| `no_momentum`       | хотя бы одно ΔEMA% не выше своего персентиля          |
| `position_open`, `cooldown`, `epoch_consumed` | как в v1               |

`entry_ref` равен close триггерного бара. Stop, TP, трейлинг и выход по
времени считаются как в v1: swing low по `swing_low_wave_n` wave-барам.

В provenance сигнала `wave` содержит `ema`, `conditions` и `ema_aligned`.
`ripple` содержит `ema.<N>.{previous, current, delta_pct, threshold_pct}`,
`percentile`, `conditions.ema_<N>_above_p<P>`, `thresholds_loaded` и
`momentum`.

## 5. Данные и конфигурация

- Binance DB подключается через `BINANCE_DB_URL` (read-only роль, уже
  прокинута в `compose.yml`). Без неё стратегия не стартует и сообщает
  `BINANCE_DB_URL is not configured`.
- Новые параметры: `ema_wave_periods`, `ema_ripple_periods`,
  `ripple_percentile`. Удалены `stochastic_*`, `fi_ema_period` и
  `oversold_threshold`.
- `kline_window ≥ max(tide slow + 1, wave slow, ripple mid + 1,
  swing_low_wave_n, trail_ripple_n, 2)`.
- Встроенный пресет: `Default`.
