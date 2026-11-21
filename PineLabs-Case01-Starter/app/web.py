import argparse
import json
import logging
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import db, documents, metrics, ocr, simulators, views
from .access import BrowserBoundary, COOKIE, exchange, has_role, issue_ticket, signed_role
from .evidence import current_extractions
from .extraction import (Amendment, SAFE_MESSAGE, SAMPLE_DOCS, SAMPLE_MERCHANT,
                         StrictModel, Submission, masked)
from .fixtures import SCENARIOS, documents as sample_documents, label_for
from .provisioning import Requeue, delivery_status, requeue_mid
from .review import Decision, LABELS, REASONS, allowed_actions, decide
from .verification import provider_secrets, receive_callback
from .workflow import (EDGES, PRE, STATES, Problem, amend, current_documents, submit, upgrade_identity_claims,
                       upgrade_legacy_evidence, upgrade_open_cases)

ROOT = Path(__file__).parent
LOG = logging.getLogger("uvicorn.error")
TEMPLATES = Jinja2Templates(directory=ROOT / "templates")
TEMPLATES.env.filters["masked"] = masked
TEMPLATES.env.filters["utc"] = lambda value: datetime.fromtimestamp(value, timezone.utc).strftime("%H:%M:%S UTC")
TEMPLATES.env.filters["ago"] = views.ago
TEMPLATES.env.filters["dur"] = views.duration
TEMPLATES.env.filters["pct"] = views.percent
TEMPLATES.env.filters["ek"] = views.event_kind
TEMPLATES.env.filters["clock"] = lambda value: (
    datetime.fromtimestamp(value, timezone.utc).strftime("%H:%M:%S.%f")[:-4] if value else "—")


class Ticket(StrictModel):
    ticket: str = Field(min_length=20, max_length=100)


def health(conn):
    row = conn.execute("SELECT value FROM settings WHERE name='heartbeat'").fetchone()
    age = max(0, time.time() - float(row[0])) if row else None
    status = {"worker_online": age is not None and age < 10, "age_seconds": age, "worker_error": None}
    error = conn.execute("SELECT value FROM settings WHERE name='worker_error'").fetchone()
    if error:
        record = json.loads(error[0])
        if time.time() - record["at"] < 60:  # recent failures only; phase and exception class, no data
            status["worker_error"] = f"{record['phase']} · {record['error']}"
    return status


def case_view(path, case):
    with closing(db.connect(path)) as conn:
        # A read-only savepoint pins a single WAL snapshot across the detail queries.
        conn.execute("PRAGMA query_only=ON")
        conn.execute("SAVEPOINT case_view")
        row = conn.execute("SELECT * FROM applications WHERE id=?", (case,)).fetchone()
        if not row:
            raise Problem(404, "Case unavailable.")
        def rows(query):
            return [dict(r) for r in conn.execute(query, (case,))]
        merchant = json.loads(row["merchant"])
        data = dict(id=case, state=row["state"], merchant=merchant, label=label_for(merchant),
                    data_revision=row["data_revision"], policy=json.loads(row["policy"]),
                    documents={k: p for k, (_, p) in current_documents(conn, case).items() if p is not None},
                    fields=json.loads(conn.execute("SELECT results FROM assessments WHERE case_id=? AND data_revision=?",
                                                   (case, row["data_revision"])).fetchone()[0]),
                    issues=rows("SELECT * FROM review_items WHERE case_id=? AND status<>'RESOLVED' ORDER BY id"),
                    attempts=rows("""SELECT a.id,a.kind,a.input_revision,a.generation,a.status,a.reference,a.sent_at,
                        a.deadline,a.settled_at,a.reason_code,a.registered_name,a.name_score,
                        c.input_revision AS current_revision, j.status AS job_status, j.due_at, j.claims,
                        j.last_reason FROM attempts a JOIN checks c USING(case_id,kind)
                        LEFT JOIN jobs j ON j.attempt_id=a.id AND j.kind='DISPATCH'
                        WHERE a.case_id=? ORDER BY a.rowid"""),
                    receipts=rows("""SELECT provider,event_id,attempt_id,outcome,disposition,received_at,receipts
                        FROM callback_inbox WHERE case_id=? ORDER BY received_at,rowid"""),
                    events=rows("SELECT seq,at,event,actor,detail FROM events WHERE case_id=? ORDER BY seq"))
        current = current_documents(conn, case)
        data["extractions"] = {kind: dict(revision=ex["revision"], source_type=ex["source_type"].upper(),
                                          engine=ex["engine"], min_confidence=ex["min_confidence"],
                                          threshold=ex["threshold"], original=ex["original_digest"][:12])
                               for kind, ex in current_extractions(conn, case, current).items()}
        # Attempts an operator accepted (a referral or a weak beneficiary match). Shown before any approval exists.
        data["accepted_attempts"] = {r["attempt_id"] for r in rows(
            "SELECT attempt_id FROM decisions WHERE case_id=? AND action IN ('ACCEPT_REFERRAL','ACCEPT_BENEFICIARY_MATCH')")}
        for issue in data["issues"]:
            issue["actions"] = allowed_actions(conn, issue, row)
        approval = conn.execute("SELECT * FROM approvals WHERE case_id=?", (case,)).fetchone()
        data["approval"] = (dict(at=approval["approved_at"], digest=approval["receipt_digest"],
                                 receipt=json.loads(approval["receipt"])) if approval else None)
        job = conn.execute("SELECT * FROM jobs WHERE kind='ISSUE_MID' AND case_id=?", (case,)).fetchone()
        data["issuance"] = (dict(status=delivery_status(job), due=job["due_at"], claims=job["claims"],
                                 cycle=job["requeues"], key=job["operation_key"], reason=job["last_reason"])
                            if job else None)
        mid = conn.execute("SELECT mid FROM merchants WHERE case_id=?", (case,)).fetchone()
        data["mid"] = mid["mid"] if mid else None
        conn.execute("RELEASE case_view")
        data["pending"] = any(a["status"] in ("SCHEDULED", "SENT") and a["input_revision"] == a["current_revision"]
                              for a in data["attempts"])
        data["pending"] |= bool(job and job["status"] in {"READY", "LEASED"})
        return data


def create_app(run_dir, timing=None, background=False, demo_controls=False):
    path = db.initialize(run_dir, timing)
    policy = db.policy()
    upgrade_open_cases(path)
    upgrade_legacy_evidence(path)
    upgrade_identity_claims(path)
    with closing(db.connect(path)) as conn:
        key = bytes.fromhex(db.setting(conn, "signing_key"))
        api_key = db.setting(conn, "sim_api_key")
        run_timing = db.timing(conn)
    secrets_by_provider = provider_secrets(path)

    @asynccontextmanager
    async def lifespan(app):
        stop = threading.Event()
        worker = app.state.simulator.start(app.state.origin, stop) if background else None
        yield
        stop.set()
        if worker:
            worker.join(5)

    app = FastAPI(title="Merchant onboarding", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.database = path
    app.state.clock = time.time
    app.state.origin = "http://127.0.0.1:8000"
    app.state.demo_controls = demo_controls
    app.state.simulator = simulators.Simulator(run_dir, secrets_by_provider, api_key, run_timing)
    app.add_middleware(BrowserBoundary)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    def render(request, template, status_code=200, **context):
        return TEMPLATES.TemplateResponse(request=request, name=template, context=context, status_code=status_code,
                                          headers={"Cache-Control": "no-store"})

    HEADLINES = {403: "You do not have access to this page", 404: "We could not find that", 409: "This page is out of date",
                 413: "That upload is too large", 415: "That file type is not accepted", 422: "Something in the request was not valid",
                 503: "Temporarily unavailable"}

    def failure(request, status, message, refresh=None):
        """Browsers navigating with GET get a readable page; API clients keep getting JSON."""
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            return render(request, "error.html", status_code=status, status=status,
                          headline=HEADLINES.get(status, "Something went wrong"), message=message, refresh=refresh)
        content = {"detail": message}
        if refresh:
            content["refresh"] = refresh
        return JSONResponse(content, status_code=status)

    @app.middleware("http")
    async def safe_access_log(request: Request, call_next):
        response = await call_next(request)
        route = request.scope.get("route")
        # Only route templates and status codes enter the access log.
        LOG.info("access %s %s %d", request.method, getattr(route, "path", "/unmatched"), response.status_code)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Problem)
    def problem(request, exc):
        return failure(request, exc.status, exc.message, exc.refresh)

    @app.exception_handler(StarletteHTTPException)
    def http_error(request, exc):
        return failure(request, exc.status_code, "That page does not exist." if exc.status_code == 404 else str(exc.detail))

    @app.exception_handler(RequestValidationError)
    def invalid(request, exc):
        known = set(SAMPLE_MERCHANT) | {"documents", "merchant", "submission_key", "data_revision", "uploads"}
        fields = sorted({part for err in exc.errors() for part in err["loc"]
                         if isinstance(part, str) and part in known})
        return JSONResponse({"detail": SAFE_MESSAGE, "fields": fields}, status_code=422)

    @app.exception_handler(sqlite3.OperationalError)
    def db_error(request, exc):
        code = getattr(exc, "sqlite_errorcode", 0) or 0
        if (code & 255) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            LOG.warning("database busy")
            return JSONResponse({"detail": "Database busy. Retry the request."}, status_code=503)
        LOG.error("database operation failed")
        return JSONResponse({"detail": "Database operation failed."}, status_code=500)

    def authorize(request, case, edit=False):
        merchant = has_role(request, key, "merchant:" + case)
        if not merchant and (edit or not has_role(request, key, "operator")):
            raise Problem(403, "Open this case with its merchant or operator access.")
        return merchant

    def require_operator(request, demo=False):
        if not has_role(request, key, "operator"):
            raise Problem(403, "Operator access required.")
        if demo and not app.state.demo_controls:
            raise Problem(404, "Demo controls are disabled for this run. Start with --demo-controls.")

    app.include_router(simulators.router(require_operator))

    @app.post("/callbacks/{provider}")
    async def callback(provider: str, request: Request):
        # HMAC needs the exact bytes, so the body is read here; storage work runs off the event loop.
        raw = await request.body()
        status, content = await run_in_threadpool(
            receive_callback, path, provider, raw, request.headers.get("x-signature"),
            app.state.clock, secrets_by_provider)
        return JSONResponse(content, status_code=status)

    @app.get("/")
    def intake(request: Request):
        response = render(request, "form.html", merchant=SAMPLE_MERCHANT,
                          documents={k: v for k, v in SAMPLE_DOCS.items() if k != "tax"},
                          samples=SAMPLE_DOCS, case=None, submission_key=str(uuid4()), revision=None,
                          scenarios=SCENARIOS)
        if not request.cookies.get("intake_session"):
            response.set_cookie("intake_session", secrets.token_urlsafe(32), **COOKIE)
        return response

    @app.post("/api/cases")
    def create(request: Request, body: Submission):
        session = request.cookies.get("intake_session")
        if not session:
            raise Problem(403, "Open the intake form before submitting.")
        case, created, owner = submit(path, body, session, policy)
        location = "/cases/" + case
        if owner and not has_role(request, key, "merchant:" + case):
            location = "/access#" + issue_ticket(path, "merchant:" + case)
        return JSONResponse({"case_id": case, "location": location}, status_code=201 if created else 200)

    @app.get("/access")
    def access_page(request: Request):
        return render(request, "access.html")

    @app.post("/access")
    def redeem(body: Ticket):
        role = exchange(path, body.ticket)
        target = "/desk" if role == "operator" else "/cases/" + role.split(":")[1]
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(role.replace(":", "_"), signed_role(key, role), **COOKIE)
        return response

    @app.get("/cases/{case}")
    def detail(request: Request, case: str):
        can_edit = authorize(request, case)
        data = case_view(path, case)
        operator = has_role(request, key, "operator")
        console = (app.state.simulator.console([a["id"] for a in data["attempts"]])
                   if operator and app.state.demo_controls else {})
        checks = views.latest_checks(data["attempts"], data["policy"]["required"], views.accepted_kinds(data),
                                     data["accepted_attempts"])
        view = dict(checks=checks, steps=views.progress(data, checks), summary=views.summary(data, checks),
                    operator_owned=any(i["owner"] == "OPERATOR" for i in data["issues"]),
                    explain={i["id"]: views.explain_issue(i) for i in data["issues"]},
                    documents=views.document_rows(data), merchant_headline=views.MERCHANT_HEADLINES.get(data["state"], data["state"]))
        return render(request, "case.html", data=data, view=view, can_edit=can_edit and data["state"] in PRE,
                      operator=operator, labels=LABELS, reasons=REASONS, console=console,
                      issuer_count=app.state.simulator.issuance_count(case) if operator and app.state.demo_controls else None,
                      demo=operator and app.state.demo_controls)

    @app.post("/api/cases/{case}/documents/{kind}")
    async def upload_evidence(request: Request, case: str, kind: str, revision: int):
        authorize(request, case, edit=True)
        data = await request.body()
        record = await run_in_threadpool(documents.upload, path, case, kind, revision, data, app.state.clock())
        return {"location": "/cases/" + case, "min_confidence": record["min_confidence"]}

    @app.get("/cases/{case}/documents/{kind}/image")
    def document_image(request: Request, case: str, kind: str):
        """The exact normalized image the OCR engine read, for the merchant and the reviewing operator."""
        authorize(request, case)
        with closing(db.connect(path)) as conn:
            row = conn.execute("""SELECT f.data FROM extractions e JOIN files f ON f.digest=e.render_digest
                WHERE e.case_id=? AND e.kind=? AND e.revision=(SELECT max(revision) FROM documents
                WHERE case_id=e.case_id AND kind=e.kind)""", (case, kind)).fetchone()
        if not row:
            raise Problem(404, "No uploaded image for this document.")
        return Response(bytes(row["data"]), media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/cases/{case}/edit")
    def edit(request: Request, case: str):
        authorize(request, case, edit=True)
        data = case_view(path, case)
        if data["state"] not in PRE:
            raise Problem(409, "This case is closed for merchant edits.", "/cases/" + case)
        return render(request, "form.html", case=case, revision=data["data_revision"], submission_key=None,
                      merchant=data["merchant"], documents=data["documents"], samples=sample_documents(data["merchant"]), scenarios=[],
                      doc_rows={r["kind"]: r for r in views.document_rows(data)},
                      requests=[views.explain_issue(i) for i in data["issues"] if i["owner"] == "MERCHANT"])

    @app.patch("/api/cases/{case}")
    def update(request: Request, case: str, body: Amendment):
        authorize(request, case, edit=True)
        amend(path, case, body, now=app.state.clock())
        return {"location": "/cases/" + case}

    @app.post("/api/cases/{case}/issues/{issue_id}/decisions")
    def review(request: Request, case: str, issue_id: int, body: Decision):
        require_operator(request)
        decide(path, case, issue_id, body, app.state.clock())
        return {"location": "/cases/" + case}

    @app.post("/api/cases/{case}/issuance/requeue")
    def requeue(request: Request, case: str, body: Requeue):
        require_operator(request)
        requeue_mid(path, case, body, app.state.clock())
        return {"location": "/cases/" + case}

    @app.get("/health")
    def status():
        with closing(db.connect(path)) as conn:
            return health(conn)

    @app.get("/desk")
    def desk(request: Request):
        if not has_role(request, key, "operator"):
            return render(request, "desk.html", authorized=False)
        with closing(db.connect(path)) as conn:
            cases = [dict(r) for r in conn.execute("""SELECT a.id,a.state,a.created,a.merchant,
                json_extract(a.merchant,'$.name') AS name,
                (SELECT group_concat(reason,', ') FROM review_items i
                 WHERE i.case_id=a.id AND i.status<>'RESOLVED') AS reason,
                (SELECT count(*) FROM attempts t JOIN checks c USING(case_id,kind,input_revision)
                 WHERE t.case_id=a.id AND t.status IN ('SCHEDULED','SENT')) AS pending
                ,j.status,j.last_reason,j.due_at,m.mid
                FROM applications a LEFT JOIN jobs j ON j.case_id=a.id AND j.kind='ISSUE_MID'
                LEFT JOIN merchants m ON m.case_id=a.id ORDER BY a.created DESC""")]
            owners = {}
            for r in conn.execute("""SELECT case_id,group_concat(DISTINCT owner) AS owners FROM review_items
                                   WHERE status<>'RESOLVED' GROUP BY case_id"""):
                owners[r["case_id"]] = r["owners"].split(",")
            attempts_by_case = {}
            for r in conn.execute("""SELECT a.id,a.case_id,a.kind,a.status,a.generation,a.input_revision,c.input_revision AS current_revision,
                    j.status AS job_status,a.sent_at,a.settled_at,a.reason_code,a.reference,a.name_score FROM attempts a
                    JOIN checks c USING(case_id,kind) LEFT JOIN jobs j ON j.attempt_id=a.id AND j.kind='DISPATCH'"""):
                attempts_by_case.setdefault(r["case_id"], []).append(dict(r))
            accepted_by_case = {}
            for r in conn.execute("""SELECT case_id,attempt_id FROM decisions
                                   WHERE action IN ('ACCEPT_REFERRAL','ACCEPT_BENEFICIARY_MATCH')"""):
                accepted_by_case.setdefault(r["case_id"], set()).add(r["attempt_id"])
            issues_by_case = {}
            for r in conn.execute("""SELECT case_id,cause,subject,owner,reason FROM review_items
                                   WHERE status<>'RESOLVED' AND cause<>'STALLED' ORDER BY id"""):
                issues_by_case.setdefault(r["case_id"], []).append(dict(r))
            kinds_by_case = {}
            for r in conn.execute("SELECT case_id,kind FROM checks ORDER BY rowid"):
                kinds_by_case.setdefault(r["case_id"], []).append(r["kind"])
            for c in cases:
                c["label"] = label_for(json.loads(c.pop("merchant")))
                c["delivery"] = delivery_status(c) if c["status"] else None
                held = owners.get(c["id"], [])
                open_issues = sorted(issues_by_case.get(c["id"], []), key=lambda i: i["owner"] != "OPERATOR")
                c["next"] = views.explain_issue(open_issues[0]) if open_issues else None
                c["queue"] = "operator" if "OPERATOR" in held else "merchant" if "MERCHANT" in held else "system" if held else "none"
                c["checks"] = (views.latest_checks(attempts_by_case.get(c["id"], []), kinds_by_case.get(c["id"], []),
                                                   accepted_attempts=accepted_by_case.get(c["id"], frozenset()))
                               if attempts_by_case.get(c["id"]) else [])
            totals = dict(conn.execute("""SELECT
                (SELECT count(*) FROM jobs WHERE status='DEAD' AND kind='DISPATCH') AS dead,
                (SELECT count(*) FROM jobs WHERE status='DEAD' AND kind='ISSUE_MID') AS dead_mid,
                (SELECT coalesce(sum(count),0) FROM rejected_callbacks) AS rejected,
                (SELECT count(*) FROM callback_inbox) AS receipts""").fetchone())
            heartbeat = health(conn)
        queues = dict(operator=sum(1 for c in cases if c["queue"] == "operator"),
                      merchant=sum(1 for c in cases if c["queue"] == "merchant"))
        return render(request, "desk.html", authorized=True, cases=cases, heartbeat=heartbeat, totals=totals,
                      m=metrics_for("all"), queues=queues)

    def metrics_for(window):
        if window not in metrics.WINDOWS:
            raise Problem(422, "Choose one of: " + ", ".join(metrics.WINDOWS) + ".")
        now = app.state.clock()
        start, end = metrics.window_bounds(window, now)
        labels = {"all": "all time", "1h": "the last hour", "24h": "the last 24 hours"}
        with closing(db.connect(path)) as conn:
            conn.execute("PRAGMA query_only=ON")
            return metrics.compute(conn, start, end, now, labels[window])

    @app.get("/api/metrics")
    def metrics_api(request: Request, window: str = "all"):
        require_operator(request)
        return metrics_for(window)

    @app.get("/metrics")
    def metrics_page(request: Request, window: str = "all"):
        if not has_role(request, key, "operator"):
            return render(request, "metrics.html", authorized=False, window=window, windows=list(metrics.WINDOWS), m=None)
        return render(request, "metrics.html", authorized=True, window=window, windows=list(metrics.WINDOWS),
                      m=metrics_for(window), state_order=metrics.STATE_ORDER)

    @app.get("/state-model")
    def state_model(request: Request):
        return render(request, "states.html", states=STATES, edges=EDGES)

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=Path("private/demo"))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--timing", choices=sorted(db.TIMING))
    parser.add_argument("--demo-controls", action="store_true")
    args = parser.parse_args()
    app = create_app(args.run_dir, args.timing, background=True, demo_controls=args.demo_controls)
    origin = f"http://127.0.0.1:{args.port}"
    app.state.origin = origin
    with db.write_tx(app.state.database) as conn:
        conn.execute("""INSERT INTO settings VALUES ('web_origin',?)
                     ON CONFLICT(name) DO UPDATE SET value=excluded.value""", (origin,))
    ticket = issue_ticket(app.state.database, "operator")
    url = f"{origin}/access#{ticket}"
    (args.run_dir / "operator-link.txt").write_text(url, encoding="utf-8")
    print(f"Merchant intake: {origin}/", flush=True)
    if sys.stdout.isatty():
        print("Private one-time operator login:", url, flush=True)
    else:
        print("Private operator login saved in the run directory.", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
