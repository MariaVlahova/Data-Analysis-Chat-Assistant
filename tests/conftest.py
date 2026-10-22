"""Shared fixtures. Tests use temporary project folders and never touch the real users.yaml or data/."""
from __future__ import annotations

from pathlib import Path

import pytest

USERS_YAML = """\
users:
  alice: {name: Alice, role: brand_manager, brands: ["Brand A", "Brand B"]}
  bob:   {name: Bob,   role: brand_manager, brands: ["Brand C"]}
  ceo:   {name: CEO,   role: ceo, all_brands: true}
  nobrands: {name: No Brands, role: brand_manager, brands: []}
"""

SECRET_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_CLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS", "OPSFLEET_CONFIG")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real keys or config paths leak into a test."""
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def users_yaml() -> str:
    return USERS_YAML


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary project folder with a minimal config.yaml and a users.yaml. The cwd is moved into it,
    so no developer .env file is picked up."""
    (tmp_path / "config.yaml").write_text("app:\n  language: en\n", encoding="utf-8")
    (tmp_path / "users.yaml").write_text(USERS_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def config_path(project: Path) -> Path:
    return project / "config.yaml"
