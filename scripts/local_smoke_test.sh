#!/usr/bin/env bash
set -euo pipefail

# Local smoke test. Loads simple KEY=VALUE entries from .env without executing it.
load_dotenv_file() {
  local file="${1:-.env}"
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" == *"="* ]] || continue
    local key="${line%%=*}"
    local value="${line#*=}"
    key="$(printf '%s' "$key" | xargs)"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -n "${!key-}" ]] && continue
    value="${value#${value%%[![:space:]]*}}"
    value="${value%${value##*[![:space:]]}}"
    if [[ "$value" =~ ^\".*\"$ || "$value" =~ ^\'.*\'$ ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "$key=$value"
  done < "$file"
}

load_dotenv_file .env

export DISABLE_AUTH=true
export MOCK_RUN_JOB_LOCALLY=true
export DEMO_EMAIL="${DEMO_EMAIL:-demo@example.com}"
export SMTP_HOST="${SMTP_HOST:-mail.smtp2go.com}"
export SMTP_PORT="${SMTP_PORT:-2525}"
export SMTP_USERNAME="${SMTP_USERNAME:-indexframe}"
export EMAIL_FROM_NAME="${EMAIL_FROM_NAME:-Indexframe Results}"

uvicorn indexframe_api:app --host 0.0.0.0 --port "${PORT:-8080}"
