# AI Sales Voice Agent — MVP

A one-time-use web app: a business pays with Razorpay, describes its offer, uploads an Excel sheet of leads,
and an AI voice agent ([Bolna](https://www.bolna.ai)) calls every lead. Results (call status, outcome,
summary) appear live and download as Excel.

**Nothing is saved.** Each visit is a fresh campaign kept in server memory only — no database, no files,
no login. Next time, the customer starts again.

## Workflow

| Step | What happens | Where |
|---|---|---|
| 1. Payment | `POST /api/campaigns` creates a campaign and a Razorpay Payment Link. After paying, Razorpay sends the customer back to the campaign page, and the server asks Razorpay whether the link is paid. | `app/payments.py` |
| 2. Business details | Business name, description, product/offer, sales instructions, opening line, language. | `PUT /api/campaigns/{id}/setup` |
| 3. Upload leads | `.xlsx` or `.csv` with name and phone columns. **Every other column** (city, company, last purchase…) is passed to the AI agent for that lead. Numbers are converted to +country format; duplicates and invalid rows are skipped. | `app/leads.py` |
| 4. AI calling | Creates a Bolna agent for the campaign from the customer's business details, welcome message and language, then calls each lead with it, sending the lead's details as call variables. The agent is deleted when the calls finish. The worker calls up to `MAX_CONCURRENT_CALLS` leads at a time. | `app/bolna.py`, `app/worker.py` |
| 5. Results | Status, outcome and summary per lead, refreshed every 4 seconds. | `GET /api/campaigns/{id}` |
| 6. Export | Excel with the uploaded columns followed by the call results. | `GET /api/campaigns/{id}/export` |

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cp .env.example .env             # Windows: copy .env.example .env
# fill in RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET and BOLNA_API_KEY in .env
python -m uvicorn app.main:create_app --factory
# open http://localhost:8000
```

The app refuses to start without the Razorpay keys and the Bolna API key. Use Razorpay **test** keys (`rzp_test_...`) for
your first runs; Razorpay's redirect back to `localhost` works from your own browser.

### Bolna

Customers never see Bolna, and you never build agents by hand. When a customer clicks **Start calling**, the app
creates a Bolna agent for that campaign from what the customer entered — sales script (business, product,
instructions), welcome message and call language — calls the leads with it, and deletes it when the calls finish.

1. Paid Bolna account (trial accounts can only call verified numbers), an outbound phone number, and your API key
   in `.env` as `BOLNA_API_KEY`.
2. `app/bolna_agent_template.json` holds the defaults every campaign agent starts from: LLM, voice (synthesizer),
   transcriber and telephony provider. Check it against your Bolna account and the
   [v2 agent API](https://www.bolna.ai/docs/api-reference/agent/v2/create). Pick a voice and transcriber that support
   the languages you offer; the app sets the transcriber's `language` code from the customer's choice.
3. Make one test call to your own number and confirm a summary and outcome come back. If agent creation fails, the
   page shows Bolna's error message, which names the setting to fix in the template.

Languages offered on the setup form are listed in `LANGUAGES` in `app/bolna.py`.

### Razorpay

1. Razorpay Dashboard → Account & Settings → API Keys: generate a key pair.
2. Put both in `.env`, set `PRICE_AMOUNT` in paise (99900 = ₹999).
3. Switch to live keys (`rzp_live_...`) after a full test run.

### Going live

Deploy with **https** and set `APP_BASE_URL` to that address. Each campaign agent then gets
`https://<your-domain>/api/webhooks/bolna` as its webhook for faster results (the app only takes the call id from it
and fetches the data from Bolna itself). Without https, the app polls Bolna every few seconds.

Run **one** server process: sessions live in that process's memory, and a restart ends any campaign in progress.

## Website

- `app/static/index.html` — the Intuvora homepage (story, how it works, demo video, features, pricing, FAQ). Every
  "Start a campaign" button opens Razorpay checkout. Price and lead limit are read from `.env`.
- **Demo video:** set `DEMO_VIDEO_URL` near the bottom of `index.html` to a YouTube embed link
  (`https://www.youtube.com/embed/VIDEO_ID`) or an MP4 placed in `app/static` (e.g. `/static/demo.mp4`).
  Empty shows a "coming soon" card.
- `app/static/campaign.html` — the customer's campaign dashboard. `app/static/style.css` styles both pages.

## Outcomes

`interested`, `not_interested`, `callback_requested`, `wrong_number`, `voicemail`, `no_conversation` come from Bolna's
extraction (`extracted_data.outcome`). Otherwise the call status is used: `no_answer`, `busy`, `failed`, `cancelled`,
`timed_out` (no final status within `CALL_TIMEOUT_MINUTES`), or `completed` / `no_conversation` by transcript.

## Tests

```bash
python -m pytest -q
```

Tests use stand-in Bolna and Razorpay clients defined in `tests/conftest.py`, so they need no keys and make no calls.

## Project layout

```
app/
  main.py        FastAPI routes (create_app factory)
  config.py      settings from env / .env
  store.py       in-memory session store (nothing written to disk)
  payments.py    Razorpay Payment Links
  bolna.py       Bolna API client, campaign agent builder, call languages
  worker.py      call dispatching and result polling
  leads.py       Excel/CSV lead parsing, extra columns, phone normalisation
  export.py      results workbook (with formula-injection protection)
  static/        index.html (pay), campaign.html (setup/upload/results)
tests/
```
