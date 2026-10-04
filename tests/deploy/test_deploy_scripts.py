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

import hashlib
import json
import os
import stat
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def catalog_document(*names: str, schema_hash: str | None = None) -> str:
    """A catalog shaped and hashed the way the agent publishes it."""
    strategies = [{"name": name} for name in names]
    execution: dict[str, object] = {"entries": []}
    canonical = json.dumps(
        {"strategies": strategies, "execution": execution},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = schema_hash or hashlib.sha256(canonical.encode()).hexdigest()
    return json.dumps(
        {
            "version": 1,
            "schema_hash": digest,
            "published_at": "2026-10-03T00:00:00+00:00",
            "strategies": strategies,
            "execution": execution,
        }
    )


DEPLOY = ROOT / "scripts/deploy.sh"
#: A catalog the agent publishes, whole: the backend's compiler accepts it.
CATALOG_FIXTURE = ROOT / "frontend/tests/fixtures/strategy-catalog.json"


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
        [[ -f "$STATE/show_sleep" ]] && sleep "$(cat "$STATE/show_sleep")"
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
            # Someone else's unit may appear between S0 and S1.
            if [[ -f "$STATE/occupied_env" ]]; then
              echo loaded >"$STATE/unit.load"; echo active >"$STATE/unit.active"
              cp "$STATE/occupied_env" "$STATE/unit.env"
            fi
            echo "Unit mentis-deploy.service was already loaded or has a fragment file." >&2
            exit 1 ;;
          vanish)
            exit 0 ;;
          hang)
            echo loaded >"$STATE/unit.load"; echo active >"$STATE/unit.active"
            echo "$env_line" >"$STATE/unit.env"
            exit 0 ;;
          handover)
            # Ours for a moment, then the name belongs to another attempt.
            echo loaded >"$STATE/unit.load"; echo active >"$STATE/unit.active"
            echo "$env_line" >"$STATE/unit.env"
            ( sleep 0.5; echo "DEPLOY_ATTEMPT=1-1-00000000 DEPLOY_REVISION=${cmd[2]}" >"$STATE/unit.env" ) &
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
            # Detached, as systemd runs it; --collect unloads it when done.
            echo loaded >"$STATE/unit.load"; echo active >"$STATE/unit.active"
            echo "$env_line" >"$STATE/unit.env"
            setsid bash -c '
              env "$@" >/dev/null 2>&1 </dev/null
              echo $$ >/dev/null
              rm -f "$STATE/unit.load" "$STATE/unit.active" "$STATE/unit.env"
            ' unit "${envs[@]}" "${cmd[@]}" >/dev/null 2>&1 </dev/null &
            echo $! >"$STATE/unit.pid" ;;
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
              elif [[ -n "${MENTIS_TAG:-}" && -f "$STATE/compose_tag_agent_only" ]]; then
                printf 'mentis-backend:latest\nmentis-agent:%s\n' "$MENTIS_TAG"
              elif [[ -n "${MENTIS_TAG:-}" && ! -f "$STATE/compose_no_tag" ]]; then
                printf 'mentis-backend:%s\nmentis-agent:%s\n' "$MENTIS_TAG" "$MENTIS_TAG"
              else
                printf 'mentis-backend:latest\nmentis-agent:latest\n'
              fi ;;
            exec)
              if [[ "$2" == backend ]]; then
                # The target's backend: the real compiler of the target
                # revision, from the tree the fence installed (compose runs
                # in APP_DIR), not of this checkout.
                shift 3  # -T backend python
                echo "backend python $1" | head -c 40 >>"$STATE/calls.log"; echo >>"$STATE/calls.log"
                cd backend && exec "$BACKEND_PYTHON" "$@"
              fi
              shift 2; shift  # -T redis redis-cli
              strict=0; [[ "$1" == -e ]] && { strict=1; shift; }
              op="$1"; key="$2"; mkdir -p "$STATE/redis"
              echo "redis $op $key" >>"$STATE/calls.log"
              [[ -f "$STATE/fail_redis_$op" ]] && exit 1
              [[ -f "$STATE/sleep_redis_$op" ]] && sleep "$(cat "$STATE/sleep_redis_$op")"
              # Like redis-cli: an error reply is printed and exits 0 unless -e.
              if [[ -f "$STATE/error_redis_$op" ]]; then
                cat "$STATE/error_redis_$op"; exit "$strict"
              fi
              case "$op" in
                SET) echo "$3" >"$STATE/redis/$key"; echo OK ;;
                DEL) rm -f "$STATE/redis/$key"; echo 1 ;;
                GET) cat "$STATE/redis/$key" 2>/dev/null || true ;;
              esac ;;
            build)
              [[ -f "$STATE/fail_build" ]] && exit 1
              for s in backend agent; do echo "id-$s-$MENTIS_TAG" >"$(img "mentis-$s:$MENTIS_TAG")"; done ;;
            up)
              [[ -f "$STATE/fail_up" && "$*" == *remove-orphans* ]] && exit 1
              if [[ -f "$STATE/fail_up_after_backend" && "$*" == *remove-orphans* ]]; then
                cp "$(img "mentis-backend:latest")" "$STATE/containers/c-backend.image"
                exit 1
              fi
              # `up -d <service>` recreates only that service, as compose does.
              for s in backend agent; do
                [[ "$*" == *remove-orphans* || " $* " == *" $s "* ]] || continue
                cp "$(img "mentis-$s:latest")" "$STATE/containers/c-$s.image" 2>/dev/null || true
              done
              mkdir -p "$STATE/redis"
              if [[ -f "$STATE/catalog_payload" ]]; then
                cp "$STATE/catalog_payload" "$STATE/redis/agent:strategies"
              elif [[ ! -f "$STATE/agent_silent" ]]; then
                cp "$STATE/valid_catalog" "$STATE/redis/agent:strategies"
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
              '{{.Image}}')
                id=$(cat "$STATE/containers/$4.image"); echo "$id"
                # gone_<container>: the image was deleted under a running container.
                [[ -f "$STATE/gone_$4" ]] || cp "$STATE/containers/$4.image" "$STATE/images/$id" ;;
              '{{.Created}}') cat "$STATE/containers/$4.created" 2>/dev/null || date -Is ;;
              *) echo "image $(cat "$STATE/containers/$4.image")" ;;
            esac ;;
          exec)
            if [[ "$*" == *psql* ]]; then
              case "$*" in *"'agent'"*) db=agent ;; *) db=app ;; esac
              cat "$STATE/db_exists_$db" 2>/dev/null || echo 1
            else
              [[ -f "$STATE/fail_dump" ]] && exit 1
              [[ -f "$STATE/empty_dump" ]] || echo "dump"
            fi ;;
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
            check=False,
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )


@pytest.fixture(params=["home"])
def server(tmp_path: Path, request: pytest.FixtureRequest) -> Server:
    home = tmp_path / request.param
    state, bin_dir = tmp_path / "state", tmp_path / "bin"
    for path in (
        home / "apps/mentis-trading-agent",
        state / "images",
        state / "containers",
        bin_dir,
    ):
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
        "DEPLOY_CALL_TIMEOUT_S": "10",
        "BACKEND_PYTHON": sys.executable,
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
    server.put("valid_catalog", CATALOG_FIXTURE.read_text())
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


def test_a_deploy_builds_fences_swaps_the_agent_and_lifts_the_fence(
    server: Server,
) -> None:
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
    assert (
        server.image(f"mentis-agent:{T0}") == f"id-agent-{T0}"
    )  # the previous one kept
    assert (server.releases / "current").read_text().strip() == REVISION
    assert (server.releases / "history").read_text().split()[-1] == REVISION
    assert (server.releases / f"{REVISION}.uv.lock").exists()
    assert not list(server.releases.glob("stage-*"))  # its own stage is removed
    assert "==> Готово" in result.stdout and "(6) снятие флага" in result.stdout
    assert (server.home / "apps/mentis-trading-agent/.env.prod").exists()


def test_a_build_failure_leaves_neither_fence_nor_a_stopped_agent(
    server: Server,
) -> None:
    server.put("fail_build")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    calls = server.calls()
    assert not any("start_fence" in call or "stop agent" in call for call in calls)
    assert server.image("mentis-agent:latest") == f"id-agent-{T0}"
    assert "скрипт ограды завершился с кодом" in result.stdout
    assert "Флаг trading:start_fence не ставился" in result.stdout
    assert "снимается только по runbook" not in result.stdout


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
    assert "снимается только по runbook" in result.stdout


def test_a_refused_fence_set_stops_before_the_agent_is_touched(server: Server) -> None:
    # Review 2026-09-30 (PRESET-09a): SET answered OOM, redis-cli exited 0,
    # and the deploy stopped the agent, advanced current and exited 0.
    server.put(
        "error_redis_SET", "OOM command not allowed when used memory > 'maxmemory'."
    )
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1, result.stdout
    assert "не поставлен" in result.stdout, result.stdout
    assert not any("stop agent" in call for call in server.calls())
    assert server.redis("trading:start_fence") is None
    assert (server.releases / "current").read_text().strip() == T0


@pytest.mark.parametrize(
    "catalog",
    ["error", "text", "truncated", "wrong-hash", "no-strategies"],
    ids=["wrongtype-error", "not-a-catalog", "truncated", "wrong-hash", "empty"],
)
def test_the_catalog_gate_needs_a_real_catalog(server: Server, catalog: str) -> None:
    # GET WRONGTYPE printed an error that counted as a publication: the fence
    # was lifted and the deploy exited 0 with no catalog. Review 2026-10-03:
    # a truncated '{"schema_hash": "not-a-catalog"' passed the regex too.
    if catalog == "error":
        server.put(
            "error_redis_GET",
            "WRONGTYPE Operation against a key holding the wrong kind of value",
        )
    elif catalog == "text":
        server.put("catalog_payload", "not json at all")  # nonempty, no schema_hash
    elif catalog == "truncated":
        server.put("catalog_payload", '{"schema_hash": "not-a-catalog"')
    elif catalog == "wrong-hash":
        server.put(
            "catalog_payload", catalog_document("tripple_screen", schema_hash="0" * 64)
        )
    else:
        server.put("catalog_payload", catalog_document())
    server.env["DEPLOY_CATALOG_WAIT_S"] = "2"
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1, result.stdout
    assert "не опубликовал agent:strategies" in result.stdout, result.stdout
    assert server.redis("trading:start_fence") is not None  # Start stays closed
    assert (server.releases / "current").read_text().strip() == T0


def rehashed(doc: dict[str, object]) -> str:
    canonical = json.dumps(
        {"strategies": doc["strategies"], "execution": doc["execution"]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return json.dumps(
        {**doc, "schema_hash": hashlib.sha256(canonical.encode()).hexdigest()}
    )


@pytest.mark.parametrize("defect", ["version", "settings-schema"])
def test_a_catalog_the_target_backend_cannot_use_keeps_the_fence(
    server: Server, defect: str
) -> None:
    # Re-review 2026-10-03: well-formed and correctly hashed, but the
    # backend refuses it (unsupported version; a settings schema that is not
    # an object). The deploy exited 0 and lifted the fence.
    doc = json.loads(CATALOG_FIXTURE.read_text())
    if defect == "version":
        doc["version"] = 999
    else:
        doc["strategies"][0]["settings_schema"] = {"type": "string"}
    server.put("catalog_payload", rehashed(doc))
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1, result.stdout
    assert "backend цели не принимает" in result.stdout, result.stdout
    expected = "unsupported catalog version 999" if defect == "version" else "object"
    assert expected in result.stdout, result.stdout
    assert server.redis("trading:start_fence") is not None
    assert (server.releases / "current").read_text().strip() == T0


def test_the_images_report_names_a_missing_container(server: Server) -> None:
    (server.state / "containers/c-backend.image").unlink()
    server.put("fail_redis_DEL")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "backend: нет контейнера" in result.stdout, result.stdout


def test_files_of_old_finished_attempts_are_rotated(server: Server) -> None:
    for i in range(25):
        attempt = f"{1_600_000_000 + i}-1-0000000{i % 10}"
        for name in (
            f"deploy-{T0}-{attempt}.log",
            f"deploy-{T0}-{attempt}.exit",
            f"deploy-fence-{attempt}.sh",
        ):
            path = server.releases / name
            path.write_text("0\n")
            os.utime(path, (1_600_000_000 + i, 1_600_000_000 + i))
    # A client between its upload and its launch (R108): no .exit yet.
    waiting = server.releases / "deploy-fence-1500000000-1-aaaaaaaa.sh"
    waiting.write_text("#!/bin/bash\n")
    os.utime(waiting, (1_500_000_000, 1_500_000_000))
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 0, result.stdout
    exits = sorted(server.releases.glob(f"deploy-{T0}-*.exit"))
    assert len(exits) == 20  # the newest 20 old ones; this attempt's is extra
    assert not (server.releases / f"deploy-{T0}-1600000000-1-00000000.log").exists()
    assert not (server.releases / "deploy-fence-1600000004-1-00000004.sh").exists()
    assert (server.releases / "deploy-fence-1600000005-1-00000005.sh").exists()
    assert waiting.exists()
    assert len(list(server.releases.glob(f"deploy-{REVISION}-*.exit"))) == 1


@pytest.mark.parametrize(
    "failure,backend", [("fail_up", T0), ("fail_up_after_backend", REVISION)]
)
def test_a_failure_inside_up_runs_the_agent_of_releases_current(
    server: Server, failure: str, backend: str
) -> None:
    server.put(failure)
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert server.image("mentis-agent:latest") == f"id-agent-{T0}"
    # :latest of the backend is what runs, whichever way up failed.
    assert server.image("mentis-backend:latest") == f"id-backend-{backend}"
    running = (server.state / "containers/c-backend.image").read_text().strip()
    assert running == f"id-backend-{backend}"
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


@pytest.mark.parametrize(
    "target",
    ["608cfa1", "e4dffb4"],
    ids=["catalog-without-execution", "backend-without-compile-published"],
)
def test_a_rollback_to_an_admitted_historical_revision_passes_g4(
    server: Server, target: str
) -> None:
    # Review 2026-10-04 (PRESET-09a): deploy.sh admits targets from 608cfa1,
    # but the fence demanded "execution" in the catalog (608cfa1 has none and
    # hashes the strategies alone) and imported compile_published (backends
    # before a66a659 lack it). Both rollbacks ended in G4 with exit 1.
    # The catalog is that revision's own fixture; the compiler is the
    # revision's backend from the installed tree.
    revision = git("rev-parse", "--short=7", target)
    for svc in ("backend", "agent"):
        server.put(f"images/mentis-{svc}:{revision}", f"id-{svc}-{revision}")
    server.put(
        "valid_catalog",
        git("show", f"{revision}:frontend/tests/fixtures/strategy-catalog.json"),
    )
    result = server.deploy("--revision", revision)
    assert result.returncode == 0, result.stdout
    assert not any(" build" in call for call in server.calls())
    assert any(call.startswith("backend python") for call in server.calls())
    assert server.redis("trading:start_fence") is None
    assert (server.releases / "current").read_text().strip() == revision


def test_a_target_without_images_or_a_tracked_lock_is_refused(server: Server) -> None:
    app_dir = server.home / "apps/mentis-trading-agent"
    (app_dir / "running-tree").write_text("the tree of the running revision")
    result = server.deploy("--revision", NO_LOCK)
    assert result.returncode == 1
    assert "lock текущего дерева не подставляется" in result.stdout
    assert not any("start_fence" in call for call in server.calls())
    # Refused before step (1): APP_DIR still holds the running revision.
    assert sorted(p.name for p in app_dir.iterdir()) == [".env.prod", "running-tree"]


def test_app_dir_gets_the_target_only_after_the_build(server: Server) -> None:
    app_dir = server.home / "apps/mentis-trading-agent"
    (app_dir / "running-tree").write_text("the tree of the running revision")
    server.put("fail_build")
    assert server.deploy("--revision", REVISION).returncode == 1
    assert sorted(p.name for p in app_dir.iterdir()) == [".env.prod", "running-tree"]
    (server.state / "fail_build").unlink()
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 0, result.stdout
    assert not (app_dir / "running-tree").exists()  # a clean install
    assert (app_dir / "compose.yml").exists()
    assert (app_dir / ".env.prod").read_text() == "WEB_PORT=8010\n"
    assert not (app_dir / ".env.prod").is_symlink()


def test_a_target_whose_compose_ignores_mentis_tag_is_not_built(server: Server) -> None:
    server.put("compose_no_tag")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "не знает MENTIS_TAG" in result.stdout
    assert not any(" build" in call for call in server.calls())


def test_a_target_without_the_agent_service_is_refused_before_step_1(
    server: Server,
) -> None:
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
    result = first_run.deploy(
        "--revision", REVISION, "--t0-revision", T0, "--no-backup"
    )
    assert result.returncode == 0, result.stdout
    assert first_run.image(f"mentis-agent:{T0}") == f"id-agent-{T0}"
    assert (first_run.releases / "pinned").read_text().split() == [T0]
    assert (first_run.releases / f"{T0}.appdir.uv.lock").read_text() == "t0 lock"
    history = [
        line.split()[1]
        for line in (first_run.releases / "history").read_text().splitlines()
    ]
    assert history == [T0, REVISION]


def test_t0_not_matching_the_last_dump_needs_a_human(first_run: Server) -> None:
    result = first_run.deploy(
        "--revision", REVISION, "--t0-revision", REVISION, "--no-backup"
    )
    assert result.returncode == 1
    assert "--t0-confirm" in result.stdout
    confirmed = first_run.deploy(
        "--revision", REVISION, "--t0-revision", REVISION, "--t0-confirm", "--no-backup"
    )
    assert confirmed.returncode == 0, confirmed.stdout


def test_t0_with_a_deleted_running_image_pins_nothing(first_run: Server) -> None:
    """Docker's containerd store drops an untagged image under a running container."""
    first_run.put("gone_c-agent")
    result = first_run.deploy(
        "--revision", REVISION, "--t0-revision", T0, "--t0-confirm", "--no-backup"
    )
    assert result.returncode == 1
    assert "уже нет в хранилище Docker" in result.stdout
    assert first_run.image(f"mentis-backend:{T0}") is None  # no half-pinned T0
    assert not (first_run.releases / "pinned").exists()
    assert not (first_run.releases / "current").exists()


def test_t0_containers_older_than_the_dump_are_refused(first_run: Server) -> None:
    for svc in ("backend", "agent"):
        first_run.put(f"containers/c-{svc}.created", "2023-01-01T00:00:00Z")
    result = first_run.deploy(
        "--revision", REVISION, "--t0-revision", T0, "--no-backup"
    )
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
    server.unit(
        "loaded", "active", f"DEPLOY_ATTEMPT={other} DEPLOY_REVISION={REVISION}"
    )
    # A previous failed attempt of the same revision must not be read (R97).
    (server.releases / f"deploy-{REVISION}-1600000000-1-00000000.exit").write_text(
        "7\n"
    )
    (server.releases / f"deploy-{REVISION}-{other}.log").write_text(
        "other attempt log\n"
    )
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
    server.unit(
        "loaded",
        "active",
        f"DEPLOY_ATTEMPT=1700000000-42-abcdef01 DEPLOY_REVISION={T0}",
    )
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
    # Nothing was launched, so this attempt's files serve no one.
    assert not list(server.releases.glob("stage-*"))
    assert not list(server.releases.glob("deploy-fence-*.sh"))


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


# --- Review round R120-R133 -------------------------------------------------------------


@pytest.mark.parametrize("server", ["home with space"], indirect=True)
def test_a_server_home_with_a_space_is_quoted_remotely(server: Server) -> None:
    """R128: "$HOME" is expanded by the server and quoted there."""
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (server.home.parent / "home").exists()
    assert (server.releases / "current").read_text().strip() == REVISION


def test_a_failed_dump_stops_before_install_and_publishes_nothing(
    server: Server,
) -> None:
    """R123: a failed pg_dump is not a dump and does not rotate a good one out."""
    backups = server.home / "backups/mentis-trading-agent"
    backups.mkdir(parents=True)
    for i in range(10):
        good = backups / f"pre-deploy-app-{T0}-2026092{i % 10}-00000{i}.dump"
        good.write_text("good")
    server.put("fail_dump")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "pg_dump app не удался" in result.stdout
    assert sorted(p.read_text() for p in backups.iterdir()) == ["good"] * 10
    assert not any("start_fence" in call for call in server.calls())
    assert not any(" build" in call for call in server.calls())


def test_an_empty_dump_is_refused(server: Server) -> None:
    server.put("empty_dump")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "пустой дамп" in result.stdout
    assert not list((server.home / "backups/mentis-trading-agent").glob("*.dump"))


def test_a_database_that_does_not_exist_yet_is_skipped(server: Server) -> None:
    server.put("db_exists_agent", "")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 0, result.stdout
    assert "базы agent ещё нет" in result.stdout
    dumps = [p.name for p in (server.home / "backups/mentis-trading-agent").iterdir()]
    assert len(dumps) == 1 and dumps[0].startswith(f"pre-deploy-app-{REVISION}-")


def test_the_t0_lock_from_app_dir_does_not_allow_a_rebuild(first_run: Server) -> None:
    """R124: T0's APP_DIR lock is kept for reference; its origin is unknown."""
    result = first_run.deploy(
        "--revision", REVISION, "--t0-revision", T0, "--no-backup"
    )
    assert result.returncode == 0, result.stdout
    assert (first_run.releases / f"{T0}.appdir.uv.lock").read_text() == "t0 lock"
    assert not (first_run.releases / f"{T0}.uv.lock").exists()
    for svc in ("backend", "agent"):  # the pinned images are lost
        (first_run.state / f"images/mentis-{svc}:{T0}").unlink()
    again = first_run.deploy("--revision", T0, "--no-backup")
    assert again.returncode == 1
    assert "lock текущего дерева не подставляется" in again.stdout


def test_a_target_with_one_hard_coded_latest_is_not_built(server: Server) -> None:
    """R125: both built images must follow MENTIS_TAG."""
    server.put("compose_tag_agent_only")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "не знает MENTIS_TAG для backend" in result.stdout
    assert not any(" build" in call for call in server.calls())
    assert server.image("mentis-backend:latest") == f"id-backend-{T0}"


def test_an_unhealthy_backend_keeps_the_fence(server: Server) -> None:
    """R130: the healthcheck runs before the fence is lifted."""
    server.put("unhealthy")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "не прошёл healthcheck" in result.stdout
    assert not any("DEL trading:start_fence" in call for call in server.calls())
    assert "failed after lift" not in (server.redis("trading:start_fence") or "")
    assert (server.releases / "current").read_text().strip() == T0


def test_a_failure_after_the_lift_sets_the_fence_again(server: Server) -> None:
    """R130: writing releases/current fails after (6) — fail closed."""
    (server.releases / "current").chmod(0o444)
    try:
        result = server.deploy("--revision", REVISION)
    finally:
        (server.releases / "current").chmod(0o644)
    assert result.returncode == 1
    assert "ставлю флаг снова" in result.stdout
    assert "failed after lift" in (server.redis("trading:start_fence") or "")


def test_a_hung_catalog_read_is_bounded_by_time(server: Server) -> None:
    """R129: each GET is bounded and the wait ends on time, not on a count."""
    server.put("sleep_redis_GET", "30")
    server.env["DEPLOY_CALL_TIMEOUT_S"] = "1"
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1, result.stdout + "\n".join(server.calls())
    assert "не опубликовал agent:strategies" in result.stdout
    assert server.redis("trading:start_fence") is not None


@pytest.mark.parametrize("where", ["S0", "S1"])
def test_look_alike_variable_names_are_not_metadata(server: Server, where: str) -> None:
    """R127: NOT_DEPLOY_ATTEMPT is not DEPLOY_ATTEMPT, in S0 or after a refusal."""
    env = f"NOT_DEPLOY_ATTEMPT=1700000000-42-abcdef01 NOT_DEPLOY_REVISION={REVISION}"
    if where == "S0":
        server.unit("loaded", "active", env)
    else:
        server.put("run_mode", "occupied")
        server.put("occupied_env", env)
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "метаданные попытки отсутствуют или некорректны" in result.stdout
    assert sum("systemd-run" in call for call in server.calls()) == (where == "S1")


def test_the_name_taken_by_another_attempt_ends_the_wait(server: Server) -> None:
    """R133: while polling, a unit with another DEPLOY_ATTEMPT is not ours."""
    server.put("run_mode", "handover")
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "завершился без кода" in result.stdout
    assert "1-1-00000000" in result.stdout


def test_the_client_timeout_leaves_the_unit_running(server: Server) -> None:
    """R126/R133: the overall wait is bounded; the unit is not stopped."""
    server.put("run_mode", "hang")
    server.env["DEPLOY_WAIT_TIMEOUT_S"] = "2"
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "таймаут 2 с" in result.stdout
    assert (server.state / "unit.load").read_text().strip() == "loaded"


def test_a_hung_show_is_bounded(server: Server) -> None:
    """R126: one show that never answers does not hold the client."""
    server.put("show_sleep", "60")
    server.env["DEPLOY_READ_RETRY_S"] = "2"
    result = server.deploy("--revision", REVISION)
    assert result.returncode == 1
    assert "состояние юнита mentis-deploy неизвестно" in result.stdout
    assert not list(server.releases.glob("stage-*"))
    assert not list(server.releases.glob("deploy-fence-*.sh"))


@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGKILL"])
def test_a_unit_killed_during_g4_is_not_a_success(
    server: Server, signal_name: str
) -> None:
    """systemctl --user stop/kill while (5) waits: never reported as success.

    TERM runs the trap, which writes a non-zero code; KILL leaves no code, and
    the client reports "ended without a code". The fence stays either way.
    """
    import signal
    import time

    server.put("agent_silent")
    server.env["DEPLOY_CATALOG_WAIT_S"] = "60"
    process = subprocess.Popen(
        ["bash", str(DEPLOY), "--revision", REVISION],
        cwd=ROOT,
        env=server.env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            logs = list(server.releases.glob(f"deploy-{REVISION}-*.log"))
            if logs and "(5) ожидание" in logs[0].read_text():
                break
            time.sleep(0.1)
        else:
            raise AssertionError("the unit never reached step (5)")
        pid = int((server.state / "unit.pid").read_text())
        os.killpg(pid, getattr(signal, signal_name))
        if signal_name == "SIGKILL":  # systemd would unload the dead unit
            for name in ("unit.load", "unit.active", "unit.env"):
                (server.state / name).unlink(missing_ok=True)
        out, _ = process.communicate(timeout=60)
    finally:
        process.kill()
    assert process.returncode == 1
    if signal_name == "SIGTERM":
        assert "завершился с кодом 143" in out
    else:
        assert "завершился без кода" in out
    assert server.redis("trading:start_fence") is not None
