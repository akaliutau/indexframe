#!/usr/bin/env bash
set -euo pipefail

# Set up Firebase for the Cloud Run-hosted Indexframe PoC.
#
# This script does NOT deploy Firebase Hosting. The UI remains served by Cloud Run.
# It only:
#   1. enables the Firebase / Identity Toolkit APIs,
#   2. adds Firebase resources to the existing Google Cloud project if needed,
#   3. creates or reuses a Firebase Web App,
#   4. fetches the public Firebase Web SDK config,
#   5. writes FIREBASE_* values back to .env safely.
#
# One-time prerequisites on your laptop:
#   gcloud auth login
#   gcloud auth application-default login
#   firebase login
#
# Usage:
#   ./deploy_firebase.sh
#
# Optional overrides:
#   ENV_FILE=.env FIREBASE_WEB_APP_DISPLAY_NAME="Indexframe Web" ./deploy_firebase.sh

CALLER_DIR="$PWD"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -n "${ENV_FILE:-}" && "$ENV_FILE" != /* ]]; then
  if [[ -f "$CALLER_DIR/$ENV_FILE" ]]; then
    ENV_FILE="$CALLER_DIR/$ENV_FILE"
  elif [[ -f "$SCRIPT_DIR/$ENV_FILE" ]]; then
    ENV_FILE="$SCRIPT_DIR/$ENV_FILE"
  else
    ENV_FILE="$CALLER_DIR/$ENV_FILE"
  fi
fi

cd "$REPO_ROOT"

ENV_FILE="${ENV_FILE:-.env}"
FIREBASE_CONFIG_JSON="${FIREBASE_CONFIG_JSON:-.firebase-web-config.json}"

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
    # Environment variables already exported by the caller win over .env.
    if [[ -n "${!key-}" ]]; then
      continue
    fi
    value="${value#${value%%[![:space:]]*}}"
    value="${value%${value##*[![:space:]]}}"
    if [[ "$value" =~ ^\".*\"$ || "$value" =~ ^\'.*\'$ ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "$key=$value"
  done < "$file"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: missing required command: $1" >&2
    exit 1
  fi
}

firebase_cmd() {
  if command -v firebase >/dev/null 2>&1; then
    firebase "$@"
  else
    npx --yes firebase-tools@latest "$@"
  fi
}

json_find_web_app_id() {
  local json_file="$1"
  local display_name="$2"
  python3 - "$json_file" "$display_name" <<'PY'
import json
import sys
from typing import Any

path, wanted = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

objects = []

def walk(x: Any):
    if isinstance(x, dict):
        objects.append(x)
        for v in x.values():
            walk(v)
    elif isinstance(x, list):
        for v in x:
            walk(v)

walk(data)

def is_web_app(o: dict) -> bool:
    platform = str(o.get("platform") or o.get("appPlatform") or "").upper()
    app_id = str(o.get("appId") or o.get("app_id") or "")
    return platform in {"WEB", "PLATFORM_WEB"} or app_id.startswith("1:")

web_apps = [o for o in objects if (o.get("appId") or o.get("app_id")) and is_web_app(o)]
for o in web_apps:
    if str(o.get("displayName") or o.get("display_name") or o.get("name") or "") == wanted:
        print(o.get("appId") or o.get("app_id"))
        raise SystemExit(0)
if web_apps:
    print(web_apps[0].get("appId") or web_apps[0].get("app_id"))
PY
}

json_extract_firebase_config() {
  local json_file="$1"
  python3 - "$json_file" <<'PY'
import json
import sys
from typing import Any

with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)

objects = []

def walk(x: Any):
    if isinstance(x, dict):
        objects.append(x)
        for v in x.values():
            walk(v)
    elif isinstance(x, list):
        for v in x:
            walk(v)

walk(data)

best = None
for o in objects:
    if "apiKey" in o and "projectId" in o:
        best = o
        break

if not best:
    raise SystemExit("Could not find Firebase SDK config in CLI JSON output")

for key in ["apiKey", "authDomain", "projectId", "appId"]:
    print(f"{key}={best.get(key, '')}")
PY
}

upsert_env_file() {
  local file="$1"
  python3 - "$file" <<'PY'
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
updates = json.loads(os.environ["ENV_UPDATES_JSON"])
path.touch(mode=0o600, exist_ok=True)

backup = path.with_name(path.name + ".bak." + time.strftime("%Y%m%d%H%M%S"))
shutil.copy2(path, backup)

lines = path.read_text(encoding="utf-8").splitlines()
seen = set()
out = []

def fmt(value: str) -> str:
    value = str(value)
    if not value:
        return ""
    if re.search(r"\s|#|\"|'|\\", value):
        return '"' + value.replace('\\', '\\\\').replace('\"', '\\"') + '"'
    return value

for line in lines:
    matched = False
    for key, value in updates.items():
        if re.match(rf"^\s*{re.escape(key)}\s*=", line):
            out.append(f"{key}={fmt(value)}")
            seen.add(key)
            matched = True
            break
    if not matched:
        out.append(line)

missing = [(k, v) for k, v in updates.items() if k not in seen]
if missing:
    if out and out[-1].strip():
        out.append("")
    out.append("# Firebase public web config for Google SSO; generated/updated by ./deploy_firebase.sh")
    for key, value in missing:
        out.append(f"{key}={fmt(value)}")

path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
print(backup)
PY
}

load_dotenv_file "$ENV_FILE"

require_cmd gcloud
require_cmd python3
if ! command -v firebase >/dev/null 2>&1; then
  require_cmd npx
fi

PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}"
PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID in ${ENV_FILE} or environment}"
FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-$PROJECT_ID}"
FIREBASE_AUTH_DOMAIN="${FIREBASE_AUTH_DOMAIN:-${FIREBASE_PROJECT_ID}.firebaseapp.com}"
FIREBASE_WEB_APP_DISPLAY_NAME="${FIREBASE_WEB_APP_DISPLAY_NAME:-Indexframe Web}"
DISABLE_AUTH="${DISABLE_AUTH:-false}"

printf '\n[1/5] Configuring gcloud project %s\n' "$PROJECT_ID"
gcloud config set project "$PROJECT_ID" >/dev/null

printf '\n[2/5] Enabling Firebase/Auth APIs\n'
gcloud services enable \
  firebase.googleapis.com \
  identitytoolkit.googleapis.com \
  --project "$PROJECT_ID"

printf '\n[3/5] Adding Firebase to existing GCP project if needed\n'
ADD_LOG="$(mktemp)"
if ! firebase_cmd projects:addfirebase "$PROJECT_ID" --non-interactive >"$ADD_LOG" 2>&1; then
  if firebase_cmd apps:list --project "$PROJECT_ID" --json >/dev/null 2>&1; then
    echo "Firebase already enabled for ${PROJECT_ID}; continuing."
  else
    cat "$ADD_LOG" >&2
    rm -f "$ADD_LOG"
    cat >&2 <<EOF

ERROR: Firebase has not been added to GCP project ${PROJECT_ID}.

You are signed in and may be Owner, but Firebase still returns 403 here when
the Firebase Terms have not been accepted for this Google account.

Open this once, accept the Firebase Terms / add Firebase, then rerun:
  https://console.firebase.google.com/project/${PROJECT_ID}/overview

EOF
    exit 1
  fi
fi
rm -f "$ADD_LOG"

printf '\n[4/5] Creating or reusing Firebase Web App: %s\n' "$FIREBASE_WEB_APP_DISPLAY_NAME"
APPS_JSON="$(mktemp)"
if ! firebase_cmd apps:list WEB --project "$PROJECT_ID" --json > "$APPS_JSON" 2>/dev/null; then
  firebase_cmd apps:list --project "$PROJECT_ID" --json > "$APPS_JSON"
fi

FIREBASE_APP_ID="$(json_find_web_app_id "$APPS_JSON" "$FIREBASE_WEB_APP_DISPLAY_NAME" | head -n 1 || true)"
rm -f "$APPS_JSON"

if [[ -z "$FIREBASE_APP_ID" ]]; then
  CREATE_JSON="$(mktemp)"
  firebase_cmd apps:create WEB "$FIREBASE_WEB_APP_DISPLAY_NAME" \
    --project "$PROJECT_ID" \
    --json > "$CREATE_JSON"
  FIREBASE_APP_ID="$(json_find_web_app_id "$CREATE_JSON" "$FIREBASE_WEB_APP_DISPLAY_NAME" | head -n 1 || true)"
  rm -f "$CREATE_JSON"
fi

if [[ -z "$FIREBASE_APP_ID" ]]; then
  echo "ERROR: could not create or find a Firebase Web App." >&2
  exit 1
fi

echo "Firebase Web App ID: ${FIREBASE_APP_ID}"

printf '\n[5/5] Fetching Firebase Web SDK config and updating %s\n' "$ENV_FILE"
firebase_cmd apps:sdkconfig WEB "$FIREBASE_APP_ID" \
  --project "$PROJECT_ID" \
  --json > "$FIREBASE_CONFIG_JSON"

# shellcheck disable=SC2046
export $(json_extract_firebase_config "$FIREBASE_CONFIG_JSON" | xargs)

if [[ -z "${apiKey:-}" || -z "${projectId:-}" || -z "${appId:-}" ]]; then
  echo "ERROR: Firebase config output is missing apiKey/projectId/appId." >&2
  echo "Inspect ${FIREBASE_CONFIG_JSON}" >&2
  exit 1
fi

ENV_UPDATES_JSON="$(python3 - <<PY
import json
print(json.dumps({
  "DISABLE_AUTH": "${DISABLE_AUTH}",
  "FIREBASE_PROJECT_ID": "${projectId}",
  "FIREBASE_API_KEY": "${apiKey}",
  "FIREBASE_AUTH_DOMAIN": "${authDomain:-$FIREBASE_AUTH_DOMAIN}",
  "FIREBASE_APP_ID": "${appId}",
  "FIREBASE_WEB_APP_DISPLAY_NAME": "${FIREBASE_WEB_APP_DISPLAY_NAME}",
}))
PY
)"
export ENV_UPDATES_JSON
BACKUP_FILE="$(upsert_env_file "$ENV_FILE")"

cat <<EOF2

Done.
Updated ${ENV_FILE}; backup: ${BACKUP_FILE}
Saved raw Firebase SDK config to: ${FIREBASE_CONFIG_JSON}

Next manual Firebase Auth step, if not already done:
  Firebase Console -> Authentication -> Sign-in method -> Google -> Enable

After Cloud Run is deployed, also add your Cloud Run host under:
  Firebase Console -> Authentication -> Settings -> Authorized domains

Then deploy/redeploy Cloud Run using script deploy_indexframe_v1.sh

EOF2
