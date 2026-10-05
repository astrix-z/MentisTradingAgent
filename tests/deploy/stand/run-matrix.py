#!/usr/bin/env python3
"""PRESET-09a acceptance on a real stack: SSH, systemd, Docker (plan §7.1).

The stub harness (tests/deploy/test_deploy_scripts.py) checks the branches of
deploy.sh and deploy-fence.sh; this runs them against a stand-in server — a
container with systemd as PID 1, sshd and its own Docker engine (Dockerfile
here) — and writes a journal: for every scenario the revision, deploy.sh's
exit code, the fence, the images and states of backend and agent,
releases/current, what was expected and whether it held.

Faults come from the stand itself: ~/bin/compose-fault is the fence's
DEPLOY_COMPOSE (a failing build, flag, catalog delete or `up`, a paused new
agent, a slow `up`), ~/bin/systemd-run answers deploy.sh's launch wrongly,
the user bus is made unreachable with chmod, linger is switched off, the
local deploy.sh is killed mid-way, the unit is killed with SIGKILL.

    python3 tests/deploy/stand/run-matrix.py --out docs/acceptance/<file>.md

Needs docker on this machine and network access for the image builds. The
stand, its volumes and image are removed at the end (--keep keeps them).
Nothing outside the stand is touched: no server, no local mentis-* stack.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
STAND_DIR = Path(__file__).resolve().parent
NAME = "mentis-09a-stand"
IMAGE = f"{NAME}:latest"
SSH_PORT = "2223"
WEB_PORT = "18011"
APP = "~/apps/mentis-trading-agent"
RELEASES = "~/apps/releases"
COMPOSE = "docker compose --env-file .env.prod -f compose.yml -f compose.prod.yml"
FAULT_COMPOSE = "/home/jb/bin/compose-fault"
#: The revision production runs (T0) and the one deployed before it.
T0 = "567f4f8"
BEFORE_T0 = "bef05d5"
#: The first commit with a tracked uv.lock; T0's images are built with it.
FIRST_LOCK = "589c9cb"
#: Older than the catalog key (deploy.sh refuses), and one with no image
#: and no tracked lock.
TOO_OLD = "c0c8cf3"
NO_LOCK = "608cfa1"
ADMIN = ("admin@example.com", "StandPass12345")


def log(message: str) -> None:
    print(f"[{datetime.now(UTC):%H:%M:%S}] {message}", flush=True)


def run(
    cmd: list[str] | str,
    *,
    check: bool = True,
    timeout: float | None = 1800,
    input: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: PLW1510 - checked below, with the output
        cmd,
        shell=isinstance(cmd, str),
        text=True,
        capture_output=True,
        timeout=timeout,
        input=input,
        env=env,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"failed ({result.returncode}): {cmd}\n{result.stdout}\n{result.stderr}"
        )
    return result


def short(rev: str) -> str:
    return run(["git", "-C", str(REPO), "rev-parse", "--short", rev]).stdout.strip()


# --- the stand ----------------------------------------------------------------------------


class Stand:
    def __init__(self, work: Path) -> None:
        self.work = work
        self.key = work / "stand_key"
        self.ssh = work / "ssh.sh"

    def build(self) -> None:
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)])
        self.ssh.write_text(
            "#!/bin/sh\nexec ssh -o BatchMode=yes -o StrictHostKeyChecking=no "
            "-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 "
            f"-i {self.key} -p {SSH_PORT} jb@127.0.0.1 \"$@\"\n"
        )
        self.ssh.chmod(0o755)
        log("building the stand image")
        run(
            [
                "docker", "build", "-q", "-t", IMAGE,
                "--build-arg", f"SSH_KEY={(self.key.with_suffix('.pub')).read_text().strip()}",
                str(STAND_DIR),
            ],
            timeout=1800,
        )
        run(
            [
                "docker", "run", "-d", "--name", NAME, "--hostname", "stand",
                "--privileged", "--cgroupns=private",
                "-v", f"{NAME}-docker:/var/lib/docker",
                "-v", f"{NAME}-containerd:/var/lib/containerd",
                "-p", f"127.0.0.1:{SSH_PORT}:22", "-p", f"127.0.0.1:{WEB_PORT}:8010",
                IMAGE,
            ]
        )
        for _ in range(60):
            if run([str(self.ssh), "docker info -f ok"], check=False).stdout.strip() == "ok":
                break
            time.sleep(2)
        else:
            raise RuntimeError("the stand did not come up")
        path = self.sh("command -v systemd-run")
        assert path.endswith("/home/jb/bin/systemd-run"), path

    def remove(self) -> None:
        run(["docker", "rm", "-f", NAME], check=False)
        run(["docker", "volume", "rm", f"{NAME}-docker", f"{NAME}-containerd"], check=False)
        run(["docker", "rmi", IMAGE], check=False)

    def sh(self, command: str, *, check: bool = True, timeout: float = 600, input: str | None = None) -> str:
        result = run([str(self.ssh), command], check=check, timeout=timeout, input=input)
        return result.stdout.strip()

    def root(self, command: str) -> str:
        return run(["docker", "exec", NAME, "sh", "-c", command]).stdout.strip()

    def compose(self, args: str, *, check: bool = True) -> str:
        return self.sh(f"cd {APP} && {COMPOSE} {args}", check=check)

    def fault(self, name: str | None, *, slow: int | None = None) -> None:
        self.sh("mkdir -p ~/faults && rm -f ~/faults/active ~/faults/slow")
        if slow is not None:
            self.sh(f"echo {slow} > ~/faults/slow")
        if name:
            self.sh(f"echo {name} > ~/faults/active")

    def fence(self) -> str:
        out = self.compose("exec -T redis redis-cli GET trading:start_fence", check=False)
        return out or "—"

    def container(self, service: str) -> str:
        cid = self.sh(f"cd {APP} && {COMPOSE} ps -aq {service} | head -n 1", check=False)
        if not cid:
            return "нет контейнера"
        state = self.sh(f"docker inspect -f '{{{{.State.Status}}}}' {cid}", check=False)
        image = self.sh(f"docker inspect -f '{{{{.Image}}}}' {cid}", check=False)
        tags = self.sh(
            f"docker image inspect -f '{{{{join .RepoTags \",\"}}}}' {image}", check=False
        )
        revs = sorted(
            t.split(":", 1)[1] for t in tags.split(",") if ":" in t and not t.endswith(":latest")
        )
        return f"{'/'.join(revs) or image[7:19]} {state}"

    def current(self) -> str:
        return self.sh(f"cat {RELEASES}/current 2>/dev/null || echo —", check=False)

    def state(self) -> dict[str, str]:
        return {
            "fence": self.fence(),
            "backend": self.container("backend"),
            "agent": self.container("agent"),
            "current": self.current(),
        }


# --- deploy.sh --------------------------------------------------------------------------------


@dataclass
class Deploy:
    code: int
    out: str


def deploy_env(stand: Stand, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        DEPLOY_SSH=str(stand.ssh),
        DEPLOY_SERVER="stand",
        DEPLOY_COMPOSE=FAULT_COMPOSE,
        DEPLOY_WAIT_TIMEOUT_S="900",
    )
    env.update(extra)
    return env


def deploy(stand: Stand, *args: str, repo: Path = REPO, **env: str) -> Deploy:
    result = run(
        [str(repo / "scripts/deploy.sh"), "--allow-dirty", *args],
        check=False,
        timeout=1800,
        env=deploy_env(stand, **env),
    )
    return Deploy(result.returncode, result.stdout + result.stderr)


class Background:
    """deploy.sh in its own process group, output collected as it comes."""

    def __init__(self, stand: Stand, *args: str, repo: Path = REPO) -> None:
        self.proc = subprocess.Popen(
            [str(repo / "scripts/deploy.sh"), "--allow-dirty", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=deploy_env(stand),
            start_new_session=True,
        )
        self.lines: list[str] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line)

    def wait_for(self, text: str, timeout: float = 600) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(text in line for line in self.lines):
                return
            if self.proc.poll() is not None:
                time.sleep(0.5)
                if any(text in line for line in self.lines):
                    return
                raise RuntimeError(f"deploy.sh ended before {text!r}:\n{self.out}")
            time.sleep(0.5)
        raise RuntimeError(f"no {text!r} in {timeout}s:\n{self.out}")

    def kill(self) -> None:
        os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait()

    def result(self, timeout: float = 1800) -> Deploy:
        code = self.proc.wait(timeout=timeout)
        self._reader.join(10)
        return Deploy(code, self.out)

    @property
    def out(self) -> str:
        return "".join(self.lines)


# --- the backend API on the stand --------------------------------------------------------------


def api(method: str, path: str, body: Any = None) -> tuple[int, Any]:
    base = f"http://127.0.0.1:{WEB_PORT}/api/v1"
    form = urllib.parse.urlencode({"username": ADMIN[0], "password": ADMIN[1]}).encode()
    with urllib.request.urlopen(
        urllib.request.Request(base + "/login/access-token", data=form), timeout=20
    ) as response:
        token = json.loads(response.read())["access_token"]
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def default_params() -> dict[str, Any]:
    _, catalog = api("GET", "/trading/strategies/")
    schema = catalog["strategies"][0]["settings_schema"]
    return {k: v["default"] for k, v in schema["properties"].items() if "default" in v}


# --- the journal ------------------------------------------------------------------------------


@dataclass
class Row:
    sid: str
    title: str
    expected: str
    revision: str
    code: str
    state: dict[str, str]
    ok: bool
    notes: list[str] = field(default_factory=list)
    output: str = ""


class Journal:
    def __init__(self) -> None:
        self.rows: list[Row] = []

    def add(
        self,
        sid: str,
        title: str,
        expected: str,
        revision: str,
        result: Deploy | None,
        state: dict[str, str],
        checks: dict[str, bool],
        *notes: str,
    ) -> None:
        ok = all(checks.values())
        failed = [name for name, held in checks.items() if not held]
        row = Row(
            sid,
            title,
            expected,
            revision,
            "—" if result is None else str(result.code),
            state,
            ok,
            [*notes, *(f"НЕ ВЫПОЛНЕНО: {name}" for name in failed)],
            "" if result is None else result.out,
        )
        self.rows.append(row)
        log(f"{sid} {'OK' if ok else 'FAIL'} {title} {failed or ''}")

    def write(self, path: Path, *, head: str, started: datetime) -> None:
        passed = sum(row.ok for row in self.rows)
        lines = [
            "# PRESET-09a: приёмка выкладки на реальном стеке",
            "",
            f"Прогон {started:%Y-%m-%d %H:%M} – {datetime.now(UTC):%H:%M} UTC, "
            f"скрипты выкладки из `{head}`. Стенд — контейнер с systemd (PID 1), sshd и "
            "своим Docker (`tests/deploy/stand/`); `deploy.sh` ходит в него по SSH "
            "(`DEPLOY_SSH`), скрипт ограды работает в юните `mentis-deploy` пользователя "
            "с linger. Отказы вносит сам стенд: `~/bin/compose-fault` (это "
            "`DEPLOY_COMPOSE` юнита), `~/bin/systemd-run`, chmod шины пользователя, "
            "`loginctl disable-linger`, `systemctl --user kill`, kill локального "
            "`deploy.sh`. Воспроизвести: `python3 tests/deploy/stand/run-matrix.py "
            "--out <файл>`.",
            "",
            f"Итог: **{passed} из {len(self.rows)}** сценариев дали ожидаемый исход.",
            "",
            "Состояние снято сразу после сценария: флаг `trading:start_fence`, образы "
            "(ревизии по тегам) и состояние контейнеров backend и agent, "
            "`releases/current`.",
            "",
            "| # | Сценарий | Ожидаемо | Ревизия | Код `deploy.sh` | Флаг | backend | agent "
            "| current | Итог |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for row in self.rows:
            cells = [
                row.sid,
                row.title,
                row.expected,
                f"`{row.revision}`",
                row.code,
                row.state.get("fence", "—"),
                row.state.get("backend", "—"),
                row.state.get("agent", "—"),
                f"`{row.state.get('current', '—')}`",
                "✅" if row.ok else "❌",
            ]
            lines.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
        lines += ["", "## Подробности", ""]
        for row in self.rows:
            lines.append(f"### {row.sid}. {row.title}")
            lines.append("")
            for note in row.notes:
                lines.append(f"- {note}")
            if row.output:
                tail = "\n".join(row.output.strip().splitlines()[-25:])
                lines += ["", "```", tail, "```"]
            lines.append("")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines))


# --- scenarios ---------------------------------------------------------------------------------


def has(result: Deploy, text: str) -> bool:
    return text in result.out


def old_deploy(stand: Stand, rev: str, services: str = "") -> None:
    """What deployed T0 on the server: the script of its time (567f4f8)."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    for db in ("app", "agent"):
        stand.sh(
            "docker exec mentis-db-1 pg_dump -Fc -U postgres "
            f"{db} > ~/backups/mentis-trading-agent/pre-deploy-{db}-{rev}-{stamp}.dump "
            f"|| rm -f ~/backups/mentis-trading-agent/pre-deploy-{db}-{rev}-{stamp}.dump",
            check=False,
        )
    tar = subprocess.Popen(["git", "-C", str(REPO), "archive", rev], stdout=subprocess.PIPE)
    subprocess.run([str(stand.ssh), f"tar -x -C {APP}"], stdin=tar.stdout, check=True)
    tar.wait()
    lock = run(["git", "-C", str(REPO), "show", f"{FIRST_LOCK}:uv.lock"]).stdout
    stand.sh(f"cat > {APP}/uv.lock", input=lock)
    stand.compose(f"build -q {services}")
    stand.compose(f"up -d --remove-orphans {services}")
    for _ in range(60):
        if stand.sh("curl -fsS http://127.0.0.1:8010/api/v1/utils/health-check/", check=False):
            return
        time.sleep(3)
    raise RuntimeError(f"old deploy of {rev}: backend unhealthy")


def setup_production_copy(stand: Stand) -> None:
    log("replaying production: bef05d5, then 567f4f8 (backend only, as it happened)")
    env = (REPO / ".env.prod.example").read_text().splitlines()
    values = {
        "SECRET_KEY": "stand-secret-key-not-for-production",
        "FIRST_SUPERUSER": ADMIN[0],
        "FIRST_SUPERUSER_PASSWORD": ADMIN[1],
        "POSTGRES_PASSWORD": "stand-postgres-password",
        "WEB_HOST": "localhost",
    }
    lines = [
        f"{line.split('=', 1)[0]}={values[line.split('=', 1)[0]]}"
        if "=" in line and line.split("=", 1)[0] in values
        else line
        for line in env
    ]
    stand.sh(f"mkdir -p {APP} ~/backups/mentis-trading-agent")
    stand.sh(f"cat > {APP}/.env.prod && chmod 600 {APP}/.env.prod", input="\n".join(lines) + "\n")
    old_deploy(stand, BEFORE_T0)
    time.sleep(5)
    # On the server the agent's image did not change in 567f4f8: only the
    # backend was recreated, after the dump.
    old_deploy(stand, T0, "backend")


def t0_scenarios(stand: Stand, journal: Journal, first: str) -> None:
    result = deploy(stand, "--revision", first)
    journal.add(
        "1", "Первый запуск без `--t0-revision`", "отказ до шага (1)", first, result,
        stand.state(),
        {
            "код 1": result.code == 1,
            "сообщение о --t0-revision": has(result, "нужен --t0-revision"),
            "флага нет": stand.fence() == "—",
            "T0 не закреплён": stand.current() == "—",
        },
    )
    result = deploy(stand, "--revision", first, "--t0-revision", BEFORE_T0)
    journal.add(
        "2", "`--t0-revision` не совпадает с последним дампом", "отказ сверки T0",
        first, result, stand.state(),
        {
            "код 1": result.code == 1,
            "сверка не пройдена": has(result, "сверка T0 не пройдена"),
            "флага нет": stand.fence() == "—",
            "T0 не закреплён": stand.current() == "—",
        },
    )
    # A deploy of T0 that failed at its build: the dump is newer than both
    # running containers.
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    fake = f"~/backups/mentis-trading-agent/pre-deploy-app-{T0}-{stamp}.dump"
    stand.sh(f"docker exec mentis-db-1 pg_dump -Fc -U postgres app > {fake}")
    result = deploy(stand, "--revision", first, "--t0-revision", T0)
    journal.add(
        "3", "Оба контейнера созданы раньше последнего дампа (выкладка T0 упала на сборке)",
        "отказ сверки T0", first, result, stand.state(),
        {
            "код 1": result.code == 1,
            "сверка не пройдена": has(result, "сверка T0 не пройдена"),
            "флага нет": stand.fence() == "—",
        },
    )
    stand.fault("build")
    result = deploy(stand, "--revision", first, "--t0-revision", T0, "--t0-confirm")
    pinned = stand.sh(f"cat {RELEASES}/pinned 2>/dev/null", check=False)
    tags = stand.sh("docker images --format '{{.Repository}}:{{.Tag}}' | grep ':" + T0 + "$' | sort", check=False)
    journal.add(
        "4", "То же с `--t0-confirm` (сборка цели затем падает)",
        "T0 закреплён решением человека; сбой сборки — ни флага, ни остановки агента",
        first, result, stand.state(),
        {
            "T0 в pinned": pinned == T0,
            "оба образа помечены :T0": tags.count(":" + T0) == 2,
            "код 1 (сборка)": result.code == 1,
            "флага нет": stand.fence() == "—",
            "агент T0 работает": stand.container("agent").endswith("running"),
        },
        f"образы T0: {tags.replace(chr(10), ', ')}",
    )
    # Back to the state before the human decision, to check the ordinary path:
    # the pin, the fake dump and the snapshot that attempt took are gone.
    stand.sh(
        f"rm -rf {RELEASES} {fake} ~/backups/mentis-trading-agent/pre-deploy-*-{first}-*"
    )
    stand.sh(f"docker rmi mentis-backend:{T0} mentis-agent:{T0}", check=False)
    stand.fault(None)
    result = deploy(stand, "--revision", first, "--t0-revision", T0)
    pinned = stand.sh(f"cat {RELEASES}/pinned 2>/dev/null", check=False)
    journal.add(
        "5", "Корректная выкладка T0: агент не пересоздавался (образ из кеша), backend — после дампа",
        "сверка пройдена, T0 закреплён, выкладка первой ревизии", first, result,
        stand.state(),
        {
            "код 0": result.code == 0,
            "T0 в pinned": pinned == T0,
            "current = первая ревизия": stand.current() == first,
            "флаг снят": stand.fence() == "—",
        },
    )


def eleven_deploys(stand: Stand, journal: Journal, revisions: list[str]) -> None:
    for i, rev in enumerate(revisions, start=1):
        log(f"deploy {i}/{len(revisions)}: {rev}")
        result = deploy(stand, "--revision", rev)
        if result.code != 0:
            journal.add(
                "6", f"Выкладка {i} из {len(revisions)}", "успех", rev, result, stand.state(),
                {"код 0": False},
            )
            return
    tags = stand.sh("docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '^mentis-' | sort", check=False)
    journal.add(
        "6", f"{len(revisions) + 1} выкладок подряд (с первой), ротация образов",
        "образы T0 на месте (pinned), ротация оставляет 10 ревизий журнала",
        revisions[-1], None, stand.state(),
        {
            "образы :T0 на месте": f"mentis-agent:{T0}" in tags and f"mentis-backend:{T0}" in tags,
            "current = последняя": stand.current() == revisions[-1],
        },
        "ревизии: " + ", ".join(f"`{r}`" for r in revisions),
        "образы после ротации: " + ", ".join(tags.splitlines()),
    )


def fault_scenarios(stand: Stand, journal: Journal, running: str, target: str) -> None:
    """``running`` is deployed; ``target`` has no images yet."""

    def agent_is(rev: str) -> bool:
        return rev in stand.container("agent").split(" ")[0].split("/")

    def app_files() -> str:
        return stand.sh(
            f"cd {APP} && find . -type f ! -name .env.prod -print0 | sort -z | xargs -0 md5sum | md5sum"
        )

    stand.fault("build")
    app_before = app_files()
    result = deploy(stand, "--revision", target)
    journal.add(
        "7", "Сбой сборки цели", "ни флага, ни остановки агента; APP_DIR — работающая ревизия",
        target, result, stand.state(),
        {
            "код 1": result.code == 1,
            "сообщает: сбой до шага (1)": has(result, "сбой до шага (1)"),
            "флага нет": stand.fence() == "—",
            "агент прежней ревизии работает": agent_is(running) and stand.container("agent").endswith("running"),
            "APP_DIR не тронут": app_files() == app_before,
        },
    )
    stand.fault("flag")
    result = deploy(stand, "--revision", target)
    latest = stand.sh("docker image inspect -f '{{.Id}}' mentis-agent:latest")
    running_id = stand.sh(f"docker image inspect -f '{{{{.Id}}}}' mentis-agent:{running}")
    journal.add(
        "8", "Сбой после сборки, до флага (SET флага не прошёл)",
        "образы цели собраны, `:latest` — работающая ревизия, флага нет",
        target, result, stand.state(),
        {
            "код 1": result.code == 1,
            "образ :target собран": bool(stand.sh(f"docker images -q mentis-agent:{target}")),
            ":latest = работающая": latest == running_id,
            "флага нет": stand.fence() == "—",
            "агент прежней ревизии работает": agent_is(running),
        },
    )
    stand.fault("catalog-del")
    result = deploy(stand, "--revision", target)
    catalog = stand.compose("exec -T redis redis-cli EXISTS agent:strategies", check=False)
    journal.add(
        "9", "Сбой между остановкой агента и `up` (удаление каталога упало)",
        "`trap` поднял прежнего агента, его каталог опубликован, флаг стоит, печатает, что делать",
        target, result, stand.state(),
        {
            "код 1": result.code == 1,
            "trap поднял прежнего": has(result, "trap: поднимаю прежнего агента"),
            "агент прежней ревизии работает": agent_is(running) and stand.container("agent").endswith("running"),
            "каталог есть": catalog == "1",
            "флаг стоит": stand.fence() != "—",
            "отсылает к runbook": has(result, "docs/DEPLOY.md §6"),
        },
    )
    stand.fault("up")
    result = deploy(stand, "--revision", target)
    backend_latest = stand.sh("docker image inspect -f '{{.Id}}' mentis-backend:latest")
    backend_running = stand.sh(f"cd {APP} && {COMPOSE} ps -q backend | xargs docker inspect -f '{{{{.Image}}}}'")
    journal.add(
        "10", "Сбой внутри `up` (частичный up)",
        "работает агент из `releases/current`, `mentis-backend:latest` = работающий backend, флаг стоит",
        target, result, stand.state(),
        {
            "код 1": result.code == 1,
            "агент из releases/current": has(result, "trap: агент из releases/current") and agent_is(running),
            "backend:latest = работающий": backend_latest == backend_running,
            "флаг стоит": stand.fence() != "—",
        },
    )
    stand.fault("catalog-timeout")
    started = time.time()
    result = deploy(stand, "--revision", target)
    waited = time.time() - started
    journal.add(
        "11", "Агент цели не публикует каталог (поставлен на паузу после `up`)",
        "ожидание 2 мин, отказ, флаг стоит, агент цели запущен",
        target, result, stand.state(),
        {
            "код 1": result.code == 1,
            "таймаут каталога": has(result, "не опубликовал agent:strategies за 120"),
            "ждал ≥ 120 с": waited >= 120,
            "флаг стоит": stand.fence() != "—",
            "агент цели (на паузе)": agent_is(target) and "paused" in stand.container("agent"),
        },
        f"`deploy.sh` ждал {waited:.0f} с",
    )
    stand.sh("docker unpause mentis-agent-1", check=False)
    first_attempts = set(stand.sh(f"ls {RELEASES} | grep '^deploy-{target}-.*\\.exit$'").split())
    result = deploy(stand, "--revision", target)
    attempts = set(stand.sh(f"ls {RELEASES} | grep '^deploy-{target}-.*\\.exit$'").split())
    journal.add(
        "12", "Повторный `deploy.sh` той же ревизии после неудачи (таймаут шага (5))",
        "новая попытка, старый `.exit` не читается, флаг снимает только новый успех",
        target, result, stand.state(),
        {
            "код 0": result.code == 0,
            "новая попытка": len(attempts - first_attempts) == 1,
            "флаг снят": stand.fence() == "—",
            "агент цели работает": agent_is(target) and stand.container("agent").endswith("running"),
            "current = цель": stand.current() == target,
        },
    )


def unit_scenarios(stand: Stand, journal: Journal, a: str, b: str) -> None:
    """``a`` is deployed, ``b`` has images; both are switched between."""
    # Kill the unit with SIGKILL while `up` sleeps: no trap, no .exit.
    stand.fault("slow-up", slow=40)
    bg = Background(stand, "--revision", b)
    bg.wait_for("==> (4)")
    time.sleep(3)
    stand.sh("systemctl --user kill --signal=SIGKILL mentis-deploy")
    started = time.time()
    result = bg.result(timeout=600)
    journal.add(
        "13", "Юнит убит `systemctl --user kill -s KILL` до записи `.exit`",
        "`deploy.sh`: «завершился без кода», не ждёт до таймаута, флаг стоит",
        b, result, stand.state(),
        {
            "код 1": result.code == 1,
            "«без кода»": has(result, "завершился без кода"),
            "не ждал таймаут (900 с)": time.time() - started < 120,
            "флаг стоит": stand.fence() != "—",
        },
        "агент был остановлен на шаге (2) и не поднят: так и должно быть без trap; "
        "следующая выкладка поднимает его (сценарий 14)",
    )
    # SSH gone right after the agent stopped: the unit finishes on its own.
    stand.fault("slow-up", slow=20)
    bg = Background(stand, "--revision", a)
    bg.wait_for("==> (2) остановка агента")
    bg.kill()
    attempt = stand.sh(f"cat {RELEASES}/deploy.current").split()[1]
    for _ in range(120):
        code = stand.sh(f"cat {RELEASES}/deploy-{a}-{attempt}.exit 2>/dev/null", check=False)
        if code:
            break
        time.sleep(2)
    journal.add(
        "14", "Обрыв SSH (kill локального `deploy.sh`) сразу после остановки агента",
        "скрипт на сервере доходит до конца; лог и код выхода в `releases/`",
        a, Deploy(-9, bg.out), stand.state(),
        {
            "юнит завершился с кодом 0": code == "0",
            "лог есть": bool(stand.sh(f"ls {RELEASES}/deploy-{a}-{attempt}.log", check=False)),
            "флаг снят": stand.fence() == "—",
            "агент цели работает": a in stand.container("agent") and stand.container("agent").endswith("running"),
        },
        f"попытка `{attempt}`, код в `.exit`: {code or '—'}",
    )
    # Two deploy.sh of one revision at once.
    stand.fault("slow-up", slow=30)
    first = Background(stand, "--revision", b)
    first.wait_for("==> Установка")
    exec_start = stand.sh("systemctl --user show -p ExecStart mentis-deploy")
    second = Background(stand, "--revision", b)
    r1, r2 = first.result(), second.result()
    logs = stand.sh(f"ls {RELEASES} | grep -c '^deploy-{b}-.*\\.log$'")
    journal.add(
        "15", "Два одновременных `deploy.sh` одной ревизии",
        "один юнит, второй подключается к его логу; оба получают исход одной попытки",
        b, Deploy(r2.code, r2.out), stand.state(),
        {
            "оба код 0": r1.code == 0 and r2.code == 0,
            "второй подключился": "подключаюсь к ней" in r2.out,
            "никто не сообщил «без кода»": "без кода" not in r1.out + r2.out,
            "скрипт ограды — абсолютный путь в releases/": "/home/jb/apps/releases/deploy-fence-" in exec_start,
        },
        f"первый: код {r1.code}; логов попыток ревизии: {logs}",
        f"ExecStart юнита: `{exec_start[:160]}`",
    )
    # The same with a failed attempt of that revision on disk.
    stand.fault("flag")
    failed = deploy(stand, "--revision", a)
    stand.fault("slow-up", slow=30)
    first = Background(stand, "--revision", a)
    first.wait_for("==> Установка")
    second = Background(stand, "--revision", a)
    r1, r2 = first.result(), second.result()
    journal.add(
        "16", "То же при прошлой неудачной попытке этой ревизии с `.exit` на диске",
        "ни один не сообщает её код; оба получают исход новой попытки",
        a, Deploy(r2.code, r2.out), stand.state(),
        {
            "прошлая попытка упала": failed.code == 1,
            "оба код 0": r1.code == 0 and r2.code == 0,
            "второй подключился": "подключаюсь к ней" in r2.out,
            "никто не сообщил прошлый код": "завершился с кодом 1" not in r1.out + r2.out,
        },
    )
    # A second deploy.sh with a changed fence script while the first runs.
    tree = Path(tempfile.mkdtemp(prefix="stand-tree-"))
    run(["git", "-C", str(REPO), "worktree", "add", "--detach", str(tree), "HEAD"], timeout=120)
    try:
        fence = tree / "scripts/deploy-fence.sh"
        fence.write_text(fence.read_text() + "\n# changed for scenario 17\n")
        stand.fault("slow-up", slow=30)
        first = Background(stand, "--revision", b)
        first.wait_for("==> Установка")
        second = Background(stand, "--revision", b, repo=tree)
        r1, r2 = first.result(), second.result()
        attempt = stand.sh(f"cat {RELEASES}/deploy.current").split()[1]
        marker = stand.sh(f"grep -o 'скрипт ограды [0-9a-f]*' {RELEASES}/deploy-{b}-{attempt}.log | head -n1")
        ours = run(["sha256sum", str(REPO / "scripts/deploy-fence.sh")]).stdout[:12]
        journal.add(
            "17", "Второй `deploy.sh` той же ревизии с изменённым скриптом ограды, пока первый жив",
            "первый дорабатывает свою версию (маркер в логе), второй подключается",
            b, Deploy(r2.code, r2.out), stand.state(),
            {
                "оба код 0": r1.code == 0 and r2.code == 0,
                "второй подключился": "подключаюсь к ней" in r2.out,
                "в логе версия первого": marker.endswith(ours),
            },
            f"маркер: `{marker}`, хеш первого `{ours}`",
        )
    finally:
        run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(tree)], check=False)
    # Another revision while one runs.
    stand.fault("slow-up", slow=30)
    first = Background(stand, "--revision", a)
    first.wait_for("==> Установка")
    stages = stand.sh(f"ls -d {RELEASES}/stage-* | wc -l")
    other = deploy(stand, "--revision", b)
    stages_after = stand.sh(f"ls -d {RELEASES}/stage-* | wc -l")
    r1 = first.result()
    journal.add(
        "18", "`deploy.sh` другой ревизии, пока идёт выкладка",
        "отказ, ничего не меняя; идущая выкладка завершается",
        b, other, stand.state(),
        {
            "отказ": other.code == 1,
            "файлов попыток не прибавилось": stages == stages_after,
            "первая выкладка — код 0": r1.code == 0,
            "current = первая ревизия": stand.current() == a,
        },
    )


def environment_scenarios(stand: Stand, journal: Journal, a: str) -> None:
    stand.root("loginctl disable-linger jb")
    before = stand.state()
    result = deploy(stand, "--revision", a)
    journal.add(
        "19", "Linger выключен у пользователя SSH", "отказ до шага (1): ни флага, ни остановки агента",
        a, result, stand.state(),
        {
            "код 1": result.code == 1,
            "про linger": has(result, "выключен linger"),
            "флага нет": stand.fence() == "—",
            "агент не тронут": stand.state()["agent"] == before["agent"],
        },
    )
    stand.root("loginctl enable-linger jb")
    for _ in range(30):
        if stand.sh("systemctl --user is-system-running", check=False) in ("running", "degraded"):
            break
        time.sleep(1)
    result = deploy(stand, "--revision", TOO_OLD)
    journal.add(
        "20", "Цель старше `608cfa1` (агент не публикует каталог)", "отказ до шага (1)",
        TOO_OLD, result, stand.state(),
        {"код 1": result.code == 1, "про 608cfa1": has(result, "старше 608cfa1"), "флага нет": stand.fence() == "—"},
    )
    result = deploy(stand, "--revision", NO_LOCK)
    journal.add(
        "21", "Цель без образов и без tracked `uv.lock`",
        "отказ, lock текущего дерева не подставляется", NO_LOCK, result, stand.state(),
        {
            "код 1": result.code == 1,
            "про uv.lock": has(result, "lock текущего дерева не подставляется"),
            "флага нет": stand.fence() == "—",
            "образов цели нет": not stand.sh(f"docker images -q mentis-agent:{NO_LOCK}", check=False),
        },
    )
    # The user manager unreachable before the launch, for longer than 30 s.
    stand.root("chmod 000 /run/user/1000/bus")
    try:
        result = deploy(stand, "--revision", a)
    finally:
        stand.root("chmod 666 /run/user/1000/bus")
    journal.add(
        "22", "Шина менеджера пользователя недоступна дольше 30 с", "«состояние неизвестно», юнит не запускается",
        a, result, stand.state(),
        {
            "код 1": result.code == 1,
            "«неизвестно»": has(result, "неизвестно"),
            "юнит не запускался": "not-found" in stand.sh("systemctl --user show -p LoadState mentis-deploy"),
        },
    )
    # Unreachable for a few seconds while deploy.sh waits for the outcome.
    stand.fault("slow-up", slow=25)
    bg = Background(stand, "--revision", a)
    bg.wait_for("==> (4)")
    stand.root("chmod 000 /run/user/1000/bus")
    time.sleep(8)
    stand.root("chmod 666 /run/user/1000/bus")
    result = bg.result()
    journal.add(
        "23", "Шина недоступна 8 с во время ожидания исхода",
        "повтор чтения; после восстановления — та же попытка, без нового запуска",
        a, result, stand.state(),
        {"код 0": result.code == 0, "«неизвестно» не было": "неизвестно" not in result.out},
    )
    # A loaded unit of the same name without the attempt's metadata.
    stand.sh("systemd-run --user --unit=mentis-deploy sleep 300")
    result = deploy(stand, "--revision", a)
    stand.sh("systemctl --user stop mentis-deploy", check=False)
    journal.add(
        "24", "Загруженный юнит `mentis-deploy` без `DEPLOY_ATTEMPT` (R109)",
        "«метаданные попытки отсутствуют или некорректны», без подключения и запуска",
        a, result, stand.state(),
        {"код 1": result.code == 1, "про метаданные": has(result, "метаданные попытки отсутствуют")},
    )
    stand.sh("mkdir -p ~/faults && echo refuse > ~/faults/systemd-run")
    result = deploy(stand, "--revision", a)
    journal.add(
        "25", "`systemd-run` отказал, юнита нет (R106)",
        "«юнит отсутствует: запуск не подтверждён…», stderr `systemd-run`, G4 не запускается",
        a, result, stand.state(),
        {
            "код 1": result.code == 1,
            "«юнит отсутствует»": has(result, "юнит mentis-deploy отсутствует"),
            "stderr systemd-run": has(result, "systemd-run-fault: refused"),
            "флага нет": stand.fence() == "—",
        },
    )
    stand.sh("echo lost-reply > ~/faults/systemd-run")
    result = deploy(stand, "--revision", a)
    journal.add(
        "26", "`systemd-run` вернул ошибку, а юнит работает со своей попыткой (R102)",
        "`deploy.sh` ждёт свою попытку и получает её исход",
        a, result, stand.state(),
        {"код 0": result.code == 0, "ответ потерян, ждёт": has(result, "ответ systemd-run потерян")},
    )


def api_scenarios(stand: Stand, journal: Journal, a: str, b: str) -> None:
    params = default_params()
    start = {"network": "testnet", "strategy": "tripple_screen", "symbols": ["BTCUSDT"], "params": params}
    stand.fault("slow-up", slow=30)
    bg = Background(stand, "--revision", b)
    bg.wait_for("==> (4)")
    s1, d1 = api("POST", "/trading/start/", start)
    s2, _ = api("POST", "/trading/commands/", {"type": "start", "payload": start})
    s3, _ = api("POST", "/trading/stop/")
    _, status = api("GET", "/trading/status/")
    result = bg.result()
    journal.add(
        "27", "Start во время выкладки (между флагом и каталогом цели), оба маршрута; Stop",
        "Start — 503 «starting is closed for maintenance»; Stop принят",
        b, result, stand.state(),
        {
            "start/ — 503": s1 == 503,
            "commands/ start — 503": s2 == 503,
            "Stop принят": s3 == 202,
            "desired.running = false": status["desired"]["running"] is False,
            "выкладка — код 0": result.code == 0,
        },
        f"ответ: {d1}",
    )
    # The delivery barrier: a Start queued while the agent is down.
    stand.compose("stop agent")
    s_start, _ = api("POST", "/trading/start/", start)
    s_stop, stopped = api("POST", "/trading/stop/")
    groups = stand.compose("exec -T redis redis-cli XINFO GROUPS agent:commands:stream")
    _, state_stop = api("GET", f"/trading/commands/{stopped['id']}/state/")
    blocked = state_stop is None or state_stop.get("status") != "succeeded"
    stand.compose("start agent")
    for _ in range(60):
        _, state_stop = api("GET", f"/trading/commands/{stopped['id']}/state/")
        if state_stop and state_stop.get("status") == "succeeded":
            break
        time.sleep(2)
    groups_after = stand.compose("exec -T redis redis-cli XINFO GROUPS agent:commands:stream")
    _, status = api("GET", "/trading/status/")
    lag = groups_after.splitlines()
    journal.add(
        "28", "Барьер доставки: Start в потоке при остановленном агенте, затем Stop",
        "барьер не проходит, пока агент не обработал обе команды; после — Stop succeeded, lag 0, pending 0, idle",
        a, None, stand.state(),
        {
            "Start и Stop приняты": s_start == 202 and s_stop == 202,
            "до запуска агента Stop не succeeded": blocked,
            "Stop succeeded": bool(state_stop) and state_stop.get("status") == "succeeded",
            "lag 0, pending 0": lag[lag.index("lag") + 1] == "0" and lag[lag.index("pending") + 1] == "0",
            "агент idle, desired.running false": status["actual"]["state"] == "idle" and not status["desired"]["running"],
        },
        "XINFO при остановленном агенте: " + " ".join(groups.split()),
    )


def rollback_to_t0(stand: Stand, journal: Journal) -> None:
    """Runbook G3 (docs/DEPLOY.md §6) to T0, on the saved images."""
    stand.compose('exec -T redis redis-cli SET trading:start_fence "rollback stand"')
    api("POST", "/trading/stop/")
    time.sleep(10)
    stand.compose("stop backend")
    stand.compose("run --rm backend alembic downgrade b3f7a1c9d2e4")
    stand.compose("stop agent")
    stand.compose("exec -T redis redis-cli DEL agent:strategies")
    builds_before = stand.sh("docker images -q | wc -l")
    result = deploy(stand, "--revision", T0)
    versions = sorted(stand.sh(f"ls {APP}/backend/app/alembic/versions").split())
    t0_versions = sorted(
        Path(p).name
        for p in run(["git", "-C", str(REPO), "ls-tree", "--name-only", f"{T0}:backend/app/alembic/versions"]).stdout.split()
    )
    current = stand.compose("exec -T backend alembic current", check=False).splitlines()
    catalog = stand.compose("exec -T redis redis-cli GET agent:strategies", check=False)
    journal.add(
        "29", "Откат на T0 по runbook после всех выкладок",
        "без сборки, на закреплённых образах; alembic = ревизия T0; в APP_DIR нет чужих миграций; каталог T0",
        T0, result, stand.state(),
        {
            "код 0": result.code == 0,
            "без сборки": has(result, f"Образы :{T0} уже есть — без сборки"),
            "образов не прибавилось": stand.sh("docker images -q | wc -l") == builds_before,
            "alembic current = b3f7a1c9d2e4": any("b3f7a1c9d2e4" in line for line in current),
            "миграции APP_DIR = T0": [v for v in versions if v.endswith(".py")] == [v for v in t0_versions if v.endswith(".py")],
            "каталог T0 (старые имена)": "swing_low_h1_n" in catalog,
            "флаг снят": stand.fence() == "—",
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--keep", action="store_true")
    options = parser.parse_args()
    head = short("HEAD")
    started = datetime.now(UTC)
    work = Path(tempfile.mkdtemp(prefix="stand-"))
    stand = Stand(work)
    journal = Journal()
    # Deployable revisions after T0, oldest first: all have a tracked uv.lock;
    # the last two have the same schema as HEAD for the fault scenarios.
    chain = run(
        ["git", "-C", str(REPO), "rev-list", "--first-parent", "--reverse", "--abbrev-commit", f"{FIRST_LOCK}..HEAD"]
    ).stdout.split()
    picks = [chain[round(i * (len(chain) - 3) / 10)] for i in range(11)]
    sequence = list(dict.fromkeys(short(r) for r in picks))
    a, b = short(chain[-3]), short(chain[-2])
    sequence = [r for r in sequence if r not in (a, b)][:11]
    try:
        stand.remove()
        stand.build()
        setup_production_copy(stand)
        t0_scenarios(stand, journal, sequence[0])
        eleven_deploys(stand, journal, [*sequence[1:], a])
        fault_scenarios(stand, journal, a, b)
        unit_scenarios(stand, journal, a, b)
        environment_scenarios(stand, journal, a)
        api_scenarios(stand, journal, a, b)
        rollback_to_t0(stand, journal)
    except Exception as exc:  # the journal says where it stopped
        journal.add("!", "Прогон прерван", "—", "—", None, {}, {"без исключения": False}, textwrap.shorten(str(exc), 900))
        raise
    finally:
        journal.write(options.out, head=head, started=started)
        log(f"journal: {options.out}")
        if not options.keep:
            stand.remove()
        shutil.rmtree(work, ignore_errors=True)
    return 0 if all(row.ok for row in journal.rows) else 1


if __name__ == "__main__":
    sys.exit(main())
