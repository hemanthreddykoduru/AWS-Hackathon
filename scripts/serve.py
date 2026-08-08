"""Web server for the whole site. Stdlib only — no Flask, no FastAPI, no uvicorn.

    python scripts/serve.py          # http://localhost:8000
    python scripts/serve.py 9000

Pages:
    /            landing        public
    /login       passcode gate  public
    /app         the workspace  gated — Review / Dashboard / Settings

API:
    GET  /api/memory              counts + runtime info (public: the landing page shows them)
    GET  /api/sample              the sample contract
    GET  /api/dashboard           audit rollups: recent, top clauses, clients
    GET  /api/memory/items        list one namespace of semantic memory
    POST /api/memory/items        add a rule or a past risk
    POST /api/memory/items/delete remove one
    POST /api/review              run the agent
    POST /api/login /api/logout

Reviews have two backends:

    LAMBDA_FUNCTION=freelance-guardian   invoke the deployed AWS Lambda (signed, via boto3)
    unset                                run lambda_function.handler in this process

Either way the same handler runs on the same event shape, so neither is a mock of the
other. The Lambda path exists because this account's Function URL returns 403 to anonymous
callers — a signed invoke reaches the identical function without one.
"""

import asyncio
import hmac
import json
import os
import sys
from hashlib import sha256
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import api, config, lambda_function  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"

LAMBDA_FUNCTION = os.getenv("LAMBDA_FUNCTION", "").strip()
LAMBDA_REGION = os.getenv("LAMBDA_REGION", "ap-south-1")


def _backend() -> str:
    return (f"AWS Lambda · {LAMBDA_FUNCTION} ({LAMBDA_REGION})" if LAMBDA_FUNCTION
            else "in-process")

# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
# One shared passcode, not user accounts. This protects a personal workspace from
# casual access; it is not multi-tenant auth and does not pretend to be.
#
# ponytail: single shared secret, no user table, no expiry. If this ever needs
# per-user access or revocation, that is a real auth system — do not grow this one.
PASSCODE = os.getenv("APP_PASSCODE", "").strip()
COOKIE = "fg_session"


def _token() -> str:
    """Cookie value proving the passcode was known. Derived, so the passcode itself
    never travels back to the browser or lands in a log."""
    return hmac.new(PASSCODE.encode(), b"freelance-guardian-v1", sha256).hexdigest()


def _authorised(handler: "Handler") -> bool:
    if not PASSCODE:
        return True  # no passcode configured — the site is open by design
    raw = handler.headers.get("Cookie")
    if not raw:
        return False
    got = SimpleCookie(raw).get(COOKIE)
    return bool(got) and hmac.compare_digest(got.value, _token())


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def review_via_lambda(body: str) -> tuple[int, str]:
    """Invoke the deployed Lambda with the same event the Function URL would deliver."""
    import boto3

    client = boto3.client("lambda", region_name=LAMBDA_REGION)
    result = client.invoke(FunctionName=LAMBDA_FUNCTION,
                           Payload=json.dumps({"body": body}).encode())
    payload = json.loads(result["Payload"].read())
    # An unhandled exception inside the function comes back as a trace, not our envelope.
    if "statusCode" not in payload:
        return 502, json.dumps({"error": f"lambda failed: {str(payload)[:300]}"})
    return payload["statusCode"], payload["body"]


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
STATIC = {
    "/theme.css": ("theme.css", "text/css; charset=utf-8"),
    "/theme.js": ("theme.js", "application/javascript; charset=utf-8"),
    "/logo.svg": ("logo.svg", "image/svg+xml"),
}
FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
           b'<rect width="16" height="16" fill="#F26522"/></svg>')


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------- #
    def _send(self, status, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict, extra: dict | None = None):
        self._send(status, json.dumps(payload).encode(), "application/json", extra)

    def _page(self, name: str):
        self._send(200, (UI / name).read_bytes(), "text/html; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length).decode()) if length else {}
        except json.JSONDecodeError:
            return {}

    def _gate(self) -> bool:
        """True when the request may proceed. Otherwise the response is already sent."""
        if _authorised(self):
            return True
        if self.path.startswith("/api/"):
            self._json(401, {"error": "sign in to continue"})
        else:
            self._send(303, b"", "text/plain", {"Location": "/login?next=" + self.path})
        return False

    # -- routes ------------------------------------------------------------- #
    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            return self._page("landing.html")
        if path == "/login":
            return self._page("login.html")
        if path in STATIC:
            name, ctype = STATIC[path]
            return self._send(200, (UI / name).read_bytes(), ctype)
        if path == "/favicon.ico":
            return self._send(200, FAVICON, "image/svg+xml")

        # Counts only, no contract content — the landing page needs them before sign-in.
        if path == "/api/memory":
            try:
                return self._json(200, asyncio.run(api.memory_stats(_backend(), bool(PASSCODE))))
            except Exception as exc:  # noqa: BLE001 - surface DB problems in the UI banner
                return self._json(503, {"error": f"{type(exc).__name__}: {exc}"})

        if not self._gate():
            return

        if path == "/app":
            return self._page("app.html")
        if path == "/api/sample":
            sample = (ROOT / "sample_data" / "risky_contract.txt").read_text()
            return self._json(200, {"contract_text": sample, "client_name": "Apex Dynamics LLC"})
        if path == "/api/dashboard":
            try:
                return self._json(200, asyncio.run(api.dashboard(_backend())))
            except Exception as exc:  # noqa: BLE001
                return self._json(503, {"error": f"{type(exc).__name__}: {exc}"})
        if path == "/api/memory/items":
            ns = (parse_qs(urlparse(self.path).query).get("namespace") or [""])[0]
            if ns not in (config.NS_RULES, config.NS_RISKS):
                return self._json(400, {"error": "namespace must be 'rules' or 'risks'"})
            try:
                return self._json(200, asyncio.run(api.list_items(ns)))
            except Exception as exc:  # noqa: BLE001
                return self._json(503, {"error": f"{type(exc).__name__}: {exc}"})

        self._json(404, {"error": f"no route for GET {path}"})

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/login":
            if not PASSCODE:
                return self._json(200, {"ok": True})  # nothing to sign in to
            supplied = str(self._body().get("passcode") or "")
            if not hmac.compare_digest(supplied, PASSCODE):
                return self._json(401, {"error": "That passcode does not match."})
            cookie = f"{COOKIE}={_token()}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800"
            return self._json(200, {"ok": True}, {"Set-Cookie": cookie})

        if path == "/api/logout":
            expired = f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
            return self._json(200, {"ok": True}, {"Set-Cookie": expired})

        if not self._gate():
            return

        if path == "/api/review":
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else "{}"
            if LAMBDA_FUNCTION:
                try:
                    status, payload = review_via_lambda(body)
                except Exception as exc:  # noqa: BLE001 - show the reason in the UI banner
                    status, payload = 502, json.dumps(
                        {"error": f"could not reach Lambda: {type(exc).__name__}: {exc}"})
            else:
                result = lambda_function.handler({"body": body})
                status, payload = result["statusCode"], result["body"]
            return self._send(status, payload.encode(), "application/json")

        if path == "/api/memory/items":
            # Validated in src/api.py because this is the trust boundary for what enters
            # the agent's beliefs — a bad row silently skews every future retrieval — and
            # both front doors must accept exactly the same thing.
            checked = api.validate_item(self._body())
            if isinstance(checked, str):
                return self._json(400, {"error": checked})
            ns, severity, body_text = checked
            try:
                return self._json(200, asyncio.run(api.add_item(ns, severity, body_text)))
            except Exception as exc:  # noqa: BLE001
                return self._json(503, {"error": f"{type(exc).__name__}: {exc}"})

        if path == "/api/memory/items/delete":
            data = self._body()
            ns, item_id = data.get("namespace"), str(data.get("id") or "")
            if ns not in (config.NS_RULES, config.NS_RISKS):
                return self._json(400, {"error": "namespace must be 'rules' or 'risks'"})
            if not item_id:
                return self._json(400, {"error": "id is required"})
            try:
                return self._json(200, asyncio.run(api.delete_item(ns, item_id)))
            except Exception as exc:  # noqa: BLE001
                return self._json(503, {"error": f"{type(exc).__name__}: {exc}"})

        self._json(404, {"error": f"no route for POST {path}"})

    def log_message(self, fmt, *args):
        print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    # Unbuffered: piping the server to a file otherwise swallows the banner that tells
    # you which review backend is live, which is the one thing you need to see.
    sys.stdout.reconfigure(line_buffering=True)
    print(config.summary())
    print(f"review backend: "
          f"{f'AWS Lambda {LAMBDA_FUNCTION!r} ({LAMBDA_REGION})' if LAMBDA_FUNCTION else 'in-process'}")
    print(f"passcode:       {'on' if PASSCODE else 'off (set APP_PASSCODE to require sign-in)'}")
    print(f"\nFreelance Guardian  ->  http://localhost:{port}\n")
    try:
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
