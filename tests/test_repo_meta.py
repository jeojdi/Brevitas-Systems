import pytest

from api.auth import hash_key
from api.store import UsageStore


def _store(tmp_path, name, organization_id="org-1"):
    store = UsageStore(str(tmp_path / f"{name}.db"))
    store.create_key(hash_key(name), name, organization_id=organization_id)
    return store, hash_key(name)


def test_add_and_list(tmp_path):
    store, kh = _store(tmp_path, "k")
    assert store.repo_meta_list(kh) == []
    row = store.repo_meta_upsert(kh, "acme/api", "Acme API", added=True)
    assert row == {"repo": "acme/api", "display_name": "Acme API", "added": True}
    assert store.repo_meta_list(kh) == [
        {"repo": "acme/api", "display_name": "Acme API", "added": True}
    ]


def test_rename_preserves_added_flag(tmp_path):
    store, kh = _store(tmp_path, "k")
    store.repo_meta_upsert(kh, "acme/api", "Acme API", added=True)
    # A later rename (added=False) must never clear the flag on a pre-added repo.
    row = store.repo_meta_upsert(kh, "acme/api", "Renamed", added=False)
    assert row["display_name"] == "Renamed"
    assert row["added"] is True


def test_rename_discovered_repo_stays_not_added(tmp_path):
    store, kh = _store(tmp_path, "k")
    row = store.repo_meta_upsert(kh, "discovered/repo", "Nice Name", added=False)
    assert row["display_name"] == "Nice Name"
    assert row["added"] is False


def test_delete(tmp_path):
    store, kh = _store(tmp_path, "k")
    store.repo_meta_upsert(kh, "acme/api", "Acme API", added=True)
    store.repo_meta_delete(kh, "acme/api")
    assert store.repo_meta_list(kh) == []


def test_org_isolation(tmp_path):
    store = UsageStore(str(tmp_path / "shared.db"))
    store.create_key(hash_key("a"), "a", organization_id="org-a")
    store.create_key(hash_key("b"), "b", organization_id="org-b")
    store.repo_meta_upsert(hash_key("a"), "a/repo", "A", added=True)
    assert [row["repo"] for row in store.repo_meta_list(hash_key("a"))] == ["a/repo"]
    # A key in a different org sees none of org-a's aliases.
    assert store.repo_meta_list(hash_key("b")) == []


def test_no_org_key_is_noop(tmp_path):
    store = UsageStore(str(tmp_path / "noorg.db"))
    store.create_key(hash_key("legacy"), "legacy")  # no organization_id bound
    assert store.repo_meta_list(hash_key("legacy")) == []
    with pytest.raises(ValueError):
        store.repo_meta_upsert(hash_key("legacy"), "x", "X", added=True)
