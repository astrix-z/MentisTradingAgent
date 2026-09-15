# Промпты для агента: управление стратегией и баланс

Этот документ разбивает целевую доработку на независимые, последовательные задачи. Каждый раздел можно передавать coding-агенту как отдельный промпт.

## Контекст проекта

- Frontend: React + TanStack Router + TanStack Query, каталог `frontend/`.
- Backend: FastAPI, каталог `backend/`.
- Agent: отдельный процесс, управляющий стратегией через Redis, каталог `agent/`.
- Команды выполняются асинхронно:
  - API принимает команду и возвращает `202 CommandEnvelope`;
  - agent пишет итог в Redis с тем же `command_id`;
  - UI получает результат через polling `GET /api/v1/trading/result/?command_id=...`.
- `desired` в `/api/v1/trading/status/` — намерение backend, а `actual` — heartbeat агента. Принятие команды не является подтверждением её выполнения.
- Доступ к торговому управлению должен оставаться только у superuser.

## Целевая модель маршрутов

```text
/strategy  — настройки стратегии, Start и Stop, фактический статус
/balance   — выбор сети, запрос баланса, результат и свежесть данных
/trading   — legacy redirect на /balance
```

---

# Prompt 1 — выделить отдельную страницу баланса

## Задача

Создай отдельную защищённую страницу `/balance` для ручного запроса и просмотра баланса. Перенеси в неё весь balance-flow, который сейчас находится на `/trading`. Маршрут `/strategy` не изменяй функционально: он остаётся единственной страницей настройки и запуска/остановки стратегии.

## До начала изменений

1. Прочитай:
   - `frontend/src/routes/_layout/trading.tsx`;
   - `frontend/src/routes/_layout/strategy.tsx`;
   - `frontend/src/hooks/useCommandTracking.ts`;
   - `frontend/src/components/Sidebar/AppSidebar.tsx`;
   - `frontend/tests/trading-contract.spec.ts`.
2. Проверь, что backend API уже предоставляет `POST /api/v1/trading/balance/` и `GET /api/v1/trading/result/`. Не добавляй backend endpoint, если текущего контракта достаточно.
3. Не редактируй `frontend/src/routeTree.gen.ts` вручную: он генерируется TanStack Router.

## Требования реализации

1. Создай `frontend/src/routes/_layout/balance.tsx`.
2. Страница должна:
   - быть доступна только superuser, по тому же принципу, что `/strategy`;
   - иметь заголовок `Balance` и корректный document title;
   - позволять явно выбрать `testnet` или `live`;
   - не позволять отправить запрос до выбора сети;
   - вызывать `TradingService.fetchBalance({ body: { network } })`;
   - показывать состояние принятой команды и её итог через существующий механизм `command_id`;
   - показывать актуальный agent status, но не содержать Start/Stop и форму параметров стратегии;
   - объяснять, что баланс heartbeat является наблюдаемым кэшем, а результат команды — ответом на конкретный ручной запрос.
3. Добавь в superuser sidebar пункт `Balance` с маршрутом `/balance`.
4. Преврати `/trading` в redirect на `/balance`, не дублируя экран и не ломая старые ссылки.
5. Адаптируй Playwright-контракты:
   - `/balance` отображается и требует superuser;
   - sidebar ведёт на `/balance`;
   - `/trading` перенаправляет на `/balance`;
   - balance-запрос отправляет именно выбранную сеть;
   - на `/balance` нет действий Start/Stop.

## Ограничения

- Не меняй Redis-протокол и backend API в этой задаче.
- Не отправляй balance без явного выбранного `network` на frontend.
- Не заменяй correlated polling по `command_id` чтением общего `agent:result`.
- Не удаляй существующие тесты, а актуализируй их.

## Условия готовности

- [ ] Маршрут `/balance` существует и защищён от не-superuser.
- [ ] В sidebar есть `Balance → /balance` только для superuser.
- [ ] `/trading` перенаправляет на `/balance`.
- [ ] На `/balance` можно выбрать `TESTNET` или `LIVE` и запросить баланс.
- [ ] Кнопка запроса неактивна без выбранной сети или во время transport-запроса.
- [ ] Страница показывает `accepted`, `pending`, `succeeded`, `failed` и `timeout`, не выдавая принятие команды за успех.
- [ ] На странице нет Start/Stop и настроек стратегии.
- [ ] Обновлён `routeTree.gen.ts` только через официальный генератор маршрутов.
- [ ] Production frontend build проходит.
- [ ] Соответствующие Playwright-тесты проходят.

## Формат финального отчёта агента

Укажи изменённые файлы, сценарии тестов, команды валидации и их фактический результат. Отдельно перечисли, если тесты нельзя было запустить, с точной причиной.

---

# Prompt 2 — сохранить lifecycle команд при навигации

## Задача

Сделай tracking асинхронных торговых команд общим для `/strategy` и `/balance`, чтобы уход со страницы не прекращал polling и не скрывал результат уже отправленной команды.

## До начала изменений

Прочитай:

- `frontend/src/hooks/useCommandTracking.ts`;
- `frontend/src/components/CommandTrackingView.tsx`;
- `frontend/src/routes/_layout.tsx`;
- `frontend/src/routes/_layout/strategy.tsx`;
- `frontend/src/routes/_layout/balance.tsx`;
- клиентские типы `CommandEnvelope` и `CommandResult`.

## Требования реализации

1. Вынеси состояние активных команд и polling в общий provider/store, живущий выше level route pages.
2. Предоставь страницам минимальный API, эквивалентный текущему:
   - зарегистрировать `CommandEnvelope`;
   - прочесть список команд и их фаз;
   - отобразить их через `CommandTrackingView`.
3. При переходе `/strategy → /balance` и `/balance → /strategy` polling уже отправленных команд должен продолжаться.
4. По успешному или неуспешному завершению команды необходимо инвалидировать `['trading-status']`.
5. Не менять semantics timeout:
   - отсутствие результата не считать ошибкой выполнения;
   - timeout не считать успехом;
   - временную ошибку polling отображать как retrying.
6. Не допускать дублирующих polling-циклов для одного `command_id`.
7. При размонтировании всего приложения abort активные HTTP-запросы корректно.

## Ограничения

- Не храни команды в `localStorage` без отдельного требования: после полной перезагрузки страницы допускается потеря локального UI-history.
- Не заменяй `command_id` общим последним результатом.
- Не изменяй backend contract в этой задаче.

## Условия готовности

- [ ] Команда, начатая на `/strategy`, продолжает отслеживаться после перехода на `/balance`.
- [ ] Команда balance, начатая на `/balance`, продолжает отслеживаться после перехода на `/strategy`.
- [ ] Один `command_id` имеет не более одного активного polling-цикла.
- [ ] Итог команды отображается корректно как success, failed или timeout.
- [ ] После terminal result обновляется query `['trading-status']`.
- [ ] Добавлены unit/component или Playwright-тесты перехода между страницами во время pending-команды.
- [ ] Existing trading contract tests не регрессируют.

## Формат финального отчёта агента

Опиши размещение provider/store, жизненный цикл polling, защиту от двойного polling и результаты тестов.

---

# Prompt 3 — безопасный Start: состояние UI и строгая frontend-валидация

## Задача

Доработай `/strategy` до безопасного UX для Start/Stop. Настройки стратегии должны по-прежнему отправляться только с командой Start. Добавь понятные состояния управления, строгую frontend-валидацию и обязательное подтверждение запуска на `live`.

## До начала изменений

Прочитай:

- `frontend/src/routes/_layout/strategy.tsx`;
- `frontend/src/components/TradingStatusView.tsx`;
- `backend/app/api/routes/trading.py`;
- `agent/agent/strategies/tripple_screen/settings.py`;
- `agent/agent/common/commands.py`.

Сопоставь правила формы с реальной валидацией `TripleScreenSettings`. Не дублируй правила по памяти.

## Требования реализации

1. Оформи состояния control panel:
   - status загружается;
   - agent offline/stale;
   - desired/actual running;
   - ожидается подтверждение Start;
   - ожидается подтверждение Stop;
   - actual agent error;
   - command timeout.
2. Кнопки должны быть семантически корректны:
   - не позволять неосознанно повторять Start, если стратегия уже подтверждённо running;
   - не показывать Stop как нормальное доступное действие, если стратегия подтверждённо остановлена;
   - не выдавать `202 Accepted` за факт запуска/остановки.
3. Для `live` Start покажи confirmation dialog. Перед подтверждением отобрази:
   - network;
   - strategy;
   - symbols;
   - ключевые параметры или их компактный summary.
4. Усиль frontend validation:
   - все числовые значения должны быть конечными (`Number.isFinite`);
   - list чисел должен отклоняться целиком, если содержит нечисловой элемент;
   - проверь обязательные поля;
   - перенеси доменные ограничения из `TripleScreenSettings`, которые можно надежно проверить в UI;
   - не отправляй запрос при локальной validation error;
   - показывай ошибку у конкретного поля или в доступной summary ошибок.
5. Сохрани принцип:
   - Start передаёт `{ network, strategy, symbols, params }`;
   - Stop не передаёт настройки;
   - Stop не затирает последние параметры в UI.

## Ограничения

- Не отправляй params отдельной командой «Save»: текущий продуктовый контракт требует применить settings именно при Start.
- Не смягчай agent validation.
- Не меняй strategy parameters молча; неизвестные или неправильные значения должны приводить к ошибке.

## Условия готовности

- [ ] Start отправляет полную конфигурацию только после успешной валидации.
- [ ] Stop отправляется без strategy settings.
- [ ] При `live` Start невозможно отправить команду без явного подтверждения.
- [ ] Невалидные numeric/list/domain значения не уходят в API.
- [ ] UI показывает, когда агент offline, команда pending, команда failed или timeout.
- [ ] `desired` и `actual` остаются визуально различимыми.
- [ ] Добавлены тесты: invalid values, live confirmation, payload Start, пустой payload Stop и mismatch desired/actual.
- [ ] Production frontend build и относящиеся к экрану тесты проходят.

## Формат финального отчёта агента

Покажи итоговый Start payload, список реализованных validation rules и результаты тестов.

---

# Prompt 4 — строгий backend-контракт Start и Balance

## Задача

Сделай FastAPI-контракты запуска стратегии и запроса баланса строгими и однозначными. Цель — отклонять неверные команды до Redis publication и сделать результат баланса пригодным для финансового UI.

## До начала изменений

Прочитай:

- `backend/app/api/routes/trading.py`;
- `agent/agent/common/commands.py`;
- `agent/agent/exchange/rest.py`;
- `agent/agent/strategies/tripple_screen/settings.py`;
- текущие backend и agent тесты торгового протокола.

Проверь, какие типы реально возвращает Binance adapter и где теряется `locked` balance.

## Требования реализации

1. Для Start:
   - валидируй допустимую strategy;
   - требуй непустой список symbols;
   - нормализуй symbols предсказуемо и отклоняй дубли/некорректный формат согласно выбранной policy;
   - сделай параметры выбранной стратегии строгими;
   - неизвестные параметры должны вернуть `422`, а не игнорироваться;
   - применяй те же domain rules, что агент.
2. Для Balance:
   - сделай `network` обязательным;
   - запрети неявный выбор network из текущего состояния agent;
   - введи typed response/snapshot:

     ```json
     {
       "network": "testnet",
       "fetched_at": "ISO-8601 datetime",
       "assets": [
         {"asset": "USDT", "free": "12.34", "locked": "0.00"}
       ]
     }
     ```

   - денежные величины сериализуй строками Decimal, не `float`;
   - определи и задокументируй policy zero balances.
3. Обнови OpenAPI-контракт и сгенерированный frontend client только официальным generator workflow.
4. Добавь backend и agent тесты для validation и результата balance.

## Ограничения

- Не ломай correlation по `command_id`.
- Не раскрывай секреты API в response или логах.
- Не меняй существующие public endpoint names без миграционного слоя.
- Если shared Pydantic schema между backend и agent создает нежелательную зависимость, используй одинаково покрытые контрактные схемы, а не копирование без тестов.

## Условия готовности

- [ ] Невалидный Start возвращает `422` до записи runtime snapshot и до публикации в Redis.
- [ ] Unknown strategy и unknown strategy parameter отклоняются.
- [ ] Невалидные symbols отклоняются по документированной policy.
- [ ] Balance без `network` возвращает validation error.
- [ ] Успешный balance result содержит network, fetched_at, asset, free и locked.
- [ ] Финансовые суммы в API не используют binary float.
- [ ] OpenAPI и frontend generated client соответствуют новому контракту.
- [ ] Добавлены тесты на validation, precision, zero-balance policy и correlation result.
- [ ] Backend и agent targeted test suites проходят.

## Формат финального отчёта агента

Опиши breaking/non-breaking изменения API, migration notes для frontend и результаты контрактных тестов.

---

# Prompt 5 — надёжная доставка и подтверждённое состояние команд

## Задача

Устрани риск потери и повторного выполнения команд Start/Stop/Balance. Redis Pub/Sub не должен оставаться единственным delivery-механизмом для пользовательских команд. Раздели requested и confirmed состояние стратегии.

## До начала изменений

Прочитай:

- `backend/app/api/routes/trading.py`;
- `backend/app/core/redis_client.py`;
- `agent/agent/main.py`;
- `agent/agent/engine.py`;
- `agent/agent/common/commands.py`;
- все тесты control-plane.

Сначала составь краткий design note: выбранный механизм delivery, lifecycle команды, idempotency, retry и retention. Только затем реализуй изменения.

## Требования реализации

1. Замени at-most-once Pub/Sub delivery для command lane на durable механизм, например Redis Streams или очередь с durable outbox/acknowledgement.
2. Храни command state по `command_id`:
   - `accepted`;
   - `dispatched`;
   - `running`;
   - `succeeded`;
   - `failed`;
   - `timed_out` или эквивалентное финальное состояние.
3. Agent должен дедуплицировать command ID:
   - повторная доставка не должна второй раз запускать стратегию;
   - повторная доставка должна вернуть/сохранить прежний result.
4. Stop является safety action:
   - должен быть идемпотентным;
   - не должен зависнуть за обычной долгой balance-командой;
   - должен иметь определённую policy при конкуренции с Start.
5. Раздели:
   - requested intent пользователя;
   - confirmed actual state агента.
6. Если Start завершился failed/cancelled:
   - не оставляй confirmed state `running`;
   - не допускай automatic resume именно неуспешной команды;
   - не затирай более новое intent при обработке старого failure.
7. Введи server-side deadline для network I/O и понятный timeout result.
8. Добавь структурированное логирование с `command_id`, type, network, elapsed time и причиной failure, без секретов.

## Ограничения

- Сохрани безопасность: отсутствие confirmation нельзя считать failed или succeeded без явной policy.
- Не допускай повторного real trade из-за retry.
- Не удаляй обратную совместимость API без migration path.
- Не делай Redis единственным долговременным audit storage, если в проекте уже есть подходящий Postgres/Alembic path; обоснуй выбранную persistence strategy.

## Условия готовности

- [ ] Команда, опубликованная до перезапуска/reconnect agent, в итоге доставляется или имеет наблюдаемое failure state.
- [ ] Повторная доставка одного `command_id` не запускает стратегию повторно.
- [ ] Stop может быть безопасно повторён и имеет приоритетную/документированную semantics.
- [ ] Зависший balance не блокирует Stop бесконечно.
- [ ] Failed Start не остаётся подтверждённо running и не auto-resume без нового действия пользователя.
- [ ] UI может отличить accepted request от confirmed execution.
- [ ] Для command history определены TTL/retention и cleanup.
- [ ] Есть integration tests с настоящим Redis для restart/reconnect, duplicate delivery, timeout и concurrent Start/Stop.
- [ ] Existing API contract tests проходят или имеют документированную миграцию.

## Формат финального отчёта агента

Предоставь design note, diagram command lifecycle, список изменённых Redis keys/streams, migration steps, тесты и известные ограничения.

---

# Общий acceptance checklist

Работа считается законченной только после выполнения всех применимых пунктов:

- [ ] `/strategy` — единственный экран настройки и Start/Stop.
- [ ] Settings стратегии уходят только с командой Start.
- [ ] Stop не отправляет и не удаляет settings.
- [ ] `/balance` — отдельный superuser-only экран.
- [ ] Выбор сети balance независим от сети текущей стратегии.
- [ ] UI различает acceptance команды, выполнение agent и stale heartbeat.
- [ ] Результат balance имеет сеть, время и точные финансовые значения.
- [ ] Командный lifecycle не теряется при переходе между страницами.
- [ ] Ошибки валидации и ошибки agent отображаются как ошибки, а не как пустые/успешные данные.
- [ ] Тесты покрывают success, failed, timeout, offline и переходы между страницами.
- [ ] Команды безопасны при повторной доставке и перезапуске agent.
