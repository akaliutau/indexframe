#!/usr/bin/env bash
set -euo pipefail

# Original/full Indexframe Cloud Run Job-style entrypoint.
# Kept separate from indexframe_echo_job.py, which is the v1 async email echo worker.

YOUTUBE_URL="${YOUTUBE_URL:?Set YOUTUBE_URL}"
OUTPUT_GCS_URI="${OUTPUT_GCS_URI:?Set OUTPUT_GCS_URI}"
OUT_DIR="${OUT_DIR:-/tmp/indexframe-run}"

python indexframe_poc.py \
  --url "$YOUTUBE_URL" \
  --out-dir "$OUT_DIR" \
  --project "${PROJECT_ID:-}" \
  --location "${VERTEX_LOCATION:-${GOOGLE_CLOUD_LOCATION:-global}}" \
  --output-gcs-uri "$OUTPUT_GCS_URI"
