"""Northwind Mutual: a sample application that uses the FillerAI chat service.

This is an *outside* application. It does not import FillerAI; it talks to a
running FillerAI server over HTTP, the way your own application would:

- Its browser page shows a customer portal with its own forms and a chat
  window (``ChatWidget`` from ``fillerai.js``).
- The chat's turns go to **this** server first (``POST /api/chat``), which
  adds the customer's record as ``context.current`` and forwards them to
  FillerAI's ``POST /v1/bot/turn`` with the API token. The token never
  reaches the browser, and the browser cannot claim to be someone else.
- When the chat says ``submit``, the page posts the values to this server's
  own submit paths (``/api/address``, ``/api/documents``), which check them,
  change the record and hand back a reference; the page then tells the chat
  with a ``submitted`` event. FillerAI never writes to this application.

Run FillerAI, add the two starter templates, then::

    python examples/sample_app/app.py --fillerai http://localhost:8000 --token flr_...

and open http://localhost:8100. The token can also come from
``$FILLERAI_TOKEN``; a FillerAI started with ``--no-auth`` needs none.

Python 3 standard library only, like the rest of the repository.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

#: The one customer this demo is signed in as, on first start.
SEED = {
    "customer": {
        "customer_id": "NW-20417",
        "full_name": "Jordan Lee",
        "email": "jordan.lee@example.com",
        "policy_number": "PA-1048822",
        "street_address": "55 Market St",
        "unit": "",
        "city": "San Francisco",
        "state": "CA",
        "postal_code": "94105",
        "country": "US",
    },
    "requests": [],
}

ADDRESS_FIELDS = ("street_address", "unit", "city", "state", "postal_code", "country")
ADDRESS_REQUIRED = ("street_address", "city", "state", "postal_code")
DOCUMENT_TYPES = ("Declarations page", "ID card", "Proof of insurance",
                  "Full policy", "Billing statement")

#: What the page may fetch from FillerAI through this server, and nothing else.
CLIENT_FILES = {"/fillerai.js": "/client/fillerai.js",
                "/fillerai-chat.css": "/client/fillerai-chat.css"}


class Rejected(Exception):
    """A request this application will not take, with the reason for the person."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Portal:
    """The application's own data and rules. Your database, in a real one."""

    def __init__(self, path: Path | None):
        self.path = path
        self.lock = threading.Lock()
        if path is not None and path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = copy.deepcopy(SEED)

    def _save(self) -> None:
        if self.path is not None:
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def me(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.data)

    def current(self) -> dict[str, str]:
        """What the chat is told the application already holds."""
        with self.lock:
            return {k: v for k, v in self.data["customer"].items()
                    if k != "customer_id" and v}

    def _reference(self, prefix: str) -> str:
        return f"{prefix}-{10001 + len(self.data['requests'])}"

    def change_address(self, values: dict[str, Any]) -> dict[str, Any]:
        new = {k: str(values.get(k) or "").strip() for k in ADDRESS_FIELDS}
        missing = [k for k in ADDRESS_REQUIRED if not new[k]]
        if missing:
            raise Rejected("missing " + ", ".join(m.replace("_", " ") for m in missing))
        if not re.fullmatch(r"\d{5}(-\d{4})?", new["postal_code"]):
            raise Rejected(f"{new['postal_code']!r} is not a ZIP code")
        if not re.fullmatch(r"[A-Z]{2}", new["state"]):
            raise Rejected(f"state should be a two-letter code, not {new['state']!r}")
        new["country"] = new["country"] or "US"
        with self.lock:
            customer = self.data["customer"]
            before = {k: customer.get(k, "") for k in ADDRESS_FIELDS}
            if before == new:
                raise Rejected("that is already the address on file")
            reference = self._reference("CHG")
            customer.update(new)
            self.data["requests"].append({
                "reference": reference, "kind": "Address change", "at": _now(),
                "detail": ", ".join(v for v in (new["street_address"], new["unit"], new["city"],
                                                new["state"], new["postal_code"]) if v),
                "changed": {k: {"before": before[k], "after": new[k]}
                            for k in ADDRESS_FIELDS if before[k] != new[k]},
            })
            self._save()
            return {"reference": reference, "customer": copy.deepcopy(customer)}

    def request_document(self, values: dict[str, Any]) -> dict[str, Any]:
        kind = str(values.get("document_type") or "").strip()
        policy = str(values.get("policy_number") or "").strip()
        match = next((d for d in DOCUMENT_TYPES if d.lower() == kind.lower()), None)
        if match is None:
            raise Rejected(f"we can't send {kind!r}" if kind else "which document?")
        with self.lock:
            customer = self.data["customer"]
            if policy and policy != customer["policy_number"]:
                raise Rejected(f"policy {policy} is not on this account", status=403)
            reference = self._reference("DOC")
            self.data["requests"].append({
                "reference": reference, "kind": "Document request", "at": _now(),
                "detail": f"{match} for policy {customer['policy_number']}, "
                          f"to {customer['email']}",
            })
            self._save()
            return {"reference": reference}


class FillerAIService:
    """The one outbound call this application makes: a chat turn."""

    def __init__(self, base: str, token: str = "", timeout: float = 30.0):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _open(self, path: str, body: dict[str, Any] | None = None):
        request = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="GET" if body is None else "POST")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        return urllib.request.urlopen(request, timeout=self.timeout)

    def turn(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            with self._open("/v1/bot/turn", body) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            except ValueError:
                return error.code, {"error": f"FillerAI answered {error.code}",
                                    "code": "upstream"}
        except (urllib.error.URLError, OSError) as error:
            return 502, {"error": f"can't reach FillerAI at {self.base} ({error})",
                         "code": "unreachable"}

    def templates(self) -> list[str]:
        with self._open("/v1/templates") as response:
            return [t["key"] for t in json.loads(response.read())["templates"]]

    def client_file(self, path: str) -> tuple[bytes, str]:
        with self._open(path) as response:
            return response.read(), response.headers.get("Content-Type", "text/plain")


def make_server(fillerai: FillerAIService, portal: Portal, host: str = "127.0.0.1",
                port: int = 8100) -> ThreadingHTTPServer:
    """The application's web server; port 0 picks a free one (tests use that)."""

    class Handler(BaseHTTPRequestHandler):
        quiet = False

        def log_message(self, fmt, *args):  # noqa: N802 - the stdlib's name
            if not self.quiet:
                super().log_message(fmt, *args)

        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 64 * 1024:
                raise Rejected("request too large", status=413)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise Rejected("the body is not JSON") from None
            if not isinstance(body, dict):
                raise Rejected("the body should be a JSON object")
            return body

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/api/me":
                return self._json(200, portal.me())
            if path in CLIENT_FILES:
                try:
                    data, kind = fillerai.client_file(CLIENT_FILES[path])
                except (urllib.error.URLError, OSError) as error:
                    return self._send(502, f"// can't reach FillerAI: {error}".encode(),
                                      "text/plain")
                return self._send(200, data, kind)
            name = "index.html" if path == "/" else path.lstrip("/")
            file = (STATIC / name).resolve()
            if STATIC not in file.parents or not file.is_file():
                return self._json(404, {"error": "not found"})
            kinds = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
                     ".css": "text/css"}
            return self._send(200, file.read_bytes(),
                              kinds.get(file.suffix, "application/octet-stream"))

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            try:
                body = self._body()
                if path == "/api/chat":
                    return self._chat(body)
                if path == "/api/address":
                    return self._json(200, portal.change_address(body.get("values") or {}))
                if path == "/api/documents":
                    return self._json(200, portal.request_document(body.get("values") or {}))
                return self._json(404, {"error": "not found"})
            except Rejected as rejected:
                return self._json(rejected.status, {"error": str(rejected)})

        def _chat(self, body: dict[str, Any]) -> None:
            # Only the conversation's own parts come from the browser. What
            # the customer holds on file is this server's to say, so a page
            # that sends its own context.current is overruled.
            context = body.get("context") if isinstance(body.get("context"), dict) else {}
            forwarded = {
                "input": body.get("input"),
                "state": body.get("state"),
                "context": {**{k: v for k, v in context.items() if k in ("template",)},
                            "current": portal.current()},
            }
            status, reply = fillerai.turn(forwarded)
            self._json(status, reply)

    Handler.quiet = os.environ.get("SAMPLE_APP_QUIET") == "1"
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.handler = Handler  # type: ignore[attr-defined]
    return httpd


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fillerai", default=os.environ.get("FILLERAI_URL",
                                                             "http://localhost:8000"),
                        help="the FillerAI server (default: %(default)s, or $FILLERAI_URL)")
    parser.add_argument("--token", default=os.environ.get("FILLERAI_TOKEN", ""),
                        help="a FillerAI API token (or $FILLERAI_TOKEN); not needed "
                             "when FillerAI runs with --no-auth")
    parser.add_argument("--host", default="127.0.0.1",
                        help="where to listen (default: %(default)s). Another address "
                             "works, but browsers only allow the microphone on "
                             "localhost or https")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--data", type=Path, default=HERE / "portal-data.json",
                        help="where the customer record is kept (default: %(default)s)")
    args = parser.parse_args(argv)

    fillerai = FillerAIService(args.fillerai, args.token)
    try:
        keys = fillerai.templates()
    except urllib.error.HTTPError as error:
        print(f"FillerAI refused the token ({error.code}). Issue one with "
              "'fillerai tokens add <user>', or on Settings -> API tokens.", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as error:
        print(f"can't reach FillerAI at {args.fillerai}: {error}", file=sys.stderr)
        return 1
    for key in ("address_change", "document_request"):
        if key not in keys:
            print(f"  note: FillerAI has no {key!r} template; add it with "
                  f"'fillerai bot add --starter {key}' or on the Bots tab.", file=sys.stderr)

    httpd = make_server(fillerai, Portal(args.data), host=args.host, port=args.port)
    print(f"  Northwind Mutual customer portal: http://localhost:{httpd.server_address[1]}")
    print(f"  chat turns go to {args.fillerai}/v1/bot/turn")
    print("  press Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
