#!/usr/bin/env bash
set -euo pipefail
: "${YOUTUBE_URL:?YOUTUBE_URL is required}"
: "${OUTPUT_GCS_URI:?OUTPUT_GCS_URI is required, e.g. gs://bucket/signalframe/run-1}"
WORKDIR="${WORKDIR:-/tmp/signalframe-output}"
rm -rf "$WORKDIR" && mkdir -p "$WORKDIR"
args=(python /app/signalframe_poc.py --url "$YOUTUBE_URL" --out-dir "$WORKDIR" --output-gcs-uri "$OUTPUT_GCS_URI")
if [[ -n "${PROJECT_ID:-}" ]]; then args+=(--project "$PROJECT_ID"); fi
if [[ -n "${VERTEX_LOCATION:-}" ]]; then args+=(--location "$VERTEX_LOCATION"); fi
if [[ -n "${YOUTUBE_API_KEY:-}" ]]; then args+=(--youtube-api-key "$YOUTUBE_API_KEY"); fi
if [[ -n "${YT_DOWNLOAD_CMD:-}" ]]; then args+=(--download-cmd "$YT_DOWNLOAD_CMD"); fi
if [[ -n "${SIGNALFRAME_MODEL:-}" ]]; then args+=(--model "$SIGNALFRAME_MODEL"); fi
if [[ -n "${SIGNALFRAME_VARIANTS:-}" ]]; then args+=(--variants "$SIGNALFRAME_VARIANTS"); fi
if [[ -n "${SIGNALFRAME_SIZE:-}" ]]; then args+=(--size "$SIGNALFRAME_SIZE"); fi
echo "[job] ${args[*]}"
"${args[@]}"
echo "[job] Done: $OUTPUT_GCS_URI"
