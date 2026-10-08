"""Cloud storage connectors: Google Drive and Microsoft OneDrive / SharePoint (§8 cloud storage, §22 search 3).

Both use the same OAuth sign-in as the mailbox of that provider (connectors.authorize), with one more read-only
scope the owner grants at sign-in:

* Google: ``https://www.googleapis.com/auth/drive.readonly`` (:data:`DRIVE_READONLY_SCOPE`). The Google Cloud
  project must have the Drive API enabled and the scope on its OAuth consent screen.
* Microsoft: ``Files.Read.All`` (:data:`GRAPH_FILES_SCOPE`, delegated) on the Entra app: the user's OneDrive and
  the SharePoint document libraries they can open.

Each connector can

* **search** for a missing document (:meth:`search`, a :class:`FileQuery`: the supplier's name, the invoice
  number, the amount as it is printed, modified since a day): Drive API v3 ``files.list`` with ``q``
  (``fullText contains`` / ``name contains``, ``modifiedTime >``, not trashed; results come by relevance,
  sorting is not allowed with full-text terms); Graph ``/drive/root/search(q='...')`` once per term;
* **download** a file (:meth:`download`): Drive ``files.get?alt=media``, a Google Docs / Sheets file exported to
  PDF (``files.export?mimeType=application/pdf``); Graph ``/items/{id}/content``, whose 302 points at a
  pre-authenticated address fetched *without* the bearer token (an Office file converted with ``?format=pdf``);
* **watch** one folder the owner chose (:meth:`watch`): the files added or changed there since the last run
  (the cursor is the latest modification time seen), with the usual §47 state, so a folder that stopped
  syncing is shown like a mailbox that did.

Every file keeps its provenance (:meth:`CloudFile.provenance`: ``source`` ``"drive"`` or ``"onedrive"``, the
provider's file id, its path and its modification time) so the evidence says where it came from (§55).
Provider errors become typed :class:`ConnectorError` s (a lost grant asks the owner to reconnect; throttling
waits), never raw.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from backoffice.domain.models import utcnow

from .choices import DRIVE_READONLY_SCOPE, GRAPH_FILES_SCOPE, drive_folder_id
from .base import (
    ConnectorError,
    ConnectorKind,
    ConnectorState,
    CountingSink,
    ProviderError,
    ReconnectRequired,
    SyncOutcome,
    TransientError,
    record_failure,
    record_success,
)
from .http import AuthorizedHttp, object_list, required_str, retry_after_seconds, same_origin
from .oauth import TokenProvider

__all__ = [
    "DRIVE_READONLY_SCOPE",
    "GOOGLE_DRIVE_API",
    "GRAPH_FILES_SCOPE",
    "CloudDownload",
    "CloudFile",
    "CloudStorageConfig",
    "FileQuery",
    "GoogleDriveConnector",
    "OneDriveConnector",
    "drive_folder_id",
]

GOOGLE_DRIVE_API = "https://www.googleapis.com/drive/v3"

# What can be evidence: PDFs, photos, e-invoices, text, saved emails.
_EVIDENCE_TYPES = frozenset({
    "application/pdf", "image/jpeg", "image/png", "image/heic", "image/heif", "image/webp", "image/tiff",
    "application/xml", "text/xml", "text/plain", "message/rfc822",
})
_GOOGLE_EXPORTS = frozenset({"application/vnd.google-apps.document", "application/vnd.google-apps.spreadsheet"})
_GOOGLE_FOLDER = "application/vnd.google-apps.folder"
_OFFICE = frozenset({"doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "rtf"})  # Graph converts to PDF
_EXTENSION_TYPES = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "heic": "image/heic", "webp": "image/webp", "tif": "image/tiff", "tiff": "image/tiff",
                    "xml": "application/xml", "txt": "text/plain", "eml": "message/rfc822"}
_TRANSIENT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "backendError",
                                "sharingRateLimitExceeded"})
_LOST_ACCESS = frozenset({"insufficientPermissions", "authError", "domainPolicy", "insufficientFilePermissions"})


@dataclass(frozen=True)
class FileQuery:
    """Files mentioning any of ``terms`` (in their name or their indexed text), modified since ``since``."""

    terms: tuple[str, ...]
    since: datetime | None = None
    limit: int = 10

    def __post_init__(self) -> None:
        cleaned = tuple(dict.fromkeys(t for t in (_clean(x) for x in self.terms) if t))
        if not cleaned:
            raise ValueError("a file search needs at least one term")
        object.__setattr__(self, "terms", cleaned[:6])
        if self.since is not None and self.since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        if not 1 <= self.limit <= 50:
            raise ValueError("a file search returns between 1 and 50 files")


@dataclass(frozen=True)
class CloudStorageConfig:
    folder: str | None = None  # the folder the owner chose to watch: an id, a folder link, or (OneDrive) a path
    history_window: timedelta = timedelta(days=90)  # the first watch reads the folder this far back (§6)
    page_size: int = 100
    max_pages: int = 50
    max_file_bytes: int = 25 * 1024 * 1024
    drive: str = "me/drive"  # Graph only: "me/drive", "drives/{drive-id}" or "sites/{site-id}/drive"
    path_depth: int = 8  # Drive: how many parent folders are named in a file's path


@dataclass(frozen=True)
class CloudFile:
    provider: str  # "google_drive" | "onedrive"
    file_id: str
    name: str
    mime_type: str | None
    modified_at: datetime | None
    size: int | None = None
    path: str | None = None
    web_url: str | None = None
    drive_id: str | None = None

    def provenance(self) -> dict[str, Any]:
        """Where the evidence came from, as its sighting keeps it (§55): source, file id, path, modified time."""
        out: dict[str, Any] = {"source": "drive" if self.provider == "google_drive" else "onedrive",
                               "provider": self.provider, "fileId": self.file_id, "name": self.name,
                               "path": self.path or f"/{self.name}",
                               "modifiedAt": self.modified_at.isoformat() if self.modified_at else None}
        if self.web_url:
            out["webUrl"] = self.web_url
        return out


@dataclass(frozen=True)
class CloudDownload:
    file: CloudFile
    data: bytes = field(repr=False)
    filename: str
    content_type: str

    def provenance(self) -> dict[str, Any]:
        return self.file.provenance()


FileSink = Callable[[CloudDownload], None]


def _clean(value: str) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:80]


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _size(value: Any) -> int | None:
    try:
        return int(str(value)) if value is not None and not isinstance(value, bool) else None
    except ValueError:
        return None


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _watch_cursor(state: ConnectorState, now: datetime, window: timedelta) -> tuple[datetime, bool]:
    if state.cursor:
        try:
            data = json.loads(state.cursor)
            since = _parse_time(data.get("modifiedAfter")) if isinstance(data, dict) and data.get("v") == 1 else None
        except ValueError:
            since = None
        if since is not None:
            return since, False
    return now - window, True


def _dump_watch_cursor(latest: datetime) -> str:
    return json.dumps({"v": 1, "modifiedAfter": latest.astimezone(timezone.utc).isoformat()}, sort_keys=True)


# --------------------------------------------------------------------------- Google Drive


def _google_reasons(response: httpx.Response) -> set[str]:
    try:
        errors = json.loads(response.content or b"{}").get("error", {}).get("errors", [])
        return {str(e.get("reason")) for e in errors if isinstance(e, dict)}
    except (ValueError, AttributeError):
        return set()


def _drive_classify(response: httpx.Response) -> ConnectorError | None:
    """Drive reports throttling as 403 with a reason: tell it apart from a grant without Drive access."""
    if response.status_code not in (403, 429):
        return None
    reasons = _google_reasons(response)
    if response.status_code == 429 or reasons & _TRANSIENT_REASONS:
        return TransientError(f"google_drive_rate_limited_{response.status_code}",
                              retry_after=retry_after_seconds(response))
    if reasons & _LOST_ACCESS or not reasons:
        return ReconnectRequired("google_drive_insufficient_permissions")
    return ProviderError(f"google_drive_forbidden_{sorted(reasons)[0]}")


def _drive_literal(value: str) -> str:
    """A string literal in a Drive ``q``: backslashes and single quotes escaped."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


_DRIVE_FIELDS = "id,name,mimeType,modifiedTime,size,parents,webViewLink,driveId"


class GoogleDriveConnector:
    """Google Drive through the Drive API v3, read-only (module docstring)."""

    kind = ConnectorKind.CLOUD_STORAGE
    provider = "google_drive"
    display_name = "Google Drive"

    def __init__(self, tokens: TokenProvider, *, client: httpx.Client | None = None,
                 config: CloudStorageConfig | None = None, base_url: str = GOOGLE_DRIVE_API,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.config = config or CloudStorageConfig()
        self.base_url = base_url.rstrip("/")
        self._http = AuthorizedHttp(client or httpx.Client(timeout=httpx.Timeout(30.0)), tokens, "google_drive",
                                    classify=_drive_classify)
        self._clock = clock
        self._names: dict[str, tuple[str, tuple[str, ...]]] = {}  # folder id -> (name, parents)

    # ----------------------------------------------------------------- search

    def search(self, query: FileQuery) -> list[CloudFile]:
        """Files whose name or indexed text holds any of the terms, modified since ``query.since``."""
        terms = " or ".join(f"fullText contains {_drive_literal(t)} or name contains {_drive_literal(t)}"
                            for t in query.terms)
        q = f"trashed = false and mimeType != '{_GOOGLE_FOLDER}' and ({terms})"
        if query.since is not None:
            q += f" and modifiedTime > '{_rfc3339(query.since)}'"
        params = {"q": q, "fields": f"nextPageToken,files({_DRIVE_FIELDS})", "pageSize": min(100, query.limit * 3),
                  "spaces": "drive", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        found: list[CloudFile] = []
        for item in self._list(params, pages=2):
            file = self._file(item)
            if self._wanted(file):
                found.append(file)
            if len(found) >= query.limit:
                break
        return found

    def _list(self, params: dict[str, Any], *, pages: int | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(pages or self.config.max_pages):
            page = self._http.get_json(f"{self.base_url}/files", params={**params, **({"pageToken": token}
                                                                                     if token else {})})
            items += object_list(page, "files", "google_drive")
            next_token = page.get("nextPageToken")
            if not next_token:
                break
            if not isinstance(next_token, str) or next_token == token:
                raise ProviderError("google_drive_repeated_page_token")
            token = next_token
        return items

    def _file(self, item: dict[str, Any]) -> CloudFile:
        file_id = required_str(item, "id", "google_drive")
        name = str(item.get("name") or file_id)
        parents = tuple(str(p) for p in item.get("parents") or () if p)
        return CloudFile(provider=self.provider, file_id=file_id, name=name, mime_type=item.get("mimeType"),
                         modified_at=_parse_time(item.get("modifiedTime")), size=_size(item.get("size")),
                         path=self._path(name, parents), web_url=item.get("webViewLink"),
                         drive_id=item.get("driveId"))

    def _wanted(self, file: CloudFile) -> bool:
        mime = (file.mime_type or "").lower()
        if mime in _GOOGLE_EXPORTS:
            return True
        if mime.startswith("application/vnd.google-apps."):
            return False  # folders, forms, shortcuts: nothing to read
        if file.size is not None and file.size > self.config.max_file_bytes:
            return False
        return mime in _EVIDENCE_TYPES or mime.startswith("image/") or _extension(file.name) in _EXTENSION_TYPES

    def _path(self, name: str, parents: tuple[str, ...]) -> str:
        """``/My Drive/Faturas/2026/EDP.pdf``: the file's folders by name, as far as ``path_depth`` reaches."""
        names: list[str] = []
        current = parents[0] if parents else None
        for _ in range(self.config.path_depth):
            if current is None:
                break
            if current not in self._names:
                try:
                    folder = self._http.get_json(f"{self.base_url}/files/{quote(current, safe='')}",
                                                 params={"fields": "id,name,parents", "supportsAllDrives": "true"})
                except ProviderError:
                    break  # a folder we may not read: the path stops there
                self._names[current] = (str(folder.get("name") or ""),
                                        tuple(str(p) for p in folder.get("parents") or () if p))
            folder_name, grand = self._names[current]
            names.append(folder_name)
            current = grand[0] if grand else None
        return "/" + "/".join([*reversed([n for n in names if n]), name])

    # ----------------------------------------------------------------- download

    def download(self, file: CloudFile) -> CloudDownload | None:
        """The file's bytes; a Google Docs or Sheets file as PDF. None when it was deleted meanwhile."""
        file_id = quote(file.file_id, safe="")
        mime = (file.mime_type or "").lower()
        if mime in _GOOGLE_EXPORTS:
            response = self._http.request("GET", f"{self.base_url}/files/{file_id}/export",
                                          params={"mimeType": "application/pdf"}, allow=(404,))
            if response.status_code == 404:
                return None
            name = file.name if file.name.lower().endswith(".pdf") else f"{file.name}.pdf"
            return CloudDownload(file, response.content, name, "application/pdf")
        response = self._http.request("GET", f"{self.base_url}/files/{file_id}",
                                      params={"alt": "media", "supportsAllDrives": "true"}, allow=(404,))
        if response.status_code == 404:
            return None
        if len(response.content) > self.config.max_file_bytes:
            return None
        ctype = file.mime_type or _EXTENSION_TYPES.get(_extension(file.name), "application/octet-stream")
        return CloudDownload(file, response.content, file.name, ctype)

    # ----------------------------------------------------------------- watching one folder

    def watch(self, state: ConnectorState, sink: FileSink, *, now: datetime | None = None) -> SyncOutcome:
        """New or changed files in the chosen folder since the last run (the first run: the history window)."""
        now = now or self._clock()
        counted = CountingSink(sink)
        since, first = _watch_cursor(state, now, self.config.history_window)
        try:
            if not self.config.folder:
                raise ProviderError("google_drive_no_folder")
            folder = drive_folder_id(self.config.folder)
            params = {"q": f"{_drive_literal(folder)} in parents and trashed = false and "
                           f"modifiedTime > '{_rfc3339(since)}'",
                      "fields": f"nextPageToken,files({_DRIVE_FIELDS})", "orderBy": "modifiedTime",
                      "pageSize": self.config.page_size, "supportsAllDrives": "true",
                      "includeItemsFromAllDrives": "true"}
            latest = since
            for item in self._list(params):
                file = self._file(item)
                if file.modified_at is not None:
                    latest = max(latest, file.modified_at)
                if not self._wanted(file):
                    continue
                download = self.download(file)
                if download is not None:
                    counted(download)
        except ValueError:
            error: ConnectorError = ProviderError("google_drive_bad_folder")
            return SyncOutcome(record_failure(state, at=now, error=error), counted.count, error=error)
        except ConnectorError as exc:
            return SyncOutcome(record_failure(state, at=now, error=exc), counted.count, error=exc)
        new_state = record_success(state, at=now, cursor=_dump_watch_cursor(latest),
                                   coverage_start=now - self.config.history_window if first else None)
        return SyncOutcome(new_state, counted.count, full_sync=first)


# --------------------------------------------------------------------------- OneDrive / SharePoint (Graph)


_GRAPH_SELECT = "id,name,file,folder,size,lastModifiedDateTime,parentReference,webUrl"


class OneDriveConnector:
    """OneDrive and SharePoint document libraries through Microsoft Graph v1.0, read-only (module docstring)."""

    kind = ConnectorKind.CLOUD_STORAGE
    provider = "onedrive"
    display_name = "OneDrive"

    def __init__(self, tokens: TokenProvider, *, client: httpx.Client | None = None,
                 config: CloudStorageConfig | None = None, base_url: str = "https://graph.microsoft.com/v1.0",
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.config = config or CloudStorageConfig()
        self.base_url = base_url.rstrip("/")
        drive = self.config.drive.strip("/")
        if not re.fullmatch(r"me/drive|drives/[A-Za-z0-9!_.-]+|sites/[A-Za-z0-9,._-]+/drive", drive):
            raise ValueError("the drive must be me/drive, drives/{id} or sites/{id}/drive")
        self.drive_root = f"{self.base_url}/{drive}"
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0))
        self._http = AuthorizedHttp(self._client, tokens, "onedrive")
        self._clock = clock

    # ----------------------------------------------------------------- search

    def search(self, query: FileQuery) -> list[CloudFile]:
        """``/drive/root/search(q='term')`` once per term (Graph matches names, text and metadata)."""
        seen: dict[str, CloudFile] = {}
        for term in query.terms[:3]:
            url = f"{self.drive_root}/root/search(q='{quote(term.replace(chr(39), chr(39) * 2), safe='')}')"
            params: dict[str, Any] | None = {"$select": _GRAPH_SELECT, "$top": min(50, query.limit * 3)}
            for _ in range(2):
                page = self._http.get_json(url, params=params)
                for item in object_list(page, "value", "onedrive"):
                    file = self._file(item)
                    if file is None or file.file_id in seen or not self._wanted(file):
                        continue
                    if query.since is not None and file.modified_at is not None and file.modified_at < query.since:
                        continue
                    seen[file.file_id] = file
                url, params = self._follow(page.get("@odata.nextLink")), None
                if not url:
                    break
            if len(seen) >= query.limit:
                break
        return list(seen.values())[: query.limit]

    def _follow(self, link: Any) -> str:
        if not link:
            return ""
        if not same_origin(str(link), self.base_url):
            raise ProviderError("onedrive_foreign_link")
        return str(link)

    def _file(self, item: dict[str, Any]) -> CloudFile | None:
        if not isinstance(item.get("file"), dict):
            return None  # a folder or a package: nothing to read
        file_id = required_str(item, "id", "onedrive")
        name = str(item.get("name") or file_id)
        parent = item.get("parentReference") if isinstance(item.get("parentReference"), dict) else {}
        folder = str(parent.get("path") or "")
        folder = folder.split(":", 1)[1] if ":" in folder else ""
        return CloudFile(provider=self.provider, file_id=file_id, name=name, mime_type=item["file"].get("mimeType"),
                         modified_at=_parse_time(item.get("lastModifiedDateTime")), size=_size(item.get("size")),
                         path=f"{folder.rstrip('/')}/{name}" if folder else f"/{name}", web_url=item.get("webUrl"),
                         drive_id=parent.get("driveId"))

    def _wanted(self, file: CloudFile) -> bool:
        if file.size is not None and file.size > self.config.max_file_bytes:
            return False
        mime = (file.mime_type or "").lower()
        ext = _extension(file.name)
        return mime in _EVIDENCE_TYPES or mime.startswith("image/") or ext in _EXTENSION_TYPES or ext in _OFFICE

    # ----------------------------------------------------------------- download

    def _item_url(self, file: CloudFile) -> str:
        if file.drive_id:
            return f"{self.base_url}/drives/{quote(file.drive_id, safe='!')}/items/{quote(file.file_id, safe='!')}"
        return f"{self.drive_root}/items/{quote(file.file_id, safe='!')}"

    def download(self, file: CloudFile) -> CloudDownload | None:
        """``/items/{id}/content``: Graph answers 302 to a short-lived pre-authenticated address, fetched without
        the bearer token (it must never leave Graph). Office files come converted to PDF (``?format=pdf``)."""
        convert = _extension(file.name) in _OFFICE
        params = {"format": "pdf"} if convert else None
        response = self._http.request("GET", f"{self._item_url(file)}/content", params=params, allow=(302, 404))
        if response.status_code == 404:
            return None
        if response.status_code == 302:
            location = response.headers.get("location") or ""
            if urlsplit(location).scheme != "https":
                raise ProviderError("onedrive_bad_download_location")
            try:
                fetched = self._client.get(location)  # pre-authenticated: no Authorization header
            except httpx.TimeoutException:
                raise TransientError("onedrive_download_timeout") from None
            except httpx.HTTPError:
                raise TransientError("onedrive_download_network") from None
            if fetched.status_code == 404:
                return None
            if fetched.status_code >= 400:
                raise (TransientError if fetched.status_code >= 500 else ProviderError)(
                    f"onedrive_download_http_{fetched.status_code}")
            data = fetched.content
        else:
            data = response.content
        if len(data) > self.config.max_file_bytes:
            return None
        if convert:
            stem = file.name.rsplit(".", 1)[0]
            return CloudDownload(file, data, f"{stem}.pdf", "application/pdf")
        ctype = file.mime_type or _EXTENSION_TYPES.get(_extension(file.name), "application/octet-stream")
        return CloudDownload(file, data, file.name, ctype)

    # ----------------------------------------------------------------- watching one folder

    def _children_url(self) -> str:
        folder = (self.config.folder or "").strip()
        if not folder:
            raise ProviderError("onedrive_no_folder")
        if folder.startswith("/"):  # a path: /drive/root:/Faturas/2026:/children
            return f"{self.drive_root}/root:{quote(folder.rstrip('/'), safe='/')}:/children"
        if not re.fullmatch(r"[A-Za-z0-9!_.-]{3,200}", folder):
            raise ProviderError("onedrive_bad_folder")
        return f"{self.drive_root}/items/{folder}/children"

    def watch(self, state: ConnectorState, sink: FileSink, *, now: datetime | None = None) -> SyncOutcome:
        """New or changed files in the chosen folder since the last run (the first run: the history window).
        Graph's delta query works on a drive's root only, so the folder's children are listed and compared with
        the cursor (the latest modification time seen)."""
        now = now or self._clock()
        counted = CountingSink(sink)
        since, first = _watch_cursor(state, now, self.config.history_window)
        latest = since
        try:
            url = self._children_url()
            params: dict[str, Any] | None = {"$select": _GRAPH_SELECT, "$top": min(200, self.config.page_size)}
            for _ in range(self.config.max_pages):
                page = self._http.get_json(url, params=params)
                for item in object_list(page, "value", "onedrive"):
                    file = self._file(item)
                    if file is None or file.modified_at is None or file.modified_at <= since:
                        continue
                    latest = max(latest, file.modified_at)
                    if not self._wanted(file):
                        continue
                    download = self.download(file)
                    if download is not None:
                        counted(download)
                url, params = self._follow(page.get("@odata.nextLink")), None
                if not url:
                    break
        except ConnectorError as exc:
            return SyncOutcome(record_failure(state, at=now, error=exc), counted.count, error=exc)
        new_state = record_success(state, at=now, cursor=_dump_watch_cursor(latest),
                                   coverage_start=now - self.config.history_window if first else None)
        return SyncOutcome(new_state, counted.count, full_sync=first)
