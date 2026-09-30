"""The owner's data, taken away or erased (GDPR, §52).

* :func:`export_account` builds one ZIP with everything the business has here:
  every event in its log (the complete history, in order), the documents with
  their details, and every file that was sent in, as sent.
* :func:`erase_account` removes the business: its event log, sessions,
  phones, API keys and stored sign-ins (database), then its files (object
  store). A permanent erasure record, without personal data, says it
  happened; if the file store refuses (an S3 Object Lock retention without
  the bypass permission) the record stays "files pending" for an operator.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import datetime
from typing import Any

from .events import Event, object_refs
from .runtime import TenantManager

__all__ = ["erase_account", "export_account"]

log = logging.getLogger("backoffice.server.account")

README = """This is everything your back office holds for {name}, exported on {when}.

account.json     who you are and which business this is
events.jsonl     every change, in order, one per line (the complete history)
documents.json   every document found, with supplier, number, date, amount and status
files/           every file that was sent in, named by its SHA-256 fingerprint

Keep it somewhere safe: it contains your invoices and bank details.
"""


def export_account(manager: TenantManager, principal: Any) -> tuple[str, bytes]:
    """``(filename, zip bytes)`` for the principal's business."""
    tenant_id = principal.tenant.id
    rows = manager.store.events(tenant_id)
    documents = manager.read(tenant_id, lambda svc: svc.documents_list({})["items"])
    now = manager.now()
    buf = io.BytesIO()
    seen: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", README.format(name=principal.tenant.name, when=now.date().isoformat()))
        z.writestr("account.json", json.dumps({
            "exportedAt": now.isoformat(),
            "user": {"id": principal.user.id, "email": principal.user.email, "name": principal.user.name},
            "business": {"id": tenant_id, "name": principal.tenant.name},
            "roles": sorted(principal.roles),
            "events": len(rows),
        }, indent=2, ensure_ascii=False))
        z.writestr("events.jsonl", "".join(r.body + "\n" for r in rows))
        z.writestr("documents.json", json.dumps({"documents": documents}, indent=2, ensure_ascii=False))
        for row in rows:
            for ref in object_refs(Event.parse(row).data):
                digest = str(ref.get("sha256", ""))
                if digest in seen:
                    continue
                seen.add(digest)
                try:
                    z.writestr(f"files/{digest}", manager.get_file(tenant_id, ref))
                except Exception:
                    log.exception("export_file_missing", extra={"tenant": tenant_id})
    return f"back-office-export-{now.date().isoformat()}.zip", buf.getvalue()


def erase_account(manager: TenantManager, principal: Any, *, bypass_governance: bool = False) -> dict[str, Any]:
    """Erase the principal's business. Returns what happened (for logs; the owner sees one sentence)."""
    tenant_id = principal.tenant.id
    now: datetime = manager.now()
    with manager.open(tenant_id):
        events = manager.store.erase_account(tenant_id, principal.user.id, now)
    manager.evict(tenant_id)
    purged = False
    try:
        from backoffice.evidence.store import S3ObjectStore

        if isinstance(manager.objects, S3ObjectStore):
            manager.objects.purge_tenant(tenant_id, bypass_governance=bypass_governance)
        else:
            manager.objects.purge_tenant(tenant_id)
        manager.store.mark_objects_purged(tenant_id, manager.now())
        purged = True
    except Exception:
        log.exception("erase_files_pending", extra={"tenant": tenant_id})
    return {"tenant": tenant_id, "events": events, "filesPurged": purged}
