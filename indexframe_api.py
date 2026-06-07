#!/usr/bin/env python3
"""Tiny FastAPI demo wrapper for Indexframe PoC.

Version 1 adds the simplest async product flow:
  - minimal static UI hosted by Cloud Run
  - Google sign-in via Firebase Auth in the browser
  - POST /api/submit verifies the Firebase token and starts a Cloud Run Job
  - the Cloud Run Job runs indexframe_echo_job.py and emails the result

The original synchronous /api/analyze endpoint is intentionally kept for local/dev
Indexframe pipeline tests. The core indexframe_poc.py logic is not changed.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from indexframe_poc import parse_size, run_pipeline

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = Path(os.getenv("INDEXFRAME_RUNS_DIR", str(BASE_DIR / "runs")))
RUNS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Indexframe PoC")
app.mount("/runs", StaticFiles(directory=str(RUNS_DIR)), name="runs")


class AnalyzeRequest(BaseModel):
    url: str
    variants: int = int(os.getenv("INDEXFRAME_VARIANTS", "6"))
    skip_gemini: bool = False
    video_path: Optional[str] = None
    transcript_file: Optional[str] = None
    heatmap_json: Optional[str] = None


class SubmitRequest(BaseModel):
    url: str


@dataclass
class AuthUser:
    uid: str
    email: str


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _firebase_config() -> Dict[str, str]:
    """Return the public Firebase config used by the browser SDK."""
    project_id = _env("FIREBASE_PROJECT_ID", "PROJECT_ID", "GOOGLE_CLOUD_PROJECT")
    api_key = _env("FIREBASE_API_KEY")
    auth_domain = _env("FIREBASE_AUTH_DOMAIN", default=f"{project_id}.firebaseapp.com" if project_id else "")
    return {
        "apiKey": api_key,
        "authDomain": auth_domain,
        "projectId": project_id,
        "appId": _env("FIREBASE_APP_ID"),
    }


def _validate_url(value: str) -> str:
    url = value.strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    if not re.match(r"^https?://", url, flags=re.I):
        raise HTTPException(status_code=400, detail="url must start with http:// or https://")
    return url


def _bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="missing Authorization: Bearer token")
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="expected Authorization: Bearer token")
    return authorization[len(prefix):].strip()


def verify_user(authorization: Optional[str]) -> AuthUser:
    """Verify Firebase Auth ID token.

    For smoke tests only, set DISABLE_AUTH=true and optionally DEMO_EMAIL.
    Do not use DISABLE_AUTH in a shared demo deployment.
    """
    if os.getenv("DISABLE_AUTH", "").lower() in {"1", "true", "yes"}:
        email = os.getenv("DEMO_EMAIL", "demo@example.com")
        return AuthUser(uid="demo", email=email)

    token = _bearer_token(authorization)
    project_id = _env("FIREBASE_PROJECT_ID", "PROJECT_ID", "GOOGLE_CLOUD_PROJECT")
    if not project_id:
        raise HTTPException(status_code=500, detail="FIREBASE_PROJECT_ID or PROJECT_ID is not configured")

    try:
        claims = id_token.verify_firebase_token(token, google_requests.Request(), audience=project_id)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"invalid Firebase token: {exc}") from exc

    email = str(claims.get("email") or "")
    if not email:
        raise HTTPException(status_code=401, detail="signed-in user has no email")
    if claims.get("email_verified") is False:
        raise HTTPException(status_code=403, detail="email is not verified")
    return AuthUser(uid=str(claims.get("user_id") or claims.get("sub") or ""), email=email)


def run_job_locally(*, submitted_url: str, user_email: str, submission_id: str) -> Dict[str, Any]:
    """Local developer path; Cloud Run uses the real run.googleapis.com call."""
    env = os.environ.copy()
    env.update({"SUBMITTED_URL": submitted_url, "USER_EMAIL": user_email, "SUBMISSION_ID": submission_id})
    completed = subprocess.run(
        ["python", str(BASE_DIR / "indexframe_echo_job.py")],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        check=False,
        timeout=60,
    )
    return {"local": True, "returncode": completed.returncode, "output": completed.stdout[-4000:]}


def start_cloud_run_job(*, submitted_url: str, user_email: str, submission_id: str) -> Dict[str, Any]:
    """Start the echo Cloud Run Job with per-submission env overrides."""
    if os.getenv("MOCK_RUN_JOB_LOCALLY", "").lower() in {"1", "true", "yes"}:
        return run_job_locally(submitted_url=submitted_url, user_email=user_email, submission_id=submission_id)

    project_id = _env("PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "GCP_PROJECT")
    region = _env("CLOUD_RUN_REGION", "GOOGLE_CLOUD_REGION", default="us-central1")
    job_name = _env("CLOUD_RUN_JOB_NAME", default="indexframe-echo-job")
    if not project_id:
        raise HTTPException(status_code=500, detail="PROJECT_ID is not configured")

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(credentials)
    endpoint = f"https://run.googleapis.com/v2/projects/{project_id}/locations/{region}/jobs/{job_name}:run"
    payload = {
        "overrides": {
            "containerOverrides": [
                {
                    "env": [
                        {"name": "SUBMITTED_URL", "value": submitted_url},
                        {"name": "USER_EMAIL", "value": user_email},
                        {"name": "SUBMISSION_ID", "value": submission_id},
                        {"name": "OUT", "value": submission_id},
                    ]
                }
            ]
        }
    }
    response = session.post(endpoint, json=payload, timeout=30)
    if response.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"Cloud Run Job start failed: {response.status_code} {response.text}")
    return response.json()


@app.get("/healthz")
async def healthz() -> Dict[str, bool]:
    return {"ok": True}


@app.get("/api/config")
async def config() -> Dict[str, Any]:
    firebase = _firebase_config()
    return {
        "firebase": firebase,
        "authEnabled": os.getenv("DISABLE_AUTH", "").lower() not in {"1", "true", "yes"},
        "configured": bool(firebase.get("apiKey") and firebase.get("projectId")),
    }


@app.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    return HTMLResponse(
        """
        <!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>IndexFrame v0.1</title>
        <style>
          :root { color-scheme: dark; }
          * { box-sizing:border-box; }
          body { margin:0; font-family:Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background:#08090c; color:#f8fafc; }
          main { min-height:100vh; display:grid; place-items:center; padding:24px; }
          section { width:100%; max-width:460px; border:1px solid #242833; background:#101219; border-radius:28px; padding:32px; box-shadow:0 30px 80px rgba(0,0,0,.35); }
          h1 { margin:0 0 8px; font-size:34px; letter-spacing:-.04em; }
          p { color:#a8b0c2; line-height:1.55; }
          input,button { width:100%; border-radius:14px; border:1px solid #2d3342; padding:15px 16px; font-size:16px; }
          input { background:#171b25; color:white; outline:none; }
          input:focus { border-color:#e5e7eb; }
          button { margin-top:12px; background:#f8fafc; color:#08090c; font-weight:750; cursor:pointer; }
          button.secondary { background:#171b25; color:#f8fafc; }
          button:disabled { opacity:.55; cursor:not-allowed; }
          .row { display:flex; gap:10px; align-items:center; }
          .row button { width:auto; flex:1; }
          .muted { color:#778196; font-size:13px; }
          .status { margin-top:16px; min-height:22px; white-space:pre-wrap; }
          .hidden { display:none; }
          .email { color:#e2e8f0; font-weight:650; }
        </style>
        <script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js"></script>
        <script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-auth-compat.js"></script>
        </head><body><main><section>
          <h1>Indexframe</h1>
          <p id="intro">Sign in with Google, paste one URL, and get the result by email.</p>

          <div id="loginPane">
            <button id="loginBtn">Continue with Google</button>
            <p class="muted">No password. Gmail / Google SSO only for this PoC.</p>
          </div>

          <div id="formPane" class="hidden">
            <p>Signed in as <span id="email" class="email"></span></p>
            <input id="url" placeholder="https://www.youtube.com/watch?v=..." autocomplete="off" />
            <button id="submitBtn">Submit</button>
            <button id="signOutBtn" class="secondary">Sign out</button>
          </div>

          <div id="donePane" class="hidden">
            <p>You will receive the link in your email.</p>
          </div>

          <div id="status" class="status muted"></div>
        </section></main>
        <script>
          const loginPane = document.getElementById('loginPane');
          const formPane = document.getElementById('formPane');
          const donePane = document.getElementById('donePane');
          const statusEl = document.getElementById('status');
          const emailEl = document.getElementById('email');
          const urlEl = document.getElementById('url');
          const submitBtn = document.getElementById('submitBtn');
          let auth = null;
          let authEnabled = true;

          function show(which) {
            loginPane.classList.toggle('hidden', which !== 'login');
            formPane.classList.toggle('hidden', which !== 'form');
            donePane.classList.toggle('hidden', which !== 'done');
          }
          function setStatus(text) { statusEl.textContent = text || ''; }

          async function boot() {
            const cfg = await fetch('/api/config').then(r => r.json());
            authEnabled = cfg.authEnabled;
            if (!authEnabled) {
              emailEl.textContent = 'demo@example.com';
              show('form');
              setStatus('Auth disabled for local smoke test.');
              return;
            }
            if (!cfg.configured) {
              show('login');
              setStatus('Firebase config missing. Set FIREBASE_API_KEY and FIREBASE_PROJECT_ID.');
              document.getElementById('loginBtn').disabled = true;
              return;
            }
            firebase.initializeApp(cfg.firebase);
            auth = firebase.auth();
            auth.onAuthStateChanged(user => {
              if (user) {
                emailEl.textContent = user.email || '(no email)';
                show('form');
                setStatus('');
              } else {
                show('login');
              }
            });
          }

          document.getElementById('loginBtn').onclick = async () => {
            try {
              setStatus('Opening Google sign-in...');
              const provider = new firebase.auth.GoogleAuthProvider();
              provider.setCustomParameters({ prompt: 'select_account' });
              await auth.signInWithPopup(provider);
            } catch (err) {
              setStatus(err.message || String(err));
            }
          };

          document.getElementById('signOutBtn').onclick = async () => {
            if (auth) await auth.signOut();
            urlEl.value = '';
            show('login');
          };

          submitBtn.onclick = async () => {
            const submittedUrl = urlEl.value.trim();
            if (!submittedUrl) { setStatus('Paste a URL first.'); return; }
            submitBtn.disabled = true;
            setStatus('Submitting...');
            try {
              let headers = {'content-type':'application/json'};
              if (authEnabled) {
                const token = await auth.currentUser.getIdToken();
                headers['authorization'] = 'Bearer ' + token;
              }
              const res = await fetch('/api/submit', {method:'POST', headers, body: JSON.stringify({url: submittedUrl})});
              const data = await res.json();
              if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
              show('done');
              setStatus('Submission id: ' + data.submission_id);
              urlEl.value = '';
              setTimeout(() => { show('form'); setStatus(''); }, 4600);
            } catch (err) {
              setStatus(err.message || String(err));
            } finally {
              submitBtn.disabled = false;
            }
          };
          boot().catch(err => setStatus(err.message || String(err)));
        </script>
        </body></html>
        """
    )


@app.post("/api/submit")
async def submit(req: SubmitRequest, authorization: Optional[str] = Header(default=None)) -> JSONResponse:
    url = _validate_url(req.url)
    user = verify_user(authorization)
    submission_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    job_response = await run_in_threadpool(
        start_cloud_run_job,
        submitted_url=url,
        user_email=user.email,
        submission_id=submission_id,
    )
    return JSONResponse(
        {
            "ok": True,
            "submission_id": submission_id,
            "email": user.email,
            "message": "Your task is on the way! You will receive the results via email.",
            "job": job_response,
        }
    )


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    """Original synchronous demo endpoint, kept unchanged in spirit for local tests."""
    if not req.url.strip():
        raise HTTPException(status_code=400, detail="url is required")
    run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    out_dir = RUNS_DIR / run_id
    try:
        result = await run_in_threadpool(
            run_pipeline,
            url=req.url,
            out_dir=out_dir,
            project=os.getenv("PROJECT_ID") or None,
            location=os.getenv("VERTEX_LOCATION", os.getenv("GOOGLE_CLOUD_LOCATION", "global")),
            youtube_api_key=os.getenv("YOUTUBE_API_KEY") or None,
            video_path=Path(req.video_path) if req.video_path else None,
            transcript_file=Path(req.transcript_file) if req.transcript_file else None,
            heatmap_json=Path(req.heatmap_json) if req.heatmap_json else None,
            download_cmd=os.getenv("YT_DOWNLOAD_CMD") or None,
            model=os.getenv("INDEXFRAME_MODEL", "gemini-2.5-flash"),
            variants=req.variants,
            size=parse_size(os.getenv("INDEXFRAME_SIZE", "1280x720")),
            output_gcs_uri=os.getenv("OUTPUT_GCS_URI") or None,
            skip_gemini=req.skip_gemini,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse(
        {
            "ok": True,
            "run_id": run_id,
            "index_url": f"/runs/{run_id}/index.html",
            "analysis_url": f"/runs/{run_id}/analysis.json",
            "variants_url": f"/runs/{run_id}/variants.json",
            "covers": [f"/runs/{run_id}/covers/{Path(p).name}" for p in result.cover_paths],
            "gcs_uri": result.gcs_uri,
            "public_urls": result.public_urls,
        }
    )
