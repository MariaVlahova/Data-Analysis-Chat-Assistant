"""Settings: `config.yaml` validated into pydantic models, plus secrets from the environment.

A typo in a key stops startup (`extra="forbid"`). Secrets never live in YAML.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator

from src.errors import ConfigError
from src.llm_steps import LLM_STEP_NAMES, STEP_DEFAULTS
from src.yaml_utils import load_yaml, validation_message

DEFAULT_CONFIG_NAME = "config.yaml"
CONFIG_ENV_VAR = "OPSFLEET_CONFIG"
ENVIRONMENT_ENV_VAR = "OPSFLEET_ENV"  # overrides app.environment, so one config file serves every environment

# The environments a run can name. Each one says which model service answers (`llm.environments`): Gemini everywhere but `local`, which is a
# model on this machine, behind an OpenAI-compatible server such as LM Studio.
ENVIRONMENTS = ("default", "develop", "staging", "prod", "local")
Environment = Literal["default", "develop", "staging", "prod", "local"]
LOCAL_MODEL = "qwen/qwen3-30b-a3b-2507"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppConfig(_Section):
    language: str = "en"  # A16
    currency: str = "USD"  # A17
    timezone: str = "UTC"  # A17
    data_dir: str = "data"
    user_hash_salt: str | None = None  # None: a generated file in data_dir (impl)
    users_file: str = "users.yaml"  # next to config.yaml; `config.local.yaml` points at `users.local.yaml` (impl)
    environment: Environment = "default"  # picks the model service in `llm.environments`; OPSFLEET_ENV overrides it (impl)


class BigQueryConfig(_Section):
    project_id: str | None = None  # None: GOOGLE_CLOUD_PROJECT or default credentials
    dataset: str = "bigquery-public-data.thelook_ecommerce"
    # "local" runs the same queries on a DuckDB file of generated sample data (`local-data build`), with no Google sign-in. Every
    # guard and the SafeQueryRunner still run; only the engine behind the client changes. For development and demos, never a
    # stand-in for the live checks of plans 03 and 05 (impl).
    backend: Literal["bigquery", "local"] = "bigquery"
    local_db: str = "data/local/thelook.duckdb"  # used only with backend: local
    max_bytes_billed: int = Field(536_870_912, gt=0)  # 512 MiB per query [OPEN] NFR6, Q3
    query_timeout_s: float = Field(30, gt=0)  # [OPEN] Q3
    sql_row_limit: int = Field(1000, ge=1)  # the forced LIMIT
    transient_retries: int = Field(2, ge=0)


_FUNCTION_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*(\.\*)?$")

# Plan 05, step 3. The SQL Guard always refuses these, whatever `guards.denied_functions` says: a typo or an emptied
# list in config.yaml must not switch a protection off. A name is matched without regard to case, and `ML.*` stands
# for every function in the ML namespace.
ALWAYS_DENIED_FUNCTIONS = (
    "EXTERNAL_QUERY",  # federated queries to other databases
    "ML.*", "AI.*", "VECTOR_SEARCH", "OBJ.*",  # model calls and vector search: cost, and data leaves BigQuery
    "SESSION_USER",  # the account name of whoever runs the query
    "APPENDS", "CHANGES", "EXTERNAL_OBJECT_TRANSFORM",  # table functions that read history or files
)


class GuardsConfig(_Section):
    k_anonymity: int = Field(10, ge=2)  # [PROPOSED] NFR13
    rows_to_model: int = Field(50, ge=1)  # [PROPOSED] NFR14
    allow_ranked_customer_lists: bool = True  # S7 rule still open
    max_input_chars: int = Field(2000, ge=1)
    denied_functions: list[str] = []  # plan 05, step 3: refused IN ADDITION to ALWAYS_DENIED_FUNCTIONS
    # Plan 05: the chat does not start while schema_allowlist.yaml says `reviewed_against_live: false`. The file was written
    # from knowledge of the dataset and nothing in it can prove it right; false lets the chat start with a warning.
    require_reviewed_allowlist: bool = True

    @field_validator("denied_functions")
    @classmethod
    def _valid_function_names(cls, names: list[str]) -> list[str]:
        # fullmatch: `$` would also accept a trailing newline (a YAML block scalar makes one), and a name with a newline
        # in it never matches a function, so the denial would silently do nothing.
        bad = [name for name in names if not _FUNCTION_PATTERN.fullmatch(name)]
        if bad:
            raise ValueError(f"not a function name or NAMESPACE.* pattern: {', '.join(repr(name) for name in bad)}")
        return names


class LoopConfig(_Section):
    max_subquestions: int = Field(4, ge=1)
    sql_repairs: int = Field(2, ge=0)  # NFR5
    broaden_attempts: int = Field(1, ge=0)  # 3.11
    max_replans: int = Field(0, ge=0, le=2)  # production default 1, ceiling 2 (Q22)
    parallel_subqueries: int = Field(1, ge=1)  # S12


class ComposerConfig(_Section):
    numeric_retry: int = Field(0, ge=0, le=1)  # Q29: 0 removes the number, 1 allows one re-ask


class TurnLimits(_Section):
    calls: int = Field(gt=0)
    seconds: float = Field(gt=0)


class BudgetConfig(_Section):
    simple: TurnLimits = TurnLimits(calls=8, seconds=60)  # [OPEN] NFR4
    why: TurnLimits = TurnLimits(calls=20, seconds=60)  # [OPEN] S3, NFR4
    report: TurnLimits = TurnLimits(calls=20, seconds=90)
    bytes_per_turn: int = Field(2_147_483_648, gt=0)  # 2 GiB [OPEN] Q3


class LlmModels(_Section):
    light: str = "gemini-3.5-flash-lite"
    main: str = "gemini-3.8-flash"
    pro: str = "gemini-3.1-pro-preview"
    embedding: str = "gemini-embedding-001"


class RateLimit(_Section):
    rpm: int = Field(gt=0)


class LlmRetry(_Section):
    base_s: float = Field(1.0, gt=0)
    cap_s: float = Field(20.0, gt=0)
    attempts: int = Field(3, ge=1)


class LlmBreaker(_Section):
    failure_threshold: int = Field(3, ge=1)
    cooldown_s: float = Field(30.0, gt=0)


class ModelPrice(_Section):
    """USD per 1M tokens. The YAML keys are `in` and `out` (`in` is a Python keyword)."""

    model_config = ConfigDict(populate_by_name=True)

    in_: float = Field(alias="in", ge=0)
    out: float = Field(ge=0)


class StepSettings(_Section):
    temperature: float = Field(0.0, ge=0, le=2)
    max_output_tokens: int = Field(2048, gt=0)


def _default_steps() -> dict[str, StepSettings]:
    """Every step with its own defaults (`src/llm_steps.py`)."""
    return {
        step.value: StepSettings(temperature=temperature, max_output_tokens=max_output_tokens)
        for step, (temperature, max_output_tokens) in STEP_DEFAULTS.items()
    }


class LlmBackend(_Section):
    """The model service behind one environment: Gemini (the SDK, with `llm.models` and `llm.rate_limits`), or any server that speaks the
    OpenAI chat-completions protocol (LM Studio, Ollama, vLLM). With the second, `model` answers every step: there are no tiers and no
    sibling to fall back to, and the Gemini rate limits do not apply (a local server has none; `rpm` only bounds a burst)."""

    provider: Literal["gemini", "openai_compatible"] = "gemini"
    base_url: str = "http://localhost:1234/v1"  # openai_compatible only; LM Studio's default
    model: str | None = None  # openai_compatible only: the model ID the server lists
    api_key_env: str | None = None  # openai_compatible only: the NAME of an environment variable that holds a key, if the server wants one
    rpm: int = Field(600, gt=0)  # openai_compatible only

    @model_validator(mode="after")
    def _a_compatible_server_needs_a_model(self) -> "LlmBackend":
        if self.provider == "openai_compatible" and not (self.model or "").strip():
            raise ValueError("an openai_compatible backend needs `model`, the model ID the server lists")
        return self


def _default_environments() -> dict[str, LlmBackend]:
    return {
        **{name: LlmBackend() for name in ENVIRONMENTS if name != "local"},
        "local": LlmBackend(provider="openai_compatible", model=LOCAL_MODEL),
    }


class LlmConfig(_Section):
    """Plan 04 owns these keys and may add more."""

    pro_enabled: bool = False
    models: LlmModels = LlmModels()
    default_rpm: int = Field(10, gt=0)
    rate_limits: dict[str, RateLimit] = {}  # per model ID, overrides default_rpm
    retry: LlmRetry = LlmRetry()
    breaker: LlmBreaker = LlmBreaker()
    request_timeout_s: float = Field(30.0, gt=0)  # one attempt; the turn's remaining time wins if it is less (impl)
    steps: dict[str, StepSettings] = Field(default_factory=_default_steps)  # a step you leave out keeps its default
    environments: dict[str, LlmBackend] = Field(default_factory=_default_environments)  # an environment you leave out keeps its default
    pricing_usd_per_1m: dict[str, ModelPrice] | None = None  # plan 02: null until filled, we never invent prices

    @field_validator("environments", mode="before")
    @classmethod
    def _fill_in_environment_defaults(cls, given: object) -> object:
        """Like the steps: an environment you leave out keeps its backend, and a setting you leave out of one you list keeps its default."""
        if not isinstance(given, dict):
            return given
        unknown = sorted(str(name) for name in set(given) - set(ENVIRONMENTS))
        if unknown:
            raise ValueError(f"unknown environment(s) {', '.join(unknown)}; the environments are {', '.join(ENVIRONMENTS)}")
        merged: dict[str, object] = {name: backend.model_dump() for name, backend in _default_environments().items()}
        for name, value in given.items():
            if isinstance(value, LlmBackend):
                value = value.model_dump(exclude_unset=True)
            merged[name] = {**merged[name], **value} if isinstance(value, dict) else value
        return merged

    @field_validator("steps", mode="before")
    @classmethod
    def _fill_in_step_defaults(cls, given: object) -> object:
        """A step you leave out keeps its defaults, and so does a setting you leave out of a step you do list:
        `composer: {temperature: 0.5}` keeps the Composer's own token cap, not a generic one."""
        if not isinstance(given, dict):
            return given  # the normal validation reports it
        unknown = sorted(str(name) for name in set(given) - set(LLM_STEP_NAMES))
        if unknown:
            raise ValueError(f"unknown step(s) {', '.join(unknown)}; the steps are {', '.join(LLM_STEP_NAMES)}")
        merged: dict[str, object] = {name: settings.model_dump() for name, settings in _default_steps().items()}
        for name, value in given.items():
            if isinstance(value, StepSettings):
                value = value.model_dump(exclude_unset=True)  # only what was set, so its generic defaults do not win
            merged[name] = {**merged[name], **value} if isinstance(value, dict) else value
        return merged


class DeleteConfig(_Section):
    large_batch_threshold: int = Field(10, ge=1)
    undo_days: int = Field(30, ge=1)  # NFR17


class RetentionConfig(_Section):
    audit_days: int = Field(365, ge=1)  # NFR21
    checkpoint_days: int = Field(90, ge=1)
    trace_days: int = Field(90, ge=1)  # [NOT SPECIFIED], Q25 default


class ObservabilityConfig(_Section):
    metrics_file: str | None = None  # None: runs.jsonl inside app.data_dir
    traces_dir: str | None = None  # None: traces/ inside app.data_dir


class GoldenConfig(_Section):
    enabled: bool = False
    top_k: int = Field(3, ge=1)
    min_similarity: float = Field(0.15, ge=0, le=1)  # chosen on the seed set, see tests/test_golden_index.py
    seed_dir: str = "golden/seed"  # one trio per YAML file (plan 10)
    glossary_file: str = "glossary.yaml"


class PersonaConfig(_Section):
    enabled: bool = True  # decision 3.9: the one static persona reaches the Composer every turn
    default: str = "default"


class Secrets(BaseModel):
    """Read from the environment only. Never printed, never in YAML."""

    model_config = ConfigDict(frozen=True)

    # repr=False and exclude=True: they never show in a repr or in a model_dump/JSON that might be logged.
    gemini_api_key: SecretStr | None = Field(None, repr=False, exclude=True)
    google_cloud_project: str | None = Field(None, repr=False, exclude=True)
    google_application_credentials: str | None = Field(None, repr=False, exclude=True)

    @classmethod
    def from_env(cls) -> "Secrets":
        def get(name: str) -> str | None:
            return os.environ.get(name, "").strip() or None

        return cls(
            gemini_api_key=get("GEMINI_API_KEY"),
            google_cloud_project=get("GOOGLE_CLOUD_PROJECT"),
            google_application_credentials=get("GOOGLE_APPLICATION_CREDENTIALS"),
        )


class Settings(_Section):
    app: AppConfig = AppConfig()
    bigquery: BigQueryConfig = BigQueryConfig()
    guards: GuardsConfig = GuardsConfig()
    loop: LoopConfig = LoopConfig()
    composer: ComposerConfig = ComposerConfig()
    budget: BudgetConfig = BudgetConfig()
    llm: LlmConfig = LlmConfig()
    delete: DeleteConfig = DeleteConfig()
    retention: RetentionConfig = RetentionConfig()
    observability: ObservabilityConfig = ObservabilityConfig()
    golden: GoldenConfig = GoldenConfig()
    persona: PersonaConfig = PersonaConfig()

    # Not YAML keys: set by `load_settings`.
    secrets: Secrets = Field(default_factory=Secrets, repr=False, exclude=True)
    base_dir: Path = Field(default=Path("."), repr=False)  # relative paths resolve against this
    config_path: Path | None = Field(default=None, repr=False)

    @property
    def llm_backend(self) -> LlmBackend:
        """The model service of this run's environment (`app.environment`, or OPSFLEET_ENV)."""
        return self.llm.environments[self.app.environment]

    def resolve(self, relative: str | Path) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else self.base_dir / path

    @property
    def data_path(self) -> Path:
        return self.resolve(self.app.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_path / "db.sqlite3"

    @property
    def log_path(self) -> Path:
        return self.data_path / "logs" / "app.log"

    @property
    def metrics_path(self) -> Path:
        configured = self.observability.metrics_file
        return self.resolve(configured) if configured else self.data_path / "runs.jsonl"

    @property
    def traces_path(self) -> Path:
        configured = self.observability.traces_dir
        return self.resolve(configured) if configured else self.data_path / "traces"

    @property
    def gcp_project(self) -> str | None:
        """Project BigQuery jobs run in: the config value, else GOOGLE_CLOUD_PROJECT."""
        return self.bigquery.project_id or self.secrets.google_cloud_project


def find_config_path(explicit: str | Path | None = None) -> Path:
    """`--config`, else `OPSFLEET_CONFIG`, else `./config.yaml`."""
    return Path(explicit or os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_NAME)


def _load_env_file(path: Path) -> None:
    """Fill in variables from a .env file. Real environment variables always win (no override)."""
    try:
        load_dotenv(path, override=False)
    except (OSError, ValueError) as exc:  # ValueError includes UnicodeDecodeError
        raise ConfigError(
            f"The .env file could not be read: {path} ({exc.__class__.__name__})",
            fix="Save it as UTF-8 text, or delete it and copy .env.example again.",
        ) from None


def load_settings(config_path: str | Path | None = None, *, load_env_file: bool = True) -> Settings:
    """Load and validate settings. Raises `ConfigError` with file, line and key on a problem."""
    if load_env_file and not (config_path or os.environ.get(CONFIG_ENV_VAR)):
        # Default location: the working folder is the project folder, and its .env may set OPSFLEET_CONFIG.
        _load_env_file(Path.cwd() / ".env")
    path = find_config_path(config_path)
    if not path.is_file():
        raise ConfigError(
            f"The config file was not found: {path}",
            fix=f"Run from the project folder, or pass --config PATH, or set {CONFIG_ENV_VAR}.",
        )
    if load_env_file:
        # Only the .env next to the config file. One from whatever folder you happen to be in could
        # point BigQuery jobs (and their bill) at another project.
        _load_env_file(path.resolve().parent / ".env")

    data = load_yaml(path, "config file")
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: the file must be a mapping of settings.")
    for reserved in ("secrets", "base_dir", "config_path"):
        if reserved in data:
            hint = "Put secrets in .env or the environment, never in YAML." if reserved == "secrets" else ""
            raise ConfigError(f"{path.name}: '{reserved}' is not a config key. {hint}".strip())

    override = os.environ.get(ENVIRONMENT_ENV_VAR, "").strip()
    if override:
        if override not in ENVIRONMENTS:
            raise ConfigError(
                f"{ENVIRONMENT_ENV_VAR} is '{override}', which is not an environment.",
                fix=f"Use one of: {', '.join(ENVIRONMENTS)}, or unset {ENVIRONMENT_ENV_VAR}.",
            )
        if data.get("app") is None or isinstance(data.get("app"), dict):
            data = {**data, "app": {**(data.get("app") or {}), "environment": override}}
    try:
        return Settings.model_validate(
            {**data, "secrets": Secrets.from_env(), "base_dir": path.resolve().parent, "config_path": path}
        )
    except ValidationError as exc:
        raise ConfigError(validation_message(path, exc), fix=f"Fix the setting in {path}.") from None
