# Intuvora — AI Sales Voice Agent

Businesses create an account, top up a pay-as-you-go wallet, describe their offer and upload an
Excel sheet of leads. An AI voice agent ([Bolna](https://www.bolna.ai)) calls every lead. Results (status, outcome,
summary, transcript) appear live and download as a formatted Excel report.

**What is stored:** customer accounts, wallet balances, top-ups, per-campaign usage totals and each customer's
last-used business details (for auto-fill) — in a SQLite file (`data/accounts.db`).
**What is never stored:** lead lists, phone numbers, transcripts and call results. They live in server memory only and
are dropped 24 hours after the customer's last visit.

## Workflow

| Step | What happens | Where |
|---|---|---|
| 1. Account | "Continue with Google" — signs in or creates the account; Google handles passwords and recovery. | `app/google_auth.py`, `/login` |
| 2. Wallet | Top up via a Razorpay Payment Link. On return the server asks Razorpay if it is paid and credits the wallet once. | `app/payments.py`, `/dashboard` |
| 3. Business details | Business, offer, sales instructions, opening line and language. Pre-filled from the last campaign. | `PUT /api/campaigns/{id}/setup` |
| 4. Upload leads | `.xlsx` or `.csv` with name and phone columns. Every other column (city, product…) is passed to the AI for that lead. | `app/leads.py` |
| 5. AI calling | Creates a Bolna agent from the campaign (script, welcome message, language) with the Intuvora voice, calls up to `MAX_CONCURRENT_CALLS` leads at a time, and deletes the agent when done. | `app/bolna.py`, `app/worker.py` |
| 6. Billing | Each answered call is charged per second at `PRICE_PER_MINUTE_PAISE` (₹10/min by default). Unanswered calls are free. Below `LOW_BALANCE_PAISE` (₹20) a campaign can't start and the next lead isn't called; calling resumes automatically after a top-up. Calls already running finish normally. | `app/worker.py` |
| 7. Report | Excel with a **Summary** sheet (every campaign detail, outcome breakdown, talk time, cost), a formatted **Results** sheet and a **Transcripts** sheet. | `app/export.py` |

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cp .env.example .env             # Windows: copy .env.example .env
# fill in RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, BOLNA_API_KEY and GOOGLE_CLIENT_ID in .env
python -m uvicorn app.main:create_app --factory
# open http://localhost:8000
```

The app refuses to start without the Razorpay keys, the Bolna API key and the Google client ID. Use Razorpay **test** keys (`rzp_test_...`)
first; Razorpay's redirect back to `localhost` works from your own browser.

### Pricing settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `PRICE_PER_MINUTE_PAISE` | `1000` | ₹10 per minute of answered calls, billed per second |
| `MIN_TOPUP_PAISE` | `10000` | Smallest top-up, ₹100 |
| `LOW_BALANCE_PAISE` | `2000` | Below ₹20: can't start, next lead not called |

### Google sign-in

1. [Google Cloud Console](https://console.cloud.google.com/) → create a project.
2. **APIs & Services → OAuth consent screen**: External, app name "Intuvora", your support email. Publish it when going live.
3. **Credentials → Create credentials → OAuth client ID → Web application.**
4. **Authorized JavaScript origins:** `http://localhost:8000` (and `http://localhost`) for testing, plus `https://your-domain` when live.
5. Copy the **Client ID** into `.env` as `GOOGLE_CLIENT_ID`. No client secret is needed.

Open the app at `http://localhost:8000` (not `127.0.0.1`) so it matches the authorized origin.

### AI voice

Every campaign, in every language, uses the first voice in `app/voices.json` — currently **Viraj** (ElevenLabs,
warm and energetic). Customers see its name and can play a sample on the campaign page.

- To change the voice, replace the entry with another voice from Bolna's voice list (`voice_id`, name, gender).
- `sample_url`: paste the voice's `preview_url` from Bolna so customers can hear it before starting.
- `temperature`, `similarity_boost`, `speed`, `style` tune how expressive the voice sounds.

### Bolna

1. Paid Bolna account (trial accounts can only call verified numbers), an outbound number, and `BOLNA_API_KEY`.
2. `app/bolna_agent_template.json` holds the LLM, transcriber and telephony defaults for every campaign agent.
3. The sales script (`build_system_prompt` in `app/bolna.py`) tells the AI how to sound human on the phone: short
   turns, everyday words, one question at a time, reacting before moving on, following the customer's language mix.
4. Make one test call to your own number and confirm the voice, summary and outcome.

### Going live

Deploy with **https** and set `APP_BASE_URL`. Session cookies are then marked secure, and each campaign agent gets
`https://<your-domain>/api/webhooks/bolna` as its webhook. Run **one** server process; campaigns in progress live in
that process's memory. Back up `data/accounts.db` — it holds customer wallets.

## Website

- `/` — homepage (`app/static/index.html`). Demo video: set `DEMO_VIDEO_URL` near the bottom of the file.
- `/login` — "Continue with Google" (sign in or create account).
- `/dashboard` — balance, top-up, new campaign, recent campaigns, usage & payments history.
- `/campaign?id=…` — set up, upload, call, watch results, download the report.

## Tests

```bash
python -m pytest -q
```

Tests use stand-in Bolna and Razorpay clients (`tests/conftest.py`); no keys or calls needed.

## Project layout

```
app/
  main.py        FastAPI routes (create_app factory)
  accounts.py    accounts, sessions, wallet, top-ups, usage, saved business details (SQLite)
  google_auth.py verifies Google sign-in tokens
  store.py       in-memory campaigns and leads (never written to disk)
  payments.py    Razorpay Payment Links for top-ups
  bolna.py       Bolna API client, sales script, voices, call languages
  worker.py      call dispatching, result polling, per-second billing
  leads.py       Excel/CSV lead parsing
  export.py      formatted Excel report
  voices.json    the AI voice used for every call
  static/        index.html, login.html, dashboard.html, campaign.html, common.js, style.css
tests/
```
