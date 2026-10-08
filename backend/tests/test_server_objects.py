"""Erasing a tenant's evidence files from the object stores (account deletion, §52)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backoffice.evidence.store import LocalObjectStore, S3ObjectStore, StorageConfigError


def test_local_store_purges_one_tenant_only(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    a1 = store.put_immutable(b"invoice one", "tenant-a", "text/plain")
    store.put_immutable(b"invoice two", "tenant-a", "text/plain")
    b1 = store.put_immutable(b"invoice one", "tenant-b", "text/plain")
    assert store.purge_tenant("tenant-a") == 2
    assert not store.exists(a1) and store.exists(b1)
    assert store.purge_tenant("tenant-a") == 0  # nothing left: still fine
    with pytest.raises(ValueError):
        store.purge_tenant("../tenant-b")


class FakeS3:
    def __init__(self, versions: list[dict[str, str]], errors: list[dict[str, str]] | None = None) -> None:
        self.versions = versions
        self.errors = errors or []
        self.deleted: list[dict[str, Any]] = []

    def get_paginator(self, name: str) -> Any:
        assert name == "list_object_versions"
        fake = self

        class Paginator:
            def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
                prefix = kwargs["Prefix"]
                found = [v for v in fake.versions if v["Key"].startswith(prefix)]
                return [{"Versions": found[:1]}, {"Versions": found[1:], "DeleteMarkers": []}]

        return Paginator()

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        self.deleted.append(kwargs)
        return {"Errors": self.errors}


def test_s3_store_deletes_every_version_under_the_tenant_prefix() -> None:
    versions = [{"Key": "evidence/tenant-a/sha256/aa/" + "a" * 64, "VersionId": "v1"},
                {"Key": "evidence/tenant-a/sha256/aa/" + "a" * 64, "VersionId": "v2"},
                {"Key": "evidence/tenant-b/sha256/bb/" + "b" * 64, "VersionId": "v3"}]
    client = FakeS3(versions)
    store = S3ObjectStore("bucket", prefix="evidence", client=client)
    assert store.purge_tenant("tenant-a", bypass_governance=True) == 2
    keys = [o["VersionId"] for call in client.deleted for o in call["Delete"]["Objects"]]
    assert keys == ["v1", "v2"] and all(c["BypassGovernanceRetention"] for c in client.deleted)


def test_s3_refusal_is_reported_not_hidden() -> None:
    client = FakeS3([{"Key": "tenant-a/sha256/aa/" + "a" * 64, "VersionId": "v1"}],
                    errors=[{"Code": "AccessDenied", "Key": "k"}])
    store = S3ObjectStore("bucket", client=client)
    with pytest.raises(StorageConfigError, match="AccessDenied"):
        store.purge_tenant("tenant-a")
    assert "BypassGovernanceRetention" not in client.deleted[0]
