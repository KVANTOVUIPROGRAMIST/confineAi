# Confine

A course-grounded study app, built first for UC San Diego. Students select a class, approve prerequisite references, add private materials, ask for explanations, and upload an assignment to generate a cited PDF solution document.

Live beta: https://confine-7ur2.onrender.com. The deployed service uses the `codex/ucsd-beta` branch in https://github.com/KVANTOVUIPROGRAMIST/confineAi. Manage hosting at https://dashboard.render.com/web/srv-db22fqks728c73ar9rv0 and the private 1 GB database at https://dashboard.render.com/d/dpg-db22co8m7kps73d7g4a0-a. The deployment was created through the Render integration; the Blueprint is also provided for repeatable setup.

## What works

- Account registration and login with hashed passwords, expiring HttpOnly sessions, CSRF protection, and per-account data isolation.
- 993 official UCSD catalog entries from CSE, MATH, MMW, PHYS, ECE, DSC, and COGS, imported from the 2026–27 catalog. The starting set is CSE 21, CSE 29, CSE 190, MATH 183, and MMW 122.
- Search and select courses; inspect official prerequisites and explicitly choose the alternatives you want to allow. Leading zeros and hyphenated codes are accepted in search.
- Class-material mode is the default; original Confine reference packs require opting into catalog mode. Catalog mode with original Confine reference material; class-material mode with uploads and explicitly approved prerequisite references.
- Private PDF/TXT/Markdown uploads, plus provider transcription for images and scanned PDFs. Stored passages retain page numbers.
- Retrieval restricted by account and course, generated steps with passage IDs, citation validation, and a separate semantic evidence check. No web tools, external retrieval, or uploaded-code execution are enabled in tutoring requests.
- Assignment preview with editable questions; durable single-worker processing; source snapshots; progress; PDF and editable Markdown downloads. Unsupported questions are marked rather than given fabricated solutions.
- Free beta: 300 monthly responses. One supported chat answer or assignment question consumes one response. Reservations prevent overspending; unsupported results and technical failures refund unused responses.
- Stripe subscription/top-up integration is implemented but disabled during beta. Planned Student plan is $15/month for 300 responses; $5 top-up adds 100 responses.
- Render Blueprint with managed PostgreSQL. All durable student data and generated PDFs live in the database; no persistent disk is required.

## Run locally

Python 3.12 is recommended. No Node build step is required.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://localhost:8000. Without an API key, accounts, course selection, text-based uploads, and assignment previews work. The app clearly disables live answer generation until a provider is connected; it never substitutes canned answers for real AI output.

### Connect AI

For a first test, create a Gemini API key at https://aistudio.google.com/apikey and put it in the ignored `.env` file:

```dotenv
AI_PROVIDER=gemini
AI_MODEL=gemini-3.5-flash-lite
GEMINI_API_KEY=your-key
```

Restart the server after changing environment variables. Model availability can vary by account. You can choose a supported Gemini model in `AI_MODEL`. Review free-tier limits and data handling at https://ai.google.dev/gemini-api/docs/pricing before uploading sensitive material. Free beta refers to student billing; provider and hosting bills still belong to the app owner.

For OpenAI, set `AI_PROVIDER=openai`, `OPENAI_API_KEY`, and an appropriate `AI_MODEL` (for example `gpt-6-luna`). Both adapters request structured JSON and omit web/search/code tools. Original file content is sent only for scan/image transcription; tutoring sends selected source passages and the student's question.

## Deploy to Render

1. Push this repository to a GitHub or GitLab repository you control.
2. In Render, choose **New → Blueprint**, connect the repository, select the `codex/ucsd-beta` branch, and select `render.yaml`. The web service also explicitly deploys that branch; update both selections when switching to another branch.
3. Review the service and database costs before creating them. The Blueprint specifies paid starter hosting and a small persistent PostgreSQL database; its free beta does not imply free hosting.
4. Set `GEMINI_API_KEY` in the Blueprint prompt or service environment. Leave `BETA_ENABLED=true` for testing.
5. Deploy. Render's `RENDER_EXTERNAL_URL` supplies the public origin automatically. For a custom domain, set `APP_URL=https://your-domain` and redeploy.
6. Create an account at the deployed URL and select your classes.

Do not run multiple web workers or instances in this version: one process owns assignment queue recovery and the in-memory rate limiter. Allow queued and running assignments to finish before deploying changes; this beta recovery strategy assumes the previous worker has stopped. PostgreSQL protects credit balances and payment fulfillment transactionally. Before scaling to thousands of concurrent users, move job ownership and rate limiting into a dedicated queue/shared store and benchmark real workloads.

## Test your courses

- **CSE 21:** upload your notes, or opt into catalog mode to try counting questions; select the CSE 20/MATH alternative you took. Add notes for methods outside the small foundational reference pack.
- **CSE 29:** upload assignment restrictions and C notes. Generated code is displayed as text; the server does not run student code.
- **CSE 190:** enter the actual section topic and upload its syllabus/notes. A variable-topic catalog entry does not authorize any specific advanced subject.
- **MATH 183:** upload your notes, or opt into catalog mode to try binomial/probability questions; select MATH 20C or your approved alternative. Provide tables for questions needing numerical critical values.
- **MMW 122:** upload the assigned readings and prompt. The tutor cannot invent historical claims, quotations, or references from the catalog description alone.

## Course metadata and references

Catalog sources:

- https://catalog.ucsd.edu/courses/CSE.html
- https://catalog.ucsd.edu/courses/MATH.html
- https://catalog.ucsd.edu/courses/MMW.html
- https://catalog.ucsd.edu/courses/PHYS.html
- https://catalog.ucsd.edu/courses/ECE.html
- https://catalog.ucsd.edu/courses/DSC.html
- https://catalog.ucsd.edu/courses/COGS.html

Refresh manually with `python scripts/sync_catalog.py` and restart the server to import the new snapshot. The importer has fixed official endpoints, follows no redirects, and performs no catalog fetching during a student's answer. A format change produces an explicit parser failure rather than silently emptying the catalog. Alternative prerequisite language is retained verbatim; course-code suggestions are not a formal enrollment eligibility checker. Confirm requirements against the linked official entry.

Original references in `app/data/references.json` are authored for this prototype. They are not UCSD instructor-approved. Only a subset of imported courses have reference packs; other courses require uploads. Additional UC campuses are displayed as not yet imported. Each campus needs an importer and appropriate topic mappings. Do not publish uploaded instructor material to a shared library without permission or an appropriate license.

## Enable payments later

In Stripe, create a $15/month recurring price and a $5 one-time top-up price. Configure:

```dotenv
BETA_ENABLED=false
STRIPE_SECRET_KEY=...
STRIPE_WEBHOOK_SECRET=...
STRIPE_STUDENT_PRICE_ID=price_...
STRIPE_TOPUP_PRICE_ID=price_...
```

Add a signed webhook endpoint at `/api/billing/webhook` for `invoice.paid`, `checkout.session.completed`, `checkout.session.async_payment_succeeded`, and `customer.subscription.deleted`. Configure the customer portal. Successful subscription creation/renewal grants 300 responses; paid top-ups grant 100. Browser redirects do not grant credits. Event IDs and Checkout Session IDs prevent duplicate fulfillment. Monthly credits do not roll over; top-ups have no scheduled expiry. Existing beta users should be migrated deliberately before opening paid signup; turning off beta does not silently change their balances or charge them.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --check static/app.js
```

Tests cover authentication/CSRF, cross-account access, retrieval boundaries, citation validation, semantic rejection, credit reservation/refunds/idempotency, assignment exports, and payment fulfillment. Provider calls are mocked during tests; live AI behavior requires a real key and course-specific evaluation.

## Limits before a public launch

This is a working beta, not a guarantee that every generated solution follows an instructor's intended method. Generative checks can miss unsupported reasoning. Keep visible citations and test questions that tempt an advanced shortcut. PDF exports are separately formatted solutions, not a reproduction of the assignment's original layout. Image transcription must be reviewed, especially equations and diagrams.

Add email verification and password recovery before unrestricted public signup. This version uses email/password accounts without an email service. Plan for abuse protection beyond per-process limits, actual storage and token budgets, database backups, and an instructor-reviewed reference library. Student-facing response caps bound question counts; model context/output limits bound individual calls. Provider transcription and preview calls incur owner costs even when no student response is consumed.
