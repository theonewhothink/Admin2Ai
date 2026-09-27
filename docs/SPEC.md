# AI Back-Office Operator — Master Product & Build Specification

Source of truth for product and build decisions. Section numbers are referenced throughout the code.

## 1. The product
An autonomous AI employee responsible for the administrative back office of a small business. It continuously monitors email, bank accounts, credit cards, supplier portals, accounting systems, cloud storage, uploaded documents, mobile scans, government/tax communications and accountant communications. It finds what matters, retrieves the actual supporting evidence, understands it, verifies it, matches it, acts when permitted, follows up when something is missing, and proves that the issue is actually closed. The owner should experience almost none of the complexity.

Promise: **Connect your business once. We handle the rest.** Owner time: **<15 minutes per month** on routine administration.

## 2. North-star experience
> **September is closed.** 218 transactions checked · 186 documents collected · 14 missing documents retrieved automatically · 7 suppliers chased · 3 accountant questions resolved · 2 tax obligations verified · 0 unresolved issues. **You spent 4 minutes.**

Not OCR, not bookkeeping, not expense management, not an AI chat interface. **The product is closure.**

## 3. The golden rule
Every administrative event follows **Discover → Acquire → Understand → Verify → Match → Act → Confirm → Close**. Never Discover → AI guesses → Done. Nothing becomes `CLOSED` merely because an AI believes an action probably happened. Closure requires evidence.

## 4. Zero-configuration principle
The SME is never asked to configure accounting software. Onboarding: (1) create account; (2) enter company VAT/NIF/company number — system retrieves company info; (3) connect email (Google / Microsoft / IMAP); (4) connect bank (Open Banking / PSD2); (5) connect accountant (enter email, invite, or select accounting software); (6) press **START**. Everything else is learned.

## 5. First 10-minute experience
Show "Learning how your business works…" with real progress: Email 12,482 messages analyzed; Documents 638 financial documents discovered; Bank 427 transactions imported; Suppliers 73 identified; Recurring expenses 18; Possible missing documents 11; Entities 1. Then "We understand 96% of your business. We need you to confirm 4 things." e.g. "This €92.40 Vodafone expense appears every month. Is it: Company telecom / Personal / Other" → "I will remember this." Maximum initial questions preferably <10.

## 6. Historical learning
Import previous 90 days by default (option 12 months). Learn suppliers, entities, recurring costs, usual amounts, payment methods, cards, invoice locations, invoice delivery dates, tax treatment patterns, accountant preferences, document naming patterns, likely categories, portal login locations, recurring obligations.

## 7. Core data model
Starts from **EVIDENCE**, not Invoice. Evidence: PDF, image, photo, screenshot, QR, email, HTML, URL, portal, XML, JSON, CSV, XLSX, UBL, SAF-T, bank transaction, card transaction, government notice, WhatsApp attachment, Drive file, ZIP, .eml, accountant message.
Relationship: Source → Evidence → Document/Event → Legal Entity → Financial Transaction / Obligation → Action → Verification → Closure.

## 8. Universal collection engine
Email: Gmail, Google Workspace, Outlook, Microsoft 365, shared accounts, aliases. Reads body, HTML, inline images, attachments, embedded PDFs, links, buttons, entire thread, previous attachments, attached emails. Does not depend on a folder called `Invoices`.

## 9. Link intelligence
For "Your invoice is ready — View Invoice": (1) find URL; (2) check URL safety; (3) follow redirects; (4) open isolated browser session; (5) determine what is behind it; (6) download original invoice when possible; (7) store original URL; (8) store retrieval timestamp; (9) preserve rendered page when needed; (10) start evidence processing. Login required → stored authorized session. MFA → mobile notification "Supplier X needs authentication."; system resumes automatically.

## 10. Supplier portals
Reusable portal adapters (VodafoneConnector, AmazonConnector, UberConnector, MetaConnector…). Each can authenticate, list invoices, retrieve invoice, retrieve statement, retrieve historical documents, detect new documents. Deterministic connectors first; AI browser navigation is fallback only.

## 11. Mobile capture
Purposes: Capture, Approve, Resolve, Ask. Camera: auto document detection, edge detection, auto crop, perspective correction, glare warning, blur detection, rotation, multiple pages, QR detection, automatic upload. No category selection, no amount typing, no supplier typing.

## 12. Mobile share extension
Critical. From WhatsApp, Gmail, Outlook, Safari, Chrome, Photos, Files → Share → Back Office. Accepts URLs, PDFs, screenshots, images, email exports, text. Native iOS Share Extension, native Android Share Intent.

## 13. OCR architecture
Multi-engine and cost-optimized; no single engine trusted. **Stage 0 — structured data first**: XML, UBL, embedded PDF text, QR, barcodes, document metadata, HTML structured data, API response. Structured evidence outranks OCR.

## 14. Primary free OCR stack
Self-host PP-OCRv6. Tiny: edge/mobile, quick preview, quality validation. Medium: primary high-volume server OCR. Covers Portuguese, Spanish, English, major Latin-script languages.

## 15. Complex document OCR
PaddleOCR-VL-1.6 (0.9B document VLM) for difficult layouts, tables, multi-column, forms, skewed pages, poor scans, charts, complex statements, irregular layouts. Self-hostable GPU and x64 CPU.

## 16. Baidu Unlimited-OCR
Experimental/secondary engine for very long PDFs, multi-page statements, difficult sequences, long-horizon parsing. Must sit behind `OCRProviderInterface` so it can be replaced instantly. Never architect around a single model.

## 17. OCR fallback chain
Structured extraction → PP-OCRv6 → (if complex) PaddleOCR-VL → (if long/complex) Unlimited-OCR → (if disagreement remains) independent commercial OCR or multimodal model → (if still ambiguous) human exception. Paid OCR is the exception.

## 18. Field-level verification
For every critical value store `value`, `source`, `confidence`, `bounding box / structured location`, `method`. Critical values: invoice number, supplier VAT number, customer VAT number, gross, net, VAT, currency, issue date, due date, IBAN, payment reference. E.g. Total €1,492.30 supported by PDF text + PP-OCRv6 + QR + arithmetic → `VERIFIED`.

## 19. Portuguese QR verification
Parse Portuguese invoice QR separately. OCR €483.60, QR €483.60, arithmetic €393.17 + VAT = €483.60, bank €483.60 → extremely high confidence. If QR says €438.60 → **CONFLICT**. Never guess.

## 20. Reconciliation engine
Every transaction tries to find evidence using amount, currency, merchant, normalized supplier, payment reference, invoice number, IBAN, card ending, date range, historic pattern, recurring sequence. Support 1→1, 1 payment→many invoices, many payments→1 invoice, deposits, partial payments, refunds, credit notes, card settlements, FX, fees.

## 21. Expected-evidence engine
Not every debit needs an invoice. Vodafone → invoice expected. Transfer between company accounts → none. Tax payment → tax notice/payment proof. Salary → payroll evidence. Loan repayment → loan statement. Bank fee → bank evidence may suffice. Prevents useless chasing.

## 22. Missing document autopilot
Transaction without evidence → search current email, historical email, Drive/files, supplier portal, accounting platform, previous recurring sequence. If absent, contact supplier if policy permits: "Hello, could you please resend invoice FT 2026/183 relating to the €117.20 payment dated 18 September? Thank you." Monitor the thread; when invoice arrives → ingest → verify → match → close. Owner does nothing.

## 23. Recurring expectations
Learn cadence (Uber monthly, Vodafone around the 24th, rent on the 1st, €39 software monthly). "Vodafone normally issues an invoice by the 26th. Today is the 29th. Invoice missing." → retrieve/chase automatically.

## 24. Administrative obligations
Detect tax deadlines, government requests, KYC requests, license renewals, insurance renewal, contract renewal, filing requirements, rent, debt collection, banking requests, payment deadlines. Create `OBLIGATION` with responsible person, amount, date, entity, consequence, required evidence, verification condition.

## 25. Action levels
Fully automatic: reading, extraction, retrieval, classification, reconciliation, duplicate merging, organization, searching, reminders. Automatic if authorized: supplier invoice requests, routine accountant responses, uploads, document delivery. Owner approval: unusual external communication, tax interpretation changes, contractual changes. Hard approval (always): money movement, tax filing, bank detail change, legally binding acceptance, deletion of original evidence.

## 26. Fraud engine
Hard-stop: changed supplier IBAN, changed email domain, new payment recipient, unusual amount, duplicate invoice with different IBAN, unusual country, invoice recipient mismatch, suspicious payment instructions, altered document. Never let AI approve changed beneficiary information.

## 27. Month-end autopilot
Continuous processing. Day −7 completeness audit; Day −5 missing evidence retrieval; Day −3 supplier chases; Day 0 accounting package prepared; Day +1 delivery confirmed; Day +2 accountant queries handled; Final: **MONTH CLOSED**.

## 28. Accountant product
Separate interface. Home table: Client | Month | Complete | Missing | Needs accountant. Client view: reconciliations, supporting evidence, anomalies, tax flags, missing documents, questions, export state. Accountants teach rules once ("Treat all Adobe subscriptions as Software"), scoped to one client or all authorized clients.

## 29. Accountant distribution loop
Accountant creates client → client receives "Your accountant has enabled Back Office for you." → connect email, connect bank. Done.

## 30–33. UI/UX
Calm, not accounting/ERP/AI complexity. Palette: background #F7F8F6, cards #FFFFFF, text #111318, secondary #667085, accent deep emerald (only for all good/closed), amber attention, red real risk only. Geist/Inter-class sans, large headings, body ≥15–16px, tabular numerals. 8px spacing, 12–16px radius, subtle shadow, 150–220ms animations that communicate state only.

## 34–41. Navigation & screens
Desktop nav max: Home, Needs You, Companies, Activity, Ask; settings under profile. Home: "Good morning. Everything is under control." Needs you 2 · Due soon 3 · September 94% closed · companies with status · "Handled for you". No accounting jargon ("We couldn't match this payment", "We can't find the invoice for this payment", "We aren't sure which company this belongs to"). Needs You: decisions not forms, "Why am I seeing this?". One-tap learning: "☑ Always use Hazel Tree for IKEA paid with card •••• 4817". Ask: "Ask your business anything"; answers link to evidence; AI memory is never financial evidence. Mobile nav: Home, Needs You, Scan (center), Activity, Ask.

## 42. Notifications
Rare. Send: approvals needed, IBAN changes/blocked payments, reconnection needed. Never "Invoice successfully processed". Quiet success.

## 43. Mobile offline
capture → encrypt locally → queue → background upload → server hash verified → local copy removed.

## 44. Technical stack
Web: Next.js, React, TypeScript. Mobile: React Native + Expo dev builds; native Swift/Kotlin for share extensions, document scanning. Backend: Python, FastAPI, Pydantic. Orchestration: Temporal. DB: PostgreSQL; pgvector for candidate finding only, never authoritative evidence. Object storage: S3-compatible EU, versioning, KMS. Queues: SQS/EventBridge-class. Cache: Redis. Browser automation: Playwright isolated workers. Infra: AWS EU region, Terraform, GitHub Actions.

## 45. Why Temporal
Workflows wait days (request invoice → wait 6 days → process reply → wait 4 days for accountant). Temporal keeps durable state through restarts, deployments, failures, waiting, retries, human approval.

## 46. Agent architecture
Never one giant agent. Discovery, Retrieval, Document, Verification, Entity, Reconciliation, Missing Evidence, Obligation, Accountant, Fraud, Closure, Auditor agents. A deterministic orchestrator controls all of them.

## 47–48. Self-healing connections
Each connector stores last successful sync, last event, cursor, auth expiry, webhook state, historical coverage, failures. Missed webhook → backfill. OAuth expired → one-tap reconnect. Bank consent expired → mobile notification. If Gmail stopped syncing, the month can **never** show green. UX: "Gmail needs reconnecting. Your email has not synced since 14:42 yesterday. [Reconnect]" — never raw errors.

## 49–50. Country packs
Core global; country pack holds tax terminology, identifiers, VAT rules, fiscal document rules, formats, government integrations, deadlines, exports. First: Portugal (NIF, Portuguese VAT, invoice terminology, invoice-receipts, receipts, credit notes, withholding, ATCUD, Portuguese invoice QR, SAF-T, accounting workflows, regional VAT differences). Integrations: TOConline, Moloni, InvoiceXpress, Portuguese open banking, accountant email. Then Spain, Italy, France, UK, Israel. Never fork by country.

## 51. Multi-entity
One human, many companies, one account, one AI, different rules.

## 52–53. Security & privacy
EU residency, tenant isolation, encryption at rest, TLS, secrets vault, KMS, malware scanning, secure OAuth, MFA, biometric mobile, immutable audit history, access logging, least privilege, export, deletion workflows, DR, tested backups, DPIA, GDPR. No training on customer evidence without authorization. Prefer local OCR; only uncertain evidence goes to external AI; redact bank accounts, addresses, unrelated sensitive content first.

## 54–55. Provenance & audit
Every AI conclusion has "Why?" (e.g. invoice total €83.21, bank charge €83.21, dates 1 day apart, supplier VAT matches, card ending matches, historical pattern monthly). Every operation stores evidence, actor, timestamp, agent, model, parser, extracted values, validations, action, response, corrections. Original never overwritten.

## 56–57. AI quality control
Permanent golden dataset; every model update reruns benchmark; rollout blocked if critical accuracy falls. GREEN verified (multiple sources agree), AMBER likely (not enough for autonomous closure), RED conflict (human needed). Never turn AMBER into GREEN to improve statistics.

## 58–59. Metrics
Activation: email connected, bank connected, historical scan complete, ≥1 document found, ≥1 transaction auto-matched, user sees time saved; time to first value <5 minutes. Owner admin minutes/month <15; zero-touch >95%; auto-resolved missing documents >90%; critical silent errors 0; unresolved month-end items <1%; onboarding <10 minutes; accountant questions requiring owner <20% of baseline.

## 60–63. Go to market
Free Business Audit (90-day scan: expenses, documents, recurring subscriptions, missing evidence, increased subscriptions, other-company items) → "Let me manage this automatically." Pricing: Free, Solo €9, Business €19, Multi-company €29, Accountant practice plan. Accountant flywheel. First market: 1–20 employees, 50–500 monthly events, owner-manager with external accountant.

## 64–68. Dogfood, MVP, build sequence, tests
MVP: one Portuguese company connects Gmail + bank and reaches month-end without manually collecting invoices. Necessary: Gmail, Outlook, bank, PDF, images, links, Paddle OCR stack, duplicates, reconciliation, missing invoice detection, automatic chasing, accountant package, owner exception UX, mobile scan/share. Phases: 0 Design, 1 Evidence, 2 Finance, 3 Closure, 4 Mobile, 5 Portugal, 6 Accountant platform.

## 69–71. Personality & definition of done
Short, calm, precise. "Done." / "I still need one thing." Never "Great job!". Status: "Everything is under control." / "I need 2 things from you." / "Action required." Never show workflow IDs, queues, API errors, parsing states. Success = owner signs up, connects email and bank, identifies accountant, <10 active minutes onboarding, no bookkeeping rules, no sorting, no searching, no chasing, no folder prep, only real exceptions, <15 minutes/month.
