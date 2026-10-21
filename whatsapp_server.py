#!/usr/bin/env python3
"""
Vidyut Sahaayam — WhatsApp channel webhook (zero dependencies, stdlib only).

What it does
------------
A citizen sends a message (or a bill photo) to your WhatsApp Business number.
Meta's WhatsApp Cloud API delivers it to this webhook, which:
  1. verifies the webhook (GET) and the payload signature (POST, HMAC-SHA256),
  2. routes the message to the SAME Crest agents the web app uses
     (Billing Assistant for the explanation/forecast, Smart Assist for
     ask / savings / schemes / checkup / grievance), and
  3. replies over WhatsApp in Telugu + English.

This is a SKELETON: the Crest plumbing, routing, formatting, signature
check and send calls are complete; the two spots that need a real
provider/OCR are marked `TODO`:
  - downloading + reading a bill PHOTO into structured data (QR/OCR),
  - the actual Graph API send (works once WHATSAPP_TOKEN is set; until
    then it runs in DRY_RUN and prints what it would send).

Everything is standard library: http.server, urllib, json, hmac, hashlib.
Configure with environment variables / a .env file next to this script
(see whatsapp.env.example). Nothing secret is baked into the image.

Run:
    python3 whatsapp_server.py          # DRY_RUN until WHATSAPP_TOKEN is set
Then point Meta's webhook at  https://<host>/webhook  with the same
WHATSAPP_VERIFY_TOKEN you set here.
"""
import os, ssl, json, time, hmac, hashlib, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))


# ----------------------------------------------------------------------------
# config (.env auto-load; real env vars win) — mirrors server.py
# ----------------------------------------------------------------------------
def _load_dotenv():
    path = os.path.join(ROOT, ".env")
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.lower().startswith("export "):
                    line = line[7:]
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                # strip an inline comment (" #...") on UNquoted values, so
                # "token   # note" becomes "token"; a '#' inside the value
                # (no preceding space) or inside quotes is kept.
                if val[:1] not in ('"', "'"):
                    if val.startswith("#"):
                        val = ""            # the whole value was just a comment
                    else:
                        hp = val.find(" #")
                        if hp != -1:
                            val = val[:hp]
                val = val.strip().strip('"').strip("'").strip()
                if key and key not in os.environ:
                    os.environ[key] = val
        return True
    except Exception as e:
        print("  WARNING: could not read .env:", e)
        return False


_DOTENV_LOADED = _load_dotenv()


def _env(name, default=""):
    return os.environ.get(name, default).strip().strip('"').strip("'")


# WhatsApp Cloud API
WA_TOKEN       = _env("WHATSAPP_TOKEN")                 # permanent/system-user token
WA_PHONE_ID    = _env("WHATSAPP_PHONE_NUMBER_ID")       # the number's phone_number_id
WA_VERIFY      = _env("WHATSAPP_VERIFY_TOKEN") or "vidyut-verify"
WA_APP_SECRET  = _env("WHATSAPP_APP_SECRET")            # app secret for X-Hub-Signature-256
GRAPH_VER      = _env("GRAPH_API_VERSION") or "v19.0"
DRY_RUN        = _env("WHATSAPP_DRY_RUN") in ("1", "true", "yes", "on") or not WA_TOKEN

# Crest (same values as the web app's server.py)
BASE        = _env("CREST_BASE_URL").rstrip("/")
ASSIST_ID   = _env("CREST_ASSIST_AGENT_ID")             # Smart Assist agent
BILLING_ID  = _env("CREST_AGENT_ID")                    # Billing Assistant agent
API_KEY     = _env("CREST_API_KEY")
RUN_PATH    = _env("CREST_RUN_PATH") or "/api/v1/agents/{agent_id}/runs"
GET_PATH    = _env("CREST_GET_PATH") or "/api/v1/agents/{agent_id}/runs/{run_id}"
POLL_SECS   = int(_env("CREST_POLL_TIMEOUT") or "120")
AUTH_MODE   = (_env("CREST_AUTH_HEADER") or "bearer").lower()
INSECURE    = _env("CREST_INSECURE_TLS") in ("1", "true", "yes", "on")
PORT        = int(_env("PORT") or "8020")

_ctx = ssl.create_default_context()
if INSECURE:
    _ctx.check_hostname = False
    _ctx.verify_mode = ssl.CERT_NONE


# ----------------------------------------------------------------------------
# Crest run API (trigger + poll) — same contract as server.py
# ----------------------------------------------------------------------------
def _crest(url, method="GET", payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if AUTH_MODE in ("bearer", "both"):
        req.add_header("Authorization", "Bearer " + API_KEY)
    if AUTH_MODE in ("x-api-key", "both"):
        req.add_header("X-API-Key", API_KEY)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, context=_ctx, timeout=60) as resp:
        raw = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw}


def crest_run(agent_id, input_payload):
    """Trigger an agent run and poll to completion. Returns the output_payload
    (a dict) or None on any failure — the caller degrades gracefully."""
    if not (BASE and agent_id and API_KEY):
        print("  Crest not configured (CREST_BASE_URL / agent id / CREST_API_KEY).")
        return None
    run_url = BASE + RUN_PATH.replace("{agent_id}", agent_id)
    try:
        run = _crest(run_url, "POST", {"input_payload": input_payload})
    except Exception as e:
        print("  Crest trigger failed:", e)
        return None
    if isinstance(run, dict) and (run.get("output_payload") is not None or run.get("status") in ("success", "failed", "cancelled")):
        return _extract_payload(run)
    run_id = (run or {}).get("run_id") or (run or {}).get("id")
    if not run_id:
        return None
    get_url = BASE + GET_PATH.replace("{agent_id}", agent_id).replace("{run_id}", str(run_id))
    terminal = {"success", "failed", "cancelled", "error"}
    deadline = time.time() + POLL_SECS
    last = run
    while time.time() < deadline:
        time.sleep(2)
        try:
            last = _crest(get_url, "GET")
        except Exception as e:
            print("  Crest poll failed:", e)
            return None
        if isinstance(last, dict) and last.get("status") in terminal:
            return _extract_payload(last) if last.get("status") == "success" else None
    return None


def _extract_payload(run):
    p = run.get("output_payload") if isinstance(run, dict) and run.get("output_payload") is not None else run
    for _ in range(4):
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                break
        elif isinstance(p, dict) and not ("action" in p or "explanation" in p or "answer_te" in p or "status" in p):
            for k in ("output", "result", "result_obj", "data"):
                if k in p:
                    p = p[k]
                    break
            else:
                break
        else:
            break
    return p if isinstance(p, dict) else None


# ----------------------------------------------------------------------------
# WhatsApp send helpers (Graph API) — DRY_RUN prints instead of sending
# ----------------------------------------------------------------------------
def _graph_post(path, payload):
    url = "https://graph.facebook.com/%s/%s" % (GRAPH_VER, path)
    if DRY_RUN:
        print("  [DRY_RUN] POST", url, "\n           ", json.dumps(payload, ensure_ascii=False)[:800])
        return {"dry_run": True}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", "Bearer " + WA_TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=_ctx, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        print("  WA send HTTP", e.code, e.read().decode("utf-8", "replace")[:400])
    except Exception as e:
        print("  WA send failed:", e)
    return None


def wa_text(to, body):
    return _graph_post("%s/messages" % WA_PHONE_ID, {
        "messaging_product": "whatsapp", "recipient_type": "individual",
        "to": to, "type": "text", "text": {"preview_url": False, "body": body[:4096]}})


def wa_menu(to):
    """An interactive list that maps rows to the app's tabs/actions."""
    rows = [
        {"id": "explain",  "title": "Explain my bill",   "description": "బిల్లు వివరణ"},
        {"id": "checkup",  "title": "Check my bill",      "description": "తనిఖీ · anomalies"},
        {"id": "save",     "title": "How can I save?",    "description": "ఆదా చిట్కాలు"},
        {"id": "schemes",  "title": "Schemes for me",     "description": "పథకాలు"},
        {"id": "solar",    "title": "Solar rooftop",      "description": "సోలార్ ROI"},
        {"id": "grievance","title": "Draft a complaint",  "description": "ఫిర్యాదు"},
    ]
    return _graph_post("%s/messages" % WA_PHONE_ID, {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "Vidyut Sahaayam"},
            "body": {"text": "మీ బిల్లుపై ఏం కావాలి? · What would you like?"},
            "action": {"button": "Menu", "sections": [{"title": "Options", "rows": rows}]}}})


# ----------------------------------------------------------------------------
# formatting agent results -> WhatsApp text (Telugu then English)
# ----------------------------------------------------------------------------
def _rs(n):
    try:
        return "Rs " + format(int(round(float(n))), ",d")
    except Exception:
        return "Rs " + str(n)


def fmt_explain(p):
    ex = p.get("explanation") or ""
    sv = p.get("savings") or ""
    out = ex if isinstance(ex, str) else ""
    if sv:
        out = out + "\n\n— " + (sv if isinstance(sv, str) else "")
    fc = p.get("forecast") or {}
    if fc.get("next_total") is not None:
        out = out + "\n\nNext month (est.): " + _rs(fc.get("next_total")) + " · " + str(fc.get("next_units") or "?") + " units"
    return out or "మీ బిల్లు సిద్ధం. · Your bill is ready."


def _bilingual(p):
    te = (p.get("answer_te") or "").strip()
    en = (p.get("answer_en") or "").strip()
    if te and en:
        return te + "\n\n— — —\n\n" + en
    return te or en or ""


def fmt_anomaly(p):
    flags = p.get("flags") or []
    if not flags:
        return "✓ బిల్లు మీ చరిత్రకు అనుగుణంగా ఉంది — సమస్యలేమీ కనిపించలేదు.\n✓ Your bill looks consistent with your history — no problems found."
    lines = ["⚠ తనిఖీ · Review:"]
    for f in flags:
        lines.append("• " + (f.get("title_en") or f.get("code") or "") + " — " + (f.get("detail_en") or ""))
        if f.get("raise_grievance"):
            lines.append("  → Reply 'grievance' for a ready complaint letter.")
    return "\n".join(lines)


def fmt_schemes(p):
    out = ["పథకాలు · Schemes:"]
    for s in (p.get("schemes") or []):
        mark = "✓" if s.get("eligible") else "—"
        out.append(mark + " " + (s.get("name") or "") + ": " + (s.get("note_en") or ""))
    out.append("\nReply 'apply <pm_surya|gruha_jyothi|tod>' for a pre-filled application.")
    return "\n".join(out)


def fmt_whatif(p):
    return ("వాట్-ఇఫ్ · What-if:\nNew usage " + str(p.get("new_units")) +
            " units → bill " + _rs(p.get("new_bill")) +
            "\nMonthly saving " + _rs(p.get("saving")) + " · yearly " + _rs(p.get("yearly")))


def fmt_solar(p):
    return ("సోలార్ · Solar:\nSuggested " + str(p.get("kw")) + " kW · net cost " + _rs(p.get("net_cost")) +
            " (after subsidy " + _rs(p.get("subsidy")) + ")\nMonthly saving " + _rs(p.get("monthly_saving")) +
            " · payback " + str(p.get("payback_years")) + " yrs")


def fmt_appliance(p):
    lines = ["ఆదా · You could save about " + _rs(p.get("monthly_saving")) + "/month (" + _rs(p.get("yearly_saving")) + "/year):"]
    for r in (p.get("recommendations") or []):
        lines.append("• " + (r.get("name") or "") + " — " + (r.get("tip_en") or "") + " (~" + _rs(r.get("saving")) + "/mo)")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# per-sender session (in memory; swap for Crest memory / a DB in production)
# ----------------------------------------------------------------------------
SESSIONS = {}  # phone -> {"bill": {...}, "await": None | "appliances"}

# A couple of built-in demo bills so you can test the flow by texting a keyword.
DEMO_BILLS = {
    "demo": {"consumer_name": "Ramesh Kumar", "service_number": "11xx 4821", "category": "Category-II (Domestic)",
             "billing_month": "Oct 2026", "units_consumed": 420, "total_amount": 2215, "fixed_charges": 90, "electricity_duty": 25,
             "charges": [{"slab": "1-200", "units": 200, "rate": 3.40, "amount": 680},
                         {"slab": "201-300", "units": 100, "rate": 4.80, "amount": 480},
                         {"slab": "301-400", "units": 100, "rate": 7.70, "amount": 770},
                         {"slab": "401-420", "units": 20, "rate": 8.50, "amount": 170}],
             "history": [["Aug", 400], ["Sep", 410], ["Oct", 420]]},
}


def _session(phone):
    return SESSIONS.setdefault(phone, {"bill": None, "await": None})


def _parse_appliances(text):
    """Accept 'AC 1500 6 1; Fridge 200 24 1' -> [{name,watts,hours,qty}]."""
    items = []
    for part in text.replace("\n", ";").split(";"):
        toks = part.strip().split()
        if len(toks) < 2:
            continue
        nums = [t for t in toks if t.replace(".", "", 1).isdigit()]
        words = [t for t in toks if not t.replace(".", "", 1).isdigit()]
        if not nums:
            continue
        watts = float(nums[0])
        hours = float(nums[1]) if len(nums) > 1 else 4.0
        qty = float(nums[2]) if len(nums) > 2 else 1.0
        name = " ".join(words) or "Appliance"
        items.append({"name": name, "watts": watts, "hours": hours, "qty": qty})
    return items


# ----------------------------------------------------------------------------
# routing
# ----------------------------------------------------------------------------
def _need_bill(phone):
    wa_text(phone, "ముందుగా మీ బిల్లు ఫోటో పంపండి (లేదా టెస్ట్‌కు 'demo' అని రాయండి).\n"
                   "Please send a photo of your bill first (or type 'demo' to try it).")


def route_command(phone, cmd, arg=""):
    s = _session(phone)
    bill = s.get("bill")
    if cmd in ("menu", "hi", "hello", "start", "నమస్తే"):
        return wa_menu(phone)
    if cmd == "demo":
        s["bill"] = DEMO_BILLS["demo"]; s["await"] = None
        wa_text(phone, "Demo bill loaded: Ramesh, 420 units.")
        return wa_menu(phone)
    if bill is None:
        return _need_bill(phone)

    if cmd == "explain":
        p = crest_run(BILLING_ID, {"bill": bill, "language": "te-IN"})
        return wa_text(phone, fmt_explain(p) if p else "సేవ అందుబాటులో లేదు. · Service unavailable, please retry.")
    if cmd == "checkup":
        p = crest_run(ASSIST_ID, {"action": "anomaly", "bill": bill})
        return wa_text(phone, fmt_anomaly(p) if p else "Service unavailable, please retry.")
    if cmd == "schemes":
        p = crest_run(ASSIST_ID, {"action": "schemes", "bill": bill})
        return wa_text(phone, fmt_schemes(p) if p else "Service unavailable, please retry.")
    if cmd == "solar":
        p = crest_run(ASSIST_ID, {"action": "solar", "bill": bill, "params": {"units": bill.get("units_consumed")}})
        return wa_text(phone, fmt_solar(p) if p else "Service unavailable, please retry.")
    if cmd == "grievance":
        p = crest_run(ASSIST_ID, {"action": "grievance", "bill": bill})
        return wa_text(phone, _bilingual(p) if p else "Service unavailable, please retry.")
    if cmd == "apply":
        p = crest_run(ASSIST_ID, {"action": "scheme_apply", "bill": bill, "params": {"scheme": arg or "pm_surya"}})
        if p:
            te = p.get("letter_te") or ""; en = p.get("letter_en") or ""
            return wa_text(phone, (te + "\n\n— — —\n\n" + en) if te and en else (te or en))
        return wa_text(phone, "Service unavailable, please retry.")
    if cmd == "save":
        s["await"] = "appliances"
        return wa_text(phone,
            "మీరు ఏ ఎలక్ట్రిక్ ఉపకరణాలు వాడుతున్నారు? పేరు, వాటేజీ, గంటలు/రోజు, సంఖ్య — ఇలా పంపండి:\n"
            "What appliances do you use? Send as 'Name Watts Hours Qty', one per line or ';'-separated.\n"
            "e.g.  AC 1500 6 1 ; Fridge 200 24 1 ; Fan 70 10 3")
    return wa_menu(phone)


def route_text(phone, body):
    s = _session(phone)
    t = (body or "").strip()
    low = t.lower()

    # 1) awaiting an appliance list?
    if s.get("await") == "appliances" and s.get("bill"):
        apps = _parse_appliances(t)
        s["await"] = None
        if not apps:
            return wa_text(phone, "ఫార్మాట్: Name Watts Hours Qty. e.g. 'AC 1500 6 1'")
        p = crest_run(ASSIST_ID, {"action": "appliance_savings", "bill": s["bill"], "params": {"appliances": apps}})
        return wa_text(phone, fmt_appliance(p) if p else "Service unavailable, please retry.")

    # 2) a pasted bill JSON?
    if t.startswith("{") and t.endswith("}"):
        try:
            b = json.loads(t)
            if isinstance(b, dict) and (b.get("units_consumed") is not None or b.get("charges")):
                s["bill"] = b; s["await"] = None
                wa_text(phone, "బిల్లు అందింది. · Bill received.")
                return wa_menu(phone)
        except Exception:
            pass

    # 3) keyword commands
    if low in ("menu", "hi", "hello", "start", "demo", "నమస్తే"):
        return route_command(phone, low)
    if low.startswith("apply"):
        parts = low.split()
        return route_command(phone, "apply", parts[1] if len(parts) > 1 else "")
    for kw, cmd in (("explain", "explain"), ("check", "checkup"), ("scheme", "schemes"),
                    ("solar", "solar"), ("grievance", "grievance"), ("complaint", "grievance")):
        if kw in low:
            return route_command(phone, cmd)
    if any(k in low for k in ("save", "reduce", "lower", "తగ్గించ", "ఆదా")) and "solar" not in low:
        return route_command(phone, "save")

    # 4) free question -> Smart Assist ask (needs a bill)
    if not s.get("bill"):
        return _need_bill(phone)
    p = crest_run(ASSIST_ID, {"action": "ask", "bill": s["bill"], "question": t, "language": "te-IN"})
    return wa_text(phone, _bilingual(p) if p else "Service unavailable, please retry.")


def handle_image(phone, media_id):
    """TODO (production): download the media and extract the bill.
       GET https://graph.facebook.com/<ver>/<media_id> -> {url}; download that
       url with the bearer token; then read the bill from its QR (jsQR-equivalent)
       or via OCR. Until that is wired, ask for the bill as text/JSON."""
    _session(phone)["await"] = None
    wa_text(phone, "ఫోటో అందింది. (డెమోలో ఫోటో చదవడం త్వరలో.) ప్రస్తుతం 'demo' అని రాసి ట్రై చేయండి.\n"
                   "Photo received. (Reading the photo is a production step.) For now type 'demo' to try the flow.")


def dispatch(value):
    """Parse one Cloud API 'value' object and act on each message."""
    for msg in (value.get("messages") or []):
        phone = msg.get("from")
        mtype = msg.get("type")
        if not phone:
            continue
        try:
            if mtype == "text":
                route_text(phone, (msg.get("text") or {}).get("body", ""))
            elif mtype == "interactive":
                inter = msg.get("interactive") or {}
                reply = inter.get("button_reply") or inter.get("list_reply") or {}
                rid = reply.get("id") or ""
                route_command(phone, rid)
            elif mtype == "image":
                handle_image(phone, (msg.get("image") or {}).get("id"))
            elif mtype == "button":
                route_command(phone, (msg.get("button") or {}).get("payload") or "menu")
            else:
                wa_menu(phone)
        except Exception as e:
            print("  handler error:", e)


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------
def _valid_signature(raw, header):
    if not WA_APP_SECRET:
        return True  # skeleton/dev: allow when no app secret is configured
    if not header or not header.startswith("sha256="):
        return False
    mac = hmac.new(WA_APP_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + mac, header)


class Handler(BaseHTTPRequestHandler):
    server_version = "VidyutWhatsApp/1.0"

    def _reply(self, code, body="", ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            return self._reply(200, json.dumps({"ok": True, "dry_run": DRY_RUN}), "application/json")
        if path == "/webhook":
            # Meta verification handshake
            q = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        q[k] = urllib.parse.unquote_plus(v)
            if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == WA_VERIFY:
                return self._reply(200, q.get("hub.challenge", ""))
            return self._reply(403, "verification failed")
        return self._reply(404, "not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/webhook":
            return self._reply(404, "not found")
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        if not _valid_signature(raw, self.headers.get("X-Hub-Signature-256")):
            return self._reply(403, "bad signature")
        # ACK immediately (Meta retries on non-2xx); process after.
        self._reply(200, "EVENT_RECEIVED")
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return
        for entry in (body.get("entry") or []):
            for change in (entry.get("changes") or []):
                dispatch(change.get("value") or {})

    def log_message(self, fmt, *args):
        print("  %s - %s" % (self.address_string(), fmt % args))


# urllib.parse is needed in do_GET; import here to keep the top tidy
import urllib.parse  # noqa: E402


def main():
    print("\n  Vidyut Sahaayam — WhatsApp webhook on http://0.0.0.0:%d/webhook" % PORT)
    print("  .env:", "loaded" if _DOTENV_LOADED else "not found (shell env only)")
    print("  WhatsApp token:", "set" if WA_TOKEN else "(missing) -> DRY_RUN")
    print("  Phone number id:", WA_PHONE_ID or "(missing)")
    print("  Verify token:", WA_VERIFY)
    print("  App secret:", "set (signatures enforced)" if WA_APP_SECRET else "(missing -> signature check skipped, dev only)")
    print("  Crest base:", BASE or "(missing)", "| assist:", ASSIST_ID or "(missing)", "| billing:", BILLING_ID or "(missing)")
    print("  Mode:", "DRY_RUN (prints sends)" if DRY_RUN else "LIVE (sends via Graph API)")
    print("  Ctrl+C to stop.\n")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  stopped.")
