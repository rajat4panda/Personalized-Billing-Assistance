#!/usr/bin/env python3
"""
Vidyut Sahaayam — local dev server + Crest proxy (zero dependencies, stdlib only).

What it does
------------
1. Serves the app (index.html and static files) from this folder.
2. Exposes POST /api/run  -> forwards the uploaded bill to your Crest agent's run
   API, injecting the API key SERVER-SIDE (the browser never sees the key, and
   there is no CORS problem because the browser only ever talks to this server).
3. Exposes GET /api/config -> tells the app whether live mode is configured.

Configure with environment variables (see .env.example):
    CREST_BASE_URL      e.g. https://dev-crestai.omfysgroup.com
    CREST_AGENT_ID      the Billing Assistant agent's UUID in Crest
    CREST_API_KEY       an API key / token with permission to run the agent
    CREST_RUN_PATH      run-endpoint path template; {agent_id} is substituted
                        default: /api/v1/agents/{agent_id}/run   <-- VERIFY against your Crest build
    CREST_AUTH_HEADER   how to send the key: "bearer" (Authorization: Bearer ...),
                        "x-api-key" (X-API-Key: ...), or "both"   default: both
    CREST_INSECURE_TLS  set to 1 to skip TLS verification (dev self-signed certs only)
    PORT                default 8700

Run:
    CREST_BASE_URL=https://dev-crestai.omfysgroup.com \
    CREST_AGENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx \
    CREST_API_KEY=sk_... \
    python3 server.py
Then open http://localhost:8700

Without the CREST_* vars the app still runs in Demo mode.
"""
import os, ssl, json, posixpath, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """Load KEY=VALUE lines from a .env file next to this script into os.environ.
    Real environment variables already set take precedence (not overwritten)."""
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
                val = val.strip().strip('"').strip("'").strip()  # tolerate quotes/spaces
                if key and key not in os.environ:
                    os.environ[key] = val
        return True
    except Exception as e:
        print("  WARNING: could not read .env:", e)
        return False


_DOTENV_LOADED = _load_dotenv()

def _env(name, default=""):
    return os.environ.get(name, default).strip().strip('"').strip("'")

BASE       = _env("CREST_BASE_URL").rstrip("/")
AGENT_ID   = _env("CREST_AGENT_ID")
API_KEY    = _env("CREST_API_KEY")
RUN_PATH   = _env("CREST_RUN_PATH") or "/api/v1/agents/{agent_id}/runs"
GET_PATH   = _env("CREST_GET_PATH") or "/api/v1/agents/{agent_id}/runs/{run_id}"
POLL_SECS  = int(_env("CREST_POLL_TIMEOUT") or "150")
AUTH_MODE  = (_env("CREST_AUTH_HEADER") or "bearer").lower()
INSECURE   = _env("CREST_INSECURE_TLS") in ("1", "true", "yes", "on")
PORT       = int(_env("PORT") or "8700")
CONFIGURED = bool(BASE and AGENT_ID and API_KEY)

_ctx = ssl.create_default_context()
if INSECURE:
    _ctx.check_hostname = False
    _ctx.verify_mode = ssl.CERT_NONE

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".ico": "image/x-icon", ".woff2": "font/woff2",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "VidyutSahaayam/1.0"

    def _reply(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- GET: static files + /api/config ----
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/config":
            return self._reply(200, json.dumps({
                "configured": CONFIGURED,
                "base": BASE if CONFIGURED else "",
                "agent": AGENT_ID if CONFIGURED else "",
            }))
        if path == "/":
            path = "/index.html"
        # resolve safely inside ROOT (no path traversal)
        rel = posixpath.normpath(path).lstrip("/")
        full = os.path.join(ROOT, rel)
        if not os.path.abspath(full).startswith(ROOT) or not os.path.isfile(full):
            return self._reply(404, '{"error":"not found"}')
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as fh:
            data = fh.read()
        return self._reply(200, data, CONTENT_TYPES.get(ext, "application/octet-stream"))

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

    # ---- POST: /api/run — trigger a run, poll until done, return the final run ----
    def do_POST(self):
        import time
        path = self.path.split("?", 1)[0]
        if path != "/api/run":
            return self._reply(404, '{"error":"not found"}')
        if not CONFIGURED:
            return self._reply(400, json.dumps({
                "error": "Crest is not configured. Set CREST_BASE_URL, CREST_AGENT_ID and CREST_API_KEY, then restart."
            }))
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            incoming = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            incoming = {}
        # the browser sends {"input_payload": {...}}; forward verbatim
        payload = incoming if "input_payload" in incoming else {"input_payload": incoming}

        run_url = BASE + RUN_PATH.replace("{agent_id}", AGENT_ID)
        try:
            status, run = self._crest(run_url, "POST", payload)
        except urllib.error.HTTPError as e:
            return self._reply(e.code, json.dumps({"error": "Crest returned HTTP %d on trigger" % e.code,
                "url": run_url, "detail": e.read().decode("utf-8", "replace")[:2000]}))
        except Exception as e:
            return self._reply(502, json.dumps({"error": "Could not reach Crest: %s" % e,
                "hint": "Check CREST_BASE_URL. Dev self-signed cert -> CREST_INSECURE_TLS=1."}))

        # If the trigger already returned a finished result, pass it straight through.
        if isinstance(run, dict) and (run.get("output_payload") is not None or run.get("status") in ("success", "failed", "cancelled")):
            return self._reply(200, json.dumps(run))

        run_id = (run or {}).get("run_id") or (run or {}).get("id")
        if not run_id:
            return self._reply(502, json.dumps({"error": "Trigger did not return a run id.", "trigger_response": run}))

        get_url = BASE + GET_PATH.replace("{agent_id}", AGENT_ID).replace("{run_id}", str(run_id))
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

    def log_message(self, fmt, *args):  # quieter console
        print("  %s - %s" % (self.address_string(), fmt % args))


def main():
    print("\n  Vidyut Sahaayam — http://localhost:%d" % PORT)
    print("  .env file:", "loaded" if _DOTENV_LOADED else "not found (using shell environment only)")
    print("  CREST_BASE_URL:", BASE or "(missing)")
    print("  CREST_AGENT_ID:", AGENT_ID or "(missing)")
    print("  CREST_API_KEY :", ("set, " + str(len(API_KEY)) + " chars") if API_KEY else "(missing)")
    print("  Mode:", "LIVE (Crest configured)" if CONFIGURED else "DEMO  <-- set all three CREST_* values for live")
    if CONFIGURED:
        print("  Crest run URL (POST):", BASE + RUN_PATH.replace("{agent_id}", AGENT_ID))
        if INSECURE:
            print("  WARNING: TLS verification is OFF (CREST_INSECURE_TLS). Dev only.")
    print("  Ctrl+C to stop.\n")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  stopped.")
