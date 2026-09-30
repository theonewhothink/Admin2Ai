"""Production readiness: the owner's go-live tracker, as data.

The internal dashboard (``backoffice.internal``, the Readiness page and the
Command Center panel) reads this list. Whoever finishes a piece of the path to
production updates its entry here, in the same change: ``status`` becomes
``"live"`` only when it is really in use, ``percent`` is an honest estimate of
the work done, and ``detail`` says in one line what remains (or, once live,
what is running).

The list is plain data so it can be read and changed without touching any
view code; :func:`readiness` validates it and adds the totals.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

__all__ = ["READINESS", "ReadinessItem", "readiness"]

Status = Literal["live", "pending"]


@dataclass(frozen=True, slots=True)
class ReadinessItem:
    id: str
    title: str
    status: Status
    percent: int  # work done, 0–100; 100 exactly when live
    detail: str  # one line: what remains, or what is running once live
    area: str  # "site" | "accounts" | "data" | "connections" | "documents" | "mobile" | "trust" | "operations" | "billing"

    def __post_init__(self) -> None:
        if not self.id or not self.title.strip() or not self.detail.strip():
            raise ValueError("a readiness item needs an id, a title and a detail line")
        if self.status not in ("live", "pending"):
            raise ValueError(f"{self.id}: status must be 'live' or 'pending'")
        if not isinstance(self.percent, int) or not 0 <= self.percent <= 100:
            raise ValueError(f"{self.id}: percent must be a whole number from 0 to 100")
        if (self.status == "live") != (self.percent == 100):
            raise ValueError(f"{self.id}: an item is live exactly when it is 100% done")


READINESS: tuple[ReadinessItem, ...] = (
    ReadinessItem(
        "static-site", "Static demo site", "live", 100,
        "Live on GitHub Pages. The real engine runs in the visitor's browser, with no server.",
        "site",
    ),
    ReadinessItem(
        "deploys", "Automatic deploys and checks", "live", 100,
        "Every push runs the backend, web, mobile, database and infrastructure checks; the site deploys itself.",
        "operations",
    ),
    ReadinessItem(
        "accounts", "Sign-in and accounts", "pending", 20,
        "Owner, accountant and admin roles are being built. Nobody can sign in to a real account yet.",
        "accounts",
    ),
    ReadinessItem(
        "database", "Saved data / database", "pending", 40,
        "Tables, row-level security and migrations are written. The app still keeps everything in memory.",
        "data",
    ),
    ReadinessItem(
        "email", "Email connection (Gmail, Outlook)", "pending", 50,
        "Gmail, Outlook and IMAP readers are built and tested. Needs Google and Microsoft app approval and keys.",
        "connections",
    ),
    ReadinessItem(
        "bank", "Bank connection", "pending", 35,
        "The open-banking reader is built. Needs a contract and keys with a licensed open-banking provider.",
        "connections",
    ),
    ReadinessItem(
        "reading", "Reading PDFs and photos", "pending", 45,
        "PDF text, invoice QR codes and structured files are read. The OCR engines still need their own servers.",
        "documents",
    ),
    ReadinessItem(
        "mobile", "Mobile app in stores", "pending", 30,
        "Capture, share and the offline queue exist. Needs Apple and Google developer accounts and store review.",
        "mobile",
    ),
    ReadinessItem(
        "security", "Security and data protection review", "pending", 15,
        "Encryption, tenant isolation and the audit chain are designed in. External review and DPIA still to do.",
        "trust",
    ),
    ReadinessItem(
        "monitoring", "Monitoring and backups", "pending", 30,
        "Alarms, backups and recovery are written for AWS. Nothing is deployed or restore-tested yet.",
        "operations",
    ),
    ReadinessItem(
        "billing", "Payments and billing", "pending", 0,
        "Not started. The plans (Free, Solo, Business, Multi-company, Accountant) need a payment provider.",
        "billing",
    ),
)


def readiness(items: tuple[ReadinessItem, ...] = READINESS) -> dict[str, Any]:
    """The checklist with its totals, as JSON-able data.

    ``percent`` is the plain average of the items (every item weighs the same),
    rounded down so the total never looks further along than it is.
    """
    ids = [i.id for i in items]
    if len(set(ids)) != len(ids):
        raise ValueError("readiness ids must be unique")
    live = sum(1 for i in items if i.status == "live")
    percent = sum(i.percent for i in items) // len(items) if items else 0
    return {
        "items": [asdict(i) for i in items],
        "live": live,
        "pending": len(items) - live,
        "total": len(items),
        "percent": percent,
    }
