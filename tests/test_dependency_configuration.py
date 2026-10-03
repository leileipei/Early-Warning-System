from __future__ import annotations

from pathlib import Path
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
import yaml


ROOT = Path(__file__).resolve().parents[1]


def _locked_requirements(filename: str) -> dict[str, Requirement]:
    requirements: dict[str, Requirement] = {}
    for line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", " ", "-")):
            continue
        requirement = Requirement(line.split(" \\ ")[0])
        requirements[canonicalize_name(requirement.name)] = requirement
    return requirements


def _pinned_version(requirement: Requirement) -> Version:
    version = next(
        specifier.version for specifier in requirement.specifier if specifier.operator == "=="
    )
    return Version(version)


def test_cryptography_constraints_exclude_known_vulnerable_releases():
    with (ROOT / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)
    cryptography = next(
        requirement
        for entry in project["project"]["dependencies"]
        if canonicalize_name((requirement := Requirement(entry)).name) == "cryptography"
    )

    assert Version("49.0.0") not in cryptography.specifier
    production_version = _pinned_version(_locked_requirements("requirements.lock")["cryptography"])
    development_version = _pinned_version(
        _locked_requirements("requirements-dev.lock")["cryptography"]
    )
    assert production_version >= Version("50.0.2")
    assert development_version == production_version
    assert production_version in cryptography.specifier


def test_development_dependencies_exclude_known_vulnerable_http_clients():
    with (ROOT / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)
    constraints = {
        canonicalize_name(requirement.name): requirement
        for entry in project["project"]["optional-dependencies"]["dev"]
        for requirement in [Requirement(entry)]
    }
    assert Version("2.7.0") not in constraints["httpx2"].specifier
    assert "urllib3" in constraints
    assert Version("2.7.0") not in constraints["urllib3"].specifier

    production = _locked_requirements("requirements.lock")
    development = _locked_requirements("requirements-dev.lock")
    for name, minimum in (("httpx2", "2.12.0"), ("httpcore2", "2.10.0"), ("urllib3", "2.8.0")):
        assert name not in production
        assert _pinned_version(development[name]) >= Version(minimum)


def test_dependency_locks_and_automation_configuration_are_release_ready():
    with (ROOT / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)

    dev_dependencies = project["project"]["optional-dependencies"]["dev"]
    assert "pip-audit>=2.7" in dev_dependencies
    assert "pip-tools>=7.4" in dev_dependencies
    assert project["project"]["requires-python"] == ">=3.11,<3.14"

    production = _locked_requirements("requirements.lock")
    development = _locked_requirements("requirements-dev.lock")
    assert not production.keys() & {"pytest", "ruff", "pip-audit", "pip-tools"}
    assert {"pip-audit", "pip-tools", "pytest", "ruff"} <= development.keys()
    assert "passlib" not in production | development
    assert _pinned_version(production["bcrypt"]) >= Version("4")
    assert _pinned_version(development["bcrypt"]) >= Version("4")

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    matrix = workflow["jobs"]["test"]["strategy"]["matrix"]
    assert matrix["python-version"] == ["3.11", "3.12", "3.13"]
    setup_python = next(
        step for step in workflow["jobs"]["test"]["steps"] if step["uses"] == "actions/setup-python@v5"
    )
    assert setup_python["with"]["cache"] == "pip"
    assert setup_python["with"]["cache-dependency-path"] == "requirements-dev.lock"
    commands = [step.get("run", "") for step in workflow["jobs"]["test"]["steps"]]
    assert "python -m pip install -r requirements-dev.lock" in commands
    assert "python -m pip check" in commands
    assert "ruff check ." in commands
    assert "python -m pytest --cov=app --cov-report=term-missing --cov-fail-under=93" in commands
    assert "pip-audit -r requirements.lock --strict" in commands
    assert "pip-audit -r requirements-dev.lock --strict" in commands

    dependabot = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    updates = {entry["package-ecosystem"]: entry for entry in dependabot["updates"]}
    assert set(updates) == {"pip", "github-actions"}
    assert all(entry["schedule"]["interval"] == "weekly" for entry in updates.values())
    assert all(entry["open-pull-requests-limit"] == 5 for entry in updates.values())
