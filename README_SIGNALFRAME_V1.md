# Indexframe PoC v1 — Cloud Run async signup + SMTP2GO echo email

This patch keeps the existing `indexframe_poc.py` pipeline intact and adds the smallest useful product shell around it:

```text
Cloud Run service
  -> minimalist web UI
  -> Google sign-in via Firebase Auth
  -> one URL input
  -> POST /api/submit
  -> Cloud Run Job execution
  -> echo worker sends email with the URL entered
```

The original synchronous endpoint is still available at `POST /api/analyze` for local testing of the real Indexframe pipeline.

## New / changed files

```text
indexframe_api.py             # UI, /api/config, /api/submit, Cloud Run Job starter
indexframe_echo_job.py        # Cloud Run Job worker: one-line mock processor + SMTP2GO email sender
Dockerfile.indexframe         # one container for both service and job
cloudbuild.indexframe.yaml    # Cloud Build config for Dockerfile.indexframe
deploy_firebase.sh            # creates/reuses Firebase Web App and writes FIREBASE_* values to .env
deploy_indexframe_v1.sh       # GCP deploy script, creates/uses Secret Manager SMTP password
setup_smtp2go_secret.sh        # one-time helper for safe SMTP2GO password storage
run_indexframe_job.sh         # full pipeline job entrypoint, kept for later real async processing
local_smoke_test.sh            # local auth-disabled smoke test
.env.example                   # expected env vars, without secrets
.gitignore                     # keeps .env and service account keys out of git
requirements.indexframe.txt   # adds google-auth + requests explicitly
```

## Your current `.env`

Your existing `.env` already contains the important GCP defaults:

```bash
PROJECT_ID=dev-linger
REGION=us-central1
GOOGLE_CLOUD_PROJECT=dev-linger
GOOGLE_CLOUD_LOCATION=global
VERTEX_LOCATION=global
```

Keep those. Add the SMTP2GO values below.

## Add SMTP2GO values to `.env`

Do **not** put the SMTP2GO password in `.env`.

```bash
SMTP_HOST=mail.smtp2go.com
SMTP_PORT=2525
SMTP_USERNAME=indexframe
SMTP_PASSWORD_SECRET=indexframe-smtp-password
EMAIL_FROM=results@demo.yourdomain.com
EMAIL_FROM_NAME=Indexframe Results
EMAIL_REPLY_TO=your-existing-email@yourdomain.com
SMTP_TLS=true
```

Replace:

```text
results@demo.yourdomain.com
```

with the sender you verified in SMTP2GO, for example:

```text
results@demo.monodromic.com
```

or any verified sender/domain address you configured.

## Safely create the SMTP password secret

Use your tested SMTP2GO password only in the shell session. Do not write it into source files.

```bash
export SMTP_PASSWORD='<your SMTP2GO password for user indexframe>'
./setup_smtp2go_secret.sh
unset SMTP_PASSWORD
```

That creates or updates this Secret Manager secret:

```text
indexframe-smtp-password
```

and grants the Cloud Run service account access:

```text
indexframe-runner@PROJECT_ID.iam.gserviceaccount.com
```

The deploy script also performs this safely if `SMTP_PASSWORD` is present, so this is optional:

```bash
export SMTP_PASSWORD='<your SMTP2GO password>'
./deploy_indexframe_v1.sh
unset SMTP_PASSWORD
```

## Local SMTP smoke test through the app

If you want the local app to actually send email, export the password in the local shell only:

```bash
export SMTP_PASSWORD='<your SMTP2GO password>'
export EMAIL_FROM=results@demo.yourdomain.com
export EMAIL_REPLY_TO=your-existing-email@yourdomain.com
export DEMO_EMAIL=your-test-recipient@gmail.com
./local_smoke_test.sh
```

Open:

```text
http://localhost:8080
```

Submit any `https://...` URL. The job runs locally and sends the echo email.

If `SMTP_PASSWORD` is not set locally, the job prints the email payload instead of sending it.

## Deploy to GCP

### 1. Create / configure Firebase Web App

The UI is still hosted by Cloud Run. Firebase is used only for Google SSO and browser-side ID tokens.

Run the separate Firebase setup script first:

```bash
chmod +x deploy_firebase.sh
./deploy_firebase.sh
```

The script uses the Firebase CLI to add Firebase to the existing GCP project if needed, create/reuse a Firebase Web App, fetch the public Web SDK config, and update `.env` with:

```bash
DISABLE_AUTH=false
FIREBASE_PROJECT_ID=dev-linger
FIREBASE_API_KEY=...
FIREBASE_AUTH_DOMAIN=dev-linger.firebaseapp.com
FIREBASE_APP_ID=...
FIREBASE_WEB_APP_DISPLAY_NAME=Indexframe Web
```

One manual Firebase Console step is still required for simplicity:

```text
Firebase Console -> Authentication -> Sign-in method -> Google -> Enable
```

After Cloud Run is deployed, add the Cloud Run service domain to:

```text
Firebase Console -> Authentication -> Settings -> Authorized domains
```

### 2. Deploy Cloud Run service and job

```bash
chmod +x deploy_indexframe_v1.sh setup_smtp2go_secret.sh local_smoke_test.sh
./deploy_indexframe_v1.sh
```

The script:

1. Loads simple `KEY=VALUE` entries from `.env` without executing the file.
2. Enables Cloud Run, Cloud Build, Artifact Registry, IAM, and Secret Manager APIs.
3. Creates/reuses the Artifact Registry repo.
4. Creates/reuses the `indexframe-runner` service account.
5. Creates or updates the SMTP2GO password secret if `SMTP_PASSWORD` is exported.
6. Builds the container using `cloudbuild.indexframe.yaml`.
7. Deploys the Cloud Run Job and Cloud Run service.

Useful optional overrides:

```bash
export REGION=us-central1
export SERVICE_NAME=indexframe-poc
export JOB_NAME=indexframe-echo-job
export REPOSITORY=indexframe
```

## Runtime behavior

### Browser

1. User opens the Cloud Run service URL.
2. User clicks **Continue with Google**.
3. Firebase Auth returns an ID token.
4. The page shows the one URL input.
5. User clicks **Submit**.
6. UI shows: `You will receive the link in your email.`
7. UI returns to the input page after a short delay.

### Backend

`POST /api/submit`:

1. Verifies the Firebase ID token.
2. Extracts the signed-in user's email.
3. Validates the submitted URL.
4. Starts the configured Cloud Run Job with env overrides:

```text
SUBMITTED_URL
USER_EMAIL
SUBMISSION_ID
```

### Cloud Run Job

`indexframe_echo_job.py` runs this one-line mock processor:

```python
import json, os; print(json.dumps({'ok': True, 'result_url': os.environ.get('SUBMITTED_URL', '')}))
```

Then it sends the user an email using SMTP2GO:

```text
SMTP_HOST=mail.smtp2go.com
SMTP_PORT=2525
SMTP_USERNAME=indexframe
SMTP_PASSWORD=<from Secret Manager>
```

## Manual Cloud Run Job test

After deploy:

```bash
gcloud run jobs execute indexframe-echo-job \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --update-env-vars "SUBMITTED_URL=https://example.com,USER_EMAIL=your-test-recipient@gmail.com,SUBMISSION_ID=manual-test" \
  --wait
```

## IAM notes

`deploy_indexframe_v1.sh` creates a simple service account named `indexframe-runner` and grants it:

```text
roles/run.developer
roles/secretmanager.secretAccessor on the SMTP password secret
```

This is intentionally broad for PoC speed, not production least privilege.

## Later replacement with real processing

When the mock is no longer enough, keep the same `/api/submit` and Cloud Run Job shape, but deploy the job command as:

```bash
--command bash --args run_indexframe_job.sh
```

or create a new worker script that calls `indexframe_poc.py`, uploads artifacts to GCS, and emails the generated signed result link.
