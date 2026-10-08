"""Supplier portal adapters (§10). Deterministic first; AI browser only as fallback.

``invoice_pages`` registers one deterministic adapter per configured supplier website (backoffice.invoice_sites):
importing this package makes them available to the server's portal worker by their key ("edp_pt").
"""

from .base import (
    AuthResult,
    AuthStatus,
    MfaChallenge,
    PortalChanged,
    PortalCredentials,
    PortalDocument,
    PortalError,
    PortalInvoiceRef,
    PortalRegistry,
    PortalSession,
    PortalSync,
    PortalSyncOutcome,
    RetrievalPlan,
    RetrievalStrategy,
    SessionExpired,
    SupplierPortalConnector,
    authentication_prompt,
    code_prompt,
    default_registry,
    plan_retrieval,
    register_portal,
)
from .invoice_pages import ADAPTERS, EdpPortal, InvoicePagePortal, parse_amount, site_adapter

__all__ = [
    "ADAPTERS",
    "AuthResult",
    "AuthStatus",
    "EdpPortal",
    "InvoicePagePortal",
    "MfaChallenge",
    "PortalChanged",
    "PortalCredentials",
    "PortalDocument",
    "PortalError",
    "PortalInvoiceRef",
    "PortalRegistry",
    "PortalSession",
    "PortalSync",
    "PortalSyncOutcome",
    "RetrievalPlan",
    "RetrievalStrategy",
    "SessionExpired",
    "SupplierPortalConnector",
    "authentication_prompt",
    "code_prompt",
    "default_registry",
    "parse_amount",
    "plan_retrieval",
    "register_portal",
    "site_adapter",
]
