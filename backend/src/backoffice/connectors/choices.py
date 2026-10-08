"""What the owner chooses when connecting a place, checked without any network library (§4, §8).

The engine runs in the browser too (the demo, Pyodide with pydantic only), so adding a source there must not import
the HTTP connectors: the sign-in scopes, a Drive folder from its link and TOConline's API data are checked here,
and the connectors import them from this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

__all__ = [
    "DRIVE_READONLY_SCOPE",
    "GRAPH_FILES_SCOPE",
    "GRAPH_MAIL_SCOPES",
    "GRAPH_SHARED_MAIL_SCOPE",
    "GRAPH_SHARED_MAIL_SCOPES",
    "TOConlineCredentials",
    "drive_folder_id",
]

# Read-only scopes: a Drive or OneDrive folder is read, never written.
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GRAPH_FILES_SCOPE = "https://graph.microsoft.com/Files.Read.All"
GRAPH_MAIL_SCOPES = ("offline_access", "https://graph.microsoft.com/Mail.Read")
GRAPH_SHARED_MAIL_SCOPE = "https://graph.microsoft.com/Mail.Read.Shared"
GRAPH_SHARED_MAIL_SCOPES = (*GRAPH_MAIL_SCOPES, GRAPH_SHARED_MAIL_SCOPE)

_TOC_HOSTS = (".toconline.pt", ".toconline.com")


def drive_folder_id(value: str) -> str:
    """A Drive folder id from what the owner pasted: the id itself or the folder's link."""
    text = (value or "").strip()
    found = re.search(r"/folders/([A-Za-z0-9_-]+)", text) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", text)
    folder = found.group(1) if found else text
    if not re.fullmatch(r"[A-Za-z0-9_-]{5,200}", folder):
        raise ValueError("that does not look like a Drive folder")
    return folder


def _toc_url(value: str, what: str) -> str:
    url = (value or "").strip().rstrip("/")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not any(host.endswith(s) or host == s[1:] for s in _TOC_HOSTS) or "@" in parts.netloc:
        raise ValueError(f"the TOConline {what} must be an https address at toconline.pt or toconline.com")
    return url


@dataclass(frozen=True)
class TOConlineCredentials:
    """The company's API data from TOConline (its company settings, API data), kept in the vault."""

    client_id: str
    client_secret: str = field(repr=False)
    oauth_url: str = ""
    api_url: str = ""

    def __post_init__(self) -> None:
        if not self.client_id or not self.client_secret:
            raise ValueError("TOConline needs the client id and secret from its API data")
        object.__setattr__(self, "oauth_url", _toc_url(self.oauth_url, "OAuth address"))
        api = _toc_url(self.api_url, "API address")
        object.__setattr__(self, "api_url", api[:-4] if api.endswith("/api") else api)
