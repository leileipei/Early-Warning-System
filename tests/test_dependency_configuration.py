from __future__ import annotations

from pathlib import Path
import tomllib

from packaging.requirements import Requirement
from packaging.version import Version
import yaml


ROOT = Path(__file__).resolve().parents[1]


def _locked_requirements(filename: str) -> dict[str, Requirement]:
    requirements: dict[str, Requirement] = {}
    for line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", " ", "-")):
            continue
        requirement = Requirement(line.split(" \\ ")[0])
        requirements[requirement.name.lower()] = requirement
    return requirements


def _pinned_version(requirement: Requirement) -> Version:
    version = next(
        specifier.version for specifier in requirement.specifier if specifier.operator == "=="
    )
    return Version(version)


def test_dependency_locks_and_automation_configuration_are_release_ready():
    with (ROOT / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)

    dev_dependencies = project["project"]["optional-dependencies"]["dev"]
    assert "pip-audit>=2.7" in dev_dependencies
    assert "pip-tools>=7.4" in dev_dependencies
    assert project["project"]["requires-python"] == ">=3.11,<3.14"

    production = _locked_requirements("requirements.lock")
    development = _locked_requirements("requirements-dev.lock")
    assert "pytest" not in production
    assert "ruff" not in production
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
    assert "pytest --cov=app --cov-report=term-missing --cov-fail-under=93" in commands
    assert "pip-audit -r requirements.lock --strict" in commands

    dependabot = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    updates = {entry["package-ecosystem"]: entry for entry in dependabot["updates"]}
    assert set(updates) == {"pip", "github-actions"}
    assert all(entry["schedule"]["interval"] == "weekly" for entry in updates.values())
    assert all(entry["open-pull-requests-limit"] == 5 for entry in updates.values())
