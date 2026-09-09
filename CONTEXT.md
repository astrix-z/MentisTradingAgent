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

- Приложение: **http://127.0.0.1:8000/** (НЕ `:80` — порт 80 занят системным httpd AlmaLinux,
  Traefik/proxy там не поднимается; это норма).
- Поднять: `docker compose up -d --build db redis backend agent`;
  миграции+суперюзер один раз: `docker compose run --rm backend bash scripts/prestart.sh`.
- Креды для входа/Баланс-ключи — в `.env` (не хардкодить в этом файле).
  См. `FIRST_SUPERUSER`/`FIRST_SUPERUSER_PASSWORD`, `BINANCE_*`, `BINANCE_TESTNET_*`.

## 7. Быстрые проверки (среда)

- backend/agent: `cd backend && uv run ruff check app && uv run mypy app`; аналогично `agent`.
- frontend: `bun node_modules/@biomejs/biome/bin/biome check <file>`;
  `bun node_modules/typescript/bin/tsc -p tsconfig.build.json`;
  сборка = vite (см. п.5).

---

## ЖУРНАЛ (последняя задача сверху)

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
- Показать, как добавить новую команду насквозь (пример: `sell_all`, `switch_strategy`).
- Реальная стратегия торговли по `strategy` (реестр стратегий) в `Engine._trading_loop`.
- Порт `80` хоста занят httpd — Traefik/proxy не поднимается (решение: освободить 80
  или перевесить proxy.ports); для просмотра использовать `:8000`.
