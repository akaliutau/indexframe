#!/usr/bin/env python3
"""Tiny FastAPI demo wrapper for SignalFrame PoC.

Run locally:
  uvicorn signalframe_api:app --host 0.0.0.0 --port 8080

POST /api/analyze with JSON {"url":"https://youtube.com/watch?v=..."}
The request is synchronous for hackathon simplicity.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from signalframe_poc import run_pipeline, parse_size

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = Path(os.getenv("SIGNALFRAME_RUNS_DIR", str(BASE_DIR / "runs")))
RUNS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="SignalFrame PoC")
app.mount("/runs", StaticFiles(directory=str(RUNS_DIR)), name="runs")


class AnalyzeRequest(BaseModel):
    url: str
    variants: int = int(os.getenv("SIGNALFRAME_VARIANTS", "6"))
    skip_gemini: bool = False
    video_path: Optional[str] = None
    transcript_file: Optional[str] = None
    heatmap_json: Optional[str] = None


@app.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    return HTMLResponse(
        """
        <!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>SignalFrame PoC</title>
        <style>
          body { margin:0; font-family:system-ui,sans-serif; background:#0e1015; color:#fff; }
          main { max-width:720px; margin:12vh auto; padding:24px; }
          input,button { width:100%; box-sizing:border-box; padding:16px; border-radius:12px; border:1px solid #333a4b; font-size:16px; }
          input { background:#171b25; color:white; }
          button { margin-top:12px; background:#eef2ff; color:#080a10; font-weight:700; cursor:pointer; }
          pre { white-space:pre-wrap; background:#171b25; border-radius:12px; padding:16px; color:#cbd5e1; }
        </style></head><body><main>
          <h1>SignalFrame</h1>
          <p>Paste a YouTube link. The PoC downloads/reads the video, extracts evidence-ranked frames, and returns cover variants.</p>
          <input id="url" placeholder="https://www.youtube.com/watch?v=..." />
          <button onclick="run()">Generate covers</button>
          <pre id="out">Ready.</pre>
          <script>
            async function run(){
              const out = document.getElementById('out');
              out.textContent = 'Running... this is synchronous for the demo.';
              const res = await fetch('/api/analyze', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({url: document.getElementById('url').value})});
              const data = await res.json();
              if(!res.ok){ out.textContent = JSON.stringify(data,null,2); return; }
              out.innerHTML = 'Done. Open: ' + data.index_url + '\n\n' + JSON.stringify(data,null,2);
              window.open(data.index_url, '_blank');
            }
          </script>
        </main></body></html>
        """
    )


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
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
            model=os.getenv("SIGNALFRAME_MODEL", "gemini-2.5-flash"),
            variants=req.variants,
            size=parse_size(os.getenv("SIGNALFRAME_SIZE", "1280x720")),
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
