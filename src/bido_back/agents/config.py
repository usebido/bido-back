"""Runtime configuration for agent runs, in our own terms.

Built once (at startup, or per test) and passed to `runner.run()`. Holds no
framework or provider objects, only the values the adapter needs to build them.
"""

from dataclasses import dataclass, field

from bido_back.settings import Settings


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    # None lets the provider SDK read OPENAI_API_KEY from the environment.
    openai_api_key: str | None = field(default=None, repr=False)
    # Where Agno keeps sessions and runs (schema `agno`, role bido_agno).
    # None = no run storage: each run stands alone (tests, evals).
    agno_database_url: str | None = field(default=None, repr=False)
    model_timeout_s: float = 120.0
    # Retries of transient provider errors, done by the provider SDK.
    model_max_retries: int = 2

    @classmethod
    def from_settings(cls, settings: Settings) -> "RunnerConfig":
        url = settings.agno_database_url
        return cls(
            openai_api_key=settings.openai_api_key,
            agno_database_url=str(url) if url is not None else None,
        )
