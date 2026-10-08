/**
 * Public API of the mobile module (Phase 4). Pure, platform-free pieces are
 * exported here for integration, tooling and tests; device adapters live in
 * the `expo` sub-folders and are wired by src/app/services.tsx.
 *
 * Offline evidence pipeline (§43)
 *   OfflinePipeline      capture → encrypt → queue → upload → verify hash → delete
 *   QueueRunner          network-aware foreground scheduling
 *   HttpEvidenceUploader POST /api/evidence/upload (multipart sha256 + file)
 *   classifyResponse / parseReceipt / encodeMultipart   wire contract helpers
 *   contract constants   mirror contracts/evidence-upload.json
 *
 * Capture (§11) and share (§12)
 *   measureQuality / assessQuality   blur, glare, darkness on a grayscale page
 *   inspectPages / savePages         scan review and hand-off to the queue
 *   routeShare / ingestShare         shared URLs, PDFs, images, .eml, text, XML
 *
 * Owner API (§34-41) and security (§52)
 *   ApiClient            /api/home, /api/needs-you, /answer, /api/activity, /api/ask
 *   AppLock              biometric lock state machine and hard-approval re-check
 *   buildHomeView etc.   screen view-models with plain-language copy
 */
export { OfflinePipeline, DEFAULT_PIPELINE_CONFIG } from "./offline/pipeline";
export type { PipelineDeps, PipelineConfig, DrainReport, RecoveryReport, QueueSummary, HeldItem } from "./offline/pipeline";
export { QueueRunner, systemTimers } from "./offline/runner";
export type { Timers } from "./offline/runner";
export { HttpEvidenceUploader, classifyResponse, parseReceipt, uploadFields } from "./offline/uploader";
export { encodeMultipart, encodeMultipartSafely } from "./offline/multipart";
export { EncryptedQueueStore, EncryptedJsonFile, parseSnapshot } from "./offline/journal";
export type { RawFile } from "./offline/journal";
export { backoffDelay, retryDelay, parseRetryAfter, DEFAULT_BACKOFF } from "./offline/backoff";
export type { BackoffPolicy } from "./offline/backoff";
export { encodeEnvelope, decodeEnvelope, frameSealed, unframeSealed } from "./offline/envelope";
export * as uploadContract from "./offline/contract";
export type * from "./offline/types";

export { measureQuality, assessQuality, rgbaToGray, DEFAULT_THRESHOLDS } from "./scan/quality";
export type { GrayImage, QualityMetrics, QualityThresholds } from "./scan/quality";
export { inspectPages, savePages, reviewNotes, needsReview, replacePage, saveMessage } from "./scan/session";
export type { ReviewedPage, CaptureSink, SaveDeps, SaveSummary } from "./scan/session";
export type { DocumentScanner, PageAnalyzer, QrDetector, ScanOutcome, ScannedPage } from "./scan/types";

export { routeShare, resolveMime, formatFor, httpUrl, safeFileName } from "./share/route";
export type { SharePayload, ShareItem, ShareRoute } from "./share/route";
export { ingestShare, shareMessage, fromShareIntent } from "./share/ingest";
export { redirectSharePath } from "./share/nativeIntent";

export { ApiClient } from "./api/client";
export type { Loaded, DataSource, AskOutcome, ApiClientOptions } from "./api/client";
export { MemorySnapshotCache, SealedSnapshotCache } from "./api/cache";
export { parseHome, parseNeedsYou, parseActivity, parseAskAnswer } from "./api/guards";
export type * from "./api/types";
export type { HttpSend, HttpRequest, HttpResponse, ApiEndpoint } from "./api/http";

export { AuthStore } from "./auth/store";
export type { AuthState, AuthUser, AuthApi, AuthHooks, TokenStorage, SignInOutcome } from "./auth/store";
export { httpAuthApi, parseSignIn } from "./auth/api";
export { onUnauthorized, emitUnauthorized } from "./auth/events";
export { PushRegistration, PUSH_KEYS } from "./notifications/push";
export type { PushPlatform, DeviceApi, PushPrefs, PermissionStatus, EnableResult } from "./notifications/push";
export { routeForNotification } from "./notifications/route";
export type { NotificationTarget } from "./notifications/route";

export { AppLock, mapAuthError, DEFAULT_GRACE_MS } from "./security/lock";
export type { Authenticator, AuthResult, LockState, SecurityLevel } from "./security/lock";

export { buildHomeView, reconnectMessage, handledToday } from "./models/home";
export { rememberLabel, rememberSentence, approvalAction, rememberFlag, openItems } from "./models/needs";
export { groupActivity, activityTone } from "./models/activity";
export { sourceNote } from "./models/source";
export { TABS, badgeText } from "./navigation/tabs";
export { copy } from "./copy";
export { colors, space, radius, motion, type as typography, toneColors } from "./theme/tokens";
export { formatMoney, toDecimal, roundDecimal } from "./lib/money";
export type { DecimalString } from "./lib/money";
