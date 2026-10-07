"""Tests do not read the developer's .env for behavior.

CI has no .env, and local values (AUTH_DEV_MODE, EMAIL_TRANSPORT, the SQS queue
URLs) change what the code under test does, so a suite that passes in CI would
fail on a laptop that followed the README. Only the database URLs are taken
from .env, so the `db` tests still find the local Postgres; everything else
comes from the process environment, as in CI.
"""

import os
from pathlib import Path

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
