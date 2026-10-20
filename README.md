# AI Sales Voice Agent — MVP

A one-time-use web app: a business pays with Razorpay, describes its offer, uploads an Excel sheet of leads,
and an AI voice agent ([Bolna](https://www.bolna.ai)) calls every lead. Results (call status, outcome,
summary) appear live and download as Excel.

**Nothing is saved.** Each visit is a fresh campaign kept in server memory only — no database, no files,
no login. The Bolna agent is deleted as soon as the calls finish. Next time, the customer starts again.

## Workflow

| Step | What happens | Where |
|---|---|---|
| 1. Payment | `POST /api/campaigns` creates a campaign and a Razorpay Payment Link. After paying, Razorpay sends the customer back to the campaign page, and the server asks Razorpay whether the link is paid. | `app/payments.py` |
| 2. Business details | Business name, description, product/offer, sales instructions, opening line, language. | `PUT /api/campaigns/{id}/setup` |
| 3. Upload leads | `.xlsx` or `.csv` with name and phone columns. **Every other column** (city, company, last purchase…) is passed to the AI agent for that lead. Numbers are converted to +country format; duplicates and invalid rows are skipped. | `app/leads.py` |
| 4. AI calling | Creates one Bolna agent from `app/bolna_agent_template.json` plus a sales script built from the business details and column names. The worker calls up to `MAX_CONCURRENT_CALLS` leads at a time. | `app/bolna.py`, `app/worker.py` |
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

The app refuses to start without the Razorpay and Bolna keys. Use Razorpay **test** keys (`rzp_test_...`) for
your first runs; Razorpay's redirect back to `localhost` works from your own browser.

### Bolna

1. Paid Bolna account (trial accounts can only call verified numbers), an outbound phone number, and your API key.
2. Check `app/bolna_agent_template.json` (LLM, voice, transcriber, telephony provider) against your Bolna account and the
   [v2 agent API](https://www.bolna.ai/docs/api-reference/agent/v2/create). The app fills in the agent name, opening line,
   webhook URL, sales script and (if missing) `summarization_details` / `extraction_details`.
3. Make one test call to your own number and confirm a summary and outcome come back.

### Razorpay

1. Razorpay Dashboard → Account & Settings → API Keys: generate a key pair.
2. Put both in `.env`, set `PRICE_AMOUNT` in paise (99900 = ₹999).
3. Switch to live keys (`rzp_live_...`) after a full test run.

### Going live

Deploy with **https** and set `APP_BASE_URL` to that address. Bolna then posts call updates to `/api/webhooks/bolna`
(the app only takes the call id from it and fetches the data from Bolna itself). Without https, the app polls Bolna instead.

Run **one** server process: sessions live in that process's memory, and a restart ends any campaign in progress.

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
  bolna.py       Bolna API client and agent/script builder
  worker.py      call dispatching and result polling
  leads.py       Excel/CSV lead parsing, extra columns, phone normalisation
  export.py      results workbook (with formula-injection protection)
  static/        index.html (pay), campaign.html (setup/upload/results)
tests/
```
