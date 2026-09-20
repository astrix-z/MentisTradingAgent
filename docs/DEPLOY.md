# Эксплуатация live/testnet-стенда

> Как устроен стенд на выделенном хосте, как выкладывать обновления, как
> восстанавливаться после сбоя. Стенд закрыт от интернета: доступ только по
> SSH-туннелю. Разработка ведётся на машине разработчика — на сервере крутится
> только длительный прогон (`docs/TRIPPLE_SCREEN_WORKING PROCESS.md`).

---

## 1. Архитектура

```
машина разработки                        сервер 192.168.55.40 «docker-host» (jb, группа docker)
┌──────────────────────────┐    ssh     ┌────────────────────────────────────────────────┐
│  git (репозиторий)       │ ─────────► │  стек docker compose «mentis»:                 │
│  ./scripts/deploy.sh     │            │    db       postgres:18 (volume, без порта)     │
│  .env  (секреты dev)     │            │    redis    redis:7 (без порта)                 │
└──────────────────────────┘            │    backend  FastAPI+SPA :8000 → 127.0.0.1:8010  │
                                        │    agent    без портов, торгует через Binance API│
                                        │  ~/apps/mentis-trading-agent   код (.env.prod)   │
                                        │  ~/backups/mentis-trading-agent/  дампы БД       │
                                        │  рядом (чужое, не трогать): plan-oplat, n8n,     │
                                        │  nginx-proxy-manager (80/81/443), postgres-shared │
                                        │  (5432) — порты заняты, поэтому наш db/redis без  │
                                        │  портов, backend — на непересекающемся 8010       │
                                        └────────────────────────────────────────────────┘
```

- **Публикации наружу нет.** `backend` слушает только на
  `${WEB_BIND:-127.0.0.1}:${WEB_PORT:-8010}` хоста (см. `compose.prod.yml`).
  Это live-trading с реальными деньгами — сознательное решение не выставлять
  его в интернет и не заводить домен/Nginx Proxy Manager, в отличие от
  `plan-oplat` на этом же хосте. Доступ:
  ```sh
  ssh -L 8010:localhost:8010 jb@192.168.55.40
  # затем открыть http://localhost:8010 в браузере на машине разработки
  ```
- **`agent` без единого опубликованного порта** — так и в базовом
  `compose.yml`: UI обязан доставать его только через `backend`.
- **БД и Redis не публикуются.** Порт 5432 на хосте и так занят чужим
  `postgres-shared` — конфликта нет по конструкции, а не по счастливой
  случайности.
- **Обе БД агента создаются сами при старте**: `backend` запускает
  `prestart.sh` (Alembic + initial data) для базы `app`; `agent` создаёт свою
  базу `agent` сам при первом запуске (`ensure_settings_database`,
  идемпотентно). Отдельного шага инициализации не нужно.
- **`proxy` (Traefik) и `adminer`** остаются в профиле `extras` из
  `compose.yml` и не поднимаются обычным деплоем. `adminer`, если понадобится,
  — тоже только на loopback (`127.0.0.1:${ADMINER_PORT:-8011}`).

## 2. Регулярный деплой (основной сценарий)

```sh
git add ... && git commit -m "..."     # деплоится только закоммиченное
./scripts/deploy.sh
```

Скрипт (по умолчанию `DEPLOY_SERVER=192.168.55.40`, `DEPLOY_USER=jb`):

1. Снимает дампы `app` и `agent` в
   `~/backups/mentis-trading-agent/pre-deploy-<база>-<ревизия>-<дата>.dump`
   (хранятся последние 10 на базу; `--no-backup` — пропустить, не рекомендуется).
2. Копирует код `HEAD` на сервер в `~/apps/mentis-trading-agent`
   (`git archive`, `.env.prod` не перезаписывается — его там вообще нет в
   архиве, он не в git).
3. Собирает образы и пересоздаёт контейнеры; backend/agent сами прогоняют
   миграции/инициализацию при старте.
4. Ждёт healthcheck `${WEB_BIND}:${WEB_PORT}/api/v1/utils/health-check/` до
   2 минут.

Флаги: `--allow-dirty`, `--no-build`, `--no-backup`.
Если пользователь на сервере не `jb`: `DEPLOY_USER=<имя> ./scripts/deploy.sh`.

**Деплой не запускает торговлю.** Поднять контейнеры и стартовать стратегию —
разные действия: `agent` после старта сидит без сделок, пока кто-то явно не
нажмёт «start» (network testnet/live) через UI/API — так же, как локально.
Задеплоить новый код на сервер можно в любой момент, не опасаясь случайно
включить live-торговлю.

## 3. Первоначальная настройка сервера (один раз)

```sh
ssh jb@192.168.55.40 'mkdir -p ~/apps/mentis-trading-agent'
scp .env.prod.example jb@192.168.55.40:~/apps/mentis-trading-agent/.env.prod
ssh jb@192.168.55.40 'chmod 600 ~/apps/mentis-trading-agent/.env.prod'
# затем на сервере: заполнить реальные значения в .env.prod (см. комментарии в файле)
./scripts/deploy.sh
```

`.env.prod.example` в git документирует все нужные переменные (порт/бинд,
секреты, Binance-ключи). Реальный `.env.prod` на сервере в git не попадает
(`.gitignore`) и `deploy.sh` его не трогает.

## 4. Наблюдение и отладка

На сервере (или через ssh):

```sh
cd ~/apps/mentis-trading-agent
docker compose --env-file .env.prod -f compose.yml -f compose.prod.yml ps
docker compose --env-file .env.prod -f compose.yml -f compose.prod.yml logs -f agent
docker exec -it mentis-db-1 psql -U postgres -d app     # или -d agent
docker compose --env-file .env.prod -f compose.yml -f compose.prod.yml exec -T agent \
  cat /app/agent/logs/signals.log
```

Здоровье изнутри сервера: `curl http://127.0.0.1:8010/api/v1/utils/health-check/`.
С машины разработки — через SSH-туннель (см. §1).

## 5. Бэкапы и восстановление

- **Перед каждым деплоем**: см. §2. Ручного планировщика (systemd-таймера) для
  ежедневных бэкапов пока нет — при необходимости завести по образцу
  `plan-oplat/scripts/server/backup-db.sh` на этом же хосте.

Восстановление (заменяет содержимое БД на момент дампа):

```sh
ssh jb@192.168.55.40
cd ~/apps/mentis-trading-agent
docker compose --env-file .env.prod -f compose.yml -f compose.prod.yml stop backend agent
cat ~/backups/mentis-trading-agent/<файл>.dump | \
  docker exec -i mentis-db-1 pg_restore -U postgres -d app --clean --if-exists   # или -d agent
docker compose --env-file .env.prod -f compose.yml -f compose.prod.yml start backend agent
```

## 6. Откат релиза

```sh
git log --oneline -5                 # найти предыдущий рабочий коммит
git checkout <ревизия>               # на машине разработки
./scripts/deploy.sh                  # задеплоит её; БД перед этим уйдёт в дамп
```

Если миграция из нового релиза необратимо изменила данные, откат кода **не**
отменяет миграцию — восстановите БД из `pre-deploy-*.dump` (§5).

## 7. Правила и предостережения

1. **Один Binance live-ключ — один активный стенд.** Ключ перенесён с машины
   разработки на этот сервер (2026-09-20). **Никогда не запускайте
   live-стратегию на двух стендах одновременно с одним и тем же ключом** —
   Binance получит дублирующиеся ордера. Если нужно снова проверять
   стратегию локально live — сначала остановите её здесь (или наоборот).
2. **Секреты** — только в `.env` (машина разработки) и
   `~/apps/mentis-trading-agent/.env.prod` (сервер, права 600). В git не
   попадают.
3. **Деплоится только закоммиченное.** Ручные правки внутри контейнеров не
   переживут пересоздание — постоянное только через репозиторий (код) или
   volume (данные БД, `agent-signal-log`).
4. **Известная проблема ре-скрининга** (`_rescreen_loop` может умереть
   насовсем при транзиентной ошибке WS-подписки — ClickUp `869f4dqry`) ещё не
   исправлена на момент первого деплоя сюда. При длительном прогоне сверяйте
   `active symbols updated` в логе `agent`, не завис ли набор символов — см.
   `docs/TRIPPLE_SCREEN_WORKING PROCESS.md`.
5. Порт `WEB_PORT`/`ADMINER_PORT` должны быть свободны на 55.40 — там живут
   другие контейнеры (`plan-oplat`, `n8n`, `nginx-proxy-manager` на 80/81/443,
   `postgres-shared` на 5432). При конфликте — сменить в `.env.prod`.

## 8. История

- **2026-09-20.** Первый деплой на 192.168.55.40, live-ключ перенесён с
  машины разработки; локальный live-стенд остановлен владельцем перед
  переездом (без пересечения по времени).
