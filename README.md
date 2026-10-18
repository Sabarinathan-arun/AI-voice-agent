# AI Sales Voice Agent — MVP

A small web app where a business pays, describes its offer, uploads an Excel sheet of leads, and an
AI voice agent ([Bolna](https://www.bolna.ai)) calls every lead. Results (call status, outcome, summary,
transcript) appear live and can be downloaded as Excel. Campaign data is deleted automatically.

There is no login. Each campaign gets an unguessable link (`/campaign?id=...`), and that link is the access key.

## Workflow

| Step | What happens | Where |
|---|---|---|
| 1. Payment | `POST /api/campaigns` creates a campaign and returns a checkout URL (Stripe Checkout or mock). Payment is confirmed by the Stripe webhook and also on return to the campaign page. | `app/payments.py` |
| 2. Sales setup | Business name, description, product/offer, sales instructions, opening line, language. | `PUT /api/campaigns/{id}/setup` |
| 3. Upload leads | `.xlsx` or `.csv`. Finds the name and phone columns from the headers (or uses column A = name, B = phone), converts numbers to E.164 and drops duplicates and invalid rows. | `app/leads.py` |
| 4. AI calling | Creates one Bolna agent per campaign from `app/bolna_agent_template.json` plus a generated system prompt, then the worker calls the leads, at most `MAX_CONCURRENT_CALLS` at a time. | `app/bolna.py`, `app/worker.py` |
| 5. Track results | The worker checks Bolna executions for status, transcript, summary and extracted outcome. A Bolna webhook speeds this up when the app has a public https URL. | `GET /api/campaigns/{id}` |
| 6. Export | Excel with one row per lead and a campaign info sheet. | `GET /api/campaigns/{id}/export` |
| 7. Auto-deletion | Paid campaigns are deleted `RETENTION_HOURS` after payment; unpaid ones after `UNPAID_RETENTION_HOURS`. The Bolna agent is deleted too. Users can also delete right away. | `CampaignWorker.cleanup` |

## Run locally (no accounts needed)

```bash
pip install -r requirements.txt
cp .env.example .env          # defaults: PAYMENT_PROVIDER=mock, BOLNA_MOCK=true
uvicorn app.main:create_app --factory --reload
# open http://localhost:8000
```

In mock mode, payment is a redirect and calls are simulated: each takes about 6 seconds and ends with a random outcome.

## Going live

1. **Bolna:** set `BOLNA_MOCK=false`, `BOLNA_API_KEY`, and optionally `BOLNA_FROM_NUMBER`. Check
   `app/bolna_agent_template.json` (LLM, voice, transcriber, telephony provider) against your Bolna
   account and the current [v2 agent API](https://www.bolna.ai/docs/api-reference/agent/v2/create). The app
   only fills in the agent name, opening line, webhook URL, system prompt and (if missing)
   `summarization_details` / `extraction_details`. The prompt uses `{customer_name}`, which comes from each call's `user_data`.
2. **Stripe:** set `PAYMENT_PROVIDER=stripe`, `STRIPE_SECRET_KEY`, `PRICE_AMOUNT`, `PRICE_CURRENCY`. Add a webhook
   endpoint for `checkout.session.completed` pointing at `{APP_BASE_URL}/api/payments/stripe/webhook`
   and set `STRIPE_WEBHOOK_SECRET`.
3. **Public URL:** set `APP_BASE_URL` to the app's https URL. Bolna then posts call updates to
   `/api/webhooks/bolna`. The app only uses the execution id from that post and fetches the data itself from the Bolna
   API, so a forged webhook cannot inject results. Without https, the app polls Bolna instead.
4. Run a single process (the worker runs as a background thread inside the web app).

## Outcomes

`interested`, `not_interested`, `callback_requested`, `wrong_number`, `voicemail`, `no_conversation`
come from Bolna's extraction (`extracted_data.outcome`). When Bolna returns no outcome, the app uses the call status instead:
`no_answer`, `busy`, `failed`, `cancelled`, `timed_out` (no final status within `CALL_TIMEOUT_MINUTES`),
or `completed` / `no_conversation`, depending on the transcript.

## Tests

```bash
python -m pytest -q
```

## Project layout

```
app/
  main.py        FastAPI routes (create_app factory)
  config.py      settings from env / .env
  db.py          SQLite schema and queries
  payments.py    Stripe Checkout + mock
  bolna.py       Bolna API client, agent payload builder, mock client
  worker.py      call dispatching, result polling, auto-deletion
  leads.py       Excel/CSV lead parsing and phone normalisation
  export.py      results workbook (with formula-injection protection)
  static/        index.html (pay), campaign.html (setup/upload/results)
tests/
```
