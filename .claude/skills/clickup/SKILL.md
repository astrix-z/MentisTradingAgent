---
name: clickup
description: ClickUp for this project — tasks of MentisTradingAgent live only in the ClickUp space "MentisTradingAgent" (list "MentisTradingAgent"). Use whenever the user mentions ClickUp, a task URL (app.clickup.com/t/...), or asks to file, find, update or close a task, bug or finding for this project. Wraps the global ClickUp skill (~/.agents/skills/clickup) with the project's ids and task conventions.
---

# ClickUp — MentisTradingAgent

Project-local layer over the global skill `~/.agents/skills/clickup` (read its `SKILL.md`
for the full CLI and `references/api.md` for raw endpoints). Everything goes through its CLI:

```bash
CU=~/.agents/skills/clickup/scripts/clickup.py
SPACE=901210252911   # space "MentisTradingAgent"
LIST=901222197558    # list "MentisTradingAgent" (the space's only list, folderless)
python3 $CU --team 36275329 <command> ...
```

The user's shell is zsh: it doesn't word-split variables, so don't put the whole command in
one variable (`CU="python3 …"; $CU …` fails). Use a function instead:
`cu() { python3 ~/.agents/skills/clickup/scripts/clickup.py --team 36275329 "$@"; }`.
When one task links another, create the linked one first and substitute its id.

Token: the global skill reads `$CLICKUP_TOKEN` / `$CLICKUP_TOKEN_FILE` /
`~/.agents/skills/clickup_token.txt`. Never print it, paste it in chat, or put a token file
in this repo.

## Scope: this space only

- Create tasks only in `$LIST`. Search with `--list $LIST` or `--space-ids $SPACE`, never
  workspace-wide: the workspace also holds the unrelated spaces "Binance Autotrader" and
  "Space" — don't read, create or change tasks there from this project.
- A task URL handed in by the user may be checked with `task <id>`; if its `list` is not
  `MentisTradingAgent`, say so and don't change it.
- If the ids above stop resolving, re-discover with `hierarchy` and fix this file.

## Before creating

1. Look for duplicates, including closed tasks:
   `python3 $CU tasks --list $LIST --include-closed --limit 0 --query "<keyword>"`.
   If one exists, comment on it or reopen it instead of filing a new one.
2. Statuses of the list: `to do`, `planning`, `in progress`, `need review`,
   `update required`, `on hold`, `complete`, `need owner`, `cancelled`
   (check `list-info $LIST` if one is rejected). New tasks: `to do`.
3. The only member is the owner (user id `54194881`). Don't assign unless asked.

## Task conventions (match existing tasks)

- **Language**: Russian, same style as `docs/` and `CONTEXT.md`.
- **Name**: `<Область>: <суть проблемы или результата>`. Областей примеры: `Execution`,
  `Triple Screen`, `Backend`, `UI`, `UI Strategy`, `Market streams`, `Heartbeat`, `Tests`,
  `Deploy`, `Alerts`, `Release`. Series of planned steps use a code: `EXEC-15: …`, `PRESET-04a: …`.
- **Priority**: `urgent` — money at risk now; `high` — blocks the live pilot or loses
  protection/data; `normal` — default; `low` — cosmetic.
- **Description** (markdown, pass long text with `-d @file`, file in the scratchpad):

  ```markdown
  ## Контекст
  Где и когда найдено (дата UTC, ревизия, сеть), ссылка на PR/раздел runbook.

  ## Причина
  Что делает код сейчас, со ссылками `path/to/file.py:line` (пути от корня репо).

  ## Последствия
  Что случится с позициями/деньгами/данными и при каких условиях.

  ## Что сделать
  1. Нумерованные шаги, включая тесты и документацию.

  ## Готово, когда
  *   Проверяемые критерии.

  — Claude-Opus
  ```

  Sections without content may be dropped; the signature line stays: every entry (descriptions and comments) is signed `— Claude-Opus`; older tasks signed `— Claude-Mentis` are left as they are. Link related tasks as
  `[869xxxxx](https://app.clickup.com/t/869xxxxx)`.
- No secrets, tokens, `.env` values, API keys or account balances in tasks or comments.

## Keeping tasks in sync with work

- Work started on a task → status `in progress`; PR opened → comment with the PR link,
  status `need review`; merged and deployed → `complete` with a comment naming the merge
  commit and the deployed revision.
- Confirm with the user before deleting tasks, bulk status changes, or closing tasks the
  user filed.
- Report every created or changed task to the user with its `url`.
