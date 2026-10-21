# Vidyut Sahaayam — production deployment

Citizen electricity-bill assistant (Telangana AIKAM #6, "Personalized Billing
Assistance"), running on the **OMFYS Crest** agentic-AI platform. Two channels,
one stack, one shared Crest config:

| Service    | What it is                                   | Port  | Reach it at            |
|------------|----------------------------------------------|-------|------------------------|
| `web`      | Citizen web app + Crest proxy (key stays server-side) | 8014 | `http://<host>:8014`    |
| `whatsapp` | WhatsApp Cloud API webhook                   | 8020  | `http://<host>:8020/webhook` |

Both are **pure-stdlib Python** (no third-party packages), so images are tiny and
build offline. The Crest API key and WhatsApp token are **injected at runtime** —
never baked into an image, never committed.

---

## 1. Deploy (3 steps)

```bash
cp .env.example .env          # then edit .env — see section 2
docker compose up -d --build
./smoke_test.sh               # verify both services are healthy
```

Open `http://<host>:8014`. Without the `CREST_*` values the app still runs in
**Demo mode** (on-device answers) so you can see the UI before wiring Crest.

Requirements on the host: Docker Engine + Docker Compose v2. Open inbound
**8014** (web) and **8020** (WhatsApp webhook) — or front them with your load
balancer (section 4).

## 2. Configure `.env`

Fill in the Crest connection (shared by both services):

```
CREST_BASE_URL=http://host.docker.internal:8000   # Crest BACKEND API (port 8000), not the UI on 3000
CREST_AGENT_ID=<Billing Assistant agent UUID>
CREST_ASSIST_AGENT_ID=<Smart Assist agent UUID>
CREST_API_KEY=<your Crest key>          # kept server-side; never sent to the browser
```

- Leave the `CREST_*` blank → **Demo mode**.
- Dev self-signed Crest cert only → `CREST_INSECURE_TLS=1` (keep `0` in prod).

WhatsApp (only the `whatsapp` service uses these; blank `WHATSAPP_TOKEN` →
**DRY_RUN**, which logs what it would send instead of calling Meta):

```
WHATSAPP_TOKEN=<permanent/system-user token>
WHATSAPP_PHONE_NUMBER_ID=<the sending number's phone_number_id>
WHATSAPP_VERIFY_TOKEN=vidyut-verify     # must match what you type in Meta's webhook setup
WHATSAPP_APP_SECRET=<app secret>        # when set, request signatures are enforced
```

> **Never commit `.env`.** `.gitignore` and `.dockerignore` already exclude it.

## 2b. Optional: universal Bill Reader (any Indian DISCOM, PDF or photo)

Today a bill is read from its **QR code** in the browser. Bills without a readable
QR (a plain photo, a PDF, another state's DISCOM) fall back to a demo chooser.
The **Bill Reader agent** closes that gap — and it is **additive**: your live
Billing Assistant agent is not modified, and if the reader is not configured the
app behaves exactly as before.

1. In Crest → Agents → **Import**, upload `agents/bill_reader_india.crest.json`.
   It is a 4-node workflow: trigger → **File Processor** (schema-driven, any
   DISCOM) → normalize + validation gate → output. Copy its agent UUID.
2. In `.env`: `CREST_READER_AGENT_ID=<that UUID>`, then `docker compose up -d`.
3. `./smoke_test.sh` should now show *Bill Reader configured (/api/read)*.

Flow: QR first (exact, instant) → otherwise the file goes to `/api/read` → the
agent returns the same `bill` object the Billing Assistant consumes → the normal
explain/forecast/assist run proceeds. Low-confidence reads show a *please verify
units and total* notice instead of silently guessing.

Notes: the File Processor OCRs photos with on-device Tesseract (sovereign; the
backend image must have `tesseract-ocr` + `poppler-utils`). It runs English OCR by
default — fine for most Indian bills, which print figures and labels in English;
regional-script-only bills can use a cloud OCR provider (`ocr_provider` +
`ocr_connection_id` on the node) or a small executor patch to pass a language pack.

## 2c. Required once: the Crest OCR patch (for bill *photos*)

Photos of thermal spot bills come back as "0 fields found, OCR used" until the
Crest backend is patched — see `crest-patch/README.md` (3 files, ~40 lines,
additive). Apply it in the crest-platform repo, rebuild the backend, and use
Bill Reader agent v1.0.2 from `agents/`.

## 3. Verify

```bash
./smoke_test.sh            # health + config + WhatsApp verify handshake
./smoke_test.sh --live     # also fires ONE real Billing run through the proxy
```

`--live` is the real end-to-end check: it confirms the proxy can reach Crest,
authenticate, trigger a run, poll it, and get a result back. Run it from the host
(or anywhere that can reach the web service) once `.env` points at a reachable
Crest instance.

Manual checks:
```bash
curl http://<host>:8014/healthz        # {"ok": true, "mode": "live"|"demo"}
curl http://<host>:8014/api/config     # what the browser sees
curl "http://<host>:8020/webhook?hub.mode=subscribe&hub.verify_token=vidyut-verify&hub.challenge=ping"   # -> ping
```

## 4. TLS / HTTPS (citizen-facing)

There is no public domain yet, so the stack serves **plain HTTP**. Two options:

**Now — terminate TLS at your OCI load balancer / reverse proxy.** Point:
- `https://<your-host>/` → `web:8014`
- your webhook hostname/path → `whatsapp:8020` (Meta requires HTTPS for the webhook)

**Later — when you have a domain,** let the bundled Caddy do automatic HTTPS:
```bash
cp Caddyfile.example Caddyfile        # set your domain + email
docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d --build
```
Caddy fetches and renews a Let's Encrypt certificate, serves `/webhook*` →
WhatsApp and everything else → the web app on one hostname, and stops publishing
the plain-HTTP ports to the host.

## 5. Point WhatsApp at this webhook

In the Meta app → WhatsApp → Configuration → Webhook:
- **Callback URL:** `https://<your-public-host>/webhook`
- **Verify token:** the same `WHATSAPP_VERIFY_TOKEN` from `.env`
- Subscribe to the **messages** field.

Until a token is set the webhook runs in DRY_RUN (verification + routing still
work; sends are logged, not delivered). One production step remains in
`whatsapp_server.py` (marked `TODO`): downloading a bill **photo** and reading it
(QR/OCR). Text, menu, `demo`, and pasted-bill flows work today.

## 6. Operate

```bash
docker compose ps                      # status + health
docker compose logs -f web             # app + proxy logs (startup banner shows mode)
docker compose logs -f whatsapp        # webhook logs
docker compose up -d --build           # redeploy after a change
docker compose down                    # stop
```

Both services have Docker **healthchecks** (`/healthz`) and `restart:
unless-stopped`. Edit `.env` and `docker compose up -d` to apply config changes.

## What's in this folder

```
index.html              the web app (single file; bilingual Telugu/English, offline-capable)
server.py               web server + Crest proxy (/api/run, /api/assist, /api/config, /healthz)
whatsapp_server.py      WhatsApp Cloud API webhook (verify, signature, routing, Crest calls)
Dockerfile              web image
Dockerfile.whatsapp     webhook image
docker-compose.yml      the stack (web + whatsapp)
docker-compose.tls.yml  optional Caddy HTTPS front (needs a domain)
Caddyfile.example       Caddy config template
.env.example            shared config template (copy to .env)
smoke_test.sh           post-deploy health/config/live check
agents/                 bill_reader_india.crest.json (import into Crest) + its universal bill schema
```

Security posture: non-root containers, API key server-side only, request-size
cap, path-traversal-safe static serving, security headers, WhatsApp signature
enforcement when the app secret is set, and no secrets in the image or git.
