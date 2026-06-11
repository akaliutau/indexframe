# 🎬 IndexFrame: Evidence-Driven YouTube Cover Factory

<p align="left">
  <img alt="Status" src="https://img.shields.io/badge/status-hackathon--prototype-orange">
  <img alt="AI" src="https://img.shields.io/badge/AI-Gemini--powered-blue">
  <img alt="Output" src="https://img.shields.io/badge/output-cover--variants-green">
</p>

**IndexFrame** is an AI system that turns a single YouTube link into a set of 
high-signal cover / hero image variants — grounded in the actual video.



HOW it works:

Paste a URL.

Indexframe analyzes the title, description, comments, transcript, optional retention evidence, and extracted video frames.
Then it generates multiple thumbnail concepts, explains the rationale, renders crisp text overlays, and packages everything into a reviewable result page.

The bigger idea is simple:

```text
YouTube video
  → evidence extraction
  → key moment discovery
  → thumbnail strategy
  → cover variants
  → result pack
  → better creative decisions
```

---

## The Idea

Most thumbnail tools start from a prompt:

```text
"make me a cool thumbnail"
```

Indexframe starts from evidence:

```text
video metadata
comments
transcript
chapters
retention / heatmap signals
visual frames
```

That matters because the best thumbnail is usually not the prettiest frame.
It is the frame that captures the strongest promise:

* a surprising claim;
* a visible transformation;
* a mistake or contradiction;
* a before / after moment;
* an emotional reaction;
* a number, result, or challenge;
* the one scene people actually care about.

Indexframe treats the thumbnail as a **packaging problem**.

The video already contains the signal.
The system’s job is to find it, sharpen it, and turn it into clickable visual options.

---

## Why Video Evidence Matters

A YouTube cover is a tiny billboard.
It has to compress the entire reason to watch into one image.

Random image generation can look good, but it often loses the truth of the video. Indexframe uses the video itself as the creative source:

```text
video
  → moments
  → frames
  → audience comments
  → transcript hooks
  → visual scoring
  → Gemini creative direction
  → deterministic final render
```

The important trick:

> Indexframe does not rely on an image model to write perfect text into the image.

Instead, Gemini helps with strategy, headline, layout, and candidate selection.
The final typography and layout are rendered by code, so covers stay crisp, readable, and reproducible.

That makes the demo reliable enough for a hackathon stage.

---

## What the System Does Today

The current prototype already implements the foundation:

1. Accepts a YouTube URL.
2. Fetches public video metadata.
3. Reads title, description, statistics, and comments when available.
4. Extracts transcript / subtitle evidence when available.
5. Accepts optional heatmap or retention JSON.
6. Downloads or receives a local video file.
7. Extracts candidate frames.
8. Scores frames locally for brightness, contrast, and edge energy.
9. Builds a contact sheet for visual reasoning.
10. Uses Gemini to identify packaging problems and cover strategies.
11. Generates multiple distinct cover variants.
12. Renders final covers with deterministic text layout.
13. Writes `index.html`, JSON analysis, frame artifacts, and cover images.
14. Optionally uploads results to GCS.
15. Optionally stores durable result packs in MongoDB.
16. Optionally runs behind a Cloud Run + Firebase Auth demo shell.
17. Optionally emails the result link after async job execution.

This is enough to demo the full product loop:

```text
one URL in → evidence-backed cover pack out
```

---

## Architecture

```text
YouTube URL
    │
    ▼
Ingestion Layer
metadata + comments + transcript + optional heatmap
    │
    ▼
Video Frame Extractor
downloaded/local video → candidate moments → frames
    │
    ▼
Evidence Scorer
timestamps + transcript hooks + comments + visual quality
    │
    ▼
Gemini Creative Director
packaging analysis + headline ideas + layout choices
    │
    ▼
Cover Renderer
Pillow templates + crisp deterministic text overlays
    │
    ▼
Result Pack
index.html + analysis.json + moments.json + variants.json + covers/
    │
    ▼
Cloud Demo Shell
Firebase sign-in + Cloud Run Job + email delivery
```

---

## Core Components

### `indexframe_poc.py`

The main pipeline.

It takes a YouTube URL, gathers evidence, extracts frames, asks Gemini for creative direction, renders cover variants, writes artifacts, and optionally uploads results.

### `indexframe_api.py`

The FastAPI demo wrapper.

It provides a minimalist web UI, Firebase Google sign-in, URL submission, `/api/submit`, and `/api/analyze` for local development.

### `indexframe_echo_job.py`

The first async worker.

For v1, it proves the Cloud Run Job + email flow with a mock result. The real worker can reuse the same shape and call `indexframe_poc.py`.

### `indexframe_result_pack_store.py`

The persistence layer.

It builds MongoDB-friendly Image Hero result packs containing metadata, analysis, variants, frames, artifacts, hashes, and the best candidate.

### `refresh_youtube_cookies.py`

A demo helper for authenticated YouTube downloads.

It uses a dedicated Playwright browser profile, exports a Netscape cookies file, and can upload it to Secret Manager for Cloud Run usage.

### `run_indexframe_job.sh`

The Cloud Run Job-style entrypoint for real async processing.

It runs the full pipeline with `YOUTUBE_URL`, `OUTPUT_GCS_URI`, and cloud environment configuration.

---

## Result Packs

Every serious run should become a result pack.

A result pack is the atomic memory unit of Indexframe:

```text
video_id
source URL
metadata
comments sample
transcript evidence
candidate frames
selected moments
Gemini analysis
cover variants
best variant
local artifacts
GCS URIs
signed URLs
model configuration
content hashes
submission id
```

Result packs are useful for:

* comparing thumbnail strategies;
* ranking cover candidates;
* debugging failed generations;
* preserving evidence;
* creating before / after case studies;
* building a creator-facing history;
* later training better thumbnail-selection agents.

A future result pack can look like this:

```json
{
  "pack_id": "a1b2c3d4",
  "project": "IndexFrame",
  "pipeline": "indexframe.image_hero",
  "video": {
    "video_id": "VIDEO_ID",
    "title": "How I Built This",
    "channel_title": "Creator Channel"
  },
  "input": {
    "size": {
      "width": 1280,
      "height": 720
    },
    "requested_variants": 6,
    "model": "gemini-2.5-flash",
    "image_model": "gemini-2.5-flash-image"
  },
  "best_variant": {
    "headline": "The Hidden Mistake",
    "layout": "big_number",
    "score_0_to_100": 91
  },
  "artifacts": {
    "index_html": "...",
    "analysis_json": "...",
    "variants_json": "...",
    "covers": ["cover_01.jpg", "cover_02.jpg"]
  }
}
```

The goal is not to create pretty files once.
The goal is to build reusable creative evidence.

---

## The Cover Optimization Loop

Indexframe is designed to become a feedback loop.

Today:

```text
generate 6 cover variants
```

Tomorrow:

```text
generate variants
  → publish or test
  → observe CTR / retention
  → compare against previous covers
  → learn creator-specific patterns
  → generate stronger variants
```

The loop can eventually optimize for:

```text
clarity > curiosity > evidence match > mobile readability > brand fit > CTR
```

This turns thumbnail generation from a guessing game into an empirical creative process.

---

## Why This Is a Great Hackathon Demo

Indexframe has a simple magic moment:

```text
Paste a YouTube link.
Get a gallery of evidence-backed covers.
```

It is visual, fast to understand, and easy to pitch.

The demo also has real technical depth:

* multimodal reasoning;
* video frame extraction;
* LLM strategy generation;
* deterministic image rendering;
* Cloud Run async jobs;
* Firebase Google sign-in;
* email delivery;
* optional GCS result hosting;
* optional MongoDB persistence.

It feels like a product, not just a script.

---

## Local Quick Start

Clone the repository:

```bash
git clone https://github.com/akaliutau/indexframe.git
cd indexframe
```

Create and activate a Conda environment:

```bash
conda create -n indexframe python=3.12 -y
conda activate indexframe
```

Install dependencies:

```bash
pip install -r requirements.indexframe.txt
```

Run the pipeline:

```bash
python indexframe_poc.py \
  --url "https://www.youtube.com/watch?v=VIDEO_ID" \
  --out-dir ./runs/demo
```

Open the result page:

```bash
open ./runs/demo/index.html
```

Run with deterministic fallback for smoke tests:

```bash
python indexframe_poc.py \
  --url "https://www.youtube.com/watch?v=VIDEO_ID" \
  --out-dir ./runs/smoke \
  --skip-gemini
```

---

## Using a Custom Downloader

Indexframe can use a pluggable downloader command.

The command template can use:

```text
{url}      original YouTube URL
{out}      expected output video path
{out_dir}  output directory
{out_base} output path without extension
```

Example:

```bash
python indexframe_poc.py \
  --url "https://www.youtube.com/watch?v=VIDEO_ID" \
  --out-dir ./runs/video-1 \
  --download-cmd 'your_yt_cli --url {url} --out {out}'
```

Example with cookie-mounted Docker smoke test:

```bash
docker build -f Dockerfile.indexframe -t indexframe-cookie-poc .

docker run --rm \
  -v "$PWD/.indexframe-youtube-cookies.txt:/secrets/youtube-cookies.txt:ro" \
  -e YT_DLP_COOKIES_FILE=/secrets/youtube-cookies.txt \
  -e INDEXFRAME_VARIANTS=1 \
  indexframe-cookie-poc \
  python -u indexframe_poc.py \
    --url 'https://www.youtube.com/watch?v=VIDEO_ID' \
    --out-dir /tmp/indexframe-cookie-smoke \
    --skip-gemini
```

---

## FastAPI Demo

Run the local API:

```bash
uvicorn indexframe_api:app --host 0.0.0.0 --port 8080
```

Open:

```text
http://localhost:8080
```

The local app supports:

```text
POST /api/analyze
```

for synchronous pipeline testing.

The async product shell supports:

```text
POST /api/submit
```

for URL submission, authenticated user extraction, Cloud Run Job execution, and email delivery.

---

## Environment

For Vertex AI Gemini:

```bash
export GOOGLE_GENAI_USE_VERTEXAI=True
export GOOGLE_CLOUD_PROJECT='your-project-id'
export GOOGLE_CLOUD_LOCATION='global'
export VERTEX_LOCATION='global'
export INDEXFRAME_MODEL='gemini-2.5-flash'
export INDEXFRAME_IMAGE_MODEL='gemini-2.5-flash-image'
```

For Gemini API-key mode:

```bash
export GOOGLE_GENAI_USE_VERTEXAI=False
export GEMINI_API_KEY='...'
```

For YouTube public metadata:

```bash
export YOUTUBE_API_KEY='...'
```

For MongoDB result packs:

```bash
export MONGODB_URI='mongodb+srv://...'
export INDEXFRAME_MONGODB_DB='indexframe'
export INDEXFRAME_MONGODB_COLLECTION='image_hero_packs'
```

For GCS output:

```bash
export OUTPUT_GCS_URI='gs://your-bucket/indexframe/demo-run'
```

For email delivery:

```bash
export SMTP_HOST='mail.smtp2go.com'
export SMTP_PORT='2525'
export SMTP_USERNAME='indexframe'
export SMTP_PASSWORD_SECRET='indexframe-smtp-password'
export EMAIL_FROM='results@your-verified-demo-domain.com'
export EMAIL_FROM_NAME='Indexframe Results'
export EMAIL_REPLY_TO='you@yourdomain.com'
export SMTP_TLS='true'
```

Do not commit secrets, cookies, `.env` files, or service account keys.

---

## Cloud Run Demo Flow

The deployed hackathon flow is intentionally simple:

```text
Cloud Run service
  → minimalist web UI
  → Google sign-in with Firebase Auth
  → one URL input
  → POST /api/submit
  → Cloud Run Job
  → email with result link
```

Runtime behavior:

1. User opens the Cloud Run service URL.
2. User signs in with Google.
3. User pastes a YouTube URL.
4. Backend verifies the Firebase token.
5. Backend starts a Cloud Run Job.
6. Job processes the submission.
7. User receives an email with the result.

Manual job execution:

```bash
gcloud run jobs execute indexframe-echo-job \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --update-env-vars "SUBMITTED_URL=https://example.com,USER_EMAIL=your-test-recipient@gmail.com,SUBMISSION_ID=manual-test" \
  --wait
```

For full processing, run the job with:

```bash
--command bash --args run_indexframe_job.sh
```

---

## Run Artifacts

Each run creates a structured folder:

```text
runs/<run-id>/
  index.html
  analysis.json
  moments.json
  variants.json
  frames/
    frame_001.jpg
    frame_002.jpg
  covers/
    cover_001.jpg
    cover_002.jpg
    cover_003.jpg
  llm_debug.jsonl
```

These artifacts make each run auditable:

* why a frame was selected;
* what evidence supported the hook;
* what headline was generated;
* what layout was used;
* which cover looked strongest;
* where the final files were stored.

---

## Roadmap

### 1. Better creative scoring

Rank covers by mobile readability, emotional clarity, novelty, and evidence match.

### 2. YouTube OAuth + Analytics

Connect creator accounts and use real CTR / retention feedback.

### 3. Automatic thumbnail refresh

Detect underperforming videos and suggest new covers.

### 4. Multi-agent creative search

Generate competing strategies: curiosity, authority, conflict, transformation, mistake, result.

### 5. Brand memory

Learn creator-specific style rules, colors, recurring visual language, and audience expectations.

### 6. A/B testing workflow

Create testable cover packs and track which visual promise wins.

### 7. Dataset builder

Turn successful thumbnail experiments into durable training examples for future creative agents.

---

## Disclaimer

Use Indexframe only with videos, accounts, cookies, and download methods you are allowed to use.

The project does not bypass login, CAPTCHA, paywalls, platform restrictions, access controls, or rate limits.

For demos, use dedicated test accounts and keep cookies and secrets out of source control.

---

## Vision

Indexframe is a system for turning video content into its own creative engine.

The video provides the evidence.
The comments reveal audience language.
The transcript exposes the hooks.
The frames provide the raw visual material.
The result pack becomes memory.
The feedback loop makes every next cover smarter.

The destination is a creator tool that can look at a video and answer:

```text
What is the most clickable truthful promise inside this content?
```

Then it turns that promise into covers people actually want to click.
