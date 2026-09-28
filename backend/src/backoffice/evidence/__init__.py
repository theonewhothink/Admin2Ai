"""Evidence ingestion (§7-9, §12, §43, §52, §55).

Everything starts from evidence: an immutable original with its provenance.

Storage (``store``)
    ObjectStore                     Protocol: put_immutable(bytes, tenant, content_type) -> key,
                                    get(key) -> bytes (hash-verified), exists(key)
    LocalObjectStore(root)          write-once filesystem store
    S3ObjectStore(bucket, ...)      EU region, KMS, versioning check, conditional writes, Object Lock
    EvidenceRegistry(store, index)  register(bytes, tenant_id=, source_kind=, format=, mime_type=, ...)
                                    -> Registration(evidence, created, sighting); dedupes by
                                    (tenant, sha256); open(tenant, id) -> verified bytes
Email (``email``)
    parse_eml(raw) / parse_gmail_raw(b64) -> ParsedEmail   bodies, attachments, cid images,
                                    attached emails, thread headers, ranked links
    EmailIngestor(registry).ingest(raw, tenant_id=...) -> EmailIngestResult
    extract_links(html, text, sender_domain=, context=subject) -> ranked LinkCandidate tuple
Archives (``archive``)
    expand_zip(bytes, ZipLimits) -> ArchiveExpansion   zip-bomb and traversal safe
Links (``links``)
    UrlSafety(UrlSafetyConfig, resolver).check(url) -> UrlCheck
    LinkFetcher(safety, transport=, browser=).fetch(url, supplier_name=, session_cookies=) -> FetchResult
                                    total deadline, pinned connections, SessionCookie for stored logins
    register_fetch(result, registry, tenant_id=) -> [Registration]  original first; ZIP/.eml contents follow
    BrowserSession Protocol; PlaywrightBrowserSession (lazy, isolated worker process)
Share extension (``share``)
    ShareIntake(registry, fetcher=).accept(tenant_id, SharePayload) -> ShareOutcome
    ShareIntake.ingest_file(tenant_id, bytes, ...) -> ShareOutcome   file routing for other callers
Offline upload (``upload``)
    UploadService(registry).receive(UploadRequest) -> UploadReceipt (delete_local when safe);
                                    queued .eml/.zip are expanded like shared files
Sniffing / domains
    sniff(bytes, declared_type=, filename=) -> Sniffed
    LookalikeChecker(known_domains).check(host); display_name_for_host(host)
"""

from .archive import (
    ArchiveExpansion,
    ArchiveMember,
    ArchiveSkip,
    SkipReason,
    ZipLimits,
    expand_zip,
    register_members,
)
from .domains import (
    LookalikeChecker,
    LookalikeFinding,
    LookalikeKind,
    display_name_for_host,
    registrable_domain,
    to_ascii_host,
    to_unicode_host,
)
from .email import (
    AttachedEmail,
    EmailAddress,
    EmailIngestor,
    EmailIngestResult,
    EmailLimits,
    EmailParseError,
    IngestedFile,
    LinkCandidate,
    MailPart,
    ParsedEmail,
    SkippedPart,
    ThreadInfo,
    decode_gmail_raw,
    extract_links,
    extract_text_links,
    is_invoice_context,
    parse_eml,
    parse_gmail_raw,
    score_link,
)
from .html_signals import Anchor, HtmlSignals, analyze_html
from .links import (
    BLOCKED_MESSAGE,
    BrowserDownload,
    BrowserError,
    BrowserSession,
    BrowserUnavailable,
    FetchPolicy,
    FetchRecord,
    FetchResult,
    LinkFetcher,
    LinkOutcome,
    PlaywrightBrowserSession,
    PlaywrightConfig,
    RedirectHop,
    RenderedPage,
    Resolver,
    SessionCookie,
    SystemResolver,
    UnsafeReason,
    UrlCheck,
    UrlSafety,
    UrlSafetyConfig,
    authentication_message,
    register_fetch,
    sign_in_message,
)
from .share import GOT_IT, ShareIntake, ShareKind, ShareOutcome, SharePayload, ShareRoute, route_for
from .sniff import Sniffed, sniff, zip_directory_shape
from .store import (
    EU_AWS_REGIONS,
    EU_DEFAULT_REGION,
    ContentScanner,
    EvidenceIndex,
    EvidenceRegistry,
    EvidenceStoreError,
    InMemoryEvidenceIndex,
    IntegrityError,
    InvalidKey,
    InvalidTenant,
    LocalObjectStore,
    ObjectNotFound,
    ObjectStore,
    Registration,
    S3ObjectStore,
    ScanVerdict,
    Sighting,
    StorageConfigError,
    check_json_metadata,
    evidence_id_for,
    object_key,
    parse_key,
    sha256_hex,
    validate_tenant,
)
from .upload import (
    InMemoryReceiptStore,
    ReceiptStore,
    RejectReason,
    UploadReceipt,
    UploadRequest,
    UploadService,
    UploadStatus,
)

__all__ = [
    "Anchor",
    "ArchiveExpansion",
    "ArchiveMember",
    "ArchiveSkip",
    "AttachedEmail",
    "BLOCKED_MESSAGE",
    "BrowserDownload",
    "BrowserError",
    "BrowserSession",
    "BrowserUnavailable",
    "ContentScanner",
    "EU_AWS_REGIONS",
    "EU_DEFAULT_REGION",
    "EmailAddress",
    "EmailIngestResult",
    "EmailIngestor",
    "EmailLimits",
    "EmailParseError",
    "EvidenceIndex",
    "EvidenceRegistry",
    "EvidenceStoreError",
    "FetchPolicy",
    "FetchRecord",
    "FetchResult",
    "GOT_IT",
    "HtmlSignals",
    "InMemoryEvidenceIndex",
    "InMemoryReceiptStore",
    "IngestedFile",
    "IntegrityError",
    "InvalidKey",
    "InvalidTenant",
    "LinkCandidate",
    "LinkFetcher",
    "LinkOutcome",
    "LocalObjectStore",
    "LookalikeChecker",
    "LookalikeFinding",
    "LookalikeKind",
    "MailPart",
    "ObjectNotFound",
    "ObjectStore",
    "ParsedEmail",
    "PlaywrightBrowserSession",
    "PlaywrightConfig",
    "ReceiptStore",
    "RedirectHop",
    "Registration",
    "RejectReason",
    "RenderedPage",
    "Resolver",
    "S3ObjectStore",
    "ScanVerdict",
    "SessionCookie",
    "ShareIntake",
    "ShareKind",
    "ShareOutcome",
    "SharePayload",
    "ShareRoute",
    "Sighting",
    "SkipReason",
    "SkippedPart",
    "Sniffed",
    "StorageConfigError",
    "SystemResolver",
    "ThreadInfo",
    "UnsafeReason",
    "UploadReceipt",
    "UploadRequest",
    "UploadService",
    "UploadStatus",
    "UrlCheck",
    "UrlSafety",
    "UrlSafetyConfig",
    "ZipLimits",
    "analyze_html",
    "authentication_message",
    "check_json_metadata",
    "decode_gmail_raw",
    "display_name_for_host",
    "evidence_id_for",
    "expand_zip",
    "extract_links",
    "extract_text_links",
    "is_invoice_context",
    "object_key",
    "parse_eml",
    "parse_gmail_raw",
    "parse_key",
    "register_fetch",
    "register_members",
    "registrable_domain",
    "route_for",
    "score_link",
    "sha256_hex",
    "sign_in_message",
    "sniff",
    "to_ascii_host",
    "to_unicode_host",
    "validate_tenant",
    "zip_directory_shape",
]
