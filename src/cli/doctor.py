"""`doctor`: one pass/fail line per setup check, with the fix for each failure (NFR28, Q14).

Plan 03 adds BigQuery checks and plan 04 a Gemini ping (only with `--live`): each registers its
checks with `register_check`.
"""
from __future__ import annotations

import importlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from rich.text import Text

from src.access.users import load_user_directory, users_path
from src.cli.render import Renderer
from src.config import Secrets, Settings
from src.errors import AgentError, ConfigError
from src.storage.db import Database

Status = Literal["ok", "warn", "fail", "skip"]
MIN_PYTHON = (3, 11)

# import name, pip name
REQUIRED_MODULES = [
    ("typer", "typer"),
    ("rich", "rich"),
    ("pydantic", "pydantic"),
    ("yaml", "PyYAML"),
    ("dotenv", "python-dotenv"),
    ("google.cloud.bigquery", "google-cloud-bigquery"),
    ("db_dtypes", "db-dtypes"),
    ("pandas", "pandas"),
    ("pyarrow", "pyarrow"),
    ("sqlglot", "sqlglot"),
    ("google.genai", "google-genai"),
    ("langgraph", "langgraph"),
    ("langgraph.checkpoint.sqlite", "langgraph-checkpoint-sqlite"),
    ("pytest", "pytest"),
]

PLACEHOLDER_BRAND = re.compile(r"^<verified brand")


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str = ""
    fix: str = ""


@dataclass
class DoctorContext:
    settings: Settings | None
    config_error: ConfigError | None = None
    adc_project: str | None = None  # project found in the default credentials, set by the credentials check


CheckFn = Callable[[DoctorContext], Check]
CHECKS: list[CheckFn] = []


def register_check(fn: CheckFn) -> CheckFn:
    CHECKS.append(fn)
    return fn


def _needs_config(name: str) -> Check:
    return Check(name, "skip", "skipped because the config file did not load")


@register_check
def check_python(ctx: DoctorContext) -> Check:
    version = ".".join(str(p) for p in sys.version_info[:3])
    if sys.version_info[:2] >= MIN_PYTHON:
        return Check("Python version", "ok", version)
    return Check("Python version", "fail", f"{version}, need {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer",
                 "Install a newer Python and recreate the virtual environment.")


@register_check
def check_packages(ctx: DoctorContext) -> Check:
    missing = []
    for module, pip_name in REQUIRED_MODULES:
        try:
            importlib.import_module(module)
        except Exception:  # noqa: BLE001 - a broken install can fail in many ways
            missing.append(pip_name)
    if missing:
        return Check("Packages", "fail", "cannot import: " + ", ".join(missing), "Run: pip install -r requirements.txt")
    return Check("Packages", "ok", f"all {len(REQUIRED_MODULES)} import")


@register_check
def check_config(ctx: DoctorContext) -> Check:
    if ctx.settings is None:
        err = ctx.config_error
        return Check("config.yaml", "fail", err.message if err else "did not load", (err.fix if err else "") or "")
    return Check("config.yaml", "ok", str(ctx.settings.base_dir / ctx.settings.config_path.name))


@register_check
def check_users(ctx: DoctorContext) -> Check:
    name = "users.yaml"
    if ctx.settings is None:
        return _needs_config(name)
    path = users_path(ctx.settings)
    try:
        users = load_user_directory(path, salt="doctor")  # fixed salt: the check must not write files
    except AgentError as exc:
        return Check(name, "fail", exc.detail or exc.user_message, f"Create {path} (see the README).")
    except ConfigError as exc:
        return Check(name, "fail", exc.message, exc.fix or "")

    notes = []
    no_brands = [u for u in users.names if users.get(u).scope_mode == "none"]
    if no_brands:
        notes.append(f"no brands for: {', '.join(no_brands)} (the chat refuses to start for them)")
    placeholders = [u for u in users.names if any(PLACEHOLDER_BRAND.match(b) for b in users.get(u).brands)]
    if placeholders:
        notes.append(f"placeholder brand names for: {', '.join(placeholders)}")
    detail = f"{len(users.names)} users ({', '.join(users.names)})"
    if notes:
        return Check(name, "warn", detail + "; " + "; ".join(notes),
                     "Give each brand manager real brand names. Plan 03 checks them against BigQuery.")
    return Check(name, "ok", detail)


@register_check
def check_gemini_key(ctx: DoctorContext) -> Check:
    if Secrets.from_env().gemini_api_key is not None:  # never print the key
        return Check("GEMINI_API_KEY", "ok", "is set")
    return Check("GEMINI_API_KEY", "fail", "is not set",
                 "Create a key in Google AI Studio and put GEMINI_API_KEY=... in .env (see .env.example).")


@register_check
def check_google_credentials(ctx: DoctorContext) -> Check:
    name = "Google Cloud credentials"
    key_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if key_file and not Path(key_file).is_file():
        return Check(name, "fail", "GOOGLE_APPLICATION_CREDENTIALS points to a file that does not exist",
                     "Fix the path in .env, or clear it and run: gcloud auth application-default login")
    previous = os.environ.get("NO_GCE_CHECK")
    os.environ["NO_GCE_CHECK"] = "true"  # skip the slow probe of a cloud metadata server on a laptop
    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError

        try:
            _, ctx.adc_project = google.auth.default()
        except DefaultCredentialsError:
            return Check(name, "fail", "no credentials found", "Run: gcloud auth application-default login")
    finally:
        if previous is None:
            os.environ.pop("NO_GCE_CHECK", None)
        else:
            os.environ["NO_GCE_CHECK"] = previous
    return Check(name, "ok", "found")


@register_check
def check_google_project(ctx: DoctorContext) -> Check:
    name = "Google Cloud project"
    configured = ctx.settings.gcp_project if ctx.settings else Secrets.from_env().google_cloud_project
    project = configured or ctx.adc_project
    if project:
        return Check(name, "ok", project)
    return Check(name, "fail", "GOOGLE_CLOUD_PROJECT is not set",
                 "Put GOOGLE_CLOUD_PROJECT=your-project-id in .env. BigQuery jobs run (and are billed) in that project.")


@register_check
def check_database(ctx: DoctorContext) -> Check:
    name = "SQLite database"
    if ctx.settings is None:
        return _needs_config(name)
    db = Database.from_settings(ctx.settings)
    try:
        applied = db.migrate()
    except ConfigError as exc:
        return Check(name, "fail", exc.message, exc.fix or "")
    finally:
        db.close()
    return Check(name, "ok", f"{ctx.settings.db_path} (applied {len(applied)} new migration(s))" if applied
                 else f"{ctx.settings.db_path} (up to date)")


def run_checks(ctx: DoctorContext) -> list[Check]:
    results = []
    for fn in CHECKS:
        try:
            results.append(fn(ctx))
        except Exception as exc:  # noqa: BLE001 - a crashing check is itself a failed check
            results.append(Check(fn.__name__.removeprefix("check_"), "fail", f"the check crashed: {type(exc).__name__}"))
    return results


_MARKS: dict[Status, tuple[str, str]] = {
    "ok": ("[ ok ]", "green"),
    "warn": ("[warn]", "yellow"),
    "fail": ("[FAIL]", "bold red"),
    "skip": ("[skip]", "dim"),
}


def render_checks(renderer: Renderer, checks: list[Check]) -> None:
    for check in checks:
        mark, style = _MARKS[check.status]
        line = Text()
        line.append(mark, style=style)
        line.append(f" {check.name}")
        if check.detail:
            line.append(f": {check.detail}", style="dim")
        renderer.console.print(line)
        if check.fix and check.status in ("fail", "warn"):
            renderer.console.print(Text(f"       Fix: {check.fix}"))
    failed = sum(c.status == "fail" for c in checks)
    warned = sum(c.status == "warn" for c in checks)
    summary = "All checks passed." if not (failed or warned) else f"{failed} failed, {warned} warning(s)."
    renderer.console.print(Text(summary, style="bold green" if not failed else "bold red"))
