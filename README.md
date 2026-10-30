# Intuvora — AI Sales Voice Agent

Businesses create an account, top up a pay-as-you-go wallet, describe their offer and upload an
Excel sheet of leads. An AI voice agent ([Bolna](https://www.bolna.ai)) calls every lead. Results (status, outcome,
summary, transcript) appear live and download as a formatted Excel report.

**Storage:** a PostgreSQL database ([Supabase](https://supabase.com) free tier) in a private `intuvora` schema —
accounts, wallet balances and a transaction log, top-ups, usage, saved business details, campaigns and leads.
Campaigns survive restarts and carry on calling. **Lead lists and call results are deleted
`RESULTS_RETENTION_DAYS` (7) days after a campaign finishes**; download the report before then.
Without `DATABASE_URL` a local SQLite file is used (development and tests).

## Workflow

| Step | What happens | Where |
|---|---|---|
| 1. Account | Email + password (with email confirmation and "Forgot password?") or "Continue with Google", all via Supabase Auth. | `app/supabase_auth.py`, `/login`, `/reset-password` |
| 2. Wallet | Top up via a Razorpay Payment Link. On return the server asks Razorpay if it is paid and credits the wallet once. | `app/payments.py`, `/dashboard` |
| 3. Business details | Business, offer, sales instructions, opening line, language and AI agent (Arjun or Kavya). Pre-filled from the last campaign, with a "Clear form" option. | `PUT /api/campaigns/{id}/setup` |
| 4. Upload leads | `.xlsx` or `.csv` with name and phone columns. Every other column (city, product…) is passed to the AI for that lead. | `app/leads.py` |
| 5. AI calling | Creates a Bolna agent from the campaign (script, welcome message, language) with the chosen agent's voice, calls up to `MAX_CONCURRENT_CALLS` leads at a time, and deletes the agent when done. | `app/bolna.py`, `app/worker.py` |
| 6. Billing | Each answered call is charged per second at `PRICE_PER_MINUTE_PAISE` (₹10/min by default). Unanswered calls are free. Below `LOW_BALANCE_PAISE` (₹20) a campaign can't start and the next lead isn't called; calling resumes automatically after a top-up. Calls already running finish normally. | `app/worker.py` |
| 7. Report | Excel with a **Summary** sheet (every campaign detail, outcome breakdown, talk time, cost), a formatted **Results** sheet and a **Transcripts** sheet. | `app/export.py` |

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cp .env.example .env             # Windows: copy .env.example .env
# fill in RAZORPAY_*, BOLNA_API_KEY, SUPABASE_URL, SUPABASE_ANON_KEY (and DATABASE_URL) in .env
python -m uvicorn app.main:create_app --factory
# open http://localhost:8000
```

The app refuses to start without the Razorpay keys, the Bolna API key and the Supabase URL and anon key. Use Razorpay **test** keys (`rzp_test_...`)
first; Razorpay's redirect back to `localhost` works from your own browser.

### Pricing settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `PRICE_PER_MINUTE_PAISE` | `1000` | ₹10 per minute of answered calls, billed per second |
| `MIN_TOPUP_PAISE` | `10000` | Smallest top-up, ₹100 |
| `LOW_BALANCE_PAISE` | `2000` | Below ₹20: can't start, next lead not called |
| `RESULTS_RETENTION_DAYS` | `7` | Days after a campaign finishes before its leads and results are deleted |

### Supabase (database + sign-in)

1. [supabase.com](https://supabase.com) → **New project** (region: Mumbai). Save the database password.
2. **Database:** Connect → **Session pooler** connection string → `.env` as `DATABASE_URL`
   (replace `[YOUR-PASSWORD]`). The app creates its tables in a private `intuvora` schema on first start;
   it is not reachable through Supabase's public REST API.
3. **API keys:** Project Settings → API → Project URL → `SUPABASE_URL`; `anon` `public` key → `SUPABASE_ANON_KEY`.
   Never put the `service_role` key in the app.
4. **Authentication → Sign In / Providers → Email:** enabled, "Confirm email" on, minimum password length 8.
5. **Authentication → URL Configuration:** Site URL `https://your-domain`; Redirect URLs
   `https://your-domain/login`, `https://your-domain/reset-password` (and the same with `http://localhost:8000` for testing).
6. **Google:** Google Cloud Console → Credentials → OAuth client ID (Web application). Authorized redirect URI:
   `https://<project-ref>.supabase.co/auth/v1/callback`. Paste the Client ID **and** Client Secret into
   Supabase → Authentication → Providers → Google.
7. **Emails:** Supabase's built-in mailer sends only a few emails per hour. For real customers add custom SMTP
   (Authentication → Emails → SMTP, e.g. [Resend](https://resend.com) free tier with your domain) and edit the
   "Confirm signup" and "Reset password" templates to say Intuvora.

### Backups

`.github/workflows/db-backup.yml` dumps the `intuvora` schema every night (02:00 IST), encrypts it with
`BACKUP_PASSPHRASE` and keeps it 14 days as a workflow artifact (lead lists and sessions are left out).
Add repository secrets `DATABASE_URL` and `BACKUP_PASSPHRASE`; it runs once the workflow is on the default branch.
Restore: `gpg -d intuvora-DATE.sql.gz.gpg | gunzip | psql "$DATABASE_URL"`.

### AI agents

Customers choose between two agents in `app/voices.json`; the campaign's calls use the chosen one. Same settings
and sales script, different voice. Both are Sarvam `bulbul:v3` voices, which speak all nine call languages
(English, Hindi, Tamil, Telugu, Kannada, Malayalam, Marathi, Bengali, Gujarati) natively:

| Agent | Voice |
|---|---|
| **Arjun** (male) | Sarvam Shubh (`shubh`) |
| **Kavya** (female) | Sarvam Ritu (`ritu`), the voice behind Sarvam's sales and customer-service styles |

- Speech recognition: Sarvam `saaras:v3` in every language (it knows Indian accents, English included).
- **Colloquial speech:** the sales script opens with "sound like a local person chatting, never like a newsreader"
  and gives, for every language, bookish sentences next to how people really say them, in that language's script
  (`COLLOQUIAL_EXAMPLES` in `app/bolna.py`). Calls use OpenAI `gpt-4.1` (temperature 0.7), which writes far more
  natural Indian-language speech than `gpt-4o-mini`; if the Bolna account doesn't offer it, `gpt-4o-mini` is used.
- **Opening line:** if left empty, a greeting in the call language introduces the agent by name
  (`DEFAULT_GREETINGS`); the setup page shows it and warns when a typed line is in English letters for a
  non-English call. "Can you hear me?" check-ins are per language too.
- **Call handling** (`app/bolna_agent_template.json`): voicemail detection off (it hung up on real people),
  no background "hmm"s, a customer's turn ends after a 0.5 s pause (Sarvam's high-sensitivity cut-off off),
  two words needed to interrupt the agent, hang up after 20 s of silence, calls up to 10 minutes.
- **Language switching:** each agent carries a transcriber and the same voice for every call language
  (Bolna `multilingual` pools, labelled `en`, `hi`, `ta`, …) and starts in the campaign's language. When the
  customer answers in, or asks for, another language, the AI switches (Bolna's `switch_language` tool) and carries
  on in that language, colloquially. Only the languages a call uses are connected.
- If Bolna refuses the agent (400), it is created again with region codes (`ta-IN`), then as a single-language
  agent, so a campaign always starts; the refusals are logged.
- The AI writes replies in the language's own script (e.g. Tamil script, not Tamil in English letters) so the
  voice pronounces them properly; everyday English words stay in English.
- `name` is what customers see and how the agent introduces itself on calls.
- `sample_url`: a link to a short sample of the voice, to show a play button.
- Other `bulbul:v3` voices: female `kavya`, `priya`, `neha`, `pooja`, `simran`, `ishita`, `shreya`; male `aditya`,
  `rahul`, `rohan`, `amit`, `dev`, `varun` (change `voice` and `voice_id`). `bulbul:v2` voices no longer work.

### Outcomes

Answered calls are always marked **Interested** or **Not interested** (or **Wrong number**), never "completed".
Bolna's extraction (configured in the agent's `llm_config`) decides; if it returns nothing, the call summary is used.
A call where the customer agreed a time is marked Interested and the time appears under **Call booked for**.
Calls nobody spoke on show **No answer**, as do unanswered and busy calls.

### Bolna

1. Paid Bolna account (trial accounts can only call verified numbers), an outbound number, and `BOLNA_API_KEY`.
2. `app/bolna_agent_template.json` holds the LLM, transcriber and telephony defaults for every campaign agent.
3. The sales script (`build_system_prompt` in `app/bolna.py`) tells the AI how to sound human on the phone: short
   turns, everyday words, one question at a time, reacting before moving on, following the customer's language mix.
4. Make one test call to your own number and confirm the voice, summary and outcome.

### Going live

Deploy with **https** and set `APP_BASE_URL`. Session cookies are then marked secure, and each campaign agent gets
`https://<your-domain>/api/webhooks/bolna` as its webhook. Run **one** server process (it runs the calling worker).

**Speed:** create the Render service in **Singapore** and the Supabase project in **Mumbai** (or both in the same
region); every database query crosses that distance. A Render service in the US adds roughly 0.25 s per query.

**Render free plan:** start command `uvicorn app.main:create_app --factory --host 0.0.0.0 --port $PORT`; put every
`.env` value in the service's Environment settings (never commit `.env`). Free services sleep after 15 minutes
without visits, which pauses calling until the next request; all data is in Supabase, so nothing is lost and
campaigns resume on wake. To keep it awake during campaigns, ping `https://your-domain/` every 5–10 minutes
(e.g. UptimeRobot) or move to a paid instance. Supabase free projects pause after a week with no activity.

## Website

- `/` — homepage (`app/static/index.html`). Demo video: set `DEMO_VIDEO_URL` near the bottom of the file.
- `/login` — sign in, create account, forgot password, Google.
- `/reset-password` — set a new password from the emailed link.
- `/dashboard` — balance, top-up, new campaign, recent campaigns, usage & payments history.
- `/campaign?id=…` — set up, upload, call, watch results, download the report.

## Tests

```bash
python -m pytest -q
```

Tests use stand-in Bolna, Razorpay and Supabase Auth clients (`tests/conftest.py`) and a temporary SQLite file;
no keys or calls needed. Set `TEST_DATABASE_URL=postgresql://…` to run them against PostgreSQL (the `intuvora`
schema in that database is dropped between tests — use a throwaway database).

## Project layout

```
app/
  main.py        FastAPI routes (create_app factory)
  db.py          database layer: PostgreSQL (Supabase) or SQLite, table definitions
  accounts.py    accounts, sessions, wallet + transaction log, top-ups, usage, saved business details
  supabase_auth.py checks Supabase sign-in tokens
  store.py       campaigns and leads (deleted 7 days after a campaign finishes)
  payments.py    Razorpay Payment Links for top-ups
  bolna.py       Bolna API client, sales script, voices, call languages
  worker.py      call dispatching, result polling, per-second billing
  leads.py       Excel/CSV lead parsing
  export.py      formatted Excel report
  voices.json    the AI agents customers choose from (Arjun, Kavya)
  static/        index.html, login.html, reset-password.html, dashboard.html, campaign.html, auth.js, common.js, style.css
tests/
```
