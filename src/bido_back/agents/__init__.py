"""Agents: typed tools, versioned prompts, and one `run()` entry point (runner.py).

Agno's usage telemetry phones home unless AGNO_TELEMETRY says otherwise, and
Agno re-reads that variable on every run with precedence over the `telemetry`
constructor flag (agno/agent/_init.py `set_telemetry`). Our `.env` only feeds
pydantic Settings, not the process environment, so it is forced off here,
before any module of this package imports Agno. The adapter also passes
`telemetry=False` and forces the variable again on every run.
"""

import os

os.environ["AGNO_TELEMETRY"] = "false"
