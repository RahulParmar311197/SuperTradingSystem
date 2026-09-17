"""The two emergency controls live only in Redis, so Redis has to keep them.

`app/core/redis.py` is the only store for both:

* **account halts** (`halt:*`) -- `halt_account` / `account_halt_reason`,
  set by `ReconciliationWorker` when local and broker state disagree and by
  `POST /orders` when a position ends up with no stop at the broker;
* **the kill switch** (blueprint §58) -- global, per-account and
  per-strategy, read by `RiskEngine.evaluate` and `evaluate_options_risk`
  on every proposal.

Both are deliberately one-way: blueprint §75 makes resuming a manual admin
action, and `POST /admin/accounts/{id}/resume` records it with an audit row.
Losing the Redis data lifts them with no such record -- an account halted
because its positions disagreed with the broker simply starts trading
again, and nothing anywhere says why.

`docker-compose.yml` gave `postgres` a named volume and gave `redis`
nothing: no volume, and no persistence configured, so the data lived in the
container's ephemeral layer and a `docker compose down && up` cleared it.
These assert the *properties* -- some durable volume, some persistence --
rather than the exact spelling, so the deployment can change how it gets
there without this failing spuriously.

Not a substitute for testing the real thing: there is no Docker in this
environment, so the restart itself has never been exercised here.
"""

import pathlib

import pytest
import yaml

_COMPOSE = pathlib.Path(__file__).resolve().parents[2] / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    assert _COMPOSE.is_file(), f"docker-compose.yml not found at {_COMPOSE}"
    return yaml.safe_load(_COMPOSE.read_text())


def test_redis_keeps_its_data_across_a_container_recreate(compose):
    redis = compose["services"]["redis"]
    mounts = redis.get("volumes") or []
    named_volumes = set(compose.get("volumes") or {})

    targets = [str(m).split(":") for m in mounts]
    durable = [
        parts for parts in targets if len(parts) >= 2 and parts[0] in named_volumes
    ]
    assert durable, (
        "the redis service mounts no named volume, so account halts and the kill "
        f"switch live in the container's ephemeral layer: volumes={mounts!r}"
    )


def test_redis_is_configured_to_persist_at_all(compose):
    """A volume with no persistence turned on is an empty directory.

    Asserted on the service's own configuration rather than on the image's
    defaults, because the defaults are exactly what this is protecting
    against relying on.
    """
    redis = compose["services"]["redis"]
    command = redis.get("command") or []
    rendered = " ".join(command) if isinstance(command, list) else str(command)
    assert "appendonly yes" in rendered or "--save" in rendered, (
        f"the redis service configures no persistence: command={command!r}"
    )


def test_postgres_is_still_durable_too(compose):
    """Control. The asymmetry between the two services is what made the gap
    easy to miss, so this pins the half that was already right -- if a
    future change strips both, this fails alongside the tests above rather
    than leaving them looking like the only concern.
    """
    postgres = compose["services"]["postgres"]
    named_volumes = set(compose.get("volumes") or {})
    mounts = [str(m).split(":") for m in (postgres.get("volumes") or [])]
    assert [parts for parts in mounts if len(parts) >= 2 and parts[0] in named_volumes], (
        f"postgres lost its named volume: {postgres.get('volumes')!r}"
    )


# --- a dead container must come back -------------------------------------

_LONG_RUNNING = ("postgres", "redis", "api", "worker")


def test_every_long_running_service_restarts_on_its_own(compose):
    """`docker-compose.yml` set no `restart:` policy on anything.

    The in-process supervisor (`app/core/supervision.py`) brings a dead
    *loop* back, but nothing brought back a dead *process*: an OOM kill, an
    unhandled exception escaping `main`, or a host reboot left the API or
    the worker down until someone noticed by hand. For a system whose whole
    point is running unattended (§54), that is the wrong default — and it
    is why `supervise` restarts loops in-process rather than exiting, a
    workaround this makes unnecessary as a last line of defence.

    Asserted as a property, not an exact spelling: any policy that brings
    the container back counts.
    """
    for name in _LONG_RUNNING:
        policy = compose["services"][name].get("restart")
        assert policy in {"always", "unless-stopped", "on-failure"}, (
            f"service {name!r} has no restart policy ({policy!r}), so a crash or host "
            "reboot leaves it down until a human notices"
        )


def test_the_one_shot_migration_job_does_not_restart(compose):
    """Control, and the reason this is not a blanket policy.

    `migrate` runs `alembic upgrade head` once, and `api`/`worker` wait on
    it with `service_completed_successfully`. A restart policy there would
    turn a failed migration into a restart loop that never completes, so
    the two services waiting on it would never start at all — a worse
    failure than the one the policy above prevents.
    """
    assert compose["services"]["migrate"].get("restart") is None, (
        "migrate is a one-shot job: a restart policy would loop a failed migration "
        "forever and block api and worker from ever starting"
    )
