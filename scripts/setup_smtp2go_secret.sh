#!/usr/bin/env bash
set -euo pipefail

# One-time helper to safely put the SMTP2GO password in GCP Secret Manager.
# Usage:
#   export SMTP_PASSWORD='<smtp2go password>'
#   ./setup_smtp2go_secret.sh
#
# It never writes the password to source files and never prints it.

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

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID in .env or environment}"
REGION="${REGION:-us-central1}"
SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-indexframe-runner}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com}"
SMTP_PASSWORD_SECRET="${SMTP_PASSWORD_SECRET:-indexframe-smtp-password}"
SMTP_PASSWORD_VALUE="${SMTP_PASSWORD:-${SMTP2GO_PASSWORD:-}}"

if [[ -z "$SMTP_PASSWORD_VALUE" ]]; then
  printf 'Set SMTP_PASSWORD in your shell first. Example:\n'
  printf "  export SMTP_PASSWORD='<your SMTP2GO password>'\n"
  exit 2
fi

gcloud config set project "$PROJECT_ID" >/dev/null
gcloud services enable secretmanager.googleapis.com iam.googleapis.com --project "$PROJECT_ID" >/dev/null

tmp="$(mktemp)"
chmod 600 "$tmp"
printf '%s' "$SMTP_PASSWORD_VALUE" > "$tmp"

if gcloud secrets describe "$SMTP_PASSWORD_SECRET" --project "$PROJECT_ID" >/dev/null 2>&1; then
  printf 'Adding a new version to Secret Manager secret %s\n' "$SMTP_PASSWORD_SECRET"
  gcloud secrets versions add "$SMTP_PASSWORD_SECRET" \
    --data-file="$tmp" \
    --project "$PROJECT_ID" >/dev/null
else
  printf 'Creating Secret Manager secret %s\n' "$SMTP_PASSWORD_SECRET"
  gcloud secrets create "$SMTP_PASSWORD_SECRET" \
    --replication-policy="automatic" \
    --data-file="$tmp" \
    --project "$PROJECT_ID" >/dev/null
fi
rm -f "$tmp"
unset SMTP_PASSWORD_VALUE SMTP_PASSWORD SMTP2GO_PASSWORD

# Create the Cloud Run service account if the deploy script has not done it yet.
gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" \
  --display-name="Indexframe PoC runner" \
  --project "$PROJECT_ID" >/dev/null 2>&1 || true

gcloud secrets add-iam-policy-binding "$SMTP_PASSWORD_SECRET" \
  --project "$PROJECT_ID" \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/secretmanager.secretAccessor" >/dev/null

cat <<EOF2
Done.
Secret: ${SMTP_PASSWORD_SECRET}
Project: ${PROJECT_ID}
Service account granted access: ${SERVICE_ACCOUNT}

Deploy/update the job with:
  ./deploy_indexframe_v1.sh
EOF2
