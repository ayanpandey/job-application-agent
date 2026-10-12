"""
Licence-gated Vertex AI proxy for the Job Agent desktop app.

The point of this service is that two things never reach a customer's machine:

  1. Google credentials. On Cloud Run this process authenticates as its own
     attached service account, so there is no key file anywhere -- not in the
     image, not in an env var, not on the client. Deleting the JSON key the app
     used to ship with is the whole reason this exists.

  2. The prompts. Clients send *variables* (a job description, a question, the
     candidate's own profile) and this service substitutes them into templates
     it holds in `prompts.yaml`. A client can see what it sent and what came
     back, but never the instructions in between -- which is the part that took
     work to tune and the part a competitor would want.

Anything the client is allowed to ask for is in TASKS below. There is
deliberately no passthrough endpoint that forwards a caller-supplied prompt: it
would turn a licence key into free general-purpose model access billed to the
operator, and would put the templates back on the client to boot.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from keystore import LicenceError, LicenceStore
from telemetry import MAX_EVENTS_PER_BATCH, MAX_RESUME_BYTES, TelemetryStore

BASE_DIR = Path(__file__).resolve().parent

# The project this service runs in, and the only thing billed to it: Cloud Run,
# Cloud Build and Artifact Registry. It holds no customer data and no licences
# -- those live in the storage project, reached with the mounted key below.
PROJECT_ID = os.getenv("GCP_PROJECT_ID", "")

# Where Vertex AI usage is billed. This is NOT allowed to fall back to
# PROJECT_ID: model calls must never be charged to the project this service runs
# in, and a silent default is exactly how that would happen after a config typo.
VERTEX_PROJECT_ID = os.getenv("VERTEX_PROJECT_ID", "")
VERTEX_SA_KEY_PATH = os.getenv("VERTEX_SA_KEY_PATH", "")

# Every object this service persists lives in this one bucket, in the same
# project as the mounted key, under one prefix so other products can share it.
STORAGE_BUCKET = os.getenv("STORAGE_BUCKET", "your-bucket-name")
STORAGE_PREFIX = os.getenv("STORAGE_PREFIX", "resumes/job-agent")

# Projects that must never be billed for model calls under any circumstances.
# PROJECT_ID is always added to this set below. Anything listed here is refused
# even if the mounted key genuinely belongs to it.
FORBIDDEN_VERTEX_PROJECTS = {
    p.strip() for p in os.getenv("FORBIDDEN_VERTEX_PROJECTS", "").split(",") if p.strip()
}

LOCATION = os.getenv("GCP_LOCATION", "global")
# Both verified callable on the billed project before being set here. The
# fallback matters more than it looks: `_generate` tries the primary and then
# drops to the fallback without surfacing anything, so a primary that does not
# exist would silently serve every customer the older model with nothing in the
# response to say so. That is why the fallback is now 3.7 rather than 2.5 -- a
# degraded call should land one step down, not three.
PRIMARY_MODEL = os.getenv("PRIMARY_MODEL", "gemini-3.8-flash")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "gemini-3.7-flash")

# A generous ceiling that still stops a licence being used to push novels
# through the operator's billing account. A compressed job description tops out
# around 3k characters; a serialised profile plus a batch of questions is well
# under 20k.
MAX_PROMPT_CHARS = int(os.getenv("MAX_PROMPT_CHARS", "24000"))


# -- prompt registry ---------------------------------------------------------

_PROMPTS: Dict[str, Any] = yaml.safe_load((BASE_DIR / "prompts.yaml").read_text(encoding="utf-8")) or {}


def _section(*path: str) -> Dict[str, Any]:
    node: Any = _PROMPTS
    for part in path:
        node = (node or {}).get(part, {})
    return node or {}


# Maps the task name a client may request onto the prompt section that answers
# it. Adding a task here is the only way to expose new model access.
TASKS: Dict[str, Dict[str, Any]] = {
    "job_matcher": {
        "section": _section("job_matcher"),
        "required": {"job_title", "job_description"},
    },
    "question_solver": {
        "section": _section("question_solver"),
        "required": {"question_text"},
    },
    "question_solver_batch": {
        "section": _section("question_solver", "batch"),
        "required": {"questions_block"},
    },
    "recruiter_outreach": {
        "section": _section("recruiter_outreach"),
        "required": {"job_title", "company_name"},
    },
    "resume_tailor": {
        "section": _section("resume_tailor"),
        "required": {"job_title"},
    },
    "resume_audit": {
        "section": _section("resume_audit"),
        "required": {"job_title"},
    },
    # Turns the raw text of the customer's own CV into the structured profile
    # everything else reads. Without it the wizard's few typed fields were the
    # whole profile -- it has no field for job bullet points at all -- so every
    # generated resume had an empty experience section.
    "profile_extract": {
        "section": _section("profile_extract"),
        "required": {"resume_text"},
    },
    # Finds a control on a page when the hard-coded selector for it has stopped
    # matching. Called only after the selector has failed, so a site that has
    # not changed its markup costs nothing -- see `dom_resolver` on the client.
    "element_resolver": {
        "section": _section("element_resolver"),
        "required": {"element_description", "dom_fragment"},
    },
    "keyword_expansion": {
        "section": _section("keyword_expansion"),
        "required": {"seed_keywords"},
    },
}


# -- app ---------------------------------------------------------------------

app = FastAPI(title="Job Agent Licence Proxy", version="1.0.0", docs_url=None, redoc_url=None)

_store: Optional[LicenceStore] = None
_genai_client = None
_credentials = None
_credentials_project = ""


def store() -> LicenceStore:
    """
    The licence store, backed by the operator's own bucket.

    Built from the same mounted key as the model client, so licence records and
    usage counters are written to the operator's project -- not to the project
    hosting this service, which holds no customer data at all.
    """
    global _store
    if _store is None:
        credentials, key_project = _mounted_credentials()
        _store = LicenceStore(
            bucket=STORAGE_BUCKET,
            credentials=credentials,
            project=key_project,
            prefix=STORAGE_PREFIX,
        )
    return _store


_telemetry: Optional[TelemetryStore] = None


def telemetry() -> TelemetryStore:
    """Activity records, in the same bucket and prefix as the licences."""
    global _telemetry
    if _telemetry is None:
        _telemetry = TelemetryStore(store())
    return _telemetry


class VertexIdentityError(RuntimeError):
    """The mounted identity is missing or is not the one we are allowed to use."""


def _mounted_credentials():
    """
    Loads the mounted service-account key, or refuses to return anything.

    This one identity serves both Vertex AI and the storage bucket, so both the
    model spend and the stored data land in the operator's own project rather
    than the project that merely hosts this service.

    There is deliberately no Application Default Credentials path here. ADC on
    Cloud Run resolves to this service's own attached identity, which lives in
    the host project -- so an ADC fallback would quietly bill model calls, and
    write licence data, to the wrong account the moment the mounted key went
    missing. That is the single failure this function exists to prevent, so a
    missing or wrong key is a hard error, never a downgrade.

    The project is taken from the key itself, not from configuration. An
    environment variable can be edited; the key's own `project_id` is what the
    credential actually authenticates as, so deriving it from the key means no
    config change can redirect spend or storage.
    """
    global _credentials, _credentials_project
    if _credentials is not None:
        return _credentials, _credentials_project

    from google.oauth2 import service_account

    if not VERTEX_SA_KEY_PATH:
        raise VertexIdentityError(
            "VERTEX_SA_KEY_PATH is not set. Model calls and storage are disabled "
            "rather than falling back to this service's own identity."
        )

    key_path = Path(VERTEX_SA_KEY_PATH)
    if not key_path.exists():
        raise VertexIdentityError(
            f"The service-account key is not mounted at {key_path}. Model calls "
            "and storage are disabled rather than falling back to this service's "
            "own identity."
        )

    # Read the project straight out of the file before handing it to google-auth.
    # The guards below must not depend on the key being well-formed enough for
    # the library to accept -- a malformed key that happens to name the host
    # project has to be refused by us, with our reason, not rejected incidentally
    # by a parser error that says nothing about billing.
    try:
        key_project = json.loads(key_path.read_text(encoding="utf-8")).get("project_id", "")
    except Exception as exc:
        raise VertexIdentityError(f"The mounted key could not be read: {exc}")
    if not key_project:
        raise VertexIdentityError("The mounted key does not name a project.")

    # Refuse to use the host project, whatever anything else says.
    forbidden = set(FORBIDDEN_VERTEX_PROJECTS)
    if PROJECT_ID:
        forbidden.add(PROJECT_ID)
    if key_project in forbidden:
        raise VertexIdentityError(
            f"The mounted key belongs to '{key_project}', which is on the "
            "forbidden list. Refusing to start."
        )

    # And refuse if it is not the project we were told to expect, so a swapped
    # secret version is caught rather than silently honoured.
    if VERTEX_PROJECT_ID and key_project != VERTEX_PROJECT_ID:
        raise VertexIdentityError(
            f"The mounted key belongs to '{key_project}' but VERTEX_PROJECT_ID "
            f"is '{VERTEX_PROJECT_ID}'. Refusing to start."
        )

    _credentials = service_account.Credentials.from_service_account_file(
        str(key_path), scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    _credentials_project = key_project
    return _credentials, _credentials_project


def _build_vertex_client():
    credentials, key_project = _mounted_credentials()
    from google import genai

    return genai.Client(
        vertexai=True,
        project=key_project,
        location=LOCATION,
        credentials=credentials,
    )


def genai_client():
    global _genai_client
    if _genai_client is None:
        _genai_client = _build_vertex_client()
    return _genai_client


def vertex_identity(probe: bool = False) -> Dict[str, Any]:
    """
    What identity model calls will actually use. Surfaced by /healthz.

    `probe` additionally mints an access token. Reading the file only proves a
    key is mounted, not that it still works -- a key that has been deleted or
    disabled in the storage project parses exactly like a live one, so without
    this the service would report healthy right up until the first customer
    request failed. The probe is opt-in because it is a network round trip and
    an uptime checker hitting /healthz every ten seconds should not make one.
    """
    try:
        credentials, key_project = _mounted_credentials()
    except VertexIdentityError as exc:
        return {"ok": False, "error": str(exc)[:200], "source": "none"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "source": "none"}

    identity: Dict[str, Any] = {
        "ok": True,
        "billed_project": key_project,
        "identity": getattr(credentials, "service_account_email", ""),
        "source": "mounted service-account key",
    }
    if probe:
        try:
            from google.auth.transport.requests import Request

            credentials.refresh(Request())
            identity["credential_valid"] = True
        except Exception as exc:
            identity["ok"] = False
            identity["credential_valid"] = False
            identity["error"] = f"The mounted key no longer authenticates: {str(exc)[:200]}"
    return identity


class TaskRequest(BaseModel):
    task: str
    variables: Dict[str, Any] = Field(default_factory=dict)
    json_mode: bool = True
    temperature: float = 0.2
    max_tokens: int = 1500
    device_id: str = ""


class ActivateRequest(BaseModel):
    device_id: str = ""
    app_version: str = ""


class TelemetryRequest(BaseModel):
    events: list = Field(default_factory=list, max_length=MAX_EVENTS_PER_BATCH)
    device_id: str = ""
    app_version: str = ""


def _licence_from_header(authorization: Optional[str], device_id: str = ""):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail={"code": "missing_licence", "message": "Supply your licence key as 'Authorization: Bearer <key>'."})
    key = authorization.split(" ", 1)[1].strip()
    try:
        return store().authenticate(key, device_id=device_id)
    except LicenceError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


@app.exception_handler(LicenceError)
async def _licence_error_handler(_: Request, exc: LicenceError):
    return JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, "message": exc.message}})


@app.exception_handler(VertexIdentityError)
async def _identity_error_handler(_: Request, exc: VertexIdentityError):
    """
    A refused identity is our outage, not the customer's fault, so it reports as
    503 rather than anything that looks like a licence problem.
    """
    return JSONResponse(
        status_code=503,
        content={"detail": {"code": "identity_unavailable", "message": str(exc)}},
    )


@app.get("/health")
def healthz(probe: bool = False):
    """
    Includes which identity model calls bill to, so the split can be confirmed
    from outside the service rather than taken on trust.

    `?probe=1` also checks the key still authenticates. Use it after a deploy
    or a key rotation; leave it off for routine uptime polling.

    The path is `/health`, NOT `/healthz`. Google Frontend intercepts `/healthz`
    on run.app domains and answers it itself with an HTML 404, so the request
    never reaches this process -- while every other path routes normally. That
    is indistinguishable from a service whose URL is not routing at all, and it
    cost an hour of chasing a phantom outage on a service that was working
    perfectly. Do not move this back.
    """
    identity = vertex_identity(probe=probe)
    return {
        "ok": True,
        "tasks": sorted(TASKS),
        "model": PRIMARY_MODEL,
        "host_project": PROJECT_ID,
        "vertex": identity,
        "storage": {"bucket": STORAGE_BUCKET, "project": identity.get("billed_project")},
    }


@app.post("/v1/activate")
def activate(body: ActivateRequest, authorization: Optional[str] = Header(default=None)):
    """
    Called once when a client enters their key, and on every app start.

    Returns enough for the desktop app to show licence state without exposing
    anything about other licences or about the prompts.
    """
    doc_id, licence = _licence_from_header(authorization, device_id=body.device_id)
    usage = store().usage_snapshot(doc_id)
    return {
        "valid": True,
        "name": licence.name,
        "expires_at": licence.expires_at,
        "days_remaining": licence.days_remaining(),
        "daily_limit": licence.daily_quota,
        "daily_used": usage["daily_used"],
        "total_limit": licence.total_quota,
        "total_used": usage["total_used"],
    }


@app.post("/v1/telemetry")
def ingest_telemetry(body: TelemetryRequest, authorization: Optional[str] = Header(default=None)):
    """
    Accepts a batch of activity events from a licensed agent.

    This is what makes the operator's board possible: application records live
    in SQLite on the customer's own machine, so without this endpoint the
    service knows how many model calls a licence made and nothing whatsoever
    about what it accomplished.

    Deliberately does not book quota. Reporting is work the operator wants the
    client to do, and charging the customer's paid allowance for it would
    penalise exactly the behaviour being asked for. The volume ceiling inside
    TelemetryStore is what bounds abuse instead.
    """
    doc_id, _ = _licence_from_header(authorization, device_id=body.device_id)
    receipt = telemetry().record(
        doc_id,
        body.events,
        device_id=body.device_id,
        app_version=body.app_version,
    )
    return {"ok": True, **receipt}


@app.post("/v1/resume")
async def store_resume(
    file: UploadFile = File(...),
    candidate: str = Form(default=""),
    device_id: str = Form(default=""),
    authorization: Optional[str] = Header(default=None),
):
    """
    Accepts one generated resume from a licensed agent.

    The client cannot write to the bucket itself -- it holds no Google
    credentials, which is the entire point of this service -- so the file comes
    here and this process stores it with the mounted key. Like telemetry, it
    books no model quota: keeping a copy of work the customer already paid to
    generate is not a second billable event.
    """
    doc_id, _ = _licence_from_header(authorization, device_id=device_id)

    content = await file.read()
    if len(content) > MAX_RESUME_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "resume_too_large", "message": "Resume exceeds the size limit."},
        )
    try:
        uri = telemetry().store_resume(doc_id, file.filename or "resume", content, candidate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "bad_resume", "message": str(exc)})
    return {"ok": True, "uri": uri}


@app.post("/v1/task")
def run_task(body: TaskRequest, authorization: Optional[str] = Header(default=None)):
    doc_id, licence = _licence_from_header(authorization, device_id=body.device_id)

    spec = TASKS.get(body.task)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "unknown_task", "message": f"Unknown task '{body.task}'."},
        )

    section = spec["section"]
    system_prompt = section.get("system", "")
    user_template = section.get("user_template", "")
    if not user_template:
        raise HTTPException(
            status_code=500,
            detail={"code": "task_unavailable", "message": f"Task '{body.task}' has no template configured."},
        )

    missing = spec["required"] - set(body.variables)
    if missing:
        raise HTTPException(
            status_code=400,
            detail={"code": "missing_variables", "message": f"Missing required variables: {sorted(missing)}."},
        )

    # Templates are trusted, variables are not. `format_map` with a defaulting
    # dict means a client omitting an optional variable gets a clearly-marked
    # gap rather than a 500, and a template placeholder that no longer exists in
    # a shipped client cannot take the service down.
    class _Defaulting(dict):
        def __missing__(self, key: str) -> str:
            return "(not provided)"

    safe_vars = _Defaulting({k: str(v) for k, v in body.variables.items()})
    try:
        user_prompt = user_template.format_map(safe_vars)
        # System prompts carry placeholders too (the resume tasks put the
        # deviation limit in theirs), so they get the same substitution rather
        # than reaching the model with a literal "{max_deviation_pct}".
        system_prompt = system_prompt.format_map(safe_vars) if system_prompt else ""
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_variables", "message": f"Could not build the prompt: {exc}"},
        )

    if len(user_prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(
            status_code=413,
            detail={"code": "prompt_too_large", "message": f"Request exceeds {MAX_PROMPT_CHARS} characters."},
        )

    # Build the Vertex client before booking quota. If the identity is wrong or
    # missing this refuses outright, and the customer should not lose a call
    # from their allowance for an outage that is entirely ours.
    try:
        genai_client()
    except VertexIdentityError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "vertex_identity", "message": str(exc)},
        )

    quota = store().consume(doc_id, licence, units=1)

    started = time.time()
    text = _generate(
        system_prompt,
        user_prompt,
        json_mode=body.json_mode,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
    )
    return {
        "text": text,
        "task": body.task,
        "latency_ms": int((time.time() - started) * 1000),
        "quota": quota,
    }


def _generate(system_prompt: str, user_prompt: str, json_mode: bool, temperature: float, max_tokens: int) -> str:
    from google.genai import types

    kwargs: Dict[str, Any] = {
        "temperature": max(0.0, min(2.0, float(temperature))),
        "max_output_tokens": max(64, min(8192, int(max_tokens))),
    }
    if json_mode:
        kwargs["response_mime_type"] = "application/json"
    try:
        # Same reasoning as the client had: this workload is extraction and
        # scoring against an explicit rubric, so the default reasoning pass adds
        # seconds per call and buys nothing.
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    except Exception:
        pass

    try:
        config = types.GenerateContentConfig(**kwargs)
    except Exception:
        kwargs.pop("thinking_config", None)
        config = types.GenerateContentConfig(**kwargs)

    contents = f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt
    client = genai_client()
    last: Optional[Exception] = None

    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        try:
            response = client.models.generate_content(model=model, contents=contents, config=config)
            if model != PRIMARY_MODEL:
                # Cloud Run collects stdout, and this is the only trace a
                # downgrade leaves: the response itself looks entirely normal.
                # Without it, a primary model that had been retired would serve
                # every customer the fallback indefinitely and look healthy.
                print(
                    f"WARNING model_fallback primary={PRIMARY_MODEL} "
                    f"served={model} reason={last}",
                    flush=True,
                )
            return (response.text or "").strip()
        except Exception as exc:
            last = exc
            continue

    raise HTTPException(
        status_code=502,
        detail={"code": "upstream_error", "message": f"Vertex AI call failed: {last}"},
    )
