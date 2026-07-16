"""Release metadata used by the bvx-installed Python model."""

import tomllib
from pathlib import Path

import brevitas


ROOT = Path(__file__).resolve().parents[1]


def test_package_and_runtime_versions_match():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["version"] == brevitas.__version__


def test_bvx_base_model_includes_retrieval_runtime():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dependencies = project["project"]["dependencies"]
    assert any(dependency.startswith("fastembed") for dependency in dependencies)
