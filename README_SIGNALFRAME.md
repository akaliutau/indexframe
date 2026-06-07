# Indexframe PoC — YouTube cover variants from video evidence

Indexframe is a hackathon-friendly MVP for the adaptive cover-engine idea:

> User submits a YouTube link. The service analyzes public metadata, comments, transcript/subtitles, optional heatmap/retention evidence, and local video frames. It returns cover/hero image variants plus a rationale for each.

This is intentionally smaller than the full managed-growth product. The paid version can later connect YouTube OAuth/Analytics and auto-manage thumbnail refreshes.

## Installation

**Clone the repository**

```bash
git clone https://github.com/akaliutau/indexframe.git
cd indexframe
```

**Create and activate a Conda environment**

```bash
conda create -n indexframe python=3.12 -y
conda activate indexframe
```

**Install dependencies**

```bash
pip install -r requirements.indexframe.txt
```

## Deployment


```bash
npm --version
jq --version
gcloud --version
gcloud auth login
gcloud auth application-default login
npm install -g firebase-tools
firebase --version
firebase login
```

Deployment commands

```bash
python refresh_youtube_cookies.py \
  --project dev-indexframe \
  --secret-name indexframe-youtube-cookies \
  --update-cloud-run-job \
  --job-name indexframe-poc \
  --region us-central1
```

Build docker image and test cookie

```bash
sudo docker build -f Dockerfile.indexframe -t indexframe-cookie-poc .
```

Test YT pipeline in the docker:

```bash
sudo docker run --rm \
  -v "$PWD/.indexframe-youtube-cookies.txt:/secrets/youtube-cookies.txt:ro" \
  -e YT_DLP_COOKIES_FILE=/secrets/youtube-cookies.txt \
  -e INDEXFRAME_VARIANTS=1 \
  indexframe-cookie-poc \
  python -u indexframe_poc.py \
    --url 'https://www.youtube.com/watch?v=AHQIbAMTUkM' \
    --out-dir /tmp/indexframe-cookie-smoke \
    --skip-gemini
```
If the pipeline fails, rotate the cookies using `refresh_youtube_cookies` script

```bash
cp env.dev-linger.updated .env

# Fill these in .env:
# EMAIL_FROM=results@your-verified-demo-domain.com
# EMAIL_REPLY_TO=your-real-email@yourdomain.com

ENV_FILE=../.env scripts/deploy_firebase.sh
ENV_FILE=../.env scripts/deploy_indexframe_v1.sh
```

## Why this is a good quick demo

- **Input:** one YouTube URL.
- **Evidence:** title, description, public comments, transcript if downloader provides subtitles, optional most-watched/retention JSON, and extracted video frames.
- **Reasoning:** Gemini ranks frames and writes thumbnail strategies.
- **Rendering:** deterministic Pillow templates overlay crisp text on real frames.
- **Output:** `index.html`, `analysis.json`, `variants.json`, `covers/*.jpg`.

The important trick: we do **not** ask an image model to write text into an image. We ask Gemini for strategy and text, then render text with code. This keeps the demo reliable.

## Files

```text
indexframe_poc.py              # CLI pipeline
indexframe_api.py              # tiny synchronous FastAPI wrapper
requirements.indexframe.txt    # dependencies
Dockerfile.indexframe          # Cloud Run/API container
run_indexframe_job.sh          # Cloud Run Job-style entrypoint
```

## Local CLI run

Use your existing YouTube downloader by passing a command template. The template can use:

- `{url}` — original YouTube URL
- `{out}` — desired output file, e.g. `/tmp/run/download/source.mp4`
- `{out_dir}` — download directory
- `{out_base}` — output base path without extension

Example with your downloader:

```bash
export PROJECT_ID="your-gcp-project"
export VERTEX_LOCATION="global"
export YOUTUBE_API_KEY="optional-public-data-api-key"

python indexframe_poc.py \
  --url "https://www.youtube.com/watch?v=VIDEO_ID" \
  --out-dir ./runs/video-1 \
  --download-cmd 'your_yt_cli --url {url} --out {out}'
```

Example using `yt-dlp` fallback:

```bash
python indexframe_poc.py \
  --url "https://www.youtube.com/watch?v=ihU82ZtsJvk" \
  --out-dir ./runs/video-3
```

Open:

```bash
open ./runs/video-1/index.html
```

## FastAPI demo

```bash
uvicorn indexframe_api:app --host 0.0.0.0 --port 8080
```

Then open `http://localhost:8080`, paste a YouTube link, and wait for the synchronous demo response.

## Cloud Run API container

```bash
gcloud builds submit --tag gcr.io/$PROJECT_ID/indexframe-poc -f Dockerfile.indexframe .
gcloud run deploy indexframe-poc \
  --image gcr.io/$PROJECT_ID/indexframe-poc \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars PROJECT_ID=$PROJECT_ID,VERTEX_LOCATION=global,YOUTUBE_API_KEY=$YOUTUBE_API_KEY
```

For a private demo, omit `--allow-unauthenticated`.

## Cloud Run Job shape

Use `run_indexframe_job.sh` as an entrypoint when you want async batch generation and GCS upload.

Required env vars:

```bash
YOUTUBE_URL="https://www.youtube.com/watch?v=VIDEO_ID"
OUTPUT_GCS_URI="gs://your-bucket/indexframe/demo-run"
PROJECT_ID="your-gcp-project"
VERTEX_LOCATION="global"
```

Optional:

```bash
YT_DOWNLOAD_CMD='your_yt_cli --url {url} --out {out}'
INDEXFRAME_VARIANTS=6
INDEXFRAME_SIZE=1280x720
```

## Evidence priority

The PoC uses a weighted candidate system:

1. **Most watched / heatmap JSON** if provided.
2. **Comment timestamps** like `1:23`.
3. **Chapters** from downloader metadata JSON.
4. **Transcript signals** containing numbers, mistakes, strong claims, or how/why language.
5. **Fallback coverage** at opening/middle/late timestamps.

Frames are then locally scored for brightness, contrast, and edge energy before Gemini receives a contact sheet.

## Optional heatmap / retention input

Public YouTube APIs do not provide a reliable public “most replayed” endpoint. For hackathon demo, use one of these:

- a JSON file exported by your downloader if it exposes heatmap data;
- an internal scraper prototype;
- or, for connected channel owners later, YouTube Analytics retention data.

Pass it with:

```bash
--heatmap-json ./heatmap.json
```

Accepted loose shape:

```json
[
  {"start": 12.4, "value": 0.91},
  {"start": 83.0, "value": 0.74}
]
```

## MVP boundaries

In scope now:

- YouTube-first URL flow.
- Public metadata/comments via YouTube Data API key.
- Local downloader integration.
- Subtitle parsing when downloader saves `.srt`/`.vtt`.
- Frame extraction with ffmpeg.
- Gemini creative analysis.
- Deterministic cover rendering.
- Optional GCS upload.

Out of scope for the quick demo:

- OAuth sign-in.
- Auto-applying YouTube thumbnails.
- True A/B testing.
- Creator-specific memory.
- Instagram/TikTok support.

## How this evolves into the paid product

1. Connect creator channel via OAuth.
2. Pull private YouTube Analytics retention and CTR data.
3. Generate new cover variants for underperforming videos.
4. Let user approve or auto-apply high-confidence updates.
5. Store outcomes per creator to learn which visual claims, layouts, and comment themes lift CTR.

That turns the demo from “AI thumbnail generator” into an evolving packaging agent.
