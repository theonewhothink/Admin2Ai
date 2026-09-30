"""Completing an account erasure: the business's files, every version, in every copy (§25, §52).

Erasing an account (server/account.py) removes the business's data at once
and leaves a record in ``tenant_erasures``. Its evidence files are then
removed here, by the sync worker:

* **In AWS** only the evidence-deletion role may delete an original (the
  bucket policy denies everyone else, §25). The worker assumes that role
  through STS (``S3_ERASURE_ROLE_ARN``) for a quarter of an hour, deletes
  every object version and delete marker under the tenant's prefix, bypassing
  GOVERNANCE retention, in the evidence bucket and in its disaster-recovery
  copy (``S3_REPLICA_BUCKET`` / ``S3_REPLICA_REGION``; deletions are not
  replicated), and only then marks the record purged.
* **Elsewhere** (a local directory, or S3-compatible storage without a
  deletion role) the files are removed with the store's own access.

A failure leaves the record pending; the next pass tries again.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

__all__ = ["ErasurePurger", "purger_from_config"]

log = logging.getLogger("backoffice.server.erasure")

SESSION_SECONDS = 900  # the shortest STS session: long enough for one tenant's files
_SESSION_NAME = re.compile(r"[^A-Za-z0-9+=,.@_-]")


class ErasurePurger:
    """Removes an erased business's files (module docstring)."""

    def __init__(
        self,
        objects: Any,
        *,
        role_arn: str = "",
        replica_bucket: str = "",
        replica_region: str = "",
        bypass_governance: bool = False,
        sts_client: Any = None,
        s3_client_factory: Callable[[Mapping[str, str], str], Any] | None = None,
    ) -> None:
        self.objects = objects
        self.role_arn = role_arn
        self.replica_bucket = replica_bucket
        self.replica_region = replica_region
        self.bypass_governance = bypass_governance
        self._sts = sts_client
        self._s3_factory = s3_client_factory or _boto3_s3

    @property
    def uses_deletion_role(self) -> bool:
        return bool(self.role_arn) and _is_s3(self.objects)

    def purge(self, tenant_id: str) -> int:
        """Delete every file version of ``tenant_id``; returns how many were removed."""
        if not _is_s3(self.objects):
            return int(self.objects.purge_tenant(tenant_id))
        if not self.role_arn:  # S3-compatible storage without a deletion role (local MinIO)
            removed = int(self.objects.purge_tenant(tenant_id, bypass_governance=self.bypass_governance))
            if self.replica_bucket:
                removed += int(self._replica().purge_tenant(tenant_id, bypass_governance=self.bypass_governance))
            return removed
        credentials = self._assume(tenant_id)
        removed = int(self.objects.purge_tenant(tenant_id, bypass_governance=True,
                                                client=self._s3_factory(credentials, self.objects.region)))
        if self.replica_bucket:
            replica = self._replica()
            removed += int(replica.purge_tenant(tenant_id, bypass_governance=True,
                                                client=self._s3_factory(credentials, replica.region)))
        return removed

    def _assume(self, tenant_id: str) -> dict[str, str]:
        if self._sts is None:
            import boto3  # lazy: server-only dependency

            self._sts = boto3.client("sts", region_name=self.objects.region)
        name = _SESSION_NAME.sub("-", f"erasure-{tenant_id}")[:64]  # shows in CloudTrail
        out = self._sts.assume_role(RoleArn=self.role_arn, RoleSessionName=name, DurationSeconds=SESSION_SECONDS)
        c = out["Credentials"]
        return {"aws_access_key_id": c["AccessKeyId"], "aws_secret_access_key": c["SecretAccessKey"],
                "aws_session_token": c["SessionToken"]}

    def _replica(self) -> Any:
        from backoffice.evidence.store import S3ObjectStore

        return S3ObjectStore(self.replica_bucket, region=self.replica_region or self.objects.region,
                             prefix=self.objects.prefix, endpoint_url=self.objects.endpoint_url)


def _is_s3(objects: Any) -> bool:
    from backoffice.evidence.store import S3ObjectStore

    return isinstance(objects, S3ObjectStore)


def _boto3_s3(credentials: Mapping[str, str], region: str) -> Any:
    import boto3  # lazy: server-only dependency
    from botocore.config import Config

    return boto3.client("s3", region_name=region, config=Config(signature_version="s3v4", retries={"mode": "standard"}),
                        **credentials)


def purger_from_config(config: Any, objects: Any) -> ErasurePurger:
    return ErasurePurger(objects, role_arn=config.s3_erasure_role_arn, replica_bucket=config.s3_replica_bucket,
                         replica_region=config.s3_replica_region, bypass_governance=config.s3_bypass_governance)
