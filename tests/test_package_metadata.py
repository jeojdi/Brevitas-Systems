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


def test_google_cloud_kms_runtime_is_pinned_in_package_and_release_locks():
    expected = {"google-cloud-kms==3.15.0", "google-crc32c==1.8.0"}
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert expected <= set(project["project"]["dependencies"])

    api_requirements = (ROOT / "api" / "requirements.txt").read_text().splitlines()
    assert expected <= set(api_requirements)
    for lock_name in ("python-runtime.lock", "python-test.lock"):
        lock = (ROOT / "scripts" / "ci" / lock_name).read_text()
        for requirement in expected:
            assert requirement in lock


def test_optimizer_distribution_does_not_shadow_bvx_manager_cli():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    scripts = project["project"]["scripts"]
    assert scripts["brevitas"] == "brevitas.cli:main"
    assert "bvx" not in scripts
