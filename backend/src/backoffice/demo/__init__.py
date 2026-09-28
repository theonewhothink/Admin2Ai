"""Demo tenant (Laura Medina: Hazel Tree, Company B, Company C) replayed through the real pipeline.

    from backoffice.demo import build_demo, DEMO_TODAY
    orchestrator = build_demo()          # September 2026 processed, as of 2 October 2026 09:30

``backoffice.demo.evidence`` holds the raw files (fiscal QR text layers, a UBL
e-invoice, emails, tax letters, bank rows); ``EDP_INVOICE`` is the chased
invoice, left out of the replay so it can be uploaded to watch a chase close.
"""

from .world import NOW as DEMO_NOW
from .world import TENANT as DEMO_TENANT
from .world import TODAY as DEMO_TODAY
from .world import build as build_demo

__all__ = ["DEMO_NOW", "DEMO_TENANT", "DEMO_TODAY", "build_demo"]
