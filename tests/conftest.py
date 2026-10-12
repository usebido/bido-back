"""Tests do not read the developer's .env for behavior.

CI has no .env, and local values (AUTH_DEV_MODE, EMAIL_TRANSPORT, the SQS queue
URLs) change what the code under test does, so a suite that passes in CI would
fail on a laptop that followed the README. Only the database URLs are taken
from .env, so the `db` tests still find the local Postgres; everything else
comes from the process environment, as in CI.
"""

import os
from pathlib import Path

import pytest

from bido_back.settings import Settings

_DB_KEYS = ("DATABASE_URL", "MIGRATION_DATABASE_URL", "AGNO_DATABASE_URL")
_DOTENV = Path(__file__).resolve().parent.parent / ".env"

if _DOTENV.is_file():
    for _line in _DOTENV.read_text().splitlines():
        _key, _sep, _value = _line.partition("=")
        _key = _key.strip()
        if _sep and _key in _DB_KEYS and _key not in os.environ:
            os.environ[_key] = _value.strip().strip("\"'")

Settings.model_config["env_file"] = None


# CI runs the suite as PYTEST_SHARD_TOTAL parallel jobs, each with its own Postgres;
# job PYTEST_SHARD_INDEX keeps its share. Whole files go to one shard (module
# fixtures are built once), heaviest first onto the lightest shard, a `db` test
# weighing more than a pure one. Every shard collects the same items, so the split
# agrees without coordination. Unset (a laptop), nothing is dropped.
_DB_TEST_WEIGHT = 5


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    total = int(os.environ.get("PYTEST_SHARD_TOTAL", "1"))
    if total <= 1:
        return
    index = int(os.environ["PYTEST_SHARD_INDEX"])
    weights: dict[str, int] = {}
    for item in items:
        path = item.nodeid.split("::", 1)[0]
        weight = _DB_TEST_WEIGHT if item.get_closest_marker("db") else 1
        weights[path] = weights.get(path, 0) + weight
    loads = [0] * total
    mine: set[str] = set()
    for path, weight in sorted(weights.items(), key=lambda pw: (-pw[1], pw[0])):
        shard = loads.index(min(loads))
        loads[shard] += weight
        if shard == index:
            mine.add(path)
    kept: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        (kept if item.nodeid.split("::", 1)[0] in mine else dropped).append(item)
    config.hook.pytest_deselected(items=dropped)
    items[:] = kept
