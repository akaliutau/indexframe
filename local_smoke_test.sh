#!/usr/bin/env bash
set -euo pipefail

export DISABLE_AUTH=true
export MOCK_RUN_JOB_LOCALLY=true
export DEMO_EMAIL="${DEMO_EMAIL:-demo@example.com}"

uvicorn indexframe_api:app --host 0.0.0.0 --port "${PORT:-8080}"
