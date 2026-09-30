"""Account erasure completes in AWS: the sync worker deletes every file version as the deletion role (§25, §52).

A fake S3 enforces the bucket policy (only the evidence-deletion role may delete an original version,
GOVERNANCE retention needs the bypass) and a fake STS hands out that role's credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _server_support import PASSWORD, bearer, harness, signup

from backoffice.evidence.store import LocalObjectStore, S3ObjectStore, StorageConfigError
from backoffice.server.config import ServerConfig
from backoffice.server.erasure import SESSION_SECONDS, ErasurePurger, purger_from_config
from backoffice.server.sync import SyncWorker

ROLE = "arn:aws:iam::123456789012:role/backoffice-production-evidence-deletion"
ROLE_KEY = "ASIAROLEDELETION"  # the access key STS issues for the role's session


class World:
    """Two versioned buckets (evidence and its disaster-recovery copy) and every delete call."""

    def __init__(self) -> None:
        self.buckets: dict[str, list[dict[str, Any]]] = {"evidence": [], "evidence-replica": []}
        self.deletes: list[tuple[str, str, str, int, bool]] = []  # principal, region, bucket, count, bypass

    def add(self, bucket: str, key: str, version: str, *, locked: bool = True, marker: bool = False) -> None:
        self.buckets[bucket].append({"Key": key, "VersionId": version, "locked": locked, "marker": marker})

    def keys(self, bucket: str, prefix: str) -> list[str]:
        return sorted(f"{v['Key']}@{v['VersionId']}" for v in self.buckets[bucket] if v["Key"].startswith(prefix))


class FakeS3:
    def __init__(self, world: World, principal: str, region: str = "eu-south-2") -> None:
        self.world, self.principal, self.region = world, principal, region

    def get_paginator(self, name: str) -> FakeS3:
        assert name == "list_object_versions"
        return self

    def paginate(self, Bucket: str, Prefix: str) -> Any:  # noqa: N803 (boto3 names)
        items = [dict(v) for v in self.world.buckets[Bucket] if v["Key"].startswith(Prefix)]
        for start in range(0, max(len(items), 1), 2):  # small pages, like a long listing
            chunk = items[start:start + 2]
            yield {"Versions": [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in chunk if not v["marker"]],
                   "DeleteMarkers": [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in chunk if v["marker"]]}

    def delete_objects(self, Bucket: str, Delete: dict[str, Any],  # noqa: N803
                       BypassGovernanceRetention: bool = False) -> dict[str, Any]:  # noqa: N803
        self.world.deletes.append((self.principal, self.region, Bucket, len(Delete["Objects"]),
                                   BypassGovernanceRetention))
        errors = []
        for o in Delete["Objects"]:
            found = next(v for v in self.world.buckets[Bucket]
                         if v["Key"] == o["Key"] and v["VersionId"] == o["VersionId"])
            if self.principal != ROLE_KEY:  # bucket policy: OnlyTheDeletionWorkflowBypassesRetention
                errors.append({"Key": o["Key"], "Code": "AccessDenied"})
            elif found["locked"] and not found["marker"] and not BypassGovernanceRetention:
                errors.append({"Key": o["Key"], "Code": "AccessDenied"})
            else:
                self.world.buckets[Bucket].remove(found)
        return {"Errors": errors} if errors else {}


class FakeSTS:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.refuse = False

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.refuse:
            raise PermissionError("not authorized to perform sts:AssumeRole")
        return {"Credentials": {"AccessKeyId": ROLE_KEY, "SecretAccessKey": "secret", "SessionToken": "token"}}


def _aws(tmp_path: Path) -> tuple[Any, World, FakeSTS, ErasurePurger]:
    world, sts = World(), FakeSTS()
    objects = S3ObjectStore("evidence", region="eu-south-2", client=FakeS3(world, "ASIAAPITASK"))
    purger = ErasurePurger(objects, role_arn=ROLE, replica_bucket="evidence-replica", replica_region="eu-west-3",
                           sts_client=sts,
                           s3_client_factory=lambda creds, region: FakeS3(world, creds["aws_access_key_id"], region))
    h = harness(tmp_path, objects=objects, purger=purger)
    return h, world, sts, purger


def _files(world: World, tenant: str) -> None:
    for bucket in ("evidence", "evidence-replica"):
        world.add(bucket, f"{tenant}/sha256/aa/{'a' * 64}", "v1")
        world.add(bucket, f"{tenant}/sha256/aa/{'a' * 64}", "v2")
        world.add(bucket, f"{tenant}/sha256/bb/{'b' * 64}", "v1", locked=False)
        world.add(bucket, f"{tenant}/sha256/bb/{'b' * 64}", "v9", marker=True)
        world.add(bucket, f"{tenant}/sha256/cc/{'c' * 64}", "v1")


def test_an_erasure_in_aws_is_completed_by_the_sync_worker_as_the_deletion_role(tmp_path: Path) -> None:
    h, world, sts, purger = _aws(tmp_path)
    gone = signup(h.client)
    kept = signup(h.client, "rui@oficina.pt", company="Oficina Rui", tax_id=None)
    tenant, other = gone["tenant"]["id"], kept["tenant"]["id"]
    _files(world, tenant)
    _files(world, other)

    res = h.client.post("/api/account/delete", json={"confirm": "DELETE", "password": PASSWORD},
                        headers=bearer(gone["token"]))
    assert res.status_code == 202
    # The api may not delete originals: it leaves the files to the worker, and says so in the record.
    assert world.deletes == [] and sts.calls == []
    assert h.store.erasure(tenant).completed_at is not None and h.store.erasure(tenant).objects_purged_at is None
    assert h.store.pending_erasures() == [tenant]

    report = SyncWorker(h.manager, purger=purger).run_once()
    assert report.erasures == [tenant] and report.errors == 0
    for bucket in ("evidence", "evidence-replica"):
        assert world.keys(bucket, f"{tenant}/") == []  # every version and delete marker, in both copies
        assert len(world.keys(bucket, f"{other}/")) == 5  # nobody else's
    assert {(p, r, b, bypass) for p, r, b, _, bypass in world.deletes} == {
        (ROLE_KEY, "eu-south-2", "evidence", True), (ROLE_KEY, "eu-west-3", "evidence-replica", True)}
    [call] = sts.calls
    assert call["RoleArn"] == ROLE and call["DurationSeconds"] == SESSION_SECONDS == 900
    assert call["RoleSessionName"] == f"erasure-{tenant}"[:64]
    assert h.store.erasure(tenant).objects_purged_at is not None and h.store.pending_erasures() == []
    assert SyncWorker(h.manager, purger=purger).run_once().erasures == []  # done once


def test_the_api_role_cannot_delete_originals(tmp_path: Path) -> None:
    world = World()
    world.add("evidence", "t1/sha256/aa/" + "a" * 64, "v1")
    store = S3ObjectStore("evidence", region="eu-south-2", client=FakeS3(world, "ASIAAPITASK"))
    with pytest.raises(StorageConfigError):
        store.purge_tenant("t1", bypass_governance=True)
    assert world.keys("evidence", "t1/") == ["t1/sha256/aa/" + "a" * 64 + "@v1"]


def test_a_refused_role_leaves_the_erasure_pending_and_it_is_retried(tmp_path: Path) -> None:
    h, world, sts, purger = _aws(tmp_path)
    account = signup(h.client)
    tenant = account["tenant"]["id"]
    _files(world, tenant)
    h.client.post("/api/account/delete", json={"confirm": "DELETE", "password": PASSWORD},
                  headers=bearer(account["token"]))
    sts.refuse = True
    worker = SyncWorker(h.manager, purger=purger)
    report = worker.run_once()
    assert report.erasures == [] and report.errors == 1 and h.store.pending_erasures() == [tenant]
    assert worker.run_once().errors == 0 and len(sts.calls) == 1  # waits before trying again
    sts.refuse = False
    h.clock.advance(minutes=2)
    assert worker.run_once().erasures == [tenant]
    assert world.keys("evidence", f"{tenant}/") == [] and h.store.pending_erasures() == []


def test_without_a_deletion_role_the_files_go_at_once_or_the_worker_finishes(tmp_path: Path,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness(tmp_path)  # local files, as in development
    account = signup(h.client)
    tenant = account["tenant"]["id"]
    h.objects.put_immutable(b"%PDF-1.7 invoice", tenant, "application/pdf")
    real = LocalObjectStore.purge_tenant

    def busy(self: LocalObjectStore, tenant: str) -> int:
        raise OSError("the disk is busy")

    monkeypatch.setattr(LocalObjectStore, "purge_tenant", busy)
    res = h.client.post("/api/account/delete", json={"confirm": "DELETE", "password": PASSWORD},
                        headers=bearer(account["token"]))
    assert res.status_code == 202 and h.store.pending_erasures() == [tenant]  # the inline attempt failed
    monkeypatch.setattr(LocalObjectStore, "purge_tenant", real)
    purger = ErasurePurger(h.objects)
    assert not purger.uses_deletion_role
    assert SyncWorker(h.manager, purger=purger).run_once().erasures == [tenant]
    assert not (tmp_path / "objects" / tenant).exists() and h.store.pending_erasures() == []


def test_the_purger_comes_from_the_configuration() -> None:
    config = ServerConfig.from_env({"S3_ERASURE_ROLE_ARN": ROLE, "S3_REPLICA_BUCKET": "evidence-replica",
                                    "S3_REPLICA_REGION": "eu-west-3"})
    objects = S3ObjectStore("evidence", region="eu-south-2", client=object())
    purger = purger_from_config(config, objects)
    assert (purger.role_arn, purger.replica_bucket, purger.replica_region) == (ROLE, "evidence-replica", "eu-west-3")
    assert purger.uses_deletion_role
    assert not purger_from_config(ServerConfig(), objects).uses_deletion_role
