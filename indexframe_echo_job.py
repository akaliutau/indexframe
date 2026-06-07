#!/usr/bin/env python3
"""Cloud Run Job worker for the v1 async demo.

This is deliberately boring: it invokes a one-line Python mock processor that only
returns the URL, then emails the result. If SMTP settings are missing, it prints
the email to Cloud Logging so the demo still proves the async path.

For SMTP2GO, configure:
  SMTP_HOST=mail.smtp2go.com
  SMTP_PORT=2525
  SMTP_USERNAME=indexframe
  SMTP_PASSWORD=<Secret Manager env var at runtime>
  EMAIL_FROM=results@demo.yourdomain.com
  EMAIL_FROM_NAME=Indexframe Results
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import subprocess
import sys
from email.message import EmailMessage
from email.utils import formataddr


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def run_one_line_mock(url: str) -> dict:
    code = "import json,os; print(json.dumps({'ok': True, 'result_url': os.environ.get('SUBMITTED_URL', '')}))"
    completed = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, "SUBMITTED_URL": url},
        check=True,
        timeout=20,
    )
    return json.loads(completed.stdout)


def from_header() -> str:
    from_email = env("EMAIL_FROM") or env("SMTP_FROM") or env("SMTP_USERNAME")
    from_name = env("EMAIL_FROM_NAME")
    if from_name and "<" not in from_email and ">" not in from_email:
        return formataddr((from_name, from_email))
    return from_email


def send_email(*, to_email: str, subject: str, text: str) -> None:
    host = env("SMTP_HOST", "mail.smtp2go.com")
    port = int(env("SMTP_PORT", "2525"))
    username = env("SMTP_USERNAME", "indexframe")
    password = env("SMTP_PASSWORD")
    use_tls = env("SMTP_TLS", "true").lower() not in {"0", "false", "no"}
    reply_to = env("EMAIL_REPLY_TO")
    sender = from_header()

    if not (sender and host and username and password):
        print("[indexframe-echo-job] SMTP is not fully configured; printing email instead.")
        print(
            json.dumps(
                {
                    "to": to_email,
                    "from": sender,
                    "reply_to": reply_to,
                    "host": host,
                    "port": port,
                    "username": username,
                    "subject": subject,
                    "text": text,
                },
                indent=2,
            )
        )
        return

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to_email
    message["Subject"] = subject
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(text)

    if use_tls:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)


def main() -> None:
    submitted_url = env("SUBMITTED_URL")
    user_email = env("USER_EMAIL")
    submission_id = env("SUBMISSION_ID", "manual")
    if not submitted_url or not user_email:
        raise SystemExit("SUBMITTED_URL and USER_EMAIL are required")

    result = run_one_line_mock(submitted_url)
    result_url = result["result_url"]
    body = f"""Indexframe demo result

Submission: {submission_id}
URL entered: {submitted_url}

Mock processing result link:
{result_url}

This is the v1 echo service. The real Indexframe worker can later reuse the same Cloud Run Job shape and call indexframe_poc.py.
"""
    send_email(to_email=user_email, subject="Your Indexframe result", text=body)
    print(json.dumps({"ok": True, "submission_id": submission_id, "email": user_email, "result_url": result_url}))


if __name__ == "__main__":
    main()
