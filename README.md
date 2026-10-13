# Vidyut Sahaayam — local demo (#6 Personalized Billing Assistance)

Citizen electricity-bill assistant for the Telangana AIKAM challenge, built on Crest.
A consumer gives a bill photo; the agent reads it, explains every charge in Telugu + English,
charts 12 months of usage, forecasts next month, and gives savings tips.

This folder runs the app two ways:

- **Demo mode** — built-in synthetic bills. Works with no setup, nothing to configure. Good for a safe live walkthrough.
- **Live mode** — the uploaded bill is sent to your real Crest agent and the screen shows what Crest returns.

---

## 1. Run it (Demo mode — zero setup)

Only Python 3 is required (no pip installs).

```bash
cd vidyut-sahaayam
python3 server.py
```

Open **http://localhost:8700**. Pick a sample bill, or "Choose file" → it loads a matching sample.

> Prefer no server at all? You can also just open `index.html` in a browser for Demo mode.
> But use `server.py` for Live mode (below), because the browser must call the proxy, not Crest directly.

---

## 2. Run it (Live mode — calls your Crest agent)

First import the agent into Crest: **Agent Studio → Import JSON →** `crest_agent_billing_assistant.json`
(the file from the chat). Note its **agent ID** and create an **API key** that can run it.

Then create a `.env` file (the server auto-loads it) and start:

```bash
cd vidyut-sahaayam
cp .env.example .env         # then edit .env and fill in the three CREST_* values
python3 server.py
```

On boot the server prints what it detected, e.g.:

```
  .env file: loaded
  CREST_BASE_URL: https://dev-crestai.omfysgroup.com
  CREST_AGENT_ID: 1234abcd-...
  CREST_API_KEY : set, 48 chars
  Mode: LIVE (Crest configured)
```

If any line says `(missing)` or Mode is `DEMO`, that value isn't being read — check `.env` is in the same
folder as `server.py` and each line is `KEY=value` (the server tolerates surrounding quotes and spaces).

> Prefer real environment variables? They still work and override `.env`:
> `export CREST_BASE_URL=... CREST_AGENT_ID=... CREST_API_KEY=...` (PowerShell: `$env:CREST_BASE_URL="..."`).
> If your Crest run route differs from the default, set `CREST_RUN_PATH` too (see "Endpoint" below).

Open **http://localhost:8700**. The badge top-right turns **"Live · Crest"**. Now **Choose file** and
upload a real bill photo — it goes to Crest and the result is rendered. (The gear icon ⚙ lets you set a
consumer ID so the agent can pull that consumer's history, and switch Demo/Live by hand.)

The API key stays on the server — the browser only ever calls `http://localhost:8700/api/run`, so there is
no CORS issue and the key is never exposed to the page.

---

## How the live call works (matches the Crest REST API)

The proxy follows the documented run API (Developer Agent Guide §7.1, §15.1):

1. **Trigger:** `POST {CREST_BASE_URL}/api/v1/agents/{agent_id}/runs` with JSON `{"input_payload": {...}}` and
   `Authorization: Bearer crest_…`. Returns `202 Accepted` with a `run_id`.
2. **Poll:** `GET {CREST_BASE_URL}/api/v1/agents/{agent_id}/runs/{run_id}` every 2s until `status` is
   `success` / `failed` / `cancelled`, then reads `output_payload`.

The browser only makes one call to `/api/run`; the proxy does the trigger + polling and returns the finished
run. Defaults (override in `.env` if your build differs):

| var | default |
| --- | --- |
| `CREST_RUN_PATH` | `/api/v1/agents/{agent_id}/runs` |
| `CREST_GET_PATH` | `/api/v1/agents/{agent_id}/runs/{run_id}` |
| `CREST_AUTH_HEADER` | `bearer` (also `x-api-key` or `both`) |
| `CREST_POLL_TIMEOUT` | `150` (seconds) |

### What the app sends
JSON, matching the agent's Trigger `input_schema`:

```json
{ "input_payload": {
    "bill": { "units_consumed": 420, "total_amount": 2215, "billing_month": "Oct 2026",
              "charges": [ { "slab": "1–200", "units": 200, "rate": 3.40, "amount": 680 } ],
              "fixed_charges": 90, "electricity_duty": 25, "sanctioned_load_kw": 3,
              "category": "Category-II (Domestic)", "due_date": "28 Oct 2026",
              "history": [ { "billing_month": "Sep", "units_consumed": 410 } ] },
    "consumer_id": "…", "language": "te-IN" } }
```

> **Why a parsed bill, not a photo:** file upload over the run API isn't documented, so the app sends the
> already-parsed bill as `input_payload.bill`. The agent's **Ingest** node reads it. OCR from a photo is the
> production front-end — add a File Processor node before Ingest (or a `/uploads` step) once you confirm how
> your run API accepts files, and I'll wire the app's file input to it.

### What the app expects back
`output_payload` holding (as an object, or a JSON string — the app parses either, and unwraps `output` /
`result` / `result_json` wrappers):

```json
{ "explanation": "Telugu + English text", "savings": "Telugu + English tips",
  "forecast": { "next_units": 345, "next_total": 1840, "trend_pct": -18, "confidence": "high", "basis": "12-month history" },
  "bill": { "...": "echoed back, including history[]" }, "audio_url": null }
```

Notes:
- `explanation` / `savings` may be strings (shown as text) or `{te,en}` arrays (shown as the bilingual list).
- `bill.history` (array of `{billing_month, units_consumed}` or `[month, units]`) drives the chart; omit it and the chart hides.
- The agent's Compose node already emits exactly these keys — keep them and the app renders unchanged.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Badge stays "Demo" after editing `.env` | The server prints what it loaded on boot — look there. `.env` must sit in the **same folder** as `server.py`, be named exactly `.env` (not `.env.txt`), and restart the server after editing. If the app was opened earlier in Demo, open ⚙ Settings → set Mode to **Live** (your earlier choice is remembered per browser). |
| `Could not reach Crest` | Check `CREST_BASE_URL` and network. Dev self-signed cert → `export CREST_INSECURE_TLS=1` (dev only). |
| `Crest returned HTTP 401/403` | Key lacks run permission, or wrong `CREST_AUTH_HEADER`. Try `bearer` or `x-api-key`. |
| `Crest returned HTTP 404` | Run route mismatch. Default is `/api/v1/agents/{agent_id}/runs` (plural). Check your build and set `CREST_RUN_PATH`/`CREST_GET_PATH`. |
| `Run finished with status: failed` | The run was created but a node errored — check the run trace in Crest. Usually the Ingest/forecast node not finding `input_payload.bill`, or an LLM provider key missing at the org level. |
| `Run did not finish within Ns` | Raise `CREST_POLL_TIMEOUT`, or the agent is stuck (check Crest run trace). |
| Result renders but fields are blank | Output JSON keys differ from the contract above. Align the output node, or the mapping in `index.html` (`normalizeLive`). |
| Telugu shows boxes | Browser lacks a Telugu font; the app loads Noto Sans Telugu from Google Fonts — needs internet on first load. |

---

## Files
- `index.html` — the app (Demo + Live, Telugu/English, chart, forecast, TTS, themes)
- `server.py` — static server + Crest proxy (Python 3 stdlib, no dependencies)
- `.env.example` — the environment variables Live mode reads
- `crest_agent_billing_assistant.json` — import into Crest Agent Studio (also sent separately in chat)

Demo data is synthetic with an illustrative Telangana domestic tariff. Swap in the current TGSPDCL slab
rates (in `index.html`, the `SAMPLES` object) before judging so a department reviewer sees familiar numbers.
