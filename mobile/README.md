# Back Office mobile (Phase 4)

The phone app for the AI back-office operator: **capture, approve, resolve,
ask** (spec §11). React Native + Expo SDK 57, expo-router, TypeScript strict.

Tabs (§40): **Home · Needs You · Scan** (large, centre) **· Activity · Ask**.

## Run it

Native modules (document scanner, share extension, secure storage, biometrics)
mean the app runs in an [Expo dev build](https://docs.expo.dev/develop/development-builds/introduction/),
not Expo Go.

```bash
cd mobile
npm install
npm run typecheck        # tsc --noEmit
npm test                 # jest (jest-expo preset)

npx expo prebuild --clean   # generates ios/ and android/ from app.json plugins
npx expo run:ios            # or run:android, on a device for camera and share
```

`ios/` and `android/` are generated; do not commit them.

From the backend, `python -m pytest tests/test_mobile_*.py` checks the upload
contract against the domain model and, when `mobile/node_modules` exists, also
runs the Jest suite and the type-check.

## Configuration

| Setting | Where | Notes |
| --- | --- | --- |
| API base URL | `EXPO_PUBLIC_API_URL` (inlined at build time) | Unset = demo mode: sample data, nothing leaves the phone, uploads stay queued. |
| Bundle id / package | `app.json` → `ios.bundleIdentifier`, `android.package` | `eu.admin2ai.backoffice` is a placeholder. Set the real ids before the first store build. |
| URL scheme | `app.json` → `scheme` (`backoffice`) | Used by the Share Extension hand-off. |
| Session token | `src/security/session.ts` (Keychain / Keystore) | Sent as `Authorization: Bearer`. Sign-in UI is not part of this phase. |

## How it works

### Offline evidence pipeline (§43), `src/offline/`

```
capture → hash (sha256) → seal (AES-256-GCM) → queue → upload when online
       → server returns its own sha256 → equal? → record receipt → delete local copy
```

- **Encryption at rest.** Each item is sealed with AES-256-GCM (`expo-crypto`,
  native). The key is 32 random bytes in `expo-secure-store`
  (`AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY`, so background uploads work while the
  phone is locked, and the key never syncs or backs up). The item id is bound as
  associated data, so a blob cannot be swapped between items. The queue index
  and cached screens are sealed with the same key.
- **Crash safety.** The sealed blob holds its own metadata. Blob first, index
  second: an interrupted capture leaves an orphan that `recover()` re-registers.
  Expired upload claims (leases) are released; a receipt that was recorded but
  whose deletion did not finish is completed.
- **Upload.** `POST /api/evidence/upload`, multipart: `sha256`, `source`,
  `format`, `captured_at`, `client_item_id`, optional `capture_id`, `page`,
  `page_count`, `original_url`, `hints`, and `file`. Header
  `Idempotency-Key: <item id>`. The exact contract is
  `contracts/evidence-upload.json`, with a byte-exact golden request in
  `contracts/fixtures/` shared with the backend test.
- **Verification.** The receipt must be JSON with `sha256` computed by the
  server over the bytes it stored. The local copy is deleted only when it
  equals the phone's hash, and only after the receipt is saved. A mismatch keeps
  the file and retries; after 5 mismatches the item is held for the owner. A
  rejected file (4xx) is held, never deleted. Held items can be retried, or
  removed by the owner after a confirmation.
- **Retries.** Exponential backoff with equal jitter (5 s base, ×2, 30 min
  cap), never sooner than a server `Retry-After` (capped at 1 h). Offline means
  no attempts; the queue drains when the network returns, when the app comes to
  the foreground, after each capture, when the next retry falls due, and in the
  background task (`expo-background-task`, about every 15 min at the OS's
  discretion, 25 s budget).
- **Idempotent re-upload.** A lost response is retried with the same
  Idempotency-Key; the server de-duplicates by hash and returns the same
  receipt. Identical bytes are never queued twice on the phone.

What the server must do (for the backend owner): hash the stored bytes itself;
de-duplicate by sha256 (return `200`/`409` with the existing receipt); answer
`422 {"error": "hash_mismatch"}` (or FastAPI `{"detail": {...}}`) when the
declared hash differs; treat `hints` as advisory, never as evidence.

### Scan (§11), `src/scan/`

`react-native-document-scanner-plugin` opens VisionKit (iOS) or the ML Kit
Document Scanner (Android): edge detection, auto-crop, perspective correction,
rotation, many pages. The app then checks each page on the device:

- **Blur**: Laplacian energy over intensity variance per detailed tile,
  corrected for sensor noise; median over the page. Contrast- and
  noise-independent.
- **Glare**: a compact clipped highlight on paper that is otherwise not clipped.
- **Too dark**: mean luminance.
- **QR**: `expo-camera` reads QR codes (e.g. Portuguese invoice QR, §19); sent
  as a hint only.

If a page looks bad the owner sees one sentence ("Page 2 looks blurry.") with
"Retake page 2" or "Use as is". Otherwise the pages are sent at once. Nothing is
typed: no category, amount or supplier. Thresholds were set on synthetic pages
and are conservative (see `src/scan/quality.ts`); the server re-checks
everything.

### Share (§12), `src/share/`

`expo-share-intent` provides the iOS Share Extension and the Android share
intent. Accepted: links, PDFs, images and screenshots, `.eml`, text, XML
e-invoices. A bare link is queued as a URL for the server's link intelligence
(§9); text with a link keeps the words and flags the link. Unsafe schemes
(`javascript:`, `file:`, `intent:`) are refused. Shares are processed after
unlock, sealed into the same queue, and the app's own plaintext copy is deleted.
Native notes and the one known gap: `native/ios/ShareExtension/README.md`,
`native/android/README.md`.

### Lock (§52), `src/security/`

The app opens locked (Face ID / Touch ID / fingerprint, falling back to the
phone passcode) and locks again after 60 s in the background. Content stays
mounted but hidden from sight and screen readers; the app switcher shows a
plain cover. Releasing a payment to changed bank details asks for identity again
right before sending (§25 hard approval). A phone with no passcode cannot be
locked; the owner is told once.

### Owner API, `src/api/`

| Call | Used by |
| --- | --- |
| `GET /api/home` | Home |
| `GET /api/needs-you` | Needs You, tab badge, Home count |
| `POST /api/needs-you/{id}/answer` `{option_id, remember}` | Decision cards |
| `GET /api/activity` | Activity |
| `POST /api/ask` `{question}` → `{answer, evidence[]}` | Ask |
| `POST /api/evidence/upload` | Offline queue |

Shapes follow the web app's contract (`web/README.md`), with money accepted as
a JSON string or number and kept as a decimal string (never a float). Optional
mobile field: `handledToday` on `/api/home` (otherwise summed from `handled`
when `handledPeriodLabel` is "Today").

Fallbacks: reads use the last response seen on this phone (sealed on disk),
then sample data, and the screen says which ("You're offline. Showing what I
knew at 09:12 today." / "These are example figures."). Writes never pretend: an
answer or question that did not reach the server says so. Sample answers and
auto-accepted decisions exist only in demo mode.

## Design

Tokens in `src/theme/tokens.ts` are the web's (`web/app/globals.css`):
background `#F7F8F6`, cards `#FFFFFF`, text `#111318`, secondary `#667085`,
emerald `#0F6B4F` only for "all good / closed", amber for attention, red for real
risk. 8 px spacing, 14 px card radius, 10 px controls, Geist, body 16 px,
tabular numerals, 150-220 ms animations for state changes only. Actions are
ink. Copy lives in `src/copy.ts`: short, calm, plain (§69-70).

Rules the view-models enforce (tested): "Everything is under control." only when
nothing is pending and every connection syncs; a stale connection shows
"Action required." and the reconnect sentence, and the month never turns green
(§47-48). Approvals are never remembered and never one-tap.

## Layout

```
app/                 expo-router routes (thin: they render src/screens)
src/offline/         §43 pipeline (pure) + expo/ adapters, background task
src/scan/            §11 quality checks, scan session (pure) + expo adapters
src/share/           §12 routing and ingest (pure)
src/security/        §52 lock state machine + expo-local-authentication, session token
src/api/             client, guards, sample data, sealed cache, expo/fetch transport
src/models/          screen view-models (pure)
src/screens/, src/ui/, src/navigation/, src/app/   React Native UI and wiring
contracts/           upload contract + golden multipart fixture
native/              notes where config plugins do not reach
```

`src/index.ts` exports the pure public API.

## Known gaps

- Sign-in and token refresh are not built; `setSessionToken` is the hook.
- Answers given while offline are not queued; the card says it could not send.
- iOS: emails shared as data (not `.eml` files) are not accepted by the
  generated extension (see the native note).
- Scan quality thresholds need tuning on real device captures.
- The queue index is re-read from disk under an in-process lock; two JS
  runtimes writing at the same moment (rare Android headless start) could lose
  the newer index write. Evidence blobs are never lost: `recover()` re-registers them.
- Not yet built on a device in this repository (no native builds were run here);
  the JS bundles for iOS and Android compile with `npx expo export`.
