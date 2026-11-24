"""Configuration loading.

Credentials come from ``config/local.toml`` (which the user creates themselves)
or from environment variables; see :func:`load_config` for which wins where.
Nothing here writes secrets back to disk.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from carrel.providers import ProviderConfig, RouteConfig, load_provider_registry

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


@dataclass(slots=True)
class MinerUConfig:
    token: str = ""
    model_version: str = "vlm"
    base_url: str = "https://mineru.net/api/v4"
    poll_interval: float = 3.0
    timeout_s: int = 900
    download_max_mb: int = 60
    daily_pages_soft_limit: int = 1000
    over_limit: str = "warn"

    @property
    def enabled(self) -> bool:
        return bool(self.token)


#: Every distinct model call the pipeline makes. ``[model.by_task.<task>]`` may
#: override ``name`` / ``thinking`` / ``max_tokens`` per task; anything not
#: overridden inherits the ``[model]`` section.
MODEL_TASKS: tuple[str, ...] = (
    "title", "preview", "extract", "summarize", "glossary", "translate", "repair",
    "term_review", "classify", "curate", "assess", "motivation", "motivation_tree",
)

#: Output ceilings per task. Reasoning tokens count against ``max_tokens`` on
#: every current model, so these are sized for the visible reply *plus* a
#: thinking trace. Real batch imports exhausted 6000 for extraction and 12000
#: for translation, even with thinking disabled in the request. Keep headroom
#: alongside bounded inputs and retries; these ceilings are not target lengths.
DEFAULT_MAX_TOKENS: dict[str, int] = {
    "title": 1500,
    "preview": 1500,
    "extract": 12000,
    "summarize": 12000,
    "glossary": 8000,
    "translate": 24000,
    "repair": 16000,
    "term_review": 4000,
    "classify": 6000,
    "curate": 2000,
    "assess": 3000,
    "motivation": 1800,
    "motivation_tree": 12000,
}

#: USD per million tokens. Only used to fill ``model_runs.cost_usd`` so the
#: usage report can be read in money. The flat rates are the public list price
#: of the default model; ``per_model`` lets a call that the gateway answered
#: with a *different* model be billed at that model's rate (the response names
#: the model that answered, and it is not always the one asked for).
DEFAULT_PRICING: dict[str, Any] = {
    "input": 1.4,
    "output": 4.4,
    "cache_read": 0.26,
    "cache_write": 1.4,
    "per_model": {
        "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0},
        "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.1, "cache_write": 2.5},
        "claude-haiku-5-5": {"input": 0.1, "output": 0.5, "cache_read": 0.01, "cache_write": 0.125},
        "glm-5.3-flash": {"input": 0.15, "output": 0.5, "cache_read": 0.03, "cache_write": 0.15},
    },
}


@dataclass(slots=True)
class TaskModelConfig:
    """Per-task override of the model selection. Empty/None means inherit."""

    name: str = ""
    thinking: str = ""
    max_tokens: int = 0


@dataclass(slots=True)
class ModelConfig:
    protocol: str = "messages"
    base_url: str = ""
    api_key_env: str = "ANTHROPIC_AUTH_TOKEN"
    api_key_file: str = ""  # fallback when the env var is absent
    name: str = "glm-5.3"
    # How much the model may reason before answering. ``disabled`` sends the
    # protocol's thinking-off switch; ``low``/``medium``/``high`` map to the
    # effort knob. Reasoning is billed as output, and on the old default it was
    # 58 % of every output token this app paid for.
    thinking: str = "disabled"
    # A single section takes 20–60 s in practice. Waiting 300 s before retrying
    # turns one slow gateway response into a 15-minute stall, so the ceiling is
    # kept well below that and retries are capped at one.
    timeout_s: int = 150
    max_retries: int = 1
    extra_headers: dict[str, str] = field(default_factory=dict)
    # Provider credentials are already resolved; never reuse the legacy env key.
    resolved_api_key: str | None = None
    pricing: dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_PRICING)))
    by_task: dict[str, TaskModelConfig] = field(default_factory=dict)

    @property
    def api_key(self) -> str:
        if self.resolved_api_key is not None:
            return self.resolved_api_key
        # The key in the config file belongs to the base_url in the config file;
        # the generic env var (ANTHROPIC_AUTH_TOKEN by default) is the fallback
        # for a config without one. Letting the env var win paired this app's
        # gateway address with another tool's token — see base_url precedence.
        # A carrel-specific env var still overrides both for scripted runs.
        return (os.environ.get("CARREL_MODEL_API_KEY", "")
                or self.api_key_file
                or os.environ.get(self.api_key_env, ""))

    def for_task(self, task: str) -> tuple[str, str, int]:
        """Resolve ``(model name, thinking, max_tokens)`` for one task.

        ``max_tokens`` is 0 for a task without a configured ceiling, which tells
        the backend to keep whatever the caller asked for.
        """
        over = self.by_task.get(task) or (self.by_task.get('preview') if task == 'title' else None) or TaskModelConfig()
        return (
            over.name or self.name,
            over.thinking or self.thinking,
            over.max_tokens or DEFAULT_MAX_TOKENS.get(task, 0),
        )


@dataclass(slots=True)
class OpenAlexConfig:
    base_url: str = "https://api.openalex.org"
    mailto: str = ""
    # Campus/office NAT shares one public IP, and that IP's free daily budget
    # is exhausted by everyone behind it — a personal (free) API key gives
    # this app its own budget regardless of the shared IP.
    api_key: str = ""
    api_key_env: str = "OPENALEX_API_KEY"
    per_page: int = 25

    @property
    def effective_api_key(self) -> str:
        import os
        return os.environ.get(self.api_key_env, "") or self.api_key


@dataclass(slots=True)
class SciverseConfig:
    token: str = ""
    base_url: str = "https://api.sciverse.space"

    @property
    def enabled(self) -> bool:
        return bool(self.token)


@dataclass(slots=True)
class ConcurrencyConfig:
    papers: int = 6
    mineru: int = 4
    download: int = 4
    arxiv_download: int = 2
    arxiv_interval_s: float = 1.0
    openalex_rps: float = 8.0
    sciverse: int = 2
    modules_per_paper: int = 3

    def __post_init__(self):
        for name in ('papers', 'mineru', 'download', 'arxiv_download', 'sciverse', 'modules_per_paper'):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= 64:
                raise ValueError(f'concurrency.{name} must be 1..64')
        if self.arxiv_interval_s < 0 or self.openalex_rps <= 0:
            raise ValueError('invalid request rate')


@dataclass(slots=True)
class RetryConfig:
    max_attempts: int = 4
    base_s: float = 2.0
    cap_s: float = 60.0
    breaker_failures: int = 5
    breaker_max_cooldown_s: float = 600.0

    def __post_init__(self):
        if (type(self.max_attempts) is not int or type(self.breaker_failures) is not int
                or not 1 <= self.max_attempts <= 10 or min(self.base_s, self.cap_s) < 0
                or self.breaker_failures < 1 or self.breaker_max_cooldown_s <= 0):
            raise ValueError('invalid retry configuration')


@dataclass(slots=True)
class AppConfig:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    mineru: MinerUConfig = field(default_factory=MinerUConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    openalex: OpenAlexConfig = field(default_factory=OpenAlexConfig)
    sciverse: SciverseConfig = field(default_factory=SciverseConfig)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    task_routes: dict[str, RouteConfig] = field(default_factory=dict)
    default_provider: str = ""
    provider_file: Path | None = None
    provider_error: str = ""
    max_parallel_modules: int = 3
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)


def _load_toml(path: Path) -> dict:
    if tomllib is None or not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def data_directory(data_dir: Path | str | None = None) -> Path:
    from platformdirs import user_data_dir

    return Path(data_dir or os.environ.get("CARREL_DATA_DIR") or user_data_dir("carrel"))


def load_config(
    data_dir: Path | str | None = None,
    config_file: Path | str | None = None,
) -> AppConfig:
    """Build the app config.

    Precedence, per field: ``CARREL_*`` env vars > ``config/local.toml`` >
    generic env vars (``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN``) >
    defaults. The generic variables rank *below* the file on purpose: other
    tools export them for their own proxies, and that proxy once answered an
    entire benchmark with a different model than the one this app asked for.
    Sections other than ``[model]`` follow the simpler env > file > defaults.
    """
    root = Path(__file__).resolve().parents[2]
    config_path = Path(config_file) if config_file else root / "config" / "local.toml"
    raw = _load_toml(config_path)

    data_root = data_directory(data_dir)

    mineru_raw = raw.get("mineru", {})
    mineru = MinerUConfig(
        token=os.environ.get("MINERU_TOKEN") or mineru_raw.get("token", ""),
        model_version=mineru_raw.get("model_version", "vlm"),
        base_url=mineru_raw.get("base_url", "https://mineru.net/api/v4"),
        poll_interval=float(mineru_raw.get("poll_interval", 3)),
        timeout_s=int(mineru_raw.get("timeout_s", 900)),
        download_max_mb=int(mineru_raw.get("download_max_mb", 60)),
        daily_pages_soft_limit=int(mineru_raw.get("daily_pages_soft_limit", 1000)),
        over_limit=mineru_raw.get("over_limit", "warn"),
    )

    model_raw = raw.get("model", {})
    pricing: dict[str, Any] = json.loads(json.dumps(DEFAULT_PRICING))
    for k, v in (model_raw.get("pricing") or {}).items():
        if k == "per_model" and isinstance(v, dict):
            for name, rates in v.items():
                if isinstance(rates, dict):
                    pricing["per_model"][str(name)] = {rk: float(rv) for rk, rv in rates.items()}
        elif k in ("input", "output", "cache_read", "cache_write"):
            pricing[k] = float(v)
    by_task: dict[str, TaskModelConfig] = {}
    for task, over in (model_raw.get("by_task") or {}).items():
        if task not in MODEL_TASKS or not isinstance(over, dict):
            continue
        by_task[task] = TaskModelConfig(
            name=str(over.get("name", "")),
            thinking=str(over.get("thinking", "")),
            max_tokens=int(over.get("max_tokens", 0) or 0),
        )
    model = ModelConfig(
        protocol=os.environ.get("CARREL_MODEL_PROTOCOL") or model_raw.get("protocol", "messages"),
        # Precedence: CARREL_MODEL_BASE_URL > config file > ANTHROPIC_BASE_URL.
        # The generic Anthropic variable is a *fallback* for a config with no
        # base_url, not an override: it is routinely set by other tools (a Claude
        # Code session exports one pointing at its own proxy), and that proxy
        # answered a whole benchmark run with a different model than the one
        # requested — every "glm-5.3" call was billed as claude-opus-5-5.
        base_url=os.environ.get("CARREL_MODEL_BASE_URL")
        or model_raw.get("base_url", "")
        or os.environ.get("ANTHROPIC_BASE_URL", ""),
        api_key_env=model_raw.get("api_key_env", "ANTHROPIC_AUTH_TOKEN"),
        api_key_file=model_raw.get("api_key", ""),
        name=os.environ.get("CARREL_MODEL") or model_raw.get("name", "glm-5.3"),
        thinking=os.environ.get("CARREL_MODEL_THINKING") or model_raw.get("thinking", "disabled"),
        extra_headers=dict(model_raw.get("extra_headers") or {}),
        timeout_s=int(model_raw.get("timeout_s", 150)),
        max_retries=int(model_raw.get("max_retries", 1)),
        pricing=pricing,
        by_task=by_task,
    )

    openalex_raw = raw.get("openalex", {})
    openalex = OpenAlexConfig(
        mailto=os.environ.get("OPENALEX_MAILTO") or openalex_raw.get("mailto", ""),
        api_key=openalex_raw.get("api_key", ""),
    )

    sciverse_raw = raw.get("sciverse", {})
    sciverse = SciverseConfig(
        token=os.environ.get("SCIVERSE_API_TOKEN") or sciverse_raw.get("token", ""),
    )

    app_raw = raw.get("app", {})
    concurrency = ConcurrencyConfig(**{k: v for k, v in raw.get('concurrency', {}).items() if k in ConcurrencyConfig.__dataclass_fields__})
    retry = RetryConfig(**{k: v for k, v in raw.get('retry', {}).items() if k in RetryConfig.__dataclass_fields__})
    if mineru.over_limit not in ('warn', 'pause') or mineru.daily_pages_soft_limit < 1 or mineru.timeout_s <= 0 or mineru.poll_interval < 0:
        raise ValueError('invalid MinerU configuration')
    provider_file = Path(data_root) / "providers.toml"
    provider_registry = load_provider_registry(provider_file)
    return AppConfig(
        data_dir=Path(data_root),
        port=int(os.environ.get("CARREL_PORT") or app_raw.get("port", 8765)),
        mineru=mineru,
        model=model,
        openalex=openalex,
        sciverse=sciverse,
        providers=provider_registry.providers,
        task_routes=provider_registry.routes,
        default_provider=provider_registry.default_provider,
        provider_file=provider_file,
        provider_error=provider_registry.error,
        concurrency=concurrency, retry=retry,
        max_parallel_modules=concurrency.modules_per_paper,
    )
