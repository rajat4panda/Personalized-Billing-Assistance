#!/usr/bin/env python3
"""
Vidyut Sahaayam — web app + Crest proxy (zero dependencies, stdlib only).

What it does
------------
1. Serves the app (index.html and static files) from this folder.
2. POST /api/run    -> forwards the uploaded bill to your Crest *Billing* agent,
   injecting the API key SERVER-SIDE (the browser never sees the key; no CORS).
3. POST /api/assist -> same, for the Crest *Smart Assist* agent (ask / savings /
   checkup / schemes / solar / grievance).
4. GET  /api/config -> tells the app whether live mode is configured.
5. GET  /healthz    -> always-200 liveness probe (used by Docker/compose/K8s/LB).

Configure with environment variables (see .env.example). Nothing secret is ever
baked into the image — the key is read at runtime from the environment/.env.

Run:
    python3 server.py            # DEMO mode until the CREST_* values are set
Then open http://localhost:8014
"""
import os, ssl, json, posixpath, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv():
    """Load KEY=VALUE lines from a .env file next to this script into os.environ.
    Real environment variables already set take precedence (not overwritten).
    Inline ' #comment' on an UNquoted value is stripped (mirrors whatsapp_server)."""
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
                if val[:1] not in ('"', "'"):
                    if val.startswith("#"):
                        val = ""
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


BASE            = _env("CREST_BASE_URL").rstrip("/")
AGENT_ID        = _env("CREST_AGENT_ID")                 # Billing Assistant (powers /api/run)
ASSIST_AGENT_ID = _env("CREST_ASSIST_AGENT_ID")          # Smart Assist (powers /api/assist)
READER_AGENT_ID = _env("CREST_READER_AGENT_ID")          # Bill Reader — any Indian DISCOM (powers /api/read; optional)
API_KEY         = _env("CREST_API_KEY")
RUN_PATH        = _env("CREST_RUN_PATH") or "/api/v1/agents/{agent_id}/runs"
GET_PATH        = _env("CREST_GET_PATH") or "/api/v1/agents/{agent_id}/runs/{run_id}"
POLL_SECS       = int(_env("CREST_POLL_TIMEOUT") or "150")
AUTH_MODE       = (_env("CREST_AUTH_HEADER") or "bearer").lower()
INSECURE        = _env("CREST_INSECURE_TLS") in ("1", "true", "yes", "on")
BIND            = _env("BIND") or "0.0.0.0"
PORT            = int(_env("PORT") or "8014")
MAX_BODY        = int(_env("MAX_BODY_BYTES") or str(16 * 1024 * 1024))   # 16 MiB request cap
SEND_CSP        = _env("SECURITY_CSP") in ("1", "true", "yes", "on")     # opt-in; off by default

CONFIGURED        = bool(BASE and AGENT_ID and API_KEY)
CONFIGURED_ASSIST = bool(BASE and ASSIST_AGENT_ID and API_KEY)
CONFIGURED_READER = bool(BASE and READER_AGENT_ID and API_KEY)

_ctx = ssl.create_default_context()
if INSECURE:
    _ctx.check_hostname = False
    _ctx.verify_mode = ssl.CERT_NONE

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".ico": "image/x-icon", ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json",
}

# Content-Security-Policy that does not break the app: it uses inline <script>
# and <style>, data: images (QR/preview) and may load an <audio> src. Opt-in via
# SECURITY_CSP=1 once you have confirmed it against your deployment.
CSP = ("default-src 'self'; "
       "script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; "
       "media-src 'self' data: blob: https:; "
       "connect-src 'self'; "
       "font-src 'self' data:; "
       "base-uri 'self'; frame-ancestors 'self'; object-src 'none'")


class Handler(BaseHTTPRequestHandler):
    server_version = "VidyutSahaayam/1.1"

    def _headers(self, extra=None):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        if SEND_CSP:
            self.send_header("Content-Security-Policy", CSP)
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)

    def _reply(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._headers(extra)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- GET: static files + /api/config + /healthz ----
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            return self._reply(200, json.dumps({"ok": True, "mode": "live" if CONFIGURED else "demo"}))
        if path == "/api/config":
            return self._reply(200, json.dumps({
                "configured": CONFIGURED,
                "base": BASE if CONFIGURED else "",
                "agent": AGENT_ID if CONFIGURED else "",
                "assist_configured": CONFIGURED_ASSIST,
                "assist_agent": ASSIST_AGENT_ID if CONFIGURED_ASSIST else "",
                "reader_configured": CONFIGURED_READER,
            }))
        if path == "/":
            path = "/index.html"
        rel = posixpath.normpath(path).lstrip("/")
        full = os.path.realpath(os.path.join(ROOT, rel))
        # confine strictly inside ROOT (no path traversal, no sibling-prefix escape)
        if not (full == ROOT or full.startswith(ROOT + os.sep)) or not os.path.isfile(full):
            return self._reply(404, '{"error":"not found"}')
        ext = os.path.splitext(full)[1].lower()
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError:
            return self._reply(404, '{"error":"not found"}')
        cache = "no-cache" if ext in (".html", ".webmanifest") else "public, max-age=3600"
        return self._reply(200, data, CONTENT_TYPES.get(ext, "application/octet-stream"),
                           extra={"Cache-Control": cache})

    def _auth(self, req):
        if AUTH_MODE in ("bearer", "both"):
            req.add_header("Authorization", "Bearer " + API_KEY)
        if AUTH_MODE in ("x-api-key", "both"):
            req.add_header("X-API-Key", API_KEY)
        req.add_header("Accept", "application/json")
        return req

    def _crest(self, url, method="GET", payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        self._auth(req)
        with urllib.request.urlopen(req, context=_ctx, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"_raw": raw}

    # ---- shared: trigger a run on an agent, poll until done, reply with the final run ----
    def _run_on(self, agent_id, label):
        if not (BASE and agent_id and API_KEY):
            return self._reply(400, json.dumps({
                "error": "Crest is not configured for %s. Set CREST_BASE_URL, CREST_API_KEY and the agent id, then restart." % label
            }))
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_BODY:
            return self._reply(413, json.dumps({"error": "Request too large (limit %d bytes)." % MAX_BODY}))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            incoming = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            incoming = {}
        payload = incoming if "input_payload" in incoming else {"input_payload": incoming}

        run_url = BASE + RUN_PATH.replace("{agent_id}", agent_id)
        try:
            status, run = self._crest(run_url, "POST", payload)
        except urllib.error.HTTPError as e:
            return self._reply(e.code, json.dumps({"error": "Crest returned HTTP %d on trigger" % e.code,
                "url": run_url, "detail": e.read().decode("utf-8", "replace")[:2000]}))
        except Exception as e:
            return self._reply(502, json.dumps({"error": "Could not reach Crest: %s" % e,
                "hint": "Check CREST_BASE_URL. Dev self-signed cert -> CREST_INSECURE_TLS=1."}))

        if isinstance(run, dict) and (run.get("output_payload") is not None or run.get("status") in ("success", "failed", "cancelled")):
            return self._reply(200, json.dumps(run))

        run_id = (run or {}).get("run_id") or (run or {}).get("id")
        if not run_id:
            return self._reply(502, json.dumps({"error": "Trigger did not return a run id.", "trigger_response": run}))

        get_url = BASE + GET_PATH.replace("{agent_id}", agent_id).replace("{run_id}", str(run_id))
        terminal = {"success", "failed", "cancelled", "error", "hitl_pending"}
        deadline = time.time() + POLL_SECS
        last = run
        while time.time() < deadline:
            time.sleep(2)
            try:
                _, last = self._crest(get_url, "GET")
            except Exception as e:
                return self._reply(502, json.dumps({"error": "Poll failed: %s" % e, "run_id": run_id}))
            if isinstance(last, dict) and last.get("status") in terminal:
                return self._reply(200, json.dumps(last))
        return self._reply(504, json.dumps({"error": "Run did not finish within %ds" % POLL_SECS,
            "run_id": run_id, "last_status": (last or {}).get("status")}))

    # ---- POST: /api/run (Billing agent) and /api/assist (Smart Assist agent) ----
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/run":
            return self._run_on(AGENT_ID, "the Billing agent (CREST_AGENT_ID)")
        if path == "/api/assist":
            return self._run_on(ASSIST_AGENT_ID, "the Smart Assist agent (CREST_ASSIST_AGENT_ID)")
        if path == "/api/read":
            # Bill Reader: {attachment_base64, attachment_filename, attachment_content_type} -> normalized bill
            return self._run_on(READER_AGENT_ID, "the Bill Reader agent (CREST_READER_AGENT_ID)")
        return self._reply(404, '{"error":"not found"}')

    def log_message(self, fmt, *args):
        print("  %s - %s" % (self.address_string(), fmt % args))


def main():
    print("\n  Vidyut Sahaayam (web) — http://%s:%d" % (BIND, PORT))
    print("  .env file:", "loaded" if _DOTENV_LOADED else "not found (using shell environment only)")
    print("  CREST_BASE_URL       :", BASE or "(missing)")
    print("  CREST_AGENT_ID       :", AGENT_ID or "(missing)")
    print("  CREST_ASSIST_AGENT_ID:", ASSIST_AGENT_ID or "(missing — Smart Assist Q&A falls back to on-device)")
    print("  CREST_API_KEY        :", ("set, " + str(len(API_KEY)) + " chars") if API_KEY else "(missing)")
    print("  Mode                 :", "LIVE (Crest configured)" if CONFIGURED else "DEMO  <-- set the three CREST_* values for live")
    print("  Smart Assist         :", "LIVE" if CONFIGURED_ASSIST else "not set (on-device fallback)")
    print("  Bill Reader (/api/read):", "LIVE" if CONFIGURED_READER else "not set (non-QR bills fall back to the chooser)")
    print("  Security CSP         :", "on" if SEND_CSP else "off (set SECURITY_CSP=1 to enable)")
    if CONFIGURED:
        print("  Crest run URL (POST) :", BASE + RUN_PATH.replace("{agent_id}", AGENT_ID))
        if INSECURE:
            print("  WARNING: TLS verification is OFF (CREST_INSECURE_TLS). Dev only.")
    print("  Ctrl+C to stop.\n")
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  stopped.")
