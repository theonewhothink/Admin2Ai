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
| `POST /api/evidence`                   | Scan / upload (browser)          | multipart field `file`, any 2xx                                   |

The three POSTs are sent from the browser, so the backend must allow CORS from the web origin.

Evidence ids use prefixes so the UI can link them: `month:<company>:<yyyy-mm>`, `needs:<item id>`, and `company:<id>` become links. Any other id (`doc:`, `txn:`, …) shows as a plain chip.

## Pages

| Route                               | What it is                                                                   |
| ----------------------------------- | ---------------------------------------------------------------------------- |
| `/`                                 | Home: greeting, status line, ask box, tiles, businesses, coming up, handled |
| `/?demo=stale`                      | Home with the "Gmail needs reconnecting" banner                              |
| `/needs-you`                        | Decision cards: IKEA (which company?), Vodafone IBAN change (hard approval)  |
| `/companies`, `/companies/[id]`     | Businesses list; month close (`?month=2026-08`) with "Why?" provenance      |
| `/activity`                         | Quiet timeline of what was handled, grouped by day                          |
| `/ask`                              | Ask box, example prompts, answers with evidence chips (`/ask?q=…`)          |
| `/scan`                             | Upload receipts; explains that capture lives on the phone                   |
| `/settings`                         | Connections, things I remember, notifications (reached from profile menu)   |
| `/onboarding`                       | Six steps: account, company number, email, bank, accountant, Start          |
| `/onboarding/learning`              | Live counters, then four one-tap questions, then Home                       |
| `/audit`                            | Free business audit result                                                  |
| `/accountant`, `/accountant/[id]`   | Accountant workspace (its own layout): clients table, client detail, rules  |

The profile menu (top right) has a **Preview** section that links to the audit, onboarding, and the stale-connection demo. It also has "Bring back answered items", which resets the answers stored for the current browser session.

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

- When the backend is set but a POST fails, the UI still reports success so the demo keeps flowing. Change this before production.
- These pages use sample data only because the backend has no endpoints for them yet: onboarding lookups and connections, the audit, the accountant workspace, and settings' "Things I remember".
- The greeting comes from the data (`HomeData.greeting`). The backend should produce it in the user's time zone.
- Times are shown in Europe/Madrid.
