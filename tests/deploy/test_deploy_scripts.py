"""scripts/deploy.sh and scripts/deploy-fence.sh against stubbed tools (PRESET-09a).

The "server" is this machine: ``DEPLOY_SSH="bash -c"`` with a temporary
``HOME``, so every remote command runs locally. ``systemctl``,
``systemd-run``, ``loginctl``, ``docker`` and ``curl`` are stubs on ``PATH``
whose state lives in files and whose calls are logged, so the tests can put
the unit, Redis, images and containers into any state and check the order of
what the scripts did (plan §4.6 G4, §7.1 "Выкладка", "T0").

What this does not show: a real systemd unit surviving a dropped SSH session,
real Docker and Redis. Those are the local-stack acceptance of 09a.

    .venv/bin/python -m pytest tests/deploy
"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "scripts/deploy.sh"


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


#: A committed revision with a tracked uv.lock (after 589c9cb) and one without.
REVISION = git("rev-parse", "--short=7", "HEAD")
NO_LOCK = git("rev-parse", "--short=7", "c26ff56")
T0 = git("rev-parse", "--short=7", "4029132")
BEFORE_CATALOG = git("rev-parse", "--short=7", "608cfa1^")

STUBS: dict[str, str] = {
    "loginctl": """
        cat "$STATE/linger" 2>/dev/null || echo yes
    """,
    "systemctl": r"""
        # systemctl --user show -p LoadState -p ActiveState -p Environment <unit>
        echo "systemctl $*" >>"$STATE/calls.log"
        if [[ -f "$STATE/show_fail" ]]; then
          n=$(cat "$STATE/show_fail")
          if (( n != 0 )); then
            (( n > 0 )) && echo $((n - 1)) >"$STATE/show_fail"
            echo "Failed to connect to bus" >&2
            exit 1
          fi
        fi
        if [[ -f "$STATE/unit.load" ]]; then
          echo "LoadState=$(cat "$STATE/unit.load")"
          echo "ActiveState=$(cat "$STATE/unit.active")"
          echo "Environment=$(cat "$STATE/unit.env")"
        else
          echo "LoadState=not-found"; echo "ActiveState=inactive"; echo "Environment="
        fi
    """,
    "systemd-run": r"""
        echo "systemd-run $*" >>"$STATE/calls.log"
        envs=(); cmd=()
        while (($#)); do
          case "$1" in
            --user|--collect|--unit=*) ;;
            --setenv=*) envs+=("${1#--setenv=}") ;;
            *) cmd=("$@"); break ;;
          esac
          shift
        done
        mode=$(cat "$STATE/run_mode" 2>/dev/null || echo run)
        env_line=""
        for e in "${envs[@]}"; do
          case "$e" in DEPLOY_ATTEMPT=*|DEPLOY_REVISION=*) env_line+="$e ";; esac
        done
        case "$mode" in
          occupied)
            echo "Unit mentis-deploy.service was already loaded or has a fragment file." >&2
            exit 1 ;;
          vanish)
            exit 0 ;;
          lost-reply)
            echo loaded >"$STATE/unit.load"; echo active >"$STATE/unit.active"
            echo "$env_line" >"$STATE/unit.env"
            rel="$HOME/apps/releases"; rev="${cmd[2]}"; att="${cmd[3]}"
            echo "fence ran for $att" >"$rel/deploy-$rev-$att.log"
            echo 0 >"$rel/deploy-$rev-$att.exit"
            echo "Connection reset" >&2
            exit 1 ;;
          *)
            # Run the unit now, as systemd would, then --collect unloads it.
            echo loaded >"$STATE/unit.load"; echo active >"$STATE/unit.active"
            echo "$env_line" >"$STATE/unit.env"
            env "${envs[@]}" "${cmd[@]}" || true
            rm -f "$STATE/unit.load" "$STATE/unit.active" "$STATE/unit.env" ;;
        esac
    """,
    "curl": """
        [[ ! -f "$STATE/unhealthy" ]]
    """,
    "docker": r"""
        echo "docker $*" >>"$STATE/calls.log"
        img() { echo "$STATE/images/${1//\//_}"; }
        if [[ "$1" == compose ]]; then
          shift
          while [[ "$1" == --env-file || "$1" == -f ]]; do shift 2; done
          sub="$1"; shift
          case "$sub" in
            config)
              if [[ "$1" == --services ]]; then
                cat "$STATE/services" 2>/dev/null || printf 'db\nredis\nbackend\nagent\n'
              elif [[ -n "${MENTIS_TAG:-}" && ! -f "$STATE/compose_no_tag" ]]; then
                printf 'mentis-backend:%s\nmentis-agent:%s\n' "$MENTIS_TAG" "$MENTIS_TAG"
              else
                printf 'mentis-backend:latest\nmentis-agent:latest\n'
              fi ;;
            exec)
              shift 2; shift  # -T redis redis-cli
              op="$1"; key="$2"; mkdir -p "$STATE/redis"
              echo "redis $op $key" >>"$STATE/calls.log"
              [[ -f "$STATE/fail_redis_$op" ]] && exit 1
              case "$op" in
                SET) echo "$3" >"$STATE/redis/$key" ;;
                DEL) rm -f "$STATE/redis/$key" ;;
                GET) cat "$STATE/redis/$key" 2>/dev/null || true ;;
              esac ;;
            build)
              [[ -f "$STATE/fail_build" ]] && exit 1
              for s in backend agent; do echo "id-$s-$MENTIS_TAG" >"$(img "mentis-$s:$MENTIS_TAG")"; done ;;
            up)
              [[ -f "$STATE/fail_up" && "$*" == *remove-orphans* ]] && exit 1
              for s in backend agent; do
                cp "$(img "mentis-$s:latest")" "$STATE/containers/c-$s.image" 2>/dev/null || true
              done
              if [[ ! -f "$STATE/agent_silent" ]]; then
                mkdir -p "$STATE/redis"; echo '{"schema_hash": "h-new"}' >"$STATE/redis/agent:strategies"
              fi ;;
            ps)
              [[ -f "$STATE/containers/c-$2.image" ]] && echo "c-$2" ;;
            stop|start|logs) ;;
          esac
          exit 0
        fi
        case "$1" in
          tag) cp "$(img "$2")" "$(img "$3")" ;;
          image)
            if [[ "$2" == inspect ]]; then cat "$(img "$5")" 2>/dev/null || exit 1
            elif [[ "$2" == rm ]]; then rm "$(img "$3")" 2>/dev/null || exit 1; fi ;;
          inspect)
            case "$3" in
              '{{.Image}}') id=$(cat "$STATE/containers/$4.image"); echo "$id"; cp "$STATE/containers/$4.image" "$STATE/images/$id" ;;
              '{{.Created}}') cat "$STATE/containers/$4.created" 2>/dev/null || date -Is ;;
              *) echo "image $(cat "$STATE/containers/$4.image")" ;;
            esac ;;
          exec) echo "dump" ;;
          ps) ls "$STATE/containers" ;;
        esac
    """,
}


@dataclass
class Server:
    home: Path
    state: Path
    env: dict[str, str]

    @property
    def releases(self) -> Path:
        return self.home / "apps/releases"

    def put(self, name: str, value: str = "") -> None:
        path = self.state / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def image(self, name: str) -> str | None:
        path = self.state / "images" / name
        return path.read_text().strip() if path.exists() else None

    def redis(self, key: str) -> str | None:
        path = self.state / "redis" / key
        return path.read_text().strip() if path.exists() else None

    def calls(self) -> list[str]:
        path = self.state / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def unit(self, load: str, active: str, env: str) -> None:
        self.put("unit.load", load)
        self.put("unit.active", active)
        self.put("unit.env", env)

    def deploy(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(DEPLOY), *args],
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )


@pytest.fixture
def server(tmp_path: Path) -> Server:
    home, state, bin_dir = tmp_path / "home", tmp_path / "state", tmp_path / "bin"
    for path in (home / "apps/mentis-trading-agent", state / "images", state / "containers", bin_dir):
        path.mkdir(parents=True)
    (home / "apps/mentis-trading-agent/.env.prod").write_text("WEB_PORT=8010\n")
    for name, body in STUBS.items():
        stub = bin_dir / name
        stub.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    env = {
        **os.environ,
        "HOME": str(home),
        "STATE": str(state),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DEPLOY_SSH": "bash -c",
        "DEPLOY_POLL_S": "0.1",
        "DEPLOY_READ_RETRY_S": "1",
        "DEPLOY_READ_PAUSE_S": "0.1",
        "DEPLOY_PAUSE_S": "0.05",
        "DEPLOY_CATALOG_WAIT_S": "1",
        "DEPLOY_HEALTH_WAIT_S": "1",
        "DEPLOY_WAIT_TIMEOUT_S": "60",
    }
    server = Server(home=home, state=state, env=env)
    # A running stack that already went through 09a once: current = T0.
    server.releases.mkdir(parents=True)
    (server.releases / "current").write_text(f"{T0}\n")
    (server.releases / "history").write_text(f"2026-09-27T00:00:00+00:00 {T0}\n")
    for svc in ("backend", "agent"):
        server.put(f"images/mentis-{svc}:latest", f"id-{svc}-{T0}")
        server.put(f"images/mentis-{svc}:{T0}", f"id-{svc}-{T0}")
        server.put(f"containers/c-{svc}.image", f"id-{svc}-{T0}")
    server.put("redis/agent:strategies", '{"schema_hash": "h-old"}')
    return server


def order(calls: list[str], *needles: str) -> list[int]:
    """Index of the first call containing each needle; all must be found."""
    found = []
    for needle in needles:
        index = next((i for i, call in enumerate(calls) if needle in call), None)
        assert index is not None, f"{needle!r} not in {calls}"
        found.append(index)
    return found


# --- A normal deploy --------------------------------------------------------------


def test_a_deploy_builds_fences_swaps_the_agent_and_lifts_the_fence(server: Server) -> None:
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = server.calls()
    steps = order(
        calls,
        "compose --env-file .env.prod -f compose.yml -f compose.prod.yml build",
        "redis SET trading:start_fence",
        "compose --env-file .env.prod -f compose.yml -f compose.prod.yml stop agent",
        "redis DEL agent:strategies",
        f"tag mentis-agent:{REVISION} mentis-agent:latest",
        "up -d --remove-orphans",
        "redis DEL trading:start_fence",
    )
    assert steps == sorted(steps)
    assert server.redis("trading:start_fence") is None
    assert server.image("mentis-agent:latest") == f"id-agent-{REVISION}"
    assert server.image(f"mentis-agent:{T0}") == f"id-agent-{T0}"  # the previous one kept
    assert (server.releases / "current").read_text().strip() == REVISION
    assert (server.releases / "history").read_text().split()[-1] == REVISION
    assert (server.releases / f"{REVISION}.uv.lock").exists()
    assert not list(server.releases.glob("stage-*"))  # its own stage is removed
    assert "==> Готово" in result.stdout and "(6) снятие флага" in result.stdout
    assert (server.home / "apps/mentis-trading-agent/.env.prod").exists()


def test_a_build_failure_leaves_neither_fence_nor_a_stopped_agent(server: Server) -> None:
    server.put("fail_build")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    calls = server.calls()
    assert not any("start_fence" in call or "stop agent" in call for call in calls)
    assert server.image("mentis-agent:latest") == f"id-agent-{T0}"
    assert "скрипт ограды завершился с кодом" in result.stdout


def test_a_failure_before_up_starts_the_previous_agent_and_keeps_the_fence(
    server: Server,
) -> None:
    server.put("fail_redis_DEL")  # step (3) fails after the agent was stopped
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    calls = server.calls()
    order(calls, "stop agent", "start agent")
    assert server.redis("trading:start_fence") is not None
    assert "Флаг trading:start_fence остаётся" in result.stdout, result.stdout


def test_a_failure_inside_up_runs_the_agent_of_releases_current(server: Server) -> None:
    server.put("fail_up")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert server.image("mentis-agent:latest") == f"id-agent-{T0}"
    calls = server.calls()
    index = order(calls, f"tag mentis-agent:{T0} mentis-agent:latest")[0]
    assert any("up -d agent" in call for call in calls[index:])
    assert server.redis("trading:start_fence") is not None
    assert (server.releases / "current").read_text().strip() == T0


def test_an_agent_that_never_publishes_fails_step_5_with_the_fence_up(
    server: Server,
) -> None:
    server.put("agent_silent")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "не опубликовал agent:strategies" in result.stdout
    assert server.redis("trading:start_fence") is not None
    assert server.image("mentis-agent:latest") == f"id-agent-{REVISION}"  # target runs


def test_a_rollback_to_saved_images_does_not_build(server: Server) -> None:
    result = server.deploy("--revision", T0)
    assert result.returncode == 0, result.stdout
    assert not any(" build" in call for call in server.calls())
    assert server.image("mentis-agent:latest") == f"id-agent-{T0}"


def test_a_target_without_images_or_a_tracked_lock_is_refused(server: Server) -> None:
    result = server.deploy("--revision", NO_LOCK)
    assert result.returncode == 1
    assert "lock текущего дерева не подставляется" in result.stdout
    assert not any("start_fence" in call for call in server.calls())


def test_a_target_whose_compose_ignores_mentis_tag_is_not_built(server: Server) -> None:
    server.put("compose_no_tag")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "не знает MENTIS_TAG" in result.stdout
    assert not any(" build" in call for call in server.calls())


def test_a_target_without_the_agent_service_is_refused_before_step_1(server: Server) -> None:
    server.put("services", "db\nredis\nbackend\n")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "нет сервиса agent" in result.stdout
    assert not any("start_fence" in call for call in server.calls())


# --- T0 -------------------------------------------------------------------------------


@pytest.fixture
def first_run(server: Server) -> Server:
    """A stack never deployed by 09a: no releases/current, T0 is running."""
    for name in ("current", "history"):
        (server.releases / name).unlink()
    for svc in ("backend", "agent"):
        (server.state / f"images/mentis-{svc}:{T0}").unlink()
    backups = server.home / "backups/mentis-trading-agent"
    backups.mkdir(parents=True)
    dump = backups / f"pre-deploy-app-{T0}-20260927-000000.dump"
    dump.write_text("dump")
    os.utime(dump, (1_700_000_000, 1_700_000_000))  # the containers are newer
    (server.home / "apps/mentis-trading-agent/uv.lock").write_text("t0 lock")
    return server


def test_a_first_run_without_t0_is_refused(first_run: Server) -> None:
    result = first_run.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "--t0-revision" in result.stdout
    assert not any("start_fence" in call for call in first_run.calls())


def test_a_first_run_pins_t0_on_the_running_images(first_run: Server) -> None:
    result = first_run.deploy("--revision", REVISION, "--t0-revision", T0, "--no-backup")
    assert result.returncode == 0, result.stdout
    assert first_run.image(f"mentis-agent:{T0}") == f"id-agent-{T0}"
    assert (first_run.releases / "pinned").read_text().split() == [T0]
    assert (first_run.releases / f"{T0}.uv.lock").read_text() == "t0 lock"
    history = [line.split()[1] for line in (first_run.releases / "history").read_text().splitlines()]
    assert history == [T0, REVISION]


def test_t0_not_matching_the_last_dump_needs_a_human(first_run: Server) -> None:
    result = first_run.deploy("--revision", REVISION, "--t0-revision", REVISION, "--no-backup")
    assert result.returncode == 1
    assert "--t0-confirm" in result.stdout
    confirmed = first_run.deploy(
        "--revision", REVISION, "--t0-revision", REVISION, "--t0-confirm", "--no-backup"
    )
    assert confirmed.returncode == 0, confirmed.stdout


def test_t0_containers_older_than_the_dump_are_refused(first_run: Server) -> None:
    for svc in ("backend", "agent"):
        first_run.put(f"containers/c-{svc}.created", "2023-01-01T00:00:00Z")
    result = first_run.deploy("--revision", REVISION, "--t0-revision", T0, "--no-backup")
    assert result.returncode == 1
    assert "ни один контейнер не создан после дампа" in result.stdout


# --- Local checks and the launch protocol (R97, R102, R106, R108, R109) ---------------


def test_without_linger_nothing_is_written(server: Server) -> None:
    server.put("linger", "no")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "linger" in result.stdout
    assert not list(server.releases.glob("stage-*"))
    assert not any("systemd-run" in call for call in server.calls())


def test_a_revision_older_than_the_catalog_key_is_refused(server: Server) -> None:
    result = server.deploy("--revision", BEFORE_CATALOG)
    assert result.returncode == 1
    assert "608cfa1" in result.stdout


def test_a_running_deploy_of_the_same_revision_is_joined(server: Server) -> None:
    other = "1700000000-42-abcdef01"
    server.unit("loaded", "active", f"DEPLOY_ATTEMPT={other} DEPLOY_REVISION={REVISION}")
    # A previous failed attempt of the same revision must not be read (R97).
    (server.releases / f"deploy-{REVISION}-1600000000-1-00000000.exit").write_text("7\n")
    (server.releases / f"deploy-{REVISION}-{other}.log").write_text("other attempt log\n")
    (server.releases / f"deploy-{REVISION}-{other}.exit").write_text("0\n")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 0, result.stdout
    assert "подключаюсь" in result.stdout and "other attempt log" in result.stdout
    assert not any("systemd-run" in call for call in server.calls())
    assert not list(server.releases.glob("stage-*"))
    assert not list(server.releases.glob("deploy-fence-*.sh"))


def test_a_running_deploy_of_another_revision_is_refused_without_writes(
    server: Server,
) -> None:
    server.unit("loaded", "active", f"DEPLOY_ATTEMPT=1700000000-42-abcdef01 DEPLOY_REVISION={T0}")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert f"идёт выкладка ревизии {T0}" in result.stdout
    assert not list(server.releases.glob("stage-*"))
    assert not any("systemd-run" in call or "docker" in call for call in server.calls())


def test_a_refused_launch_with_the_unit_gone_is_not_retried(server: Server) -> None:
    server.put("run_mode", "occupied")
    (server.releases / "deploy.current").write_text("abc1234 1-1-00000000\n")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "запуск не подтверждён либо попытка уже выгружена" in result.stdout
    assert "already loaded" in result.stdout and "abc1234 1-1-00000000" in result.stdout
    assert sum("systemd-run" in call for call in server.calls()) == 1
    assert not list(server.releases.glob("stage-*"))


def test_a_lost_launch_reply_waits_for_its_own_attempt(server: Server) -> None:
    server.put("run_mode", "lost-reply")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 0, result.stdout
    assert "ответ systemd-run потерян" in result.stdout
    assert "fence ran for" in result.stdout


def test_a_unit_without_attempt_metadata_is_neither_joined_nor_launched(
    server: Server,
) -> None:
    server.unit("loaded", "active", f"DEPLOY_REVISION={REVISION}")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "метаданные попытки отсутствуют или некорректны" in result.stdout
    assert not any("systemd-run" in call for call in server.calls())
    assert not list(server.releases.glob("stage-*"))


def test_an_unreadable_unit_state_is_retried_then_refused(server: Server) -> None:
    server.put("show_fail", "-1")  # the bus never answers
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "состояние юнита mentis-deploy неизвестно" in result.stdout
    assert not any("systemd-run" in call for call in server.calls())


def test_a_bus_that_recovers_is_read_again_without_a_new_launch(server: Server) -> None:
    server.put("show_fail", "2")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 0, result.stdout
    assert sum("systemd-run" in call for call in server.calls()) == 1


def test_a_unit_that_dies_without_an_exit_code_is_reported(server: Server) -> None:
    server.put("run_mode", "vanish")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "завершился без кода" in result.stdout


def test_another_attempts_stage_is_kept(server: Server) -> None:
    """A stage prepared by another client and not launched yet (R108)."""
    theirs = server.releases / "stage-1600000000-1-00000000"
    (theirs / "tree").mkdir(parents=True)
    os.utime(theirs, (1_600_000_000, 1_600_000_000))
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 0, result.stdout
    assert theirs.exists()
    assert "для ручной уборки" in result.stdout
