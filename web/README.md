# Admin2Ai web (Phase 0)

The clickable web app for the AI back-office operator. It runs entirely on sample data, and uses the backend as soon as one is reachable.

Stack: Next.js 16 (App Router, Turbopack), React 19, TypeScript (strict). Styling is hand-written CSS (`app/globals.css` for tokens and primitives, CSS modules per component). The font is Geist from the `geist` package, bundled locally, so builds work offline. There are no UI or chart libraries.

## Run it

```bash
cd web
npm install
npm run dev        # http://localhost:3000
```

| Script              | What it does                                               |
| ------------------- | ---------------------------------------------------------- |
| `npm run dev`       | Development server                                         |
| `npm run build`     | Production build                                           |
| `npm start`         | Serve the production build                                 |
| `npm run typecheck` | Generate route types (`next typegen`), then `tsc --noEmit` |
| `npm run lint`      | ESLint with `eslint-config-next`                           |
| `npm run test:ocr`  | `node --test` for the in-browser reading's bridge          |
| `npm run build:pages` | The static demo (`out/`): engine, snapshot, OCR files, export |

## Modes

The mode is decided once, at build time, in `lib/mode.ts` (Next.js inlines `NEXT_PUBLIC_*`):

| Mode         | Build with                                                  | What it is                                                                 |
| ------------ | ----------------------------------------------------------- | -------------------------------------------------------------------------- |
| `demo`       | `NEXT_PUBLIC_ENGINE=browser` (`npm run build:pages`)        | The static GitHub Pages site. No sign-in, no redirects, no auth code runs. |
| `production` | `NEXT_PUBLIC_API_URL=…` and `NEXT_PUBLIC_REQUIRE_SIGNIN=1`   | Real customers: sign-in, onboarding against the API, Account in Settings.  |
| `api`        | `NEXT_PUBLIC_API_URL=…` only                                | A local backend in demo mode, no sign-in; falls back to sample data.       |
| `sample`     | nothing                                                     | Sample data only.                                                          |

## Photos and PDFs on the static demo

The static site has no server, so a photo or PDF uploaded on Scan is read in the visitor's browser before it reaches the engine (`lib/ocr.ts`), with no key and nothing sent anywhere:

- **Text:** tesseract.js 7 (Tesseract LSTM in WebAssembly) with the Portuguese and English models.
- **Fiscal QR code:** jsQR on the photo, or on the first pages of a PDF: the strongest second source, so a receipt whose QR code and text agree is verified.
- **PDFs:** pdf.js gives a born-digital PDF's own text and metadata (no OCR needed); a scanned PDF's pages are rendered and read.
- **Blur:** the photo's edge sharpness, measured exactly as the server does, so a photo too blurred to read becomes the same "take it again" task.

What was read goes with the upload (`POST /api/evidence` with `reading`, built in `lib/ocr-bridge.ts`); the engine's `BrowserReader` (`backend/src/backoffice/reading/browser.py`) runs its normal Stage 0, field reading and verification on it. A reading is one source: never verified on its own. The upload, reading included, is journaled, so a reload replays it without reading the file again. The production server ignores readings sent by a browser and reads every file itself.

The engines are self-hosted under `public/ocr/` (`npm run engine:ocr`, part of `build:pages`; copied from `node_modules`, never committed) and loaded only when a photo or PDF is uploaded, so the first load is unchanged: about 18 MB on disk, of which a photo fetches about 8.5 MB once (tesseract.js 0.2 MB, one 3.9 MB WebAssembly core, the Portuguese 1.4 MB and English 3.0 MB models) and a PDF adds pdf.js (1.8 MB). `python tests/e2e.py ocr` (after `npm run build:pages`) uploads a photographed café receipt, a scanned PDF and a blurred photo in Chromium and checks what Documents shows, also after a reload; the Pages workflow runs it before publishing.

## Production mode

```bash
NEXT_PUBLIC_API_URL=https://api.admin2ai.eu \
NEXT_PUBLIC_REQUIRE_SIGNIN=1 \
NEXT_PUBLIC_SUPPORT_EMAIL=support@admin2ai.eu \
npm run build && npm start
```

- **Data loads in the browser.** Every page renders the same client components as the static demo (`components/live/pages.tsx`), and every call goes from the browser to the API with the session cookie (`credentials: "include"`). No page fetches on the server, so nothing has to forward cookies, and nothing is ever filled in from sample data: a failed read shows "This page didn't load.", a failed write says it failed.
- **CSRF.** Every state-changing call carries `X-Requested-With: admin2ai` (`apiFetch` in `lib/api.ts`).
- **Sessions.** A 401 anywhere sends the owner to `/signin?next=<where they were>`; after signing in they come back there (`safeNext` accepts only paths inside the app). The header's name and initials come from `GET /api/auth/me`.
- **Pages.** `/signin` and `/signup` (`app/(flow)/*/page.prod.tsx`), sign-out in the profile menu, "Forgot password?" pointing at `NEXT_PUBLIC_SUPPORT_EMAIL` (the API has no reset endpoint). `/onboarding` runs Company → Email → Bank → Accountant → Start against `POST /api/onboarding/company`, `GET /api/oauth/start`, `POST /api/connections/bank/start` and `POST /api/onboarding/accountant`; the step is kept in `sessionStorage`, so it resumes after the round trip to Google, Microsoft or the bank (`components/flow/OnboardingReturn.tsx` brings the owner back from `/sources?signin=…` or `?ref=…`). Settings gets an **Account** section: signed-in email, download my data (`GET /api/account/export`), delete my account (`POST /api/account/delete`, type DELETE + password).
- **Not shown in production:** the Preview menu, the sample "Things I remember", the sample forwarding address on `/scan`, the sample learning counters (`/onboarding/learning` goes Home).
- **Banks.** The onboarding bank list is `lib/banks.ts` (GoCardless institution ids for Portugal). Check the ids against your GoCardless account and override without a code change: `NEXT_PUBLIC_BANKS="Millennium bcp=MILLENNIUMBCP_BCOMPTPL;Other=OTHER_ID"`.
- **Hosting.** The session cookie is `SameSite=Lax`, so the web app and the API must be on the same site (for example `app.example.eu` and `api.example.eu`, or one origin). The API must allow the web origin with credentials (CORS `Access-Control-Allow-Credentials: true`, the `X-Requested-With` header) and send owners back to the web app after OAuth (`BACKOFFICE_WEB_URL`).
- **Security headers** (server builds only; a static export can't set headers): Content-Security-Policy (`default-src 'self'`, `connect-src` = self + the API origin + api.anthropic.com for the chat's own-key mode, `frame-ancestors 'none'`, `object-src 'none'`), HSTS, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, a Permissions-Policy that turns off camera, microphone, location and payments, and COOP. Scripts and styles allow `'unsafe-inline'` because Next.js hydrates with inline scripts and the components use style props.

### Browser checks

`python tests/e2e.py production` builds in production mode against a small mock of the production API written in the test (cookie session, CSRF header, 401s; data endpoints answered by the real demo engine) and drives Chromium through sign-in, sign-up, onboarding, Settings → Account and sign-out, checking the headers, labels, `aria-describedby`, focus and plain-language errors. `python tests/e2e.py demo` (after `npm run build:pages`) checks that the static demo never redirects to sign-in or calls an auth endpoint; `python tests/e2e.py ocr` checks that photos and scanned PDFs are read in the browser (above). Needs `pip install playwright` and `python -m playwright install chromium`; `SCREENSHOT_DIR=…` saves screenshots at 1440 and 390 px. CI runs the production check.

## Connecting the backend

Set `NEXT_PUBLIC_API_URL` **at build time** (Next.js inlines it), for example:

```bash
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run build && npm start
```

All data access goes through `lib/api.ts`. Each call works as follows:

- If `NEXT_PUBLIC_API_URL` is unset, it returns sample data from `lib/data.ts` and the pages are prerendered statically.
- If it is set, it calls the backend with a 4 s timeout and no caching, and those pages render per request.
- If the request fails, times out, returns a non-2xx status, is not JSON, or has an unexpected shape, it falls back to sample data. It logs a warning in development and never shows an error to the user.

| Method & path                          | Used by                          | Expected response (see `lib/types.ts`)                            |
| -------------------------------------- | -------------------------------- | ----------------------------------------------------------------- |
| `GET /api/home`                        | Home, Settings                   | `HomeData`                                                        |
| `GET /api/needs-you`                   | Needs you, nav badge, Home       | `NeedsYouItem[]` or `{ items: NeedsYouItem[] }`                   |
| `POST /api/needs-you/{id}/answer`      | Decision cards (browser)         | body `{ option_id, remember }`, any 2xx                           |
| `GET /api/activity`                    | Activity                         | `ActivityItem[]` or `{ today?, items }`                           |
| `GET /api/companies`                   | Companies, company page          | `CompanySummary[]` or `{ companies: [...] }`                      |
| `GET /api/months/{company_id}/{yyyy-mm}` | Company page                   | `MonthClose`                                                      |
| `POST /api/ask`                        | Ask (browser)                    | body `{ question }` → `{ answer, evidence: [{ label, id }] }`     |
| `POST /api/evidence`                   | Scan / upload (browser)          | multipart field `file`, any 2xx (static demo: JSON with `reading`) |

The three POSTs are sent from the browser, so the backend must allow CORS from the web origin.

Evidence ids use prefixes so the UI can link them: `month:<company>:<yyyy-mm>`, `needs:<item id>`, and `company:<id>` become links. Any other id (`doc:`, `txn:`, …) shows as a plain chip.

## Pages

| Route                               | What it is                                                                   |
| ----------------------------------- | ---------------------------------------------------------------------------- |
| `/`                                 | Home: greeting, status line, ask box, tiles, businesses, coming up, handled |
| `/?demo=stale`                      | Home with the "Gmail needs reconnecting" banner                              |
| `/needs-you`                        | Decision cards: IKEA (which company?), Vodafone IBAN change (hard approval), a supplier website's one-time sign-in code (`kind: "code"`: one box, `POST { code }` to the item's `code.submitPath`, `/api/portals/{id}/code`; the list is read again once it goes through) |
| `/sources`                          | Everything connected or learned (`GET /api/sources`), with "+ Add" per group (`POST /api/sources`): email, banks, cards, supplier websites (`kind: "portal"`), cloud storage (`kind: "files"`: Google Drive, OneDrive or SharePoint), accounting software (`kind: "accounting"`: TOConline, Moloni, InvoiceXpress), suppliers and the rest; remove (`POST /api/sources/{id}/remove`). Production: last month from the accounting software as a ZIP (`GET /api/accounting/{id}/export?month=`) |
| `/companies`, `/companies/[id]`     | Businesses list; month close (`?month=2026-08`) with "Why?" provenance and what is left (including "Catching up on 3 days of email from …" while a connection reads back days it missed); the EU VAT register's details in one tap while the owner has not chosen (`identityCheck`, `POST /api/companies/{id}/identity { use }`) |
| `/activity`                         | Quiet timeline of what was handled, grouped by day                          |
| `/ask`                              | Ask box, example prompts, answers with evidence chips (`/ask?q=…`)          |
| `/scan`                             | Upload receipts; explains that capture lives on the phone                   |
| `/settings`                         | People, deadlines and plan links; "What I do on my own" switches (`GET/POST /api/settings/automation`); monthly report; connections; notifications (reached from profile menu) |
| `/settings/people`                  | People and their company cards (`GET/POST /api/employees`, `POST /api/employees/{id}`); expense claims with one-tap "pay them back" or not (`GET/POST /api/expense-claims`, answered through `POST /api/needs-you/{id}/answer`) |
| `/settings/plan`                    | Plan, this month's use against its limits (`GET /api/billing`); "Choose …" goes to Stripe's page (`POST /api/billing/checkout`), payment details to its portal (`POST /api/billing/portal`). The demo says it is the demo and never calls either |
| `/deadlines`                        | Every deadline (`GET /api/obligations`): who does it, what proves it done, and "It is done" with the confirmations the API offers (`POST /api/obligations/{id}/done`) |
| `/companies/cost-center?id=…`       | One job, property, vehicle …: spent, received, open items, payments and documents with their originals, and for a property the owner statement (`GET /api/cost-centers/{id}`, `…/statement?month=`); rename, owner and fee, archive (`POST /api/cost-centers/{id}`). The company page lists them in the company's own word (`GET/POST /api/companies/{id}/cost-centers`) |
| `/documents/detail?id=…`            | One document (`GET /api/documents/{id}`): proof, payments, credit notes and refunds, parts and held back, import chain, a supplier statement checked line by line, history; "Mark as sensitive" (`POST /api/documents/{id}/sensitive`) |
| `/payments/detail?id=…`             | One payment (`GET /api/transactions/{id}`): what it needs, its documents and why they match, history; "It never has an invoice / It always has one" (`POST /api/transactions/{id}/evidence`) |
| `/onboarding`                       | Six steps: account, company number, email, bank, accountant, Start (production: five, against the API) |
| `/signin`, `/signup`                | Production only (server builds; not in the static demo)                     |
| `/onboarding/learning`              | Live counters, then four one-tap questions, then Home                       |
| `/audit`                            | Free business audit result                                                  |
| `/accountant`, `/accountant/[id]`   | Accountant workspace (its own layout): clients table and client invitations; client detail with the reconciliation (payment ↔ document, "Why?"), what is still open, the originals as links (`GET /api/accountant/clients/{id}/evidence/{evidence}/file`), export (`…/export`) and rules (`POST …/rules`) |
| `/invite`                           | Production only: the invited owner accepts their accountant's invitation (`POST /api/invitations/accept`) |

The top bar has exactly the five places of spec §34 (Home, Needs You, Companies, Activity, Ask). Everything else is in the profile menu (top right), two taps away: Documents, Deadlines, People and expenses, Sources, Diagram, Plan, Settings, Add documents (and, on phones, Your businesses). Detail pages that take an `?id=` read it in the browser, because ids are made at run time (a new job, a new document) and the static demo cannot pre-build a page per id.

The profile menu also has a **Preview** section that links to the audit, onboarding, and the stale-connection demo. It also has "Bring back answered items", which resets the answers stored for the current browser session.

## How the demo keeps its state

Answers given on Needs you are saved in `sessionStorage` (`lib/resolved-store.ts`), so Home, the nav badge and company statuses update right away while the app runs on sample data. With a live backend, answered items stop coming back from the API and this store has no visible effect.

## Design system

The tokens live at the top of `app/globals.css`:

- Colours: background `#F7F8F6`, cards `#FFFFFF`, text `#111318`, secondary `#667085`.
- Emerald `#0F6B4F` is used almost only for "all good" or "closed". Amber means attention; red is kept for real risk. Primary actions are ink, not colour.
- 8px spacing scale, 14px card radius, near-invisible shadows, and 150–220 ms transitions that only communicate state.
- Tabular numerals everywhere.
- Phones get a 16px gutter and a bottom nav (Home, Needs You, Scan, Activity, Ask). Desktop uses a top nav with five items; settings sit in the profile menu.
- `prefers-reduced-motion` is respected.

## Known Phase 0 gaps

- In `api` mode (backend without sign-in), a failed POST still reports success so the demo keeps flowing. Production mode reports it.
- These pages use sample data only because the backend has no endpoints for them yet: onboarding lookups and connections, the audit, the accountant workspace, and settings' "Things I remember".
- The greeting comes from the data (`HomeData.greeting`). The backend should produce it in the user's time zone.
- Times are shown in Europe/Lisbon, the same time zone as the engine (Portugal pack).
