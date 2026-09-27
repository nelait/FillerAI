"""Northwind Mutual: a sample application that uses the FillerAI chat service.

This is an *outside* application. It does not import the rest of FillerAI;
it talks to a running FillerAI server over HTTP, the way your own
application would. ``fillerai serve`` starts it next to itself (turn that off
with ``--no-sample-app``), and it can be run on its own too::

    python -m fillerai.sampleapp --fillerai http://localhost:8000 --token flr_...

What it does:

- Its page is a customer portal with a form for **every template the bot
  service has**. It asks FillerAI for them (``GET /v1/templates``) each time
  the page loads, so a template added on the Bots tab is a form here on the
  next reload. The forms are ordinary forms: they are all on the page from
  the start, and can be filled and saved by hand.
- The chat's turns go to **this** server first (``POST /api/chat``), which
  adds the customer's record as ``context.current`` and forwards them to
  FillerAI's ``POST /v1/bot/turn`` with the API token. The token never
  reaches the browser, and the browser cannot claim to be someone else.
- As the conversation goes, the page fills in the form the chat is on. When
  the chat says ``submit``, the page posts the values to this server's own
  submit path (``/api/submit/<template>``), which checks them against the
  template and its own rules, changes the record and hands back a
  reference; the page then tells the chat with a ``submitted`` event.
  FillerAI never writes to this application.

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
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

#: The one customer this demo is signed in as, on first start. A submitted
#: value changes the record when the record has that field; anything else
#: (which document, how to send it) belongs to the request, not the customer.
SEED = {
    "customer": {
        "customer_id": "NW-20417",
        "full_name": "Jordan Lee",
        "email": "jordan.lee@example.com",
        "phone": "(415) 555-0142",
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

#: What the page may fetch from FillerAI through this server, and nothing else.
CLIENT_FILES = {"/fillerai.js": "/client/fillerai.js",
                "/fillerai-chat.css": "/client/fillerai-chat.css"}

_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class Rejected(Exception):
    """A request this application will not take, with the reason for the person."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _check(field: dict[str, Any], value: str, customer: dict[str, str]) -> str:
    """One value against the template's field and this application's rules.

    The template says what is required and which options there are; the
    rest is the application's own business, keyed on the field's semantic
    type, the way a real one would check a ZIP code whatever the form
    calls it.
    """
    label = field.get("label") or field["name"]
    options = field.get("options") or []
    if options:
        match = next((o for o in options if o.lower() == value.lower()), None)
        if match is None:
            raise Rejected(f"{label} can't be {value!r}; it is one of {', '.join(options)}")
        return match
    kind = field.get("semantic_type") or ""
    if kind == "postal_code" and not re.fullmatch(r"\d{5}(-\d{4})?", value):
        raise Rejected(f"{value!r} is not a ZIP code")
    if kind == "state":
        if not re.fullmatch(r"[A-Za-z]{2}", value):
            raise Rejected(f"state should be a two-letter code, not {value!r}")
        return value.upper()
    if kind == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise Rejected(f"{value!r} is not an email address")
    if kind == "policy_number" and customer.get("policy_number") \
            and value != customer["policy_number"]:
        raise Rejected(f"policy {value} is not on this account", status=403)
    return value


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
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def me(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.data)

    def current(self) -> dict[str, str]:
        """What the chat is told the application already holds."""
        with self.lock:
            return {k: v for k, v in self.data["customer"].items()
                    if k != "customer_id" and v}

    def submit(self, template: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        """A form for one template, from the page or from the chat."""
        fields = template.get("fields") or []
        with self.lock:
            customer = self.data["customer"]
            new: dict[str, str] = {}
            missing = []
            for field in fields:
                raw = " ".join(str(values.get(field["name"]) or "").split())
                if not raw:
                    if field.get("required"):
                        missing.append(field.get("label") or field["name"])
                    new[field["name"]] = ""
                    continue
                new[field["name"]] = _check(field, raw, customer)
            if missing:
                raise Rejected("missing " + ", ".join(_word(m) for m in missing))

            on_record = [f["name"] for f in fields if f["name"] in customer]
            changed = {n: {"before": customer.get(n, ""), "after": new[n]}
                       for n in on_record if new[n] != (customer.get(n) or "")}
            if on_record and len(on_record) == len(fields) and not changed:
                raise Rejected("that is already what we have on file")

            reference = f"{_prefix(template)}-{10001 + len(self.data['requests'])}"
            for name in changed:
                customer[name] = new[name]
            said = [new[f["name"]] for f in fields if new[f["name"]]]
            self.data["requests"].append({
                "reference": reference, "template": template.get("key"),
                "kind": template.get("name") or template.get("key"), "at": _now(),
                "detail": ", ".join(said)[:200], "changed": changed,
            })
            self._save()
            return {"reference": reference, "customer": copy.deepcopy(customer)}


def _word(label: str) -> str:
    """A label mid-sentence: "Street address" -> "street address", "ZIP code" stays."""
    first = label.split(" ", 1)[0]
    return label if first.isupper() and len(first) > 1 else label[:1].lower() + label[1:]


def _prefix(template: dict[str, Any]) -> str:
    words = re.findall(r"[A-Za-z]+", template.get("name") or template.get("key") or "")
    return ("".join(w[0] for w in words[:3]) or "REQ").upper()


class FillerAIService:
    """What this application asks FillerAI for: templates, and chat turns."""

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
        return self.call("/v1/bot/turn", body)

    def transcribe(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return self.call("/v1/bot/transcribe", body)

    def call(self, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        try:
            with self._open(path, body) as response:
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

    post = call  # the name the first version of this example used

    def templates(self) -> tuple[int, dict[str, Any]]:
        """Every template, whole: the list only carries a summary of each."""
        status, listed = self.call("/v1/templates")
        if status != 200:
            return status, listed
        whole = []
        for card in listed.get("templates", []):
            status, one = self.template(card["key"])
            if status == 200:
                whole.append(one)
        return 200, {"templates": whole}

    def template(self, key: str) -> tuple[int, dict[str, Any]]:
        status, body = self.call("/v1/templates/" + urllib.parse.quote(key))
        return status, body.get("template", body) if status == 200 else body

    def client_file(self, path: str) -> tuple[bytes, str]:
        with self._open(path) as response:
            return response.read(), response.headers.get("Content-Type", "text/plain")


def make_server(fillerai: FillerAIService, portal: Portal, host: str = "127.0.0.1",
                port: int = 8100, server_speech: bool = False,
                fillerai_page: str = "") -> ThreadingHTTPServer:
    """The application's web server; port 0 picks a free one (tests use that).

    ``fillerai_page`` is where a person goes to add templates, for the page
    to link to when there are none; empty leaves the link out.
    """

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
            # A recording is the one big thing the page sends.
            if length > (8 * 1024 * 1024 if self.path.startswith("/api/transcribe") else 64 * 1024):
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
                return self._json(200, {**portal.me(), "server_speech": server_speech,
                                        "fillerai_page": fillerai_page})
            if path == "/api/templates":
                # Asked for on every page load, so the forms are whatever
                # the bot service has now.
                return self._json(*fillerai.templates())
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
                if path == "/api/transcribe" and server_speech:
                    return self._json(*fillerai.transcribe(
                        {k: body.get(k) for k in ("audio", "mime", "language")}))
                if path.startswith("/api/submit/"):
                    return self._submit(path[len("/api/submit/"):], body)
                return self._json(404, {"error": "not found"})
            except Rejected as rejected:
                return self._json(rejected.status, {"error": str(rejected)})

        def _submit(self, key: str, body: dict[str, Any]) -> None:
            if not _KEY.match(key):
                raise Rejected("not a template", status=404)
            # The template's own definition decides what a complete form is,
            # read fresh rather than trusting the page's copy of it.
            status, template = fillerai.template(key)
            if status != 200:
                return self._json(status, template)
            values = body.get("values") if isinstance(body.get("values"), dict) else {}
            return self._json(200, portal.submit(template, values))

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
    parser = argparse.ArgumentParser(prog="python -m fillerai.sampleapp",
                                     description=__doc__.split("\n\n")[0])
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
    parser.add_argument("--server-speech", action="store_true",
                        help="record the microphone in the page and have FillerAI "
                             "transcribe it (FillerAI must run with --bot-transcribe), "
                             "for where the browser's speech service is blocked")
    parser.add_argument("--data", type=Path, default=Path("portal-data.json"),
                        help="where the customer record is kept (default: %(default)s)")
    args = parser.parse_args(argv)

    fillerai = FillerAIService(args.fillerai, args.token)
    status, found = fillerai.templates()
    if status in (401, 403):
        print(f"FillerAI refused the token ({status}). Issue one with "
              "'fillerai tokens add <user>', or on Settings -> API tokens.", file=sys.stderr)
        return 1
    if status != 200:
        print(found.get("error", f"FillerAI answered {status}"), file=sys.stderr)
        return 1
    if not found["templates"]:
        print("  note: FillerAI has no bot templates yet, so there are no forms. Add "
              "some on the Bots tab, then reload the page.", file=sys.stderr)

    httpd = make_server(fillerai, Portal(args.data), host=args.host, port=args.port,
                        server_speech=args.server_speech, fillerai_page=args.fillerai)
    print(f"  Northwind Mutual customer portal: http://localhost:{httpd.server_address[1]}")
    print(f"  forms for {len(found['templates'])} template(s); "
          f"chat turns go to {args.fillerai}/v1/bot/turn")
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
