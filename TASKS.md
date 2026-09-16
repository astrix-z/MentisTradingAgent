# Задания агенту: известные недоработки

Дата: 2026-09-15. Собрано по итогам задач «Working Symbols» (коммиты `a2c2820` — агент,
`b6cfee6` — backend, `2ad80cd` — frontend) и «метка Monitoring» (`ae87817`, `16c681b`,
`789c19c`); T07 найден при работе над второй и там же закрыт.

Это не список падений продукта: основной сценарий работает. Это список того, из-за
чего **проверкам нельзя доверять в полной мере** (T01, T02), что **мешает локальной
работе** (T03, T04) и что **оставляет пробелы в покрытии** (T05, T06).

Состояние проверок на момент составления (перепроверять перед работой):

- agent: `pytest` 192 passed / 6 skipped; `ruff check` чисто; `mypy agent tests` чисто
  (и теперь действительно строгий — см. «Исправлено» ниже).
- backend: `pytest` 172 passed / 1 skipped, coverage 93% (гейт 90%); `ruff check` чисто.
- frontend: `tsc -p tsconfig.build.json` и `biome` чисто на исходниках; `vite build` ок;
  Playwright mock **23 passed**; живой e2e **83 passed, 2 failed** (см. T05).

## Сводка

| #   | Тема                                                    | Тип           | Приоритет |
| --- | ------------------------------------------------------- | ------------- | --------- |
| T01 | Backend вообще не проверяется строгим mypy              | дыра в гейте  | P1        |
| T02 | `tsc` не проверяет типы тестов фронтенда                | дыра в гейте  | P2        |
| T05 | Живой e2e требует mailpit и падает сырым `ECONNREFUSED`  | неполный прогон | P2      |
| T03 | `biome check` и генератор фикстуры не сходятся          | бумажная боль | P3        |
| T04 | `biome` сканирует gitignore-артефакты e2e               | бумажная боль | P3        |
| T06 | У `/working-symbols` нет живого e2e                     | покрытие      | P3        |

### Исправлено

- **T07 (был P2).** `agent/README.md` заявлял проверку `mypy agent tests`, которая падала
  на 6 давних ошибках в тестах. Ошибки починены по-настоящему — `parsed[Model]()`
  разворачивает `from_dict` (он `Optional`), `reported()` проверяет `isinstance`,
  Redis-клиент аннотирован там, где `from_url` возвращает `Any`, и два узких
  `# type: ignore[misc]` с объяснением на `hgetall` (так же поступает продакшен-код).
  Проверка добавлена хуком `local-mypy-agent` и **обязана** запускаться из каталога
  агента: из корня mypy не находит `agent/pyproject.toml` и молча теряет `strict`
  (проверено пробой — неаннотированная функция проходит из корня и падает из `agent/`).
  Проверено: `cd agent && uv run mypy agent tests` → 0; `prek run local-mypy-agent`
  проходит, а с искусственной ошибкой типов в тесте — падает.

---

## T01. Backend не проверяется строгим mypy

**Симптом.** `backend/pyproject.toml` объявляет `strict = true`, журнал и README
ссылаются на «strict mypy», но ни один гейт его не применяет: строгий mypy по
`backend/app` сейчас дал бы 19 ошибок, и никто об этом не узнаёт.

**Где.**
- `.github/workflows/test-backend.yml` — джоба `Test Backend` делает только
  `prestart.sh`, `tests-start.sh` и `coverage report`. Шага mypy в ней **нет вообще**,
  поэтому «Test Backend» зелёный независимо от типов.
- `.pre-commit-config.yaml` — хук `local-mypy` (`entry: uv run mypy backend/app`)
  запускается **из корня репозитория**. В корневом `pyproject.toml` секции
  `[tool.mypy]` нет, поэтому mypy идёт с настройками по умолчанию: без `--strict`
  и без `exclude = ["venv", ".venv", "alembic"]` из `backend/pyproject.toml`
  (конфиг подхватывается из текущего каталога, а не из каталога проверяемого пакета).

**Воспроизведение.**

```bash
uv run mypy backend/app              # Success: no issues found in 28 source files
uv run mypy --strict backend/app     # Found 19 errors in 7 files (checked 28 source files)
cd backend && ../.venv/bin/mypy app  # 1 error: unused "type: ignore" (22 source files)
```

Разница 28 против 22 файлов — это `backend/app/alembic/`, который `exclude` не
отфильтровал, потому что конфиг не был прочитан.

**Доказательство, что из корня `strict` действительно теряется** (та же проба, что
закрыла T07): временный файл с неаннотированной функцией.

```bash
# A) из корня (так запускается хук) — strict не применён, ошибка не найдена:
uv run mypy agent/agent agent/tests              # Success: no issues found in 36 source files
# B) из каталога проекта — strict применён, ошибка найдена:
uv run --directory agent mypy agent tests        # error: Function is missing a type annotation
```

**Что сделать.**

1. Выбрать **одну** каноничную команду и привести к ней всё. Рекомендация: запускать
   из каталога проекта, чтобы читался его `pyproject.toml` — `entry:
   uv run --directory backend mypy app` в хуке вместо `uv run mypy backend/app`.
   (Так уже сделано для агента: хук `local-mypy-agent`.)
2. Удалить устаревший `# type: ignore[arg-type]` в `backend/app/core/strategy_catalog.py`
   — это и есть единственная настоящая ошибка строгого режима.
3. Добавить шаг mypy в `.github/workflows/test-backend.yml` (или в
   `backend/scripts/tests-start.sh`), чтобы гейт был в CI, а не только в хуке.

**Критерий приёмки.** Выбранная каноничная команда возвращает 0; она же запускается
в CI; `--strict` действительно применяется — это проверяется тем, что искусственная
ошибка типов в `backend/app` валит джобу.

---

## T02. `tsc` не проверяет типы тестов фронтенда

**Симптом.** Типы тестового кода не проверяются никогда, поэтому там уже висит
реальная ошибка.

**Где.** `frontend/package.json` → `build: tsc -p tsconfig.build.json && vite build`;
`frontend/tsconfig.build.json` исключает `tests/**/*.ts`. При этом
`frontend/tsconfig.json` включает `src`, `tests`, `playwright.config.ts` и задаёт
`target/lib: ES2020`.

**Воспроизведение.**

```bash
cd frontend
bun ../node_modules/.bin/tsc -p tsconfig.json --noEmit
# tests/trading-contract.spec.ts: error TS2550: Property 'at' does not exist on type
# 'string[]'. Do you need to change your target library? Try 'es2022' or later.
```

`Array.prototype.at` появился в ES2022, а `lib` — ES2020.

**Что сделать (любой вариант, но осознанно).**

- Поднять `target`/`lib` до ES2022 или выше, предварительно сверившись с поддержкой
  браузеров/Node в проекте; либо
- заменить `.at(-2)` на `[length - 2]`;
- и обязательно завести проверку типов, **включающую тесты** (отдельный
  `tsconfig.test.json` + шаг в хуке/CI) — иначе следующая такая ошибка снова
  проживёт незамеченной.

**Критерий приёмки.** Проверка типов, включающая `frontend/tests`, возвращает 0 и
запускается автоматически.

---

## T05. Живой e2e не самодостаточен (mailpit + `EMAILS_FROM_EMAIL`)

**Симптом.** Полный живой прогон всегда заканчивается двумя падениями, причём с
сырой сетевой ошибкой, а не с внятной причиной:

```
  2 failed
    [chromium] › tests/reset-password.spec.ts:32 › User can reset password successfully using the link
    [chromium] › tests/reset-password.spec.ts:83 › Weak new password validation
    Error: get: ECONNREFUSED
```

**Причина.** Тесты читают письмо из mailpit (`MAILPIT_HOST`), но сервис объявлен с
`profiles: ["extras"]`, и `docker compose up -d` его не поднимает;
`EMAILS_FROM_EMAIL` в окружении backend пустой.

**Воспроизведение.**

```bash
docker compose exec -T backend printenv EMAILS_FROM_EMAIL    # пусто
docker compose ps --format '{{.Service}}' | grep -c mailpit  # 0
```

**Что сделать (выбрать одно).**

- Сделать прогон самодостаточным: поднимать mailpit из конфигурации Playwright
  (`globalSetup`) или отдельной командой, и требовать непустой `EMAILS_FROM_EMAIL`.
- Либо честно **скипать** эти тесты, когда `MAILPIT_HOST` недостижим, с сообщением
  «нужен профиль extras»: два вечных падения маскируют настоящие регрессии.
- В любом случае описать в `development.md`, как получить полный зелёный прогон.

**Критерий приёмки.** Прогон на чистом стеке даёт либо 0 падений, либо явные скипы с
причиной; `ECONNREFUSED` без объяснения в отчёте не остаётся.

---

## T03. `biome check` и генератор фикстуры не сходятся

**Симптом.** `frontend/tests/fixtures/strategy-catalog.json` — отслеживаемый файл, но
`biome` считает его неформатированным. Поэтому любой прогон
`bun run lint` (`biome check --write --unsafe ./`) оставляет в дереве несвязанный
diff, а автор вынужден его откатывать.

**Причина.** `scripts/write_strategy_catalog_fixture.py` пишет
`json.dumps(describe_catalog(), indent=2)`: короткие массивы разворачиваются в
многострочные, а Biome сворачивает их обратно. Один файл, два форматтера, разный
результат. `biome.json` при этом исключает только сгенерированный код
(`src/client`, `src/routeTree.gen.ts`, `src/components/ui`), но не эту фикстуру.

**Что сделать (выбрать одно).**

- В конце `scripts/write_strategy_catalog_fixture.py` прогнать файл через Biome
  (`bun ../node_modules/.bin/biome format --write <файл>`), сделав форматтер
  источником истины; либо
- исключить `frontend/tests/fixtures/**` в `frontend/biome.json` (`files.includes`)
  как сгенерированный артефакт.

**Критерий приёмки.** `bun run lint` не меняет ни одного отслеживаемого файла;
повторный запуск генератора тоже не пачкает дерево.

---

## T04. `biome` сканирует gitignore-артефакты e2e

**Симптом.** После локального прогона Playwright проверка сообщает ошибки не про код:

```
::error title=format,file=playwright/.auth/user.json,line=1
::error title=format,file=test-results/.last-run.json,line=1
```

Пути перечислены в `frontend/.gitignore`, но `frontend/biome.json` не подключает
gitignore и не исключает их в `files.includes`. Пока артефактов нет — проверка
чистая; после прогона e2e — нет, и `--write` перезаписывает эти файлы.

**Что сделать.** Подключить в `frontend/biome.json` gitignore-интеграцию (в Biome 2.x
это `vcs.enabled` + `vcs.clientKind: "git"` + `vcs.useIgnoreFile`; точные ключи
сверить со схемой установленной версии) и/или добавить в `files.includes` исключения
`!**/test-results`, `!**/playwright/.auth`, `!**/blob-report`.

**Критерий приёмки.** `biome check ./` из `frontend/` зелёный и **после** прогона
живого e2e.

---

## T06. У `/working-symbols` нет живого e2e

Страница покрыта только mock-сьютом (`frontend/tests/trading-contract.spec.ts`,
тесты «working symbols: …» и «rows per page offers 100 and All»). Живого теста нет,
и это осознанный компромисс: отметки зависят от состояния пользователя, а в CI агент
может вообще не опубликовать вселенную.

**Что сделать.** Либо добавить тест, устойчивый к состоянию: страница показывает
**либо** таблицу с сортировкой, **либо** сообщение «The agent has not published a
symbol list yet», и ни в одном из случаев не падает. Либо отказаться осознанно и
зафиксировать решение здесь.

**Критерий приёмки.** Либо тест есть и проходит на чистом стеке, либо в этом пункте
дописан явный отказ с причиной.

---

## Требует решения человека (не кода)

- **12 символов исключены как догадка** по шаблону `<TICKER>B`:
  `AAOIBUSDT, ALABBUSDT, BNCBUSDT, CBRSBUSDT, DRAMBUSDT, INTWBUSDT, MUUBUSDT,
  MVLLBUSDT, QNTUSDT, SNXXBUSDT, USARBUSDT, YBUSDT`. Если какой-то из них —
  реальная монета, добавить его в `CRYPTO_ENDING_IN_B` (`agent/agent/symbols.py`).
- **Memecoins только помечаются**, не исключаются: список `MEMECOINS` засеян вручную
  и предполагает правки. Дополнять по мере необходимости.
