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


def test_customer_install_declares_every_module_scope_third_party_import():
    """`pip install brevitas-systems` must resolve everything the SDK imports eagerly.

    brevitas/security/envelope.py, brevitas/security/kms.py and brevitas/semantic_cache.py
    import cryptography at module scope. It used to reach only the server image, via
    api/requirements.txt, and brevitas/proxy.py swallows the resulting ImportError, so a
    customer install could lose the local cache with no error surfaced anywhere.
    """
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = project["project"]["dependencies"]
    names = {requirement.split(">=")[0].split("==")[0].split("[")[0] for requirement in declared}

    eager_importers = (
        "brevitas/security/envelope.py",
        "brevitas/security/kms.py",
        "brevitas/semantic_cache.py",
    )
    for path in eager_importers:
        source = (ROOT / path).read_text()
        assert "\nfrom cryptography" in source or "\nimport cryptography" in source, path
    assert "cryptography" in names

    # The declared floor must not exceed what the server image already installs.
    api_floor = next(line for line in (ROOT / "api" / "requirements.txt").read_text().splitlines()
                     if line.startswith("cryptography"))
    sdk_floor = next(line for line in declared if line.startswith("cryptography"))
    assert sdk_floor == api_floor


def test_optimizer_distribution_does_not_shadow_bvx_manager_cli():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    scripts = project["project"]["scripts"]
    assert scripts["brevitas"] == "brevitas.cli:main"
    assert "bvx" not in scripts
