"""One-use fragment tickets, signed role cookies and bounded same-origin requests."""
import hashlib
import hmac
import re
import secrets
import time

from starlette.responses import JSONResponse

from .db import digest, write_tx
from .workflow import Problem

COOKIE = dict(httponly=True, samesite="strict", max_age=8 * 3600, path="/")


def issue_ticket(path, role):
    ticket = secrets.token_urlsafe(32)
    now = time.time()
    with write_tx(path) as conn:
        conn.execute("DELETE FROM tickets WHERE expires <= ?", (now,))
        conn.execute("INSERT INTO tickets VALUES (?,?,?)", (digest(ticket), role, now + 1800))
    return ticket


def exchange(path, ticket):
    with write_tx(path) as conn:
        row = conn.execute("DELETE FROM tickets WHERE digest=? AND expires>? RETURNING role",
                           (digest(ticket), time.time())).fetchone()
        if not row:
            raise Problem(403, "This access link is unavailable or expired.")
        return row[0]


def signed_role(key, role):
    return hmac.new(key, role.encode(), hashlib.sha256).hexdigest()


def has_role(request, key, role):
    cookie = request.cookies.get(role.replace(":", "_"), "")
    return cookie.isascii() and hmac.compare_digest(cookie, signed_role(key, role))


MACHINE_PATHS = re.compile(r"^/(callbacks/[A-Z]{3,4}|sim/[A-Z]{3,4}/requests|sim/mids)$")
# A document upload is a browser request carrying raw file bytes, bounded at 2 MiB plus a little framing.
UPLOAD_PATH = re.compile(r"^/api/cases/[0-9a-f]{32}/documents/(identity|tax|bank)$")
UPLOAD_TYPES = {b"image/png", b"image/jpeg", b"application/pdf"}
UPLOAD_LIMIT = 2 * 1024 * 1024


class BrowserBoundary:
    """Browser writes must be same-origin JSON. Machine endpoints (provider requests and signed callbacks)
    authenticate with an API key or HMAC instead. Every write body is buffered within a fixed bound."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PATCH", "PUT", "DELETE"}:
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        expected = scope["scheme"].encode() + b"://" + headers.get(b"host", b"")
        machine = scope["method"] == "POST" and MACHINE_PATHS.match(scope["path"])
        if not machine and headers.get(b"origin") != expected:
            return await JSONResponse({"detail": "Same-origin request required."}, 403)(scope, receive, send)
        content_type = headers.get(b"content-type", b"").split(b";")[0]
        upload = bool(UPLOAD_PATH.match(scope["path"]))
        if upload and content_type not in UPLOAD_TYPES:
            return await JSONResponse({"detail": "Upload a PNG, JPEG or PDF document."}, 415)(scope, receive, send)
        if not upload and content_type != b"application/json":
            return await JSONResponse({"detail": "Structured JSON evidence required. Documents are uploaded to an "
                                                 "existing case."}, 415)(scope, receive, send)
        limit = UPLOAD_LIMIT if upload else 32768
        body = bytearray()
        while True:
            part = await receive()
            if part["type"] == "http.disconnect":
                return
            chunk = part.get("body", b"")
            if len(body) + len(chunk) > limit:
                size = "2 MiB" if upload else "32 KiB"
                return await JSONResponse({"detail": f"Payload exceeds {size}."}, 413)(scope, receive, send)
            body.extend(chunk)
            if not part.get("more_body"):
                break
        pending = True

        async def read():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        return await self.app(scope, read, send)
