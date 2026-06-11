#!/usr/bin/env python3
"""
Indexframe PoC: YouTube URL -> evidence-ranked keyframes -> cover hero variants.

Design goals:
- Quick hackathon demo.
- Google Cloud only for managed services: Vertex/Gemini + optional GCS + optional YouTube Data API.
- Deterministic final typography/layout so generated covers are crisp and repeatable.
- Pluggable downloader: use your existing CLI to fetch the video stream locally.

Typical use:
  python indexframe_poc.py \
    --url 'https://www.youtube.com/watch?v=VIDEO_ID' \
    --out-dir ./runs/demo \
    --download-cmd 'your_yt_cli --url {url} --out {out}'

The downloader command should write an mp4/webm/mov file to {out}, or to the directory containing {out}.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import html
import io
import json
import math
import mimetypes
import os
import smtplib
import ssl
import random
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
import google.auth
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import dotenv
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

try:
    from google import genai
    from google.genai import types
except Exception as exc:  # pragma: no cover
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    GENAI_IMPORT_ERROR = str(exc)
else:
    GENAI_IMPORT_ERROR = ""

try:
    from google.cloud import storage
except Exception as exc:  # pragma: no cover
    storage = None  # type: ignore[assignment]
    STORAGE_IMPORT_ERROR = str(exc)
else:
    STORAGE_IMPORT_ERROR = ""

try:
    import imageio_ffmpeg
except Exception:  # pragma: no cover
    imageio_ffmpeg = None  # type: ignore[assignment]

from indexframe_result_pack_store import build_image_hero_pack, maybe_store_image_hero_pack

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_SIZE = (1280, 720)
MAX_COMMENT_CHARS = 8000
MAX_TRANSCRIPT_CHARS = 14000
MAX_DESCRIPTION_CHARS = 2500
AI_COVER_REQUEST_INTERVAL_SEC = max(0.0, float(os.getenv("INDEXFRAME_AI_COVER_INTERVAL_SEC", "25")))
AI_COVER_MAX_GENERATED = max(0, int(os.getenv("INDEXFRAME_AI_COVER_MAX_GENERATED", "0")))  # 0 means no cap
AI_COVER_MAX_ATTEMPTS = max(1, int(os.getenv("INDEXFRAME_AI_COVER_MAX_ATTEMPTS", "2")))
AI_COVER_RETRY_BASE_SEC = max(0.0, float(os.getenv("INDEXFRAME_AI_COVER_RETRY_BASE_SEC", "8")))
AI_COVER_RETRY_MAX_SEC = max(AI_COVER_RETRY_BASE_SEC, float(os.getenv("INDEXFRAME_AI_COVER_RETRY_MAX_SEC", "45")))
AI_COVER_REF_FALLBACK = os.getenv("INDEXFRAME_AI_COVER_REF_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}
AI_COVER_TEMPERATURE = float(os.getenv("INDEXFRAME_AI_COVER_TEMPERATURE", "0.85"))
AI_COVER_MIN_MEAN_ABS_DIFF = max(0.0, float(os.getenv("INDEXFRAME_AI_COVER_MIN_MEAN_ABS_DIFF", "7.5")))
AI_COVER_MIN_HASH_DISTANCE = max(0, int(os.getenv("INDEXFRAME_AI_COVER_MIN_HASH_DISTANCE", "6")))
AI_COVER_REF_JPEG_QUALITY = min(95, max(70, int(os.getenv("INDEXFRAME_AI_COVER_REF_JPEG_QUALITY", "90"))))



@dataclass
class MomentCandidate:
    ts: float
    source: str
    weight: float
    reason: str
    text: str = ""


@dataclass
class FrameCandidate:
    frame_id: str
    ts: float
    path: str
    source: str
    weight: float
    reason: str
    visual_score: float
    width: int
    height: int


@dataclass
class RunResult:
    run_dir: str
    index_html: str
    analysis_json: str
    variants_json: str
    cover_paths: List[str]
    gcs_uri: Optional[str] = None
    public_urls: Optional[List[str]] = None


# ------------------------- small utilities -------------------------


def log(message: str) -> None:
    print(f"[indexframe] {message}", flush=True)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def dump_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_text(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def email_from_header() -> str:
    from_email = env_text("EMAIL_FROM") or env_text("SMTP_FROM") or env_text("SMTP_USERNAME")
    from_name = env_text("EMAIL_FROM_NAME")
    if from_name and "<" not in from_email and ">" not in from_email:
        return formataddr((from_name, from_email))
    return from_email


def send_email(*, to_email: str, subject: str, text: str, html_body: str | None = None) -> None:
    """Send a result email using the same SMTP env vars as indexframe_echo_job.py."""
    host = env_text("SMTP_HOST", "mail.smtp2go.com")
    port = int(env_text("SMTP_PORT", "2525"))
    username = env_text("SMTP_USERNAME", "indexframe")
    password = env_text("SMTP_PASSWORD")
    use_tls = env_text("SMTP_TLS", "true").lower() not in {"0", "false", "no"}
    reply_to = env_text("EMAIL_REPLY_TO")
    sender = email_from_header()

    if not (sender and host and username and password):
        print("[indexframe] SMTP is not fully configured; printing email instead.")
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
                    "html": html_body,
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
    if html_body:
        message.add_alternative(html_body, subtype="html")

    if use_tls:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)


def build_result_email_body(
    *,
    submitted_url: str,
    submission_id: str,
    metadata: Dict[str, Any],
    summary: Dict[str, Any],
    public_url_items: Optional[List[Dict[str, str]]],
    final_result: Optional[Dict[str, Any]] = None,
) -> tuple[str, str]:
    title = str(metadata.get("title") or "YouTube video")
    safe_title = html.escape(title)
    safe_submission_id = html.escape(submission_id)
    safe_submitted_url = html.escape(submitted_url)

    index_url = signed_url_for_relative_path(public_url_items, "index.html") or str(summary.get("index_html") or "")
    analysis_url = signed_url_for_relative_path(public_url_items, "analysis.json")
    variants_url = signed_url_for_relative_path(public_url_items, "variants.json")

    final_result = final_result or {}
    best_variant = final_result.get("best_variant") or {}
    analysis = final_result.get("analysis") or {}
    rationale = str(
        best_variant.get("rationale")
        or analysis.get("dominant_packaging_problem")
        or analysis.get("video_summary")
        or ""
    ).strip()
    if len(rationale) > 520:
        rationale = rationale[:517].rstrip() + "..."

    variants_by_rel: Dict[str, Dict[str, Any]] = {}
    for variant in final_result.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        cover_name = Path(str(variant.get("cover_path") or "")).name
        if cover_name:
            variants_by_rel[f"covers/{cover_name}"] = variant

    cover_items = [
        item
        for item in public_url_items or []
        if str(item.get("relative_path") or "").startswith("covers/") and item.get("signed_url")
    ][:6]

    thumb_cells = []
    for idx, item in enumerate(cover_items, start=1):
        rel = str(item.get("relative_path") or "")
        link = str(item.get("signed_url") or "")
        variant = variants_by_rel.get(rel, {})
        headline = str(variant.get("headline") or f"Cover {idx}")
        score = variant.get("score_0_to_100")
        meta = f"Score {score}" if score else "Open cover"
        thumb_cells.append(
            f"""
            <td style="width:50%;padding:8px;vertical-align:top;">
              <a href="{html.escape(link, quote=True)}" style="text-decoration:none;color:#111827;">
                <img src="{html.escape(link, quote=True)}" width="248" style="display:block;width:100%;max-width:248px;border-radius:14px;border:1px solid #e5e7eb;aspect-ratio:16/9;object-fit:cover;" alt="{html.escape(headline, quote=True)}">
                <div style="padding:10px 2px 0;font:700 14px Arial,Helvetica,sans-serif;color:#111827;line-height:1.25;">{html.escape(headline)}</div>
                <div style="padding:4px 2px 0;font:12px Arial,Helvetica,sans-serif;color:#6b7280;">{html.escape(meta)}</div>
              </a>
            </td>
            """
        )

    thumb_rows = ""
    for i in range(0, len(thumb_cells), 2):
        row_cells = "".join(thumb_cells[i : i + 2])
        if i + 1 >= len(thumb_cells):
            row_cells += '<td style="width:50%;padding:8px;"></td>'
        thumb_rows += f"<tr>{row_cells}</tr>"

    artifact_links = []
    if analysis_url:
        artifact_links.append(f'<a href="{html.escape(analysis_url, quote=True)}" style="color:#6d28d9;text-decoration:none;">analysis.json</a>')
    if variants_url:
        artifact_links.append(f'<a href="{html.escape(variants_url, quote=True)}" style="color:#6d28d9;text-decoration:none;">variants.json</a>')

    text_lines = [
        "Indexframe result",
        "",
        f"Submission: {submission_id}",
        f"Video: {title}",
        f"Original link: {submitted_url}",
    ]
    if index_url:
        text_lines.extend(["", f"Result page: {index_url}"])
    if rationale:
        text_lines.extend(["", "Model rationale:", rationale])
    if cover_items:
        text_lines.extend(["", "Cover links:"])
        for idx, item in enumerate(cover_items, start=1):
            rel = str(item.get("relative_path") or f"Cover {idx}")
            text_lines.append(f"- {rel}: {item.get('signed_url')}")
    if summary.get("gcs_uri"):
        text_lines.extend(["", f"GCS folder: {summary['gcs_uri']}"])
    text_lines.append("\nThis email was sent by the full Indexframe pipeline.")
    text_body = "\n".join(text_lines).rstrip() + "\n"

    html_body = f"""
    <!doctype html>
    <html>
      <body style="margin:0;padding:0;background:#f4f0ea;">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f0ea;padding:28px 12px;">
          <tr>
            <td align="center">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#ffffff;border-radius:24px;overflow:hidden;border:1px solid #eadfd2;box-shadow:0 18px 50px rgba(31,22,12,.12);">
                <tr>
                  <td style="padding:34px 34px 24px;background:linear-gradient(135deg,#111827,#3b0764);">
                    <div style="font:700 13px Arial,Helvetica,sans-serif;letter-spacing:.14em;text-transform:uppercase;color:#c4b5fd;">Indexframe result</div>
                    <h1 style="margin:12px 0 0;font:800 28px Arial,Helvetica,sans-serif;line-height:1.15;color:#ffffff;">Your thumbnail pack is ready</h1>
                    <p style="margin:12px 0 0;font:15px Arial,Helvetica,sans-serif;line-height:1.55;color:#ddd6fe;">{safe_title}</p>
                  </td>
                </tr>
                <tr>
                  <td style="padding:26px 34px 8px;">
                    {"<a href=\"" + html.escape(index_url, quote=True) + "\" style=\"display:inline-block;background:#111827;color:#ffffff;text-decoration:none;border-radius:999px;padding:13px 20px;font:700 14px Arial,Helvetica,sans-serif;\">Open result page</a>" if index_url else ""}
                    <p style="margin:18px 0 0;font:13px Arial,Helvetica,sans-serif;line-height:1.6;color:#6b7280;">
                      <strong style="color:#374151;">Submission:</strong> {safe_submission_id}<br>
                      <strong style="color:#374151;">Original link:</strong> <a href="{html.escape(submitted_url, quote=True)}" style="color:#6d28d9;text-decoration:none;">{safe_submitted_url}</a>
                      {("<br><strong style=\"color:#374151;\">GCS folder:</strong> " + html.escape(str(summary.get("gcs_uri")))) if summary.get("gcs_uri") else ""}
                    </p>
                  </td>
                </tr>
                {f'''
                <tr>
                  <td style="padding:16px 34px 4px;">
                    <div style="padding:16px 18px;background:#faf7f2;border:1px solid #efe4d7;border-radius:18px;">
                      <div style="font:800 13px Arial,Helvetica,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:#92400e;">Model rationale</div>
                      <p style="margin:8px 0 0;font:15px Arial,Helvetica,sans-serif;line-height:1.6;color:#374151;">{html.escape(rationale)}</p>
                    </div>
                  </td>
                </tr>
                ''' if rationale else ""}
                {f'''
                <tr>
                  <td style="padding:20px 26px 8px;">
                    <div style="padding:0 8px 8px;font:800 16px Arial,Helvetica,sans-serif;color:#111827;">Clickable cover thumbnails</div>
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0">{thumb_rows}</table>
                  </td>
                </tr>
                ''' if thumb_rows else ""}
                {f'''
                <tr>
                  <td style="padding:12px 34px 26px;">
                    <div style="font:13px Arial,Helvetica,sans-serif;color:#6b7280;">Artifacts: {" &nbsp;·&nbsp; ".join(artifact_links)}</div>
                  </td>
                </tr>
                ''' if artifact_links else ""}
                <tr>
                  <td style="padding:20px 34px;background:#fbfaf8;border-top:1px solid #eee7dc;font:12px Arial,Helvetica,sans-serif;color:#9ca3af;">
                    Sent by the full Indexframe pipeline.
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """.strip()

    return text_body, html_body



def signed_url_for_relative_path(public_url_items: Optional[List[Dict[str, str]]], relative_path: str) -> str:
    for item in public_url_items or []:
        if item.get("relative_path") == relative_path and item.get("signed_url"):
            return str(item["signed_url"])
    return ""


def build_result_email_body2(
    *,
    submitted_url: str,
    submission_id: str,
    metadata: Dict[str, Any],
    summary: Dict[str, Any],
    public_url_items: Optional[List[Dict[str, str]]],
) -> str:
    title = str(metadata.get("title") or "YouTube video")
    lines = [
        "Indexframe result",
        "",
        f"Submission: {submission_id}",
        f"Video: {title}",
        f"URL entered: {submitted_url}",
        "",
    ]

    index_url = signed_url_for_relative_path(public_url_items, "index.html")
    analysis_url = signed_url_for_relative_path(public_url_items, "analysis.json")
    variants_url = signed_url_for_relative_path(public_url_items, "variants.json")

    if index_url:
        lines.extend(["Result page:", index_url, ""])
    elif summary.get("index_html"):
        lines.extend(["Result page:", str(summary["index_html"]), ""])

    if summary.get("gcs_uri"):
        lines.extend(["GCS folder:", str(summary["gcs_uri"]), ""])

    artifact_links = []
    if analysis_url:
        artifact_links.append(("analysis.json", analysis_url))
    if variants_url:
        artifact_links.append(("variants.json", variants_url))
    if artifact_links:
        lines.append("Artifacts:")
        for label, link in artifact_links:
            lines.append(f"- {label}: {link}")
        lines.append("")

    cover_items = [
        item
        for item in public_url_items or []
        if str(item.get("relative_path") or "").startswith("covers/") and item.get("signed_url")
    ]
    if cover_items:
        lines.append("Cover URLs:")
        for item in cover_items:
            lines.append(f"- {item.get('relative_path')}: {item.get('signed_url')}")
        lines.append("")
    elif summary.get("public_urls"):
        lines.append("Public URLs:")
        for url in summary.get("public_urls") or []:
            lines.append(f"- {url}")
        lines.append("")

    if summary.get("signed_url_errors"):
        lines.append("Some signed URLs could not be generated. See gcs_upload.json for details.")
        lines.append("")

    lines.append("This email was sent by the full Indexframe pipeline.")
    return "\n".join(lines).rstrip() + "\n"


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return value[:80] or "video"


def parse_size(value: str) -> Tuple[int, int]:
    match = re.match(r"^(\d+)x(\d+)$", value.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError("size must look like 1280x720")
    return int(match.group(1)), int(match.group(2))


def ffmpeg_exe() -> str:
    if imageio_ffmpeg is not None:
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    return os.getenv("FFMPEG", "ffmpeg")


def is_sensitive_env_name(name: str) -> bool:
    upper = name.upper()
    if upper.endswith("_FILE") or upper.endswith("_PATH") or upper in {"PATH", "PYTHONPATH"}:
        return False
    return any(token in upper for token in ["PASSWORD", "TOKEN", "SECRET", "PRIVATE", "CREDENTIAL", "API_KEY"])


def safe_env_value(name: str, value: Optional[str]) -> str:
    if value is None:
        return "<unset>"
    if value == "":
        return "<empty>"
    if is_sensitive_env_name(name):
        return f"<set:{len(value)} chars>"
    return value


def log_env_snapshot(names: Iterable[str]) -> None:
    log("diagnostics: selected env vars")
    for name in names:
        log(f"  env {name}={safe_env_value(name, os.getenv(name))}")


def path_debug(path_value: str) -> Dict[str, Any]:
    path = Path(path_value)
    data: Dict[str, Any] = {
        "path": str(path),
        "absolute": str(path if path.is_absolute() else Path.cwd() / path),
        "exists": path.exists(),
        "is_file": path.is_file(),
        "is_dir": path.is_dir(),
        "is_symlink": path.is_symlink(),
        "readable": os.access(path, os.R_OK),
    }
    try:
        resolved = path.resolve(strict=False)
        data["resolved"] = str(resolved)
    except Exception as exc:
        data["resolve_error"] = f"{exc.__class__.__name__}: {exc}"
    try:
        st = path.stat()
        data.update({
            "mode": oct(st.st_mode & 0o777),
            "uid": st.st_uid,
            "gid": st.st_gid,
            "bytes": st.st_size,
        })
    except Exception as exc:
        data["stat_error"] = f"{exc.__class__.__name__}: {exc}"
    if path.is_file():
        try:
            with path.open("rb") as fh:
                sample = fh.read(8192)
            data["first_8kb_bytes"] = len(sample)
            data["first_8kb_newlines"] = sample.count(b"\n")
        except Exception as exc:
            data["read_sample_error"] = f"{exc.__class__.__name__}: {exc}"
    return data


def log_path_snapshot(label: str, path_value: Optional[str]) -> None:
    if not path_value:
        log(f"diagnostics: {label}: <unset>")
        return
    log(f"diagnostics: {label}: " + json.dumps(path_debug(path_value), sort_keys=True))


def log_dir_listing(label: str, path_value: str, *, max_items: int = 20) -> None:
    path = Path(path_value)
    if not path.exists() or not path.is_dir():
        log(f"diagnostics: {label}: not a directory: {path_value}")
        return
    items: List[Dict[str, Any]] = []
    try:
        for child in sorted(path.iterdir(), key=lambda p: p.name)[:max_items]:
            item = path_debug(str(child))
            # Keep the directory listing compact in Cloud Run logs.
            item = {k: item.get(k) for k in ["path", "exists", "is_file", "is_dir", "is_symlink", "mode", "bytes", "readable"]}
            items.append(item)
    except Exception as exc:
        log(f"diagnostics: {label}: list failed: {exc.__class__.__name__}: {exc}")
        return
    log(f"diagnostics: {label}: " + json.dumps(items, sort_keys=True))


def log_executable_snapshot(name: str) -> None:
    resolved = shutil.which(name)
    log(f"diagnostics: executable {name}: which={resolved or '<not found>'}")
    if not resolved:
        return
    try:
        completed = subprocess.run(
            [resolved, "--version"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
        log(f"diagnostics: executable {name}: version rc={completed.returncode} output={completed.stdout.strip()[:1000]!r}")
    except Exception as exc:
        log(f"diagnostics: executable {name}: version failed: {exc.__class__.__name__}: {exc}")


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except Exception:
        return str(left) == str(right)


def _default_secret_cookie_file() -> Optional[Path]:
    candidate = Path("/secrets/youtube-cookies.txt")
    return candidate if candidate.is_file() else None


def prepare_writable_yt_dlp_cookies_file(dl_dir: Path) -> Optional[Path]:
    """Copy mounted cookies to an absolute writable download workdir.

    Cloud Run secret volumes are read-only. yt-dlp can read a Netscape cookie file from a
    secret mount, but it may also try to persist updated cookies back to the same path. Passing
    the read-only mount directly can therefore fail with Errno 30. This function keeps the
    mounted secret immutable and gives yt-dlp a per-run copy it can update safely.
    """
    source_value = (os.getenv("YT_DLP_COOKIES_FILE_ORIGINAL") or os.getenv("YT_DLP_COOKIES_FILE") or "").strip()
    if not source_value:
        default_secret = _default_secret_cookie_file()
        if default_secret is not None:
            source_value = str(default_secret)
            os.environ["YT_DLP_COOKIES_FILE"] = source_value
            log(f"diagnostics: inferred YT_DLP_COOKIES_FILE={source_value}")

    if not source_value:
        return None

    source = Path(source_value)
    log_path_snapshot("YT_DLP_COOKIES_FILE source", str(source))

    if not source.exists():
        raise FileNotFoundError(f"YT_DLP_COOKIES_FILE does not exist: {source}")
    if not source.is_file():
        raise FileNotFoundError(f"YT_DLP_COOKIES_FILE is not a regular file: {source}")
    if not os.access(source, os.R_OK):
        raise PermissionError(f"YT_DLP_COOKIES_FILE is not readable: {source}")

    # Use absolute paths because the downloader is executed with cwd=dl_dir.
    # A relative cookie path like "178.../download/cookies/youtube-cookies.txt"
    # would otherwise be resolved relative to dl_dir and fail with FileNotFoundError.
    dl_dir = ensure_dir(dl_dir).resolve(strict=False)
    cookie_dir = ensure_dir(dl_dir / "cookies").resolve(strict=False)
    writable_copy = (cookie_dir / "youtube-cookies.txt").resolve(strict=False)

    if not _same_path(source, writable_copy):
        tmp = writable_copy.with_name(writable_copy.name + ".tmp")
        shutil.copyfile(source, tmp)
        os.chmod(tmp, 0o600)
        tmp.replace(writable_copy)
        log(f"diagnostics: copied yt-dlp cookies to writable workdir path {writable_copy}")
    else:
        log(f"diagnostics: yt-dlp cookies already point at writable workdir path {writable_copy}")

    os.environ.setdefault("YT_DLP_COOKIES_FILE_ORIGINAL", str(source))
    os.environ["YT_DLP_COOKIES_FILE"] = str(writable_copy)
    log_path_snapshot("YT_DLP_COOKIES_FILE writable", str(writable_copy))
    return writable_copy


def _looks_like_cookie_file_path(value: str) -> bool:
    if not value:
        return False
    lowered = value.lower()
    return (
        value.startswith("/")
        or value.startswith(".")
        or "/" in value
        or "\\" in value
        or lowered.endswith((".txt", ".cookies", ".cookie", ".netscape"))
        or "$yt_dlp_cookies_file" in lowered
        or "${yt_dlp_cookies_file}" in lowered
    )


def _is_yt_dlp_command(cmd: List[str]) -> bool:
    if not cmd:
        return False
    executable = Path(cmd[0]).name.lower()
    if executable in {"yt-dlp", "yt-dlp.exe"} or "yt-dlp" in executable:
        return True
    return len(cmd) >= 3 and Path(cmd[0]).name.lower().startswith("python") and cmd[1:3] == ["-m", "yt_dlp"]


def _command_has_cookie_file_arg(cmd: List[str]) -> bool:
    return any(
        part in {"--cookies", "--cookies-from-browser"}
        or part.startswith("--cookies=")
        or part.startswith("--cookies-from-browser=")
        for part in cmd
    )


def apply_writable_cookie_file_to_command(cmd: List[str], cookies_file: Optional[Path]) -> List[str]:
    """Force yt-dlp cookie-file args to use the writable copy.

    Also repairs the common mistake `--cookies-from-browser /path/to/cookies.txt` by converting
    it to `--cookies /writable/copy`, while leaving real browser names such as chrome/firefox
    untouched.
    """
    if not cookies_file:
        return cmd

    cookies = str(cookies_file.resolve(strict=False))
    rewritten: List[str] = []
    changed = False
    i = 0

    while i < len(cmd):
        part = cmd[i]

        if part == "--cookies":
            rewritten.extend(["--cookies", cookies])
            i += 2 if i + 1 < len(cmd) else 1
            changed = True
            continue

        if part.startswith("--cookies="):
            rewritten.append(f"--cookies={cookies}")
            i += 1
            changed = True
            continue

        if part == "--cookies-from-browser":
            browser_or_path = cmd[i + 1] if i + 1 < len(cmd) else ""
            if _looks_like_cookie_file_path(browser_or_path):
                rewritten.extend(["--cookies", cookies])
                i += 2 if i + 1 < len(cmd) else 1
                changed = True
                log("diagnostics: replaced --cookies-from-browser <cookie-file-path> with --cookies <writable-copy>")
                continue

            rewritten.append(part)
            if i + 1 < len(cmd):
                rewritten.append(browser_or_path)
                i += 2
            else:
                i += 1
            continue

        if part.startswith("--cookies-from-browser="):
            browser_or_path = part.split("=", 1)[1]
            if _looks_like_cookie_file_path(browser_or_path):
                rewritten.append(f"--cookies={cookies}")
                changed = True
                log("diagnostics: replaced --cookies-from-browser=<cookie-file-path> with --cookies=<writable-copy>")
            else:
                rewritten.append(part)
            i += 1
            continue

        rewritten.append(part)
        i += 1

    if not _command_has_cookie_file_arg(rewritten) and _is_yt_dlp_command(rewritten):
        rewritten = [rewritten[0], "--cookies", cookies, *rewritten[1:]]
        changed = True
        log("diagnostics: added --cookies <writable-copy> to yt-dlp command")

    if changed:
        log("diagnostics: downloader argv was rewritten to use writable cookie file")
    elif cookies_file and not _command_has_cookie_file_arg(rewritten):
        log("diagnostics: WARNING writable cookies exist but command is not recognized as yt-dlp; not injecting --cookies automatically.")

    return rewritten


def log_downloader_diagnostics(*, dl_dir: Path, cmd: List[str], cmd_template: str) -> None:
    log("diagnostics: downloader preflight start")
    log_env_snapshot([
        "SUBMITTED_URL",
        "YOUTUBE_URL",
        "OUT",
        "PROJECT_ID",
        "OUTPUT_GCS_URI",
        "YT_DLP_COOKIES_FILE_ORIGINAL",
        "YT_DLP_COOKIES_FILE",
        "YT_DOWNLOAD_CMD",
        "PATH",
        "HOME",
        "PWD",
    ])
    log(f"diagnostics: cwd={Path.cwd()} dl_dir={dl_dir} dl_dir_exists={dl_dir.exists()} dl_dir_writable={os.access(dl_dir, os.W_OK)}")
    log_path_snapshot("YT_DLP_COOKIES_FILE_ORIGINAL", os.getenv("YT_DLP_COOKIES_FILE_ORIGINAL"))
    log_path_snapshot("YT_DLP_COOKIES_FILE", os.getenv("YT_DLP_COOKIES_FILE"))
    cookies_file = os.getenv("YT_DLP_COOKIES_FILE", "").strip()
    if cookies_file:
        log_dir_listing("YT_DLP_COOKIES_FILE parent", str(Path(cookies_file).parent))
    if Path("/secrets").exists():
        log_dir_listing("/secrets", "/secrets")
    else:
        log("diagnostics: /secrets does not exist")
    if cmd:
        log_executable_snapshot(cmd[0])
    log(f"diagnostics: downloader template={cmd_template}")
    log("diagnostics: downloader argv=" + json.dumps(cmd))
    if cookies_file and not _command_has_cookie_file_arg(cmd):
        log("diagnostics: WARNING YT_DLP_COOKIES_FILE is set but the downloader command does not include --cookies/--cookies-from-browser; yt-dlp will not use that env var automatically.")
    log("diagnostics: downloader preflight end")


def run_cmd(cmd: List[str], *, cwd: Optional[Path] = None, timeout: Optional[int] = None) -> str:
    log("$ " + " ".join(shlex.quote(part) for part in cmd))
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        log(f"command timed out after {timeout}s: {' '.join(shlex.quote(part) for part in cmd)}")
        if output:
            log("command output before timeout:\n" + output[-12000:])
        raise
    except Exception as exc:
        log(f"command failed before completion: {exc.__class__.__name__}: {exc}")
        raise

    log(f"command exit code: {completed.returncode}")
    if completed.stdout:
        log("command output:\n" + completed.stdout[-12000:])
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(completed.returncode, cmd, output=completed.stdout)
    return completed.stdout


def parse_youtube_id(url_or_id: str) -> str:
    raw = url_or_id.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw):
        return raw
    parsed = urllib.parse.urlparse(raw)
    host = parsed.netloc.lower()
    if "youtu.be" in host:
        video_id = parsed.path.strip("/").split("/")[0]
        if video_id:
            return video_id
    query = urllib.parse.parse_qs(parsed.query)
    if "v" in query and query["v"]:
        return query["v"][0]
    shorts = re.search(r"/shorts/([A-Za-z0-9_-]{11})", parsed.path)
    if shorts:
        return shorts.group(1)
    raise ValueError(f"Could not parse YouTube video id from: {url_or_id}")


def http_get_json(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "IndexframePoC/0.1"})
    with urllib.request.urlopen(req, timeout=20) as response:  # nosec: demo utility
        return json.loads(response.read().decode("utf-8"))


def maybe_make_genai_client(project: Optional[str], location: str) -> Any:
    if genai is None:
        raise RuntimeError(f"google-genai is not installed: {GENAI_IMPORT_ERROR}")
    kwargs: Dict[str, Any] = {"http_options": {"api_version": "v1"}}
    if project:
        kwargs.update({"vertexai": True, "project": project, "location": location or "global"})
    return genai.Client(**kwargs)


def json_from_model(
    client: Any,
    *,
    model: str,
    prompt: str,
    schema: Dict[str, Any],
    media_parts: Optional[List[Any]] = None,
    temperature: float = 0.25,
    debug_jsonl: Optional[Path] = None,
    op_name: str = "json_from_model",
    extra_debug: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    contents: List[Any] = [prompt]
    if media_parts:
        contents.extend(media_parts)

    input_components = [
        {
            "name": "prompt",
            "type": "text",
            "chars": len(prompt),
            "bytes": len(prompt.encode("utf-8")),
        }
    ]
    input_components.extend(
        {"name": f"media_part_{idx}", "type": "image_or_media"}
        for idx, _part in enumerate(media_parts or [], start=1)
    )

    started = time.time()
    record: Dict[str, Any] = {
        "time_epoch": started,
        "op": op_name,
        "model": model,
        "temperature": temperature,
        "prompt_chars": len(prompt),
        "prompt_bytes": len(prompt.encode("utf-8")),
        "media_parts_count": len(media_parts or []),
        "input_components": input_components,
        "schema_top_level_keys": sorted((schema.get("properties") or {}).keys()),
        **(extra_debug or {}),
    }
    log(
        "LLM start "
        f"op={op_name} model={model} temp={temperature} "
        f"prompt_chars={record['prompt_chars']} prompt_bytes={record['prompt_bytes']} "
        f"media_parts={record['media_parts_count']} debug_jsonl={debug_jsonl or '<none>'}"
    )
    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config={
                "temperature": temperature,
                "response_mime_type": "application/json",
                "response_schema": schema,
            },
        )
        record.update(response_debug_summary(response, started=started))
        record["status"] = "ok"
        if debug_jsonl:
            append_jsonl(debug_jsonl, record)
        log(
            "LLM done "
            f"op={op_name} status=ok latency={record.get('latency_sec')}s "
            f"response_chars={record.get('response_text_chars')} "
            f"candidates={record.get('candidate_count')} parts={record.get('part_count')}"
        )
        return json.loads(response.text)
    except Exception as exc:
        record.update({
            "status": "error",
            "latency_sec": round(time.time() - started, 3),
            "error_type": exc.__class__.__name__,
            "error": short_error(exc, 2000),
            "quota_like_error": is_ai_quota_error(exc),
        })
        if debug_jsonl:
            append_jsonl(debug_jsonl, record)
        log(
            "LLM done "
            f"op={op_name} status=error latency={record['latency_sec']}s "
            f"error={record['error_type']}: {record['error']}"
        )
        raise


def media_part(path: Path) -> Any:
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or "image/jpeg"
    return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)


# ------------------------- YouTube metadata/evidence -------------------------


def fetch_youtube_public_data(video_id: str, api_key: Optional[str], comment_limit: int = 80) -> Dict[str, Any]:
    """Fetch metadata and top-level comments through YouTube Data API when an API key is available."""
    if not api_key:
        return {"video_id": video_id, "metadata_source": "none", "comments": []}

    base = "https://www.googleapis.com/youtube/v3"
    params = urllib.parse.urlencode(
        {
            "part": "snippet,contentDetails,statistics",
            "id": video_id,
            "key": api_key,
        }
    )
    data = http_get_json(f"{base}/videos?{params}")
    items = data.get("items") or []
    payload: Dict[str, Any] = {"video_id": video_id, "metadata_source": "youtube_data_api", "comments": []}
    if items:
        item = items[0]
        snippet = item.get("snippet") or {}
        payload.update(
            {
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "channel_title": snippet.get("channelTitle", ""),
                "published_at": snippet.get("publishedAt", ""),
                "thumbnails": snippet.get("thumbnails", {}),
                "statistics": item.get("statistics") or {},
                "content_details": item.get("contentDetails") or {},
            }
        )

    comments: List[Dict[str, Any]] = []
    page_token = ""
    while len(comments) < comment_limit:
        q = {
            "part": "snippet",
            "videoId": video_id,
            "maxResults": min(100, comment_limit - len(comments)),
            "order": "relevance",
            "textFormat": "plainText",
            "key": api_key,
        }
        if page_token:
            q["pageToken"] = page_token
        try:
            cdata = http_get_json(f"{base}/commentThreads?{urllib.parse.urlencode(q)}")
        except Exception as exc:
            payload["comment_error"] = str(exc)
            break
        for item in cdata.get("items") or []:
            sn = (((item.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {})
            text = sn.get("textDisplay") or sn.get("textOriginal") or ""
            comments.append(
                {
                    "text": text,
                    "like_count": sn.get("likeCount", 0),
                    "published_at": sn.get("publishedAt", ""),
                    "author": sn.get("authorDisplayName", ""),
                }
            )
        page_token = cdata.get("nextPageToken") or ""
        if not page_token:
            break
    payload["comments"] = comments
    return payload


def merge_info_json(youtube_data: Dict[str, Any], info_json_path: Optional[Path]) -> Dict[str, Any]:
    if not info_json_path or not info_json_path.exists():
        return youtube_data
    info = read_json(info_json_path)
    merged = dict(youtube_data)
    # yt-dlp-style keys; keep API data if it exists, otherwise fill from local metadata.
    for src_key, dst_key in [
        ("title", "title"),
        ("description", "description"),
        ("channel", "channel_title"),
        ("uploader", "channel_title"),
        ("duration", "duration_sec"),
        ("thumbnail", "existing_thumbnail_url"),
        ("chapters", "chapters"),
        ("heatmap", "heatmap"),
    ]:
        value = info.get(src_key)
        if value and not merged.get(dst_key):
            merged[dst_key] = value
    merged["local_info_json"] = str(info_json_path)
    return merged


def extract_timestamp_seconds(text: str) -> List[float]:
    values: List[float] = []
    # Supports 1:23, 01:23, 1:02:33.
    for match in re.finditer(r"(?<!\d)(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?!\d)", text or ""):
        hour = int(match.group(1) or 0)
        minute = int(match.group(2))
        sec = int(match.group(3))
        values.append(float(hour * 3600 + minute * 60 + sec))
    return values


def parse_srt_or_vtt(path: Path) -> List[Dict[str, Any]]:
    if not path or not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    entries: List[Dict[str, Any]] = []

    def ts_to_sec(value: str) -> float:
        value = value.replace(",", ".")
        parts = value.split(":")
        if len(parts) == 3:
            h, m, s = parts
        else:
            h, m, s = "0", parts[0], parts[1]
        return int(h) * 3600 + int(m) * 60 + float(s)

    pattern = re.compile(
        r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{3}|\d{1,2}:\d{2}[,.]\d{3})\s*-->\s*"
        r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{3}|\d{1,2}:\d{2}[,.]\d{3})(?P<body>.*?)(?=\n\s*\n|\Z)",
        re.S,
    )
    for match in pattern.finditer(text):
        body = match.group("body")
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\{[^}]+\}", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
        if body:
            entries.append({"start": ts_to_sec(match.group("start")), "end": ts_to_sec(match.group("end")), "text": body})
    return entries


def compact_transcript(entries: List[Dict[str, Any]], max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    chunks: List[str] = []
    total = 0
    stride = max(1, len(entries) // 140)
    for entry in entries[::stride]:
        line = f"[{int(entry['start'])}s] {entry['text']}"
        if total + len(line) > max_chars:
            break
        chunks.append(line)
        total += len(line) + 1
    return "\n".join(chunks)


def compact_comments(comments: List[Dict[str, Any]], max_chars: int = MAX_COMMENT_CHARS) -> str:
    # Prefer liked comments; relevance order from YouTube is already useful.
    comments = sorted(comments, key=lambda c: int(c.get("like_count") or 0), reverse=True)
    out: List[str] = []
    total = 0
    for c in comments[:100]:
        line = re.sub(r"\s+", " ", str(c.get("text") or "")).strip()
        if not line:
            continue
        line = f"({c.get('like_count', 0)} likes) {line}"
        if total + len(line) > max_chars:
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


# ------------------------- local video acquisition -------------------------


def find_latest_file(root: Path, exts: Iterable[str]) -> Optional[Path]:
    candidates = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in set(exts)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def download_or_resolve_video(url: str, out_dir: Path, video_path: Optional[Path], download_cmd: Optional[str]) -> Tuple[Path, Optional[Path], List[Path]]:
    if video_path:
        if not video_path.exists():
            raise FileNotFoundError(video_path)
        return video_path, None, []

    # Keep downloader paths absolute. run_cmd executes yt-dlp with cwd=dl_dir, so any
    # relative path inserted into -o/--cookies can be interpreted relative to dl_dir.
    dl_dir = ensure_dir(out_dir / "download").resolve(strict=False)
    target = (dl_dir / "source.mp4").resolve(strict=False)
    writable_cookies_file = prepare_writable_yt_dlp_cookies_file(dl_dir)

    cmd_template = download_cmd or os.getenv("YT_DOWNLOAD_CMD", "")

    if not cmd_template:
        # Fallback for local dev. If cookies are configured, pass the writable copy rather
        # than the read-only Cloud Run secret mount.
        cookie_args = "--cookies {cookies} " if writable_cookies_file else ""
        cmd_template = (
            f"yt-dlp {cookie_args}-f bv*+ba/best --merge-output-format mp4 --remote-components ejs:npm "
            "--write-info-json --write-auto-subs --sub-lang en --convert-subs srt "
            "-o {out_base}.%(ext)s {url}"
        )

    values = {
        "url": url,
        "out": str(target),
        "out_dir": str(dl_dir),
        "out_base": str(dl_dir / "source"),
        "cookies": str(writable_cookies_file or ""),
        "cookies_file": str(writable_cookies_file or ""),
    }
    cmd_str = cmd_template.format(**values)
    cmd = shlex.split(cmd_str)
    cmd = apply_writable_cookie_file_to_command(cmd, writable_cookies_file)
    log_downloader_diagnostics(dl_dir=dl_dir, cmd=cmd, cmd_template=cmd_template)
    run_cmd(cmd, cwd=dl_dir, timeout=900)

    log_dir_listing("download directory after downloader", str(dl_dir))
    video = target if target.exists() else find_latest_file(dl_dir, VIDEO_EXTS)
    if not video:
        raise RuntimeError(f"Downloader finished but no video file found in {dl_dir}")

    info_json = find_latest_file(dl_dir, {".json"})
    subtitle_files = [p for p in sorted(dl_dir.rglob("*")) if p.suffix.lower() in {".srt", ".vtt"}]
    return video, info_json, subtitle_files


def probe_duration(video_path: Path) -> float:
    cmd = [
        ffmpeg_exe(),
        "-hide_banner",
        "-i",
        str(video_path),
    ]
    try:
        output = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30).stdout
    except Exception:
        return 0.0
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not match:
        return 0.0
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


# ------------------------- moment + frame selection -------------------------


def heatmap_candidates(heatmap: Any, duration: float) -> List[MomentCandidate]:
    """Accept loose shapes from yt-dlp/custom scraper/YouTube UI reverse-engineering exports."""
    points: List[Tuple[float, float]] = []
    if isinstance(heatmap, dict):
        for key in ["markers", "heatMarkers", "points", "data", "segments"]:
            if isinstance(heatmap.get(key), list):
                heatmap = heatmap[key]
                break
    if not isinstance(heatmap, list):
        return []

    for item in heatmap:
        if not isinstance(item, dict):
            continue
        value = item.get("value") or item.get("score") or item.get("heat") or item.get("intensity") or item.get("heatMarkerIntensityScoreNormalized")
        start = item.get("start") or item.get("startTime") or item.get("start_time") or item.get("time")
        # Some heatmaps are ratios.
        if start is None and item.get("ratio") is not None:
            start = float(item["ratio"]) * duration
        if value is None:
            value = item.get("y")
        if start is None or value is None:
            continue
        try:
            points.append((float(start), float(value)))
        except Exception:
            continue
    if not points:
        return []
    points.sort(key=lambda pair: pair[1], reverse=True)
    top = points[:5]
    max_value = max(v for _, v in top) or 1.0
    return [
        MomentCandidate(ts=t, source="heatmap", weight=1.2 + 1.3 * (v / max_value), reason="most-replayed/heatmap peak")
        for t, v in top
        if 0 <= t <= duration
    ]


def build_moment_candidates(
    metadata: Dict[str, Any],
    transcript_entries: List[Dict[str, Any]],
    duration: float,
    extra_heatmap_path: Optional[Path],
) -> List[MomentCandidate]:
    candidates: List[MomentCandidate] = []

    # Baseline early hook and broad coverage.
    for ts, reason, weight in [
        (1.0, "first impression", 0.6),
        (3.0, "opening hook", 0.8),
        (10.0, "early substance", 0.6),
        (duration * 0.33, "middle coverage", 0.35),
        (duration * 0.66, "late coverage", 0.30),
    ]:
        if 0 <= ts <= duration:
            candidates.append(MomentCandidate(ts=ts, source="fallback", weight=weight, reason=reason))

    # Comment timestamps usually mark moments viewers cared enough to mention.
    for comment in metadata.get("comments") or []:
        text = str(comment.get("text") or "")
        like_bonus = min(1.0, math.log1p(float(comment.get("like_count") or 0)) / 5.0)
        for ts in extract_timestamp_seconds(text):
            if 0 <= ts <= duration:
                candidates.append(
                    MomentCandidate(
                        ts=ts,
                        source="comment_timestamp",
                        weight=1.2 + like_bonus,
                        reason="timestamp mentioned in comments",
                        text=text[:180],
                    )
                )

    # Chapters are often human-curated semantic moments.
    for chapter in metadata.get("chapters") or []:
        ts = chapter.get("start_time") or chapter.get("start") or 0
        title = str(chapter.get("title") or "chapter")
        try:
            tsf = float(ts)
        except Exception:
            continue
        if 0 <= tsf <= duration:
            candidates.append(MomentCandidate(ts=tsf, source="chapter", weight=0.9, reason=f"chapter: {title}", text=title))

    # Transcript lines with concrete numbers / superlatives / emotional markers.
    signal_re = re.compile(r"\b(\d+[%$kKmM]?|secret|mistake|wrong|never|best|worst|why|how|failed|built|launch|revenue|users|watch|look)\b", re.I)
    for entry in transcript_entries:
        txt = entry.get("text", "")
        if signal_re.search(txt):
            candidates.append(
                MomentCandidate(
                    ts=float(entry.get("start") or 0),
                    source="transcript_signal",
                    weight=0.75,
                    reason="semantic transcript signal",
                    text=txt[:180],
                )
            )

    if metadata.get("heatmap"):
        candidates.extend(heatmap_candidates(metadata.get("heatmap"), duration))
    if extra_heatmap_path and extra_heatmap_path.exists():
        try:
            candidates.extend(heatmap_candidates(read_json(extra_heatmap_path), duration))
        except Exception as exc:
            log(f"Could not parse heatmap json: {exc}")

    return dedupe_moments(candidates, duration, min_gap_sec=4.0, limit=18)


def dedupe_moments(candidates: List[MomentCandidate], duration: float, min_gap_sec: float, limit: int) -> List[MomentCandidate]:
    valid = [c for c in candidates if 0 <= c.ts <= max(0.0, duration - 0.5)]
    valid.sort(key=lambda c: c.weight, reverse=True)
    chosen: List[MomentCandidate] = []
    for cand in valid:
        if all(abs(cand.ts - other.ts) >= min_gap_sec for other in chosen):
            # Avoid exact scene-cut frame: pull 0.4s after timestamp.
            cand.ts = min(max(cand.ts + 0.4, 0.0), max(0.0, duration - 0.1))
            chosen.append(cand)
        if len(chosen) >= limit:
            break
    chosen.sort(key=lambda c: c.ts)
    return chosen


def extract_frame(video_path: Path, out_path: Path, ts: float, width: int = 1280) -> None:
    ensure_dir(out_path.parent)
    vf = f"scale='min({width},iw)':-2"
    cmd = [
        ffmpeg_exe(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, ts):.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        vf,
        "-q:v",
        "2",
        "-y",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)


def visual_quality_score(path: Path) -> float:
    img = Image.open(path)
    try:
        img = ImageOps.exif_transpose(img).convert("RGB")
        thumb = img.resize((256, max(1, int(256 * img.height / img.width))), Image.Resampling.LANCZOS)
        gray = thumb.convert("L")
        stat = ImageStat.Stat(gray)
        mean = stat.mean[0]
        std = stat.stddev[0]
        # Edge energy as cheap sharpness proxy.
        edges = gray.filter(ImageFilter.FIND_EDGES)
        edge_mean = ImageStat.Stat(edges).mean[0]
        brightness = max(0, 1.0 - abs(mean - 132) / 132)
        contrast = min(1.0, std / 64)
        sharp = min(1.0, edge_mean / 38)
        score = 100 * (0.34 * brightness + 0.34 * contrast + 0.32 * sharp)
        return round(float(score), 2)
    finally:
        img.close()


def extract_candidate_frames(video_path: Path, moments: List[MomentCandidate], out_dir: Path) -> List[FrameCandidate]:
    frame_dir = ensure_dir(out_dir / "frames")
    frames: List[FrameCandidate] = []
    for idx, moment in enumerate(moments, start=1):
        frame_id = f"f{idx:02d}"
        path = frame_dir / f"{frame_id}_{int(moment.ts):05d}s.jpg"
        try:
            extract_frame(video_path, path, moment.ts)
            with Image.open(path) as img:
                w, h = img.size
            frames.append(
                FrameCandidate(
                    frame_id=frame_id,
                    ts=moment.ts,
                    path=str(path),
                    source=moment.source,
                    weight=moment.weight,
                    reason=moment.reason,
                    visual_score=visual_quality_score(path),
                    width=w,
                    height=h,
                )
            )
        except Exception as exc:
            log(f"Frame extraction failed at {moment.ts:.1f}s: {exc}")
    return frames


def make_contact_sheet(frames: List[FrameCandidate], out_path: Path, cols: int = 3, cell_w: int = 420) -> Path:
    if not frames:
        raise ValueError("No frames")
    cell_h = int(cell_w * 9 / 16) + 56
    rows = math.ceil(len(frames) / cols)
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), (18, 18, 22))
    draw = ImageDraw.Draw(sheet)
    font = load_font(22)
    small = load_font(16)
    for idx, frame in enumerate(frames):
        x = (idx % cols) * cell_w
        y = (idx // cols) * cell_h
        img = Image.open(frame.path).convert("RGB")
        try:
            img.thumbnail((cell_w, cell_h - 56), Image.Resampling.LANCZOS)
            px = x + (cell_w - img.width) // 2
            py = y
            sheet.paste(img, (px, py))
        finally:
            img.close()
        label = f"{frame.frame_id} | {frame.ts:.1f}s | {frame.source} | q{frame.visual_score:.0f}"
        draw.text((x + 12, y + cell_h - 48), label, font=font, fill=(255, 255, 255))
        draw.text((x + 12, y + cell_h - 23), frame.reason[:48], font=small, fill=(200, 200, 205))
    ensure_dir(out_path.parent)
    sheet.save(out_path, quality=90)
    return out_path


# ------------------------- Gemini creative direction -------------------------


ANALYSIS_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "video_summary": {"type": "STRING"},
        "audience_signals": {"type": "ARRAY", "items": {"type": "STRING"}},
        "dominant_packaging_problem": {"type": "STRING"},
        "frame_rankings": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "frame_id": {"type": "STRING"},
                    "score_0_to_100": {"type": "INTEGER"},
                    "why": {"type": "STRING"},
                    "best_angle": {"type": "STRING"},
                },
                "required": ["frame_id", "score_0_to_100", "why", "best_angle"],
            },
        },
        "cover_variants": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "variant_id": {"type": "STRING"},
                    "frame_id": {"type": "STRING"},
                    "angle": {"type": "STRING"},
                    "headline": {"type": "STRING"},
                    "subheadline": {"type": "STRING"},
                    "layout": {"type": "STRING", "enum": ["left_text", "right_text", "bottom_bar", "big_number", "split_claim"]},
                    "score_0_to_100": {"type": "INTEGER"},
                    "rationale": {"type": "STRING"},
                    "risk": {"type": "STRING"},
                },
                "required": ["variant_id", "frame_id", "angle", "headline", "subheadline", "layout", "score_0_to_100", "rationale", "risk"],
            },
        },
    },
    "required": ["video_summary", "audience_signals", "dominant_packaging_problem", "frame_rankings", "cover_variants"],
}


def analyze_with_gemini(
    client: Any,
    *,
    model: str,
    metadata: Dict[str, Any],
    transcript_text: str,
    comment_text: str,
    frames: List[FrameCandidate],
    contact_sheet: Path,
    variants: int,
    debug_jsonl: Optional[Path] = None,
) -> Dict[str, Any]:
    frame_table = "\n".join(
        f"- {f.frame_id}: {f.ts:.1f}s, source={f.source}, reason={f.reason}, local_visual_score={f.visual_score:.0f}, path={Path(f.path).name}"
        for f in frames
    )
    prompt = textwrap.dedent(
        f"""
        You are Indexframe, an elite YouTube thumbnail director and title editor.

        Goal: use ALL available evidence from the URL ingestion — metadata, description, comments, transcript,
        and the stop-frame contact sheet — to choose source moments and design {variants} expressive cover candidates.

        What must change from boring/background variants:
        - Think in thumbnail concepts, not decorative backgrounds. Every candidate needs a clear visual idea.
        - The stop-frames are evidence and raw material, not a locked background. Prefer frames with faces, conflict,
          transformation, stakes, objects, errors, reveals, scale, or a visually obvious question.
        - Generate a catchy visible title for each cover. It should be the main overlay text rendered by code.
        - Use no extra visible copy unless it materially improves the click promise; keep subheadline empty when in doubt.
        - Avoid generic phrases like "watch this", "you won't believe", "amazing story", or vague hype.
        - Prefer concrete hooks from the video: numbers, before/after, mistake, contradiction, result, audience pain,
          challenge, cliffhanger, or emotionally specific reaction.

        Output rules:
        - Return only JSON matching the schema. No markdown, no commentary.
        - Choose frame_id only from the contact sheet.
        - headline is the catchy cover title: 2 to 5 words, punchy, readable at mobile size.
        - subheadline is optional: 0 to 4 words. Empty string is allowed and often preferred.
        - Make all {variants} variants conceptually and visually distinct.
        - layout must be one of: left_text, right_text, bottom_bar, big_number, split_claim.
        - Use comments as audience evidence, not as truth. Do not invent unsupported claims.

        Available context summary:
        metadata_title_chars={len(str(metadata.get('title', '') or ''))}
        description_chars={len(str(metadata.get('description', '') or ''))}
        transcript_chars={len(transcript_text or '')}
        comment_chars={len(comment_text or '')}
        stop_frames={len(frames)}

        YouTube metadata:
        title: {metadata.get('title', '')}
        channel: {metadata.get('channel_title', '')}
        description: {str(metadata.get('description', ''))[:MAX_DESCRIPTION_CHARS]}
        stats: {json.dumps(metadata.get('statistics') or {}, ensure_ascii=False)}

        Candidate stop-frames:
        {frame_table}

        Transcript snippets, timestamped:
        {transcript_text or 'No transcript available.'}

        Top/relevant comments:
        {comment_text or 'No comments available.'}
        """
    ).strip()
    return json_from_model(
        client,
        model=model,
        prompt=prompt,
        schema=ANALYSIS_SCHEMA,
        media_parts=[media_part(contact_sheet)],
        temperature=0.45,
        debug_jsonl=debug_jsonl,
        op_name="packaging_analysis",
        extra_debug={
            "content_components": {
                "metadata_title_chars": len(str(metadata.get("title", "") or "")),
                "description_chars": len(str(metadata.get("description", "") or "")),
                "transcript_chars": len(transcript_text or ""),
                "comment_chars": len(comment_text or ""),
                "stop_frame_count": len(frames),
                "contact_sheet": image_file_debug(contact_sheet),
            },
            "requested_variants": variants,
        },
    )


def fallback_analysis(metadata: Dict[str, Any], frames: List[FrameCandidate], variants: int) -> Dict[str, Any]:
    title = str(metadata.get("title") or "Untitled video")
    words = [w for w in re.findall(r"[A-Za-z0-9$%]+", title) if len(w) > 2]
    core = " ".join(words[:4]) or "Watch This"
    ranked = sorted(frames, key=lambda f: f.visual_score + f.weight * 20, reverse=True)
    layouts = ["left_text", "right_text", "bottom_bar", "big_number", "split_claim"]
    angles = ["curiosity", "outcome", "mistake", "how-to", "contrarian", "comment bait"]
    cover_variants = []
    for i in range(variants):
        f = ranked[i % max(1, len(ranked))]
        headline = core[:36]
        if i == 1:
            headline = "The Real Result"
        elif i == 2:
            headline = "Biggest Mistake"
        elif i == 3:
            headline = "How It Works"
        elif i == 4:
            headline = "Nobody Shows This"
        cover_variants.append(
            {
                "variant_id": f"v{i + 1:02d}",
                "frame_id": f.frame_id,
                "angle": angles[i % len(angles)],
                "headline": headline,
                "subheadline": "",
                "layout": layouts[i % len(layouts)],
                "score_0_to_100": int(min(95, 62 + f.visual_score / 3)),
                "rationale": "Fallback creative variant generated from title and visual frame score.",
                "risk": "No Gemini analysis was available.",
            }
        )
    return {
        "video_summary": title,
        "audience_signals": [],
        "dominant_packaging_problem": "No model analysis; using deterministic fallback.",
        "frame_rankings": [
            {"frame_id": f.frame_id, "score_0_to_100": int(f.visual_score), "why": f.reason, "best_angle": f.source}
            for f in ranked
        ],
        "cover_variants": cover_variants,
    }


# ------------------------- deterministic cover renderer -------------------------


def load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        os.getenv("INDEXFRAME_FONT", ""),
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


def crop_cover(img: Image.Image, size: Tuple[int, int]) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("RGB")
    target_w, target_h = size
    src_w, src_h = img.size
    src_ratio = src_w / src_h
    target_ratio = target_w / target_h
    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        left = max(0, (src_w - new_w) // 2)
        img = img.crop((left, 0, left + new_w, src_h))
    else:
        new_h = int(src_w / target_ratio)
        top = max(0, (src_h - new_h) // 2)
        img = img.crop((0, top, src_w, top + new_h))
    return img.resize(size, Image.Resampling.LANCZOS)


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int, max_lines: int) -> List[str]:
    words = text.strip().split()
    if not words:
        return []
    lines: List[str] = []
    current: List[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        bbox = draw.textbbox((0, 0), candidate, font=font, stroke_width=3)
        width = bbox[2] - bbox[0]
        if current and width > max_width and len(lines) + 1 < max_lines:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines[:max_lines]


def draw_text_block(
    img: Image.Image,
    *,
    headline: str,
    subheadline: str,
    layout: str,
    angle: str,
    score: int,
) -> Image.Image:
    w, h = img.size
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    # Directional scrim makes text readable while preserving the frame.
    if layout in {"left_text", "big_number", "split_claim"}:
        od.rectangle((0, 0, int(w * 0.58), h), fill=(0, 0, 0, 142))
        text_x, text_y, box_w = int(w * 0.055), int(h * 0.18), int(w * 0.48)
    elif layout == "right_text":
        od.rectangle((int(w * 0.42), 0, w, h), fill=(0, 0, 0, 142))
        text_x, text_y, box_w = int(w * 0.49), int(h * 0.18), int(w * 0.45)
    else:
        od.rectangle((0, int(h * 0.58), w, h), fill=(0, 0, 0, 172))
        text_x, text_y, box_w = int(w * 0.06), int(h * 0.62), int(w * 0.88)
    img = Image.alpha_composite(img.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(img)

    headline = re.sub(r"\s+", " ", headline.strip()).upper()[:58]
    subheadline = re.sub(r"\s+", " ", subheadline.strip())[:72]
    angle = re.sub(r"\s+", " ", angle.strip()).upper()[:28]

    # Make headline as large as possible within box.
    font_size = 88
    while font_size >= 42:
        font = load_font(font_size)
        lines = wrap_text(draw, headline, font, box_w, 3)
        line_h = max(font_size + 4, 48)
        if len(lines) * line_h <= int(h * 0.38):
            widest = max((draw.textbbox((0, 0), line, font=font, stroke_width=3)[2] for line in lines), default=0)
            if widest <= box_w:
                break
        font_size -= 4
    font = load_font(font_size)
    subfont = load_font(max(26, int(font_size * 0.34)))
    tagfont = load_font(24)

    # Tag pill.
    #tag = f"{angle}  ·  {score}"
    #tb = draw.textbbox((0, 0), tag, font=tagfont)
    #pill = (text_x, max(24, text_y - 58), text_x + tb[2] - tb[0] + 28, max(24, text_y - 58) + 38)
    #draw.rounded_rectangle(pill, radius=18, fill=(255, 255, 255, 232))
    #draw.text((pill[0] + 14, pill[1] + 6), tag, font=tagfont, fill=(10, 10, 12))

    y = text_y
    for line in wrap_text(draw, headline, font, box_w, 3):
        draw.text((text_x, y), line, font=font, fill=(255, 255, 255), stroke_width=4, stroke_fill=(0, 0, 0))
        y += int(font_size * 1.02)
    if subheadline:
        y += 16
        for line in wrap_text(draw, subheadline, subfont, box_w, 2):
            draw.text((text_x, y), line, font=subfont, fill=(245, 245, 245), stroke_width=2, stroke_fill=(0, 0, 0))
            y += int(font_size * 0.42)

    # Safe-zone/crop hint for demo credibility.
    mark_font = load_font(18)
    draw.text((w - 210, h - 34), "Indexframe PoC", font=mark_font, fill=(255, 255, 255, 180))
    return img.convert("RGB")


def render_cover_variants(
    analysis: Dict[str, Any],
    frames: List[FrameCandidate],
    out_dir: Path,
    size: Tuple[int, int],
) -> List[Dict[str, Any]]:
    frame_by_id = {f.frame_id: f for f in frames}
    cover_dir = ensure_dir(out_dir / "covers")
    rendered: List[Dict[str, Any]] = []
    for idx, variant in enumerate(analysis.get("cover_variants") or [], start=1):
        frame = frame_by_id.get(str(variant.get("frame_id"))) or frames[(idx - 1) % len(frames)]
        bg = Image.open(frame.path)
        try:
            cover = crop_cover(bg, size)
        finally:
            bg.close()
        # Mild stylization: blur a copy under original for nicer compression.
        cover = Image.blend(cover.filter(ImageFilter.GaussianBlur(1.2)), cover, 0.78)
        cover = draw_text_block(
            cover,
            headline=str(variant.get("headline") or "Watch This"),
            subheadline=str(variant.get("subheadline") or ""),
            layout=str(variant.get("layout") or "left_text"),
            angle=str(variant.get("angle") or "curiosity"),
            score=int(variant.get("score_0_to_100") or 70),
        )
        out_path = cover_dir / f"cover_{idx:02d}_{slugify(str(variant.get('angle') or 'variant'))}.jpg"
        cover.save(out_path, quality=92, optimize=True)
        item = dict(variant)
        item.update({"cover_path": str(out_path), "timestamp_sec": frame.ts, "source_frame_path": frame.path})
        rendered.append(item)
    return rendered



# ------------------------- optional AI cover generator -------------------------


AI_COVER_PLAN_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "creative_direction": {"type": "STRING"},
        "material_notes": {"type": "ARRAY", "items": {"type": "STRING"}},
        "ai_cover_variants": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "variant_id": {"type": "STRING"},
                    "source_frame_id": {"type": "STRING"},
                    "angle": {"type": "STRING"},
                    "headline": {"type": "STRING"},
                    "subheadline": {"type": "STRING"},
                    "text_labels": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "layout": {"type": "STRING", "enum": ["left_text", "right_text", "bottom_bar", "big_number", "split_claim"]},
                    "visual_prompt": {"type": "STRING"},
                    "composition_notes": {"type": "STRING"},
                    "score_0_to_100": {"type": "INTEGER"},
                    "rationale": {"type": "STRING"},
                    "risk": {"type": "STRING"},
                },
                "required": [
                    "variant_id",
                    "source_frame_id",
                    "angle",
                    "headline",
                    "subheadline",
                    "text_labels",
                    "layout",
                    "visual_prompt",
                    "composition_notes",
                    "score_0_to_100",
                    "rationale",
                    "risk",
                ],
            },
        },
    },
    "required": ["creative_direction", "material_notes", "ai_cover_variants"],
}


def aspect_ratio_for_size(size: Tuple[int, int]) -> str:
    width, height = size
    if width == height:
        return "1:1"
    if height > width:
        return "9:16"
    return "16:9"


def response_parts(response: Any) -> List[Any]:
    parts = list(getattr(response, "parts", []) or [])
    if parts:
        return parts
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        parts.extend(list(getattr(content, "parts", []) or []))
    return parts


def inline_part_to_image(part: Any) -> Optional[Image.Image]:
    if not getattr(part, "inline_data", None):
        return None
    try:
        return part.as_image().convert("RGB")
    except Exception:
        data = inline_data_bytes(part.inline_data)
        if not data:
            return None
        return Image.open(io.BytesIO(data)).convert("RGB")


def to_plain_json(value: Any, *, max_string: int = 2000) -> Any:
    """Best-effort conversion of SDK/Pydantic objects to safe JSON debug data."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= max_string else value[: max_string - 3] + "..."
    if isinstance(value, (bytes, bytearray)):
        return {"bytes_len": len(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_plain_json(v, max_string=max_string) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_plain_json(v, max_string=max_string) for v in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return to_plain_json(model_dump(exclude_none=True), max_string=max_string)
        except Exception:
            pass
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return to_plain_json(to_dict(), max_string=max_string)
        except Exception:
            pass
    if dataclasses.is_dataclass(value):
        try:
            return to_plain_json(dataclasses.asdict(value), max_string=max_string)
        except Exception:
            pass
    return repr(value)[:max_string]


def append_jsonl(path: Path, item: Dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(to_plain_json(item), ensure_ascii=False) + "\n")


def image_file_debug(path: Path) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
    }
    if not path.exists():
        return data
    data.update({
        "bytes": path.stat().st_size,
        "mime_type": mimetypes.guess_type(str(path))[0] or "application/octet-stream",
        "sha256_12": hashlib.sha256(path.read_bytes()).hexdigest()[:12],
    })
    try:
        with Image.open(path) as img:
            data.update({
                "width": img.width,
                "height": img.height,
                "aspect_ratio": round(img.width / max(1, img.height), 4),
                "mode": img.mode,
            })
    except Exception as exc:
        data["image_error"] = short_error(exc)
    return data


def inline_data_bytes(inline_data: Any) -> bytes:
    data = getattr(inline_data, "data", None)
    if data is None:
        return b""
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, str):
        # Some SDK versions expose base64 text here; others expose raw bytes.
        try:
            import base64

            return base64.b64decode(data, validate=False)
        except Exception:
            return data.encode("utf-8", errors="ignore")
    return b""


def response_part_summary(part: Any) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"type": part.__class__.__name__}
    text = getattr(part, "text", None)
    if text:
        text_str = str(text)
        summary.update({"has_text": True, "text_chars": len(text_str), "text_preview": text_str[:320]})
    inline_data = getattr(part, "inline_data", None)
    if inline_data:
        data = inline_data_bytes(inline_data)
        summary.update({
            "has_inline_data": True,
            "inline_mime_type": getattr(inline_data, "mime_type", None),
            "inline_bytes": len(data),
        })
        if data:
            try:
                with Image.open(io.BytesIO(data)) as img:
                    summary.update({
                        "inline_image_width": img.width,
                        "inline_image_height": img.height,
                        "inline_image_aspect_ratio": round(img.width / max(1, img.height), 4),
                    })
            except Exception as exc:
                summary["inline_image_decode_error"] = short_error(exc)
    return summary


def response_usage_dict(response: Any) -> Dict[str, Any]:
    usage = getattr(response, "usage_metadata", None) or getattr(response, "usageMetadata", None)
    if usage is None:
        return {}
    return to_plain_json(usage) if isinstance(to_plain_json(usage), dict) else {"raw": to_plain_json(usage)}


def response_debug_summary(response: Any, *, started: float) -> Dict[str, Any]:
    parts = response_parts(response)
    part_summaries = [response_part_summary(part) for part in parts]
    text = getattr(response, "text", None) or ""
    inline_bytes = sum(int(part.get("inline_bytes") or 0) for part in part_summaries)
    inline_image_count = sum(1 for part in part_summaries if part.get("inline_image_width"))
    candidates = list(getattr(response, "candidates", []) or [])
    finish_reasons = []
    safety_ratings = []
    for candidate in candidates:
        finish_reasons.append(to_plain_json(getattr(candidate, "finish_reason", None) or getattr(candidate, "finishReason", None)))
        safety_ratings.append(to_plain_json(getattr(candidate, "safety_ratings", None) or getattr(candidate, "safetyRatings", None)))
    return {
        "latency_sec": round(time.time() - started, 3),
        "usage_metadata": response_usage_dict(response),
        "response_text_chars": len(str(text)),
        "response_text_preview": str(text)[:600],
        "candidate_count": len(candidates),
        "finish_reasons": finish_reasons,
        "safety_ratings": safety_ratings,
        "part_count": len(parts),
        "parts": part_summaries,
        "inline_image_count": inline_image_count,
        "inline_image_bytes": inline_bytes,
        "response_class": response.__class__.__name__,
    }


def extract_response_images(response: Any) -> List[Image.Image]:
    images: List[Image.Image] = []
    for part in response_parts(response):
        image = inline_part_to_image(part)
        if image is not None:
            images.append(image)
    for generated in getattr(response, "generated_images", []) or []:
        image_obj = getattr(generated, "image", None)
        if isinstance(image_obj, Image.Image):
            images.append(image_obj.convert("RGB"))
        elif image_obj is not None:
            try:
                pil_image = getattr(image_obj, "_pil_image", None) or getattr(image_obj, "image", None)
                if isinstance(pil_image, Image.Image):
                    images.append(pil_image.convert("RGB"))
            except Exception:
                pass
    return images


def average_hash_bits(img: Image.Image, hash_size: int = 8) -> Tuple[int, ...]:
    small = img.convert("L").resize((hash_size, hash_size), Image.Resampling.LANCZOS)
    pixels = list(small.getdata())
    avg = sum(pixels) / max(1, len(pixels))
    return tuple(1 if px >= avg else 0 for px in pixels)


def hamming_distance(a: Tuple[int, ...], b: Tuple[int, ...]) -> int:
    return sum(1 for left, right in zip(a, b) if left != right)


def image_similarity_metrics(reference_path: Path, generated: Image.Image, size: Tuple[int, int]) -> Dict[str, Any]:
    with Image.open(reference_path) as ref_img:
        ref = crop_cover(ImageOps.exif_transpose(ref_img).convert("RGB"), size)
    gen = crop_cover(generated.convert("RGB"), size)
    ref_small = ref.resize((256, max(1, int(256 * size[1] / max(1, size[0])))), Image.Resampling.LANCZOS)
    gen_small = gen.resize(ref_small.size, Image.Resampling.LANCZOS)
    diff = ImageChops.difference(ref_small, gen_small)
    stat = ImageStat.Stat(diff)
    mean_abs_diff = round(float(sum(stat.mean) / max(1, len(stat.mean))), 3)
    rms_diff = round(float(math.sqrt(sum(v * v for v in stat.rms) / max(1, len(stat.rms)))), 3)
    hash_distance = hamming_distance(average_hash_bits(ref_small), average_hash_bits(gen_small))
    return {
        "reference_width": ref.width,
        "reference_height": ref.height,
        "generated_width": generated.width,
        "generated_height": generated.height,
        "target_width": size[0],
        "target_height": size[1],
        "mean_abs_diff_0_255": mean_abs_diff,
        "rms_diff_0_255": rms_diff,
        "average_hash_hamming_64": hash_distance,
        "too_similar": mean_abs_diff < AI_COVER_MIN_MEAN_ABS_DIFF and hash_distance < AI_COVER_MIN_HASH_DISTANCE,
        "min_mean_abs_diff": AI_COVER_MIN_MEAN_ABS_DIFF,
        "min_hash_distance": AI_COVER_MIN_HASH_DISTANCE,
    }


def prepare_ai_reference_image(source_path: Path, out_path: Path, size: Tuple[int, int]) -> Path:
    ensure_dir(out_path.parent)
    with Image.open(source_path) as img:
        prepared = crop_cover(ImageOps.exif_transpose(img).convert("RGB"), size)
    prepared.save(out_path, quality=AI_COVER_REF_JPEG_QUALITY, optimize=True)
    return out_path


class AINoInlineImage(RuntimeError):
    pass


class AIImageTooSimilar(RuntimeError):
    pass


def short_error(exc: Exception, limit: int = 240) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message if len(message) <= limit else message[: limit - 3] + "..."


class AIImageQuotaExhausted(RuntimeError):
    pass


def is_ai_quota_error(exc: Exception) -> bool:
    message = str(exc).upper()
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return status_code == 429 or "RESOURCE_EXHAUSTED" in message or "QUOTA" in message or "429" in message


def is_retryable_ai_error(exc: Exception) -> bool:
    if isinstance(exc, (AINoInlineImage, AIImageTooSimilar)):
        return True
    message = str(exc).upper()
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    retry_markers = (
        "RESOURCE_EXHAUSTED",
        "RATE LIMIT",
        "TOO MANY REQUESTS",
        "TIMED OUT",
        "TIMEOUT",
        "UNAVAILABLE",
        "INTERNAL",
        "BAD GATEWAY",
        "SERVICE UNAVAILABLE",
    )
    return status_code in {408, 409, 429, 500, 502, 503, 504} or any(marker in message for marker in retry_markers)


def sleep_before_ai_retry(attempt: int) -> None:
    if AI_COVER_RETRY_BASE_SEC <= 0:
        return
    delay = min(AI_COVER_RETRY_MAX_SEC, AI_COVER_RETRY_BASE_SEC * (2 ** max(0, attempt - 1)))
    time.sleep(delay + random.uniform(0.0, min(2.0, delay * 0.15)))


def compact_evidence_text(value: str, limit: int = 1600) -> str:
    compact = re.sub(r"\s+", " ", value or "").strip()
    return compact[:limit]


def safe_cover_label(value: Any, fallback: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return (text or fallback)[:limit]


def fallback_ai_cover_plan(analysis: Dict[str, Any], frames: List[FrameCandidate], variants: int) -> Dict[str, Any]:
    base_variants = list(analysis.get("cover_variants") or [])
    if not base_variants:
        base_variants = fallback_analysis({}, frames, variants).get("cover_variants", [])
    planned: List[Dict[str, Any]] = []
    for idx in range(variants):
        base = dict(base_variants[idx % len(base_variants)])
        headline = safe_cover_label(base.get("headline"), "Watch This", 44)
        subheadline = safe_cover_label(base.get("subheadline"), "", 54)
        frame_id = str(base.get("frame_id") or frames[idx % len(frames)].frame_id)
        planned.append(
            {
                "variant_id": str(base.get("variant_id") or f"ai{idx + 1:02d}"),
                "source_frame_id": frame_id,
                "angle": str(base.get("angle") or "curiosity"),
                "headline": headline,
                "subheadline": subheadline,
                "text_labels": [headline, subheadline],
                "layout": str(base.get("layout") or "left_text"),
                "visual_prompt": (
                    "Create a bold finished YouTube thumbnail concept from the referenced stop-frame and video context. "
                    "Use the frame as evidence, then re-compose it with expressive lighting, subject separation, tension, and a strong focal point. "
                    "Avoid a plain decorative background. No text, no logos, no watermark, no UI."
                ),
                "composition_notes": "Use the referenced frame as grounding material; make a dramatic visual idea with clean title space.",
                "score_0_to_100": int(base.get("score_0_to_100") or 70),
                "rationale": str(base.get("rationale") or "Fallback AI-cover plan derived from the existing cover variant."),
                "risk": str(base.get("risk") or "AI base image may drift from the source frame."),
            }
        )
    return {
        "creative_direction": "Generate expressive thumbnail concepts from stop-frames and URL context, then render the catchy title deterministically.",
        "material_notes": ["Fallback plan reused existing cover variants and removed non-essential extra text."],
        "ai_cover_variants": planned,
    }


def normalize_ai_cover_plan(plan: Dict[str, Any], analysis: Dict[str, Any], frames: List[FrameCandidate], variants: int) -> Dict[str, Any]:
    valid_frames = {frame.frame_id for frame in frames}
    fallback = fallback_ai_cover_plan(analysis, frames, variants)
    items = list(plan.get("ai_cover_variants") or [])
    if not items:
        return fallback

    fallback_items = fallback["ai_cover_variants"]
    normalized: List[Dict[str, Any]] = []
    for idx in range(variants):
        item = dict(items[idx] if idx < len(items) else fallback_items[idx % len(fallback_items)])
        fb = fallback_items[idx % len(fallback_items)]
        frame_id = str(item.get("source_frame_id") or item.get("frame_id") or fb["source_frame_id"])
        if frame_id not in valid_frames:
            frame_id = fb["source_frame_id"]
        headline = safe_cover_label(item.get("headline"), fb["headline"], 44)
        subheadline = safe_cover_label(item.get("subheadline"), fb["subheadline"], 54)
        labels = [safe_cover_label(label, "", 36) for label in (item.get("text_labels") or [])]
        labels = [label for label in labels if label]
        if not labels:
            labels = [headline]
            if subheadline:
                labels.append(subheadline)
        layout = str(item.get("layout") or fb["layout"])
        if layout not in {"left_text", "right_text", "bottom_bar", "big_number", "split_claim"}:
            layout = fb["layout"]
        visual_prompt = safe_cover_label(item.get("visual_prompt"), fb["visual_prompt"], 1800)
        normalized.append(
            {
                **fb,
                **item,
                "variant_id": safe_cover_label(item.get("variant_id"), fb["variant_id"], 24),
                "source_frame_id": frame_id,
                "headline": headline,
                "subheadline": subheadline,
                "text_labels": labels[:3],
                "layout": layout,
                "visual_prompt": visual_prompt,
                "score_0_to_100": int(item.get("score_0_to_100") or fb["score_0_to_100"]),
            }
        )
    plan["ai_cover_variants"] = normalized
    plan.setdefault("creative_direction", fallback["creative_direction"])
    plan.setdefault("material_notes", [])
    return plan


def plan_ai_cover_variants(
    client: Any,
    *,
    model: str,
    metadata: Dict[str, Any],
    analysis: Dict[str, Any],
    transcript_text: str,
    comment_text: str,
    frames: List[FrameCandidate],
    contact_sheet: Path,
    variants: int,
    custom_prompt: str,
    debug_jsonl: Optional[Path] = None,
) -> Dict[str, Any]:
    frame_table = "\n".join(
        f"- {f.frame_id}: {f.ts:.1f}s, source={f.source}, reason={f.reason}, visual_score={f.visual_score:.0f}"
        for f in frames
    )
    prompt = textwrap.dedent(
        f"""
        You are Indexframe's senior thumbnail concept artist and title editor.

        Stage 1 task: design {variants} expressive AI-generated YouTube cover candidates.
        Do not generate images in this step. Return only JSON matching the schema.

        Use all collected context:
        - public URL metadata: title, channel, description, stats when present
        - transcript hooks and timestamped moments
        - public comments as audience-language evidence
        - every stop-frame in the contact sheet
        - the existing packaging analysis, but improve it when it is generic
        - the optional user creative prompt

        New creative direction:
        - Create thumbnail IDEAS, not stable title cards and not generic cinematic backgrounds.
        - Each candidate must be visually different: close-up emotion, reveal, contradiction, before/after,
          object-as-proof, scale, danger/tension, result, failure, or absurd juxtaposition.
        - Use source_frame_id as grounding evidence, but the image prompt may re-compose the scene into a
          poster-like thumbnail while keeping the real subject/context believable.
        - The generated image must contain no text. The code will render the title on top.

        Visible text rule:
        - headline is the only required visible title. Make it catchy, 2 to 5 words, high-CTR but truthful.
        - subheadline should usually be an empty string. Use it only for a tiny qualifier of 1 to 4 words.
        - text_labels should contain only the title and optional tiny qualifier; no sentences.
        - Do not output extra prose outside JSON.

        Image prompt rule:
        - Every visual_prompt must explicitly include: no text, no letters, no numbers, no logos, no watermark, no UI.
        - Avoid prompts that merely say "cinematic background". Describe subject, tension, lighting, camera,
          composition, negative space for title, and what makes the cover emotionally clickable.
        - Keep claims grounded. Comments can reveal audience confusion/desire, not factual proof.

        Optional user creative prompt:
        {custom_prompt or 'No extra prompt provided.'}

        Available context sizes:
        title_chars={len(str(metadata.get('title', '') or ''))}
        description_chars={len(str(metadata.get('description', '') or ''))}
        transcript_chars={len(transcript_text or '')}
        comment_chars={len(comment_text or '')}
        stop_frames={len(frames)}

        YouTube metadata:
        title: {metadata.get('title', '')}
        channel: {metadata.get('channel_title', '')}
        description: {str(metadata.get('description', ''))[:MAX_DESCRIPTION_CHARS]}
        stats: {json.dumps(metadata.get('statistics') or {}, ensure_ascii=False)}

        Candidate stop-frames:
        {frame_table}

        Existing packaging analysis:
        {json.dumps({
            'video_summary': analysis.get('video_summary'),
            'audience_signals': analysis.get('audience_signals'),
            'dominant_packaging_problem': analysis.get('dominant_packaging_problem'),
            'frame_rankings': analysis.get('frame_rankings'),
            'cover_variants': analysis.get('cover_variants'),
        }, indent=2, ensure_ascii=False)}

        Transcript evidence:
        {compact_evidence_text(transcript_text, 2200)}

        Comment evidence:
        {compact_evidence_text(comment_text, 1800)}
        """
    ).strip()
    try:
        plan = json_from_model(
            client,
            model=model,
            prompt=prompt,
            schema=AI_COVER_PLAN_SCHEMA,
            media_parts=[media_part(contact_sheet)],
            temperature=0.72,
            debug_jsonl=debug_jsonl,
            op_name="ai_cover_plan",
            extra_debug={
                "content_components": {
                    "metadata_title_chars": len(str(metadata.get("title", "") or "")),
                    "description_chars": len(str(metadata.get("description", "") or "")),
                    "transcript_chars": len(transcript_text or ""),
                    "comment_chars": len(comment_text or ""),
                    "stop_frame_count": len(frames),
                    "contact_sheet": image_file_debug(contact_sheet),
                },
                "requested_variants": variants,
            },
        )
    except Exception as exc:
        log(f"AI cover planning failed; using fallback plan. error={short_error(exc, 1000)}")
        plan = fallback_ai_cover_plan(analysis, frames, variants)
    plan = normalize_ai_cover_plan(plan, analysis, frames, variants)
    plan["planner_model"] = model
    plan["custom_prompt"] = custom_prompt
    return plan


def ai_cover_generation_prompt(item: Dict[str, Any], metadata: Dict[str, Any], size: Tuple[int, int]) -> str:
    labels = ", ".join(item.get("text_labels") or [])
    return textwrap.dedent(
        f"""
        Generate a finished expressive YouTube thumbnail base image from the referenced stop-frame and video context.

        Treat the reference frame as grounding evidence, not as a background to copy. Re-compose it into a more
        clickable editorial/poster-like scene while preserving the believable subject, setting, and core moment.

        Video title context: {metadata.get('title', '')}
        Cover concept angle: {item.get('angle', '')}
        Title that code will render later, DO NOT render it in the image: {labels}
        Composition notes: {item.get('composition_notes', '')}
        Visual concept: {item.get('visual_prompt', '')}

        Output requirements:
        - aspect ratio {aspect_ratio_for_size(size)}
        - make the image feel like a complete thumbnail concept, not a plain background
        - strong focal point, expressive emotion/tension, punchy lighting, depth, and subject separation
        - clear negative space where the title can be overlaid by code
        - noticeably different from the raw frame: changed crop, lighting, atmosphere, depth, and visual hierarchy
        - grounded in the real video context; do not add unrelated people, products, brands, or impossible claims
        - absolutely no visible text, letters, numbers, captions, subtitles, logos, watermark, UI, or fake typography
        """
    ).strip()


def generate_ai_cover_base_image(
    client: Any,
    *,
    model: str,
    prompt: str,
    ref_paths: List[Path],
    size: Tuple[int, int],
    debug_jsonl: Optional[Path] = None,
    variant_id: str = "",
    source_frame_id: str = "",
) -> Tuple[Image.Image, Dict[str, Any]]:
    """Generate one cover base and return both the PIL image and structured debug metadata.

    The function only returns status=generated after an inline image is received and it is not
    nearly identical to the prepared reference image. This prevents source-frame fallbacks or
    model echo responses from being mislabeled as successful AI generation.
    """
    aspect_ratio = aspect_ratio_for_size(size)
    last_error: Optional[Exception] = None
    attempts: List[Dict[str, Any]] = []
    ref_count_plan = [min(1, len(ref_paths))]
    if AI_COVER_REF_FALLBACK and ref_paths:
        ref_count_plan.append(0)

    for ref_count in ref_count_plan:
        used_paths = ref_paths[:ref_count]
        parts = [media_part(path) for path in used_paths]
        for attempt in range(1, AI_COVER_MAX_ATTEMPTS + 1):
            started = time.time()
            request_record: Dict[str, Any] = {
                "time_epoch": started,
                "op": "ai_cover_image",
                "variant_id": variant_id,
                "source_frame_id": source_frame_id,
                "model": model,
                "attempt": attempt,
                "ref_count": ref_count,
                "aspect_ratio_requested": aspect_ratio,
                "target_size": {"width": size[0], "height": size[1]},
                "temperature": AI_COVER_TEMPERATURE,
                "prompt_chars": len(prompt),
                "prompt_bytes": len(prompt.encode("utf-8")),
                "input_components": [
                    {"name": "prompt", "type": "text", "chars": len(prompt), "bytes": len(prompt.encode("utf-8"))},
                    *[
                        {"name": f"reference_image_{idx}", "type": "image", **image_file_debug(path)}
                        for idx, path in enumerate(used_paths, start=1)
                    ],
                ],
                "reference_images": [image_file_debug(path) for path in used_paths],
            }
            log(
                "LLM image start "
                f"variant={variant_id or '<unknown>'} model={model} attempt={attempt} "
                f"prompt_chars={request_record['prompt_chars']} refs={ref_count} "
                f"aspect_ratio={aspect_ratio} debug_jsonl={debug_jsonl or '<none>'}"
            )
            try:
                if AI_COVER_REQUEST_INTERVAL_SEC > 0:
                    time.sleep(AI_COVER_REQUEST_INTERVAL_SEC)
                response = client.models.generate_content(
                    model=model,
                    contents=[prompt, *parts],
                    config={
                        "temperature": AI_COVER_TEMPERATURE,
                        "response_modalities": ["TEXT", "IMAGE"],
                        "image_config": {"aspect_ratio": aspect_ratio},
                    },
                )
                request_record.update(response_debug_summary(response, started=started))
                images = extract_response_images(response)
                if not images:
                    response_text = str(getattr(response, "text", "") or "")
                    err_msg = "Model response contained no inline image."
                    if response_text:
                        err_msg += f" text={response_text[:500]}"
                    raise AINoInlineImage(err_msg)

                image = images[0].convert("RGB")
                request_record["raw_generated_image"] = {
                    "width": image.width,
                    "height": image.height,
                    "aspect_ratio": round(image.width / max(1, image.height), 4),
                }
                if used_paths:
                    similarity = image_similarity_metrics(used_paths[0], image, size)
                    request_record["similarity_to_reference"] = similarity
                    if similarity.get("too_similar"):
                        raise AIImageTooSimilar(
                            "Model returned an image too similar to the reference "
                            f"mean_abs_diff={similarity.get('mean_abs_diff_0_255')} "
                            f"hash={similarity.get('average_hash_hamming_64')}"
                        )

                request_record["status"] = "generated"
                attempts.append(request_record)
                if debug_jsonl:
                    append_jsonl(debug_jsonl, request_record)
                log(
                    "LLM image done "
                    f"variant={variant_id or '<unknown>'} status=generated "
                    f"latency={request_record.get('latency_sec')}s "
                    f"inline_images={request_record.get('inline_image_count')} "
                    f"bytes={request_record.get('inline_image_bytes')}"
                )
                return image, {
                    "status": "generated",
                    "attempts": attempts,
                    "accepted_attempt": request_record,
                    "raw_generated_image": request_record.get("raw_generated_image"),
                    "similarity_to_reference": request_record.get("similarity_to_reference"),
                }
            except Exception as exc:
                last_error = exc
                request_record.update({
                    "status": "error",
                    "latency_sec": round(time.time() - started, 3),
                    "error_type": exc.__class__.__name__,
                    "error": short_error(exc, 2000),
                    "quota_like_error": is_ai_quota_error(exc),
                    "retryable": is_retryable_ai_error(exc),
                })
                attempts.append(request_record)
                if debug_jsonl:
                    append_jsonl(debug_jsonl, request_record)
                log(
                    "AI cover image attempt failed "
                    f"variant={variant_id or '<unknown>'} refs={ref_count} attempt={attempt}: "
                    f"{exc.__class__.__name__}: {short_error(exc)}"
                )
                if is_ai_quota_error(exc):
                    raise AIImageQuotaExhausted(short_error(exc, 1000)) from exc
                if attempt >= AI_COVER_MAX_ATTEMPTS or not is_retryable_ai_error(exc):
                    break
                sleep_before_ai_retry(attempt)

    raise RuntimeError(short_error(last_error, 1000) if last_error else "AI image generation failed")

def render_ai_cover_variants(
    client: Any,
    *,
    planner_model: str,
    image_model: str,
    metadata: Dict[str, Any],
    analysis: Dict[str, Any],
    transcript_text: str,
    comment_text: str,
    frames: List[FrameCandidate],
    contact_sheet: Path,
    out_dir: Path,
    size: Tuple[int, int],
    variants: int,
    custom_prompt: str = "",
) -> List[Dict[str, Any]]:
    cover_dir = ensure_dir(out_dir / "covers")
    ai_dir = ensure_dir(out_dir / "ai_cover_bases")
    ref_dir = ensure_dir(out_dir / "ai_cover_refs")
    debug_jsonl = out_dir / "ai_model_calls.jsonl"
    frame_by_id = {f.frame_id: f for f in frames}
    plan = plan_ai_cover_variants(
        client,
        model=planner_model,
        metadata=metadata,
        analysis=analysis,
        transcript_text=transcript_text,
        comment_text=comment_text,
        frames=frames,
        contact_sheet=contact_sheet,
        variants=variants,
        custom_prompt=custom_prompt,
        debug_jsonl=debug_jsonl,
    )
    dump_json(out_dir / "ai_cover_plan.json", plan)
    log(
        f"AI cover plan ready variants={len(plan.get('ai_cover_variants') or [])} "
        f"planner_model={planner_model} image_model={image_model} debug_jsonl={debug_jsonl}"
    )

    rendered: List[Dict[str, Any]] = []
    generation_results: List[Dict[str, Any]] = []
    generated_count = 0
    quota_limited = False
    for idx, item in enumerate(plan.get("ai_cover_variants") or [], start=1):
        frame = frame_by_id.get(str(item.get("source_frame_id"))) or frames[(idx - 1) % len(frames)]
        variant_id = str(item.get("variant_id") or f"ai_{idx:02d}")
        prompt = ai_cover_generation_prompt(item, metadata, size)
        log(
            f"AI cover candidate {idx}/{variants} variant={variant_id} "
            f"frame={frame.frame_id} headline={str(item.get('headline') or '')!r} "
            f"prompt_chars={len(prompt)}"
        )
        slug = slugify(str(item.get("angle") or item.get("variant_id") or "cover"))
        base_path = ai_dir / f"ai_base_{idx:02d}_{slug}.png"
        raw_path = ai_dir / f"ai_raw_{idx:02d}_{slug}.png"
        ref_path = ref_dir / f"ai_ref_{idx:02d}_{frame.frame_id}_{size[0]}x{size[1]}.jpg"
        prepare_ai_reference_image(Path(frame.path), ref_path, size)

        status = "pending"
        error = ""
        model_debug: Dict[str, Any] = {}
        generation_skipped_reason = ""
        raw_generated_path = ""

        generation_cap_reached = AI_COVER_MAX_GENERATED > 0 and generated_count >= AI_COVER_MAX_GENERATED
        if base_path.exists():
            status = "reused_existing_base"
            generation_skipped_reason = "existing ai_base_path already existed before this run"
            base_img = Image.open(base_path).convert("RGB")
        elif quota_limited or generation_cap_reached:
            if quota_limited:
                status = "fallback_source_frame_quota_guard"
                generation_skipped_reason = "prior model call hit a quota-like error"
            else:
                status = "fallback_source_frame_generation_cap"
                generation_skipped_reason = f"INDEXFRAME_AI_COVER_MAX_GENERATED={AI_COVER_MAX_GENERATED} cap reached"
            with Image.open(ref_path) as source_img:
                base_img = source_img.convert("RGB")
            base_img.save(base_path)
        else:
            try:
                generated_img, model_debug = generate_ai_cover_base_image(
                    client,
                    model=image_model,
                    prompt=prompt,
                    ref_paths=[ref_path],
                    size=size,
                    debug_jsonl=debug_jsonl,
                    variant_id=variant_id,
                    source_frame_id=frame.frame_id,
                )
                generated_img.save(raw_path)
                raw_generated_path = str(raw_path)
                base_img = crop_cover(generated_img, size)
                generated_img.close()
                base_img.save(base_path)
                generated_count += 1
                status = "generated"
            except AIImageQuotaExhausted as exc:
                error = short_error(exc, 1000)
                quota_limited = True
                log(f"AI image quota exhausted; using source frames for remaining covers. error={error}")
                with Image.open(ref_path) as source_img:
                    base_img = source_img.convert("RGB")
                base_img.save(base_path)
                status = "fallback_source_frame_quota"
                generation_skipped_reason = "quota-like model error during this variant"
            except Exception as exc:
                error = short_error(exc, 1000)
                log(f"AI cover generation failed for {variant_id}; using source frame. error={exc.__class__.__name__}: {error}")
                with Image.open(ref_path) as source_img:
                    base_img = source_img.convert("RGB")
                base_img.save(base_path)
                status = "fallback_source_frame"
                generation_skipped_reason = f"model call failed: {exc.__class__.__name__}"

        try:
            cover = crop_cover(base_img, size)
        finally:
            base_img.close()
        label_headline = safe_cover_label(item.get("headline") or (item.get("text_labels") or [""])[0], "Watch This", 58)
        labels = list(item.get("text_labels") or [])
        label_subheadline = safe_cover_label(item.get("subheadline") or " · ".join(labels[1:]), "", 72)
        cover = draw_text_block(
            cover,
            headline=label_headline,
            subheadline=label_subheadline,
            layout=str(item.get("layout") or "left_text"),
            angle=str(item.get("angle") or "AI cover"),
            score=int(item.get("score_0_to_100") or 70),
        )
        out_path = cover_dir / f"cover_{idx:02d}_ai_{slug}.jpg"
        cover.save(out_path, quality=93, optimize=True)
        cover.close()

        cover_mode = "ai_generated" if status == "generated" else "source_frame_fallback"
        result = {
            **item,
            "cover_path": str(out_path),
            "cover_mode": cover_mode,
            "generation_status": status,
            "generation_error": error,
            "generation_skipped_reason": generation_skipped_reason,
            "ai_base_path": str(base_path),
            "raw_generated_path": raw_generated_path,
            "prepared_reference_path": str(ref_path),
            "source_frame_path": frame.path,
            "timestamp_sec": frame.ts,
            "image_model": image_model,
            "ai_cover_max_generated": AI_COVER_MAX_GENERATED,
            "ai_cover_interval_sec": AI_COVER_REQUEST_INTERVAL_SEC,
            "ai_cover_temperature": AI_COVER_TEMPERATURE,
            "ai_model_calls_jsonl": str(debug_jsonl),
            "source_frame_debug": image_file_debug(Path(frame.path)),
            "prepared_reference_debug": image_file_debug(ref_path),
            "base_image_debug": image_file_debug(base_path),
            "raw_generated_debug": image_file_debug(raw_path) if raw_path.exists() else {},
            "similarity_to_reference": model_debug.get("similarity_to_reference"),
            "raw_generated_image": model_debug.get("raw_generated_image"),
            "generation_attempt_count": len(model_debug.get("attempts") or []),
            "generation_prompt": prompt,
        }
        rendered.append(result)
        generation_record = {
            "variant_id": item.get("variant_id"),
            "status": status,
            "cover_mode": cover_mode,
            "error": error,
            "generation_skipped_reason": generation_skipped_reason,
            "ai_base_path": str(base_path),
            "raw_generated_path": raw_generated_path,
            "prepared_reference_path": str(ref_path),
            "cover_path": str(out_path),
            "source_frame_id": frame.frame_id,
            "source_frame_path": frame.path,
            "image_model": image_model,
            "similarity_to_reference": model_debug.get("similarity_to_reference"),
            "raw_generated_image": model_debug.get("raw_generated_image"),
            "attempts": model_debug.get("attempts") or [],
            "debug_jsonl": str(debug_jsonl),
        }
        generation_results.append(generation_record)
        dump_json(out_dir / "ai_cover_generation.json", generation_results)
    return rendered


# ------------------------- output/GCS -------------------------


def split_gs_uri(uri: str) -> Tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError("Expected gs://bucket/prefix")
    rest = uri[5:]
    bucket, _, prefix = rest.partition("/")
    if not bucket:
        raise ValueError("Missing bucket")
    return bucket, prefix.rstrip("/")


def clean_gcs_prefix_part(value: str) -> str:
    raw = re.sub(r"^[A-Za-z]:", "", str(value or "").replace("\\", "/")).strip()
    parts: List[str] = []
    for part in raw.split("/"):
        part = part.strip()
        if not part or part in {".", ".."}:
            continue
        cleaned = re.sub(r"[^A-Za-z0-9._=-]+", "-", part).strip("-._")
        if cleaned:
            parts.append(cleaned[:80])
    return "/".join(parts) or "run"


def join_gcs_prefix(*parts: str) -> str:
    return "/".join(part.strip("/") for part in parts if part and part.strip("/"))


def generate_signed_get_url(blob: Any, client: Any, *, expiration_sec: int = 60 * 60 * 24) -> str:
    kwargs = {
        "version": "v4",
        "expiration": expiration_sec,
        "method": "GET",
        "response_disposition": "inline",
    }
    try:
        return blob.generate_signed_url(**kwargs)
    except Exception as first_exc:
        credentials = getattr(client, "_credentials", None) or getattr(client, "credentials", None)
        credential_email = (
            getattr(credentials, "service_account_email", None)
            or getattr(credentials, "signer_email", None)
            or ""
        )
        service_account_email = (
            os.getenv("GOOGLE_SERVICE_ACCOUNT_EMAIL", "").strip()
            or (credential_email if credential_email != "default" else "")
        )

        try:
            from google.auth.transport.requests import Request  # type: ignore

            if credentials is None or not service_account_email:
                raise RuntimeError("No service account email available for IAM signed URL fallback")
            if not getattr(credentials, "valid", False) or not getattr(credentials, "token", None):
                credentials.refresh(Request())
            access_token = getattr(credentials, "token", None)
            if not access_token:
                raise RuntimeError("No access token available for IAM signed URL fallback")
            return blob.generate_signed_url(
                **kwargs,
                service_account_email=service_account_email,
                access_token=access_token,
            )
        except Exception as second_exc:
            raise RuntimeError(
                "Could not generate signed URL. "
                f"private-key signing failed: {short_error(first_exc, 500)}; "
                f"IAM/access-token signing failed: {short_error(second_exc, 500)}"
            ) from second_exc


def upload_dir_to_gcs(
    local_dir: Path,
    output_uri: str,
    project: Optional[str],
    signed_url_paths: Optional[Iterable[Path]] = None,
) -> Dict[str, Any]:
    if storage is None:
        raise RuntimeError(f"google-cloud-storage not installed: {STORAGE_IMPORT_ERROR}")
    bucket_name, prefix = split_gs_uri(output_uri)
    upload_prefix = join_gcs_prefix(prefix, clean_gcs_prefix_part(local_dir.as_posix()))
    upload_root_uri = f"gs://{bucket_name}/{upload_prefix}" if upload_prefix else f"gs://{bucket_name}"
    credentials, adc_project = google.auth.default(
        scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    )
    client = storage.Client(
        project = project or adc_project or None,
        credentials = credentials,
    )
    bucket = client.bucket(bucket_name)
    uploaded: List[str] = []
    uploaded_items: List[Dict[str, str]] = []
    public_urls: List[str] = []
    signed_url_items: List[Dict[str, str]] = []
    signed_url_errors: List[Dict[str, str]] = []
    signed_path_set = {path.resolve() for path in signed_url_paths} if signed_url_paths is not None else None
    for path in sorted(local_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(local_dir).as_posix()
        blob_name = join_gcs_prefix(upload_prefix, rel)
        blob = bucket.blob(blob_name)
        content_type, _ = mimetypes.guess_type(str(path))
        blob.upload_from_filename(str(path), content_type=content_type or "application/octet-stream")
        gcs_uri = f"gs://{bucket_name}/{blob_name}"
        uploaded.append(gcs_uri)
        uploaded_items.append({"local_path": str(path), "relative_path": rel, "gcs_uri": gcs_uri})
        should_sign = path.resolve() in signed_path_set if signed_path_set is not None else (path.suffix.lower() in IMAGE_EXTS or path.name.endswith(".html"))
        if should_sign:
            try:
                signed_url = generate_signed_get_url(blob, client)
                public_urls.append(signed_url)
                signed_url_items.append({"local_path": str(path), "relative_path": rel, "gcs_uri": gcs_uri, "signed_url": signed_url})
            except Exception as exc:
                signed_url_errors.append({"local_path": str(path), "relative_path": rel, "gcs_uri": gcs_uri, "error": short_error(exc, 1000)})
    return {
        "requested_output_uri": output_uri,
        "output_uri": upload_root_uri,
        "uploaded_prefix": upload_prefix,
        "uploaded": uploaded,
        "uploaded_items": uploaded_items,
        "signed_urls": public_urls,
        "signed_url_items": signed_url_items,
        "signed_url_errors": signed_url_errors,
    }


def write_index_html(out_dir: Path, metadata: Dict[str, Any], analysis: Dict[str, Any], rendered: List[Dict[str, Any]]) -> Path:
    cards = []
    for item in rendered:
        rel = Path(item["cover_path"]).relative_to(out_dir).as_posix()
        cards.append(
            f"""
            <article class="card">
              <img src="{html.escape(rel)}" />
              <h2>{html.escape(str(item.get('headline', '')))}</h2>
              <p class="meta">{html.escape(str(item.get('angle', '')))} · score {html.escape(str(item.get('score_0_to_100', '')))} · {float(item.get('timestamp_sec', 0)):.1f}s</p>
              <p>{html.escape(str(item.get('rationale', '')))}</p>
              <p class="risk">Risk: {html.escape(str(item.get('risk', '')))}</p>
            </article>
            """
        )
    doc = f"""
    <!doctype html>
    <html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Indexframe PoC</title>
    <style>
      body {{ margin:0; font-family: Inter, system-ui, -apple-system, Segoe UI, sans-serif; background:#0e1015; color:#f5f7fb; }}
      header {{ max-width:1100px; margin:0 auto; padding:36px 24px 18px; }}
      h1 {{ font-size:34px; margin:0 0 8px; }}
      .summary {{ color:#c7ccd7; max-width:900px; line-height:1.5; }}
      .grid {{ max-width:1100px; margin:0 auto; padding:18px 24px 40px; display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:18px; }}
      .card {{ background:#171b25; border:1px solid #252b3a; border-radius:18px; overflow:hidden; box-shadow:0 14px 40px rgba(0,0,0,.25); }}
      .card img {{ display:block; width:100%; aspect-ratio:16/9; object-fit:cover; }}
      .card h2 {{ padding:16px 16px 0; margin:0; font-size:20px; }}
      .card p {{ padding:0 16px 14px; margin:8px 0 0; color:#c7ccd7; line-height:1.45; }}
      .card .meta {{ color:#9fb1ff; font-size:13px; }}
      .card .risk {{ color:#f1b4b4; font-size:13px; }}
      code {{ color:#b6f3c8; }}
    </style></head><body>
      <header>
        <h1>Indexframe cover variants</h1>
        <p class="summary"><strong>{html.escape(str(metadata.get('title') or 'YouTube video'))}</strong><br>{html.escape(str(analysis.get('video_summary') or ''))}</p>
        <p class="summary">Packaging problem: {html.escape(str(analysis.get('dominant_packaging_problem') or ''))}</p>
        <p class="summary">Artifacts: <code>analysis.json</code>, <code>moments.json</code>, <code>frames/</code>, <code>covers/</code>.</p>
      </header>
      <main class="grid">{''.join(cards)}</main>
    </body></html>
    """
    path = out_dir / "index.html"
    path.write_text(doc, encoding="utf-8")
    return path


# ------------------------- main pipeline -------------------------


def run_pipeline(
    *,
    url: str,
    out_dir: Path,
    project: Optional[str] = None,
    location: str = "global",
    youtube_api_key: Optional[str] = None,
    video_path: Optional[Path] = None,
    transcript_file: Optional[Path] = None,
    heatmap_json: Optional[Path] = None,
    download_cmd: Optional[str] = None,
    model: str = "gemini-2.5-flash",
    variants: int = 6,
    size: Tuple[int, int] = DEFAULT_SIZE,
    output_gcs_uri: Optional[str] = None,
    skip_gemini: bool = False,
    ai_covers: Optional[bool] = None,
    image_model: Optional[str] = None,
    ai_cover_prompt: str = "",
    email_to: Optional[str] = None,
    submission_id: Optional[str] = None,
) -> RunResult:
    started = time.time()
    if ai_covers is None:
        ai_covers = env_flag("INDEXFRAME_AI_COVERS", True)
    image_model = image_model or os.getenv("INDEXFRAME_IMAGE_MODEL", "gemini-2.5-flash-image")
    ai_cover_prompt = ai_cover_prompt or os.getenv("INDEXFRAME_AI_COVER_PROMPT", "")
    email_to = (email_to or env_text("USER_EMAIL") or env_text("RESULT_EMAIL_TO") or env_text("EMAIL_TO")).strip()
    submission_id = (submission_id or env_text("SUBMISSION_ID", "manual")).strip()
    ensure_dir(out_dir)
    video_id = parse_youtube_id(url)
    log(f"video_id={video_id}")

    metadata = fetch_youtube_public_data(video_id, youtube_api_key or os.getenv("YOUTUBE_API_KEY"))
    log(f"metadata title={metadata.get('title', '<none>')!r}, comments={len(metadata.get('comments') or [])}")

    video, info_json, subtitle_files = download_or_resolve_video(url, out_dir, video_path, download_cmd)
    log(f"video={video}")
    metadata = merge_info_json(metadata, info_json)
    duration = float(metadata.get("duration_sec") or 0) or probe_duration(video)
    metadata["duration_sec"] = duration
    metadata["source_video_path"] = str(video)
    dump_json(out_dir / "metadata.json", metadata)

    transcript_entries: List[Dict[str, Any]] = []
    if transcript_file:
        transcript_entries = parse_srt_or_vtt(transcript_file)
    if not transcript_entries:
        for sub in subtitle_files:
            if re.search(r"\.en\.|en[-_]", sub.name, flags=re.I) or not transcript_entries:
                transcript_entries = parse_srt_or_vtt(sub)
                if transcript_entries:
                    break
    dump_json(out_dir / "transcript_entries.json", transcript_entries)
    transcript_text = compact_transcript(transcript_entries)
    comment_text = compact_comments(metadata.get("comments") or [])

    moments = build_moment_candidates(metadata, transcript_entries, duration, heatmap_json)
    dump_json(out_dir / "moments.json", [dataclasses.asdict(m) for m in moments])
    log(f"moments={len(moments)} duration={duration:.1f}s")

    frames = extract_candidate_frames(video, moments, out_dir)
    if not frames:
        raise RuntimeError("No candidate frames extracted; check downloader/ffmpeg/video input.")
    dump_json(out_dir / "frames.json", [dataclasses.asdict(f) for f in frames])
    contact_sheet = make_contact_sheet(frames, out_dir / "contact_sheet.jpg")
    llm_debug_jsonl = out_dir / "llm_calls.jsonl"
    client: Any = None
    log(f"contact_sheet={contact_sheet} frames={len(frames)} llm_debug_jsonl={llm_debug_jsonl}")
    log(f"skip_gemini? {skip_gemini}; ai_covers? {ai_covers}")
    if skip_gemini:
        analysis = fallback_analysis(metadata, frames, variants)
    else:
        try:
            client = maybe_make_genai_client(project=project or os.getenv("PROJECT_ID") or None, location=location)
            analysis = analyze_with_gemini(
                client,
                model=model,
                metadata=metadata,
                transcript_text=transcript_text,
                comment_text=comment_text,
                frames=frames,
                contact_sheet=contact_sheet,
                variants=variants,
                debug_jsonl=llm_debug_jsonl,
            )
        except Exception as exc:
            log(f"Gemini analysis failed; using fallback. error={exc}")
            analysis = fallback_analysis(metadata, frames, variants)
    analysis["run_meta"] = {
        "video_id": video_id,
        "duration_sec": duration,
        "model": model if not skip_gemini else "fallback",
        "generated_at_epoch": time.time(),
        "elapsed_sec_so_far": round(time.time() - started, 2),
    }
    dump_json(out_dir / "analysis.json", analysis)

    if ai_covers and skip_gemini:
        log("AI covers requested but --skip-gemini is enabled; using deterministic renderer.")
        rendered = render_cover_variants(analysis, frames, out_dir, size)
    elif ai_covers:
        try:
            if client is None:
                client = maybe_make_genai_client(project=project or os.getenv("PROJECT_ID") or None, location=location)
            rendered = render_ai_cover_variants(
                client,
                planner_model=model,
                image_model=image_model,
                metadata=metadata,
                analysis=analysis,
                transcript_text=transcript_text,
                comment_text=comment_text,
                frames=frames,
                contact_sheet=contact_sheet,
                out_dir=out_dir,
                size=size,
                variants=variants,
                custom_prompt=ai_cover_prompt,
            )
        except Exception as exc:
            log(f"AI cover path failed; using deterministic renderer. error={exc}")
            rendered = render_cover_variants(analysis, frames, out_dir, size)
    else:
        rendered = render_cover_variants(analysis, frames, out_dir, size)
    dump_json(out_dir / "variants.json", rendered)
    index = write_index_html(out_dir, metadata, analysis, rendered)

    public_urls: Optional[List[str]] = None
    public_url_items: Optional[List[Dict[str, str]]] = None
    cover_gcs_uris: Optional[List[str]] = None
    signed_url_errors: Optional[List[Dict[str, str]]] = None
    gcs_uri: Optional[str] = None
    if output_gcs_uri:
        cover_paths = [Path(item["cover_path"]) for item in rendered]
        signed_url_paths = [index, out_dir / "analysis.json", out_dir / "variants.json", *cover_paths]
        upload = upload_dir_to_gcs(out_dir, output_gcs_uri, project or os.getenv("PROJECT_ID") or None, signed_url_paths=signed_url_paths)
        dump_json(out_dir / "gcs_upload.json", upload)
        gcs_uri = str(upload.get("output_uri") or output_gcs_uri)
        public_urls = upload.get("signed_urls") or []
        public_url_items = upload.get("signed_url_items") or []
        cover_path_set = {str(path.resolve()) for path in cover_paths}
        cover_gcs_uris = [
            str(item.get("gcs_uri"))
            for item in public_url_items
            if item.get("local_path") and str(Path(str(item.get("local_path"))).resolve()) in cover_path_set and item.get("gcs_uri")
        ]
        if not cover_gcs_uris:
            cover_gcs_uris = [
                str(item.get("gcs_uri"))
                for item in (upload.get("uploaded_items") or [])
                if item.get("local_path") and str(Path(str(item.get("local_path"))).resolve()) in cover_path_set and item.get("gcs_uri")
            ]
        signed_url_errors = upload.get("signed_url_errors") or []
        if signed_url_errors:
            log(f"signed URL generation failed for {len(signed_url_errors)} uploaded artifact(s); see gcs_upload.json")

    summary = {
        "ok": True,
        "run_dir": str(out_dir),
        "index_html": str(index),
        "analysis_json": str(out_dir / "analysis.json"),
        "variants_json": str(out_dir / "variants.json"),
        "cover_paths": [item["cover_path"] for item in rendered],
        "gcs_uri": gcs_uri,
        "cover_gcs_uris": cover_gcs_uris,
        "public_urls": public_urls,
        "public_url_items": public_url_items,
        "signed_url_errors": signed_url_errors,
        "email_to": email_to or None,
        "email_sent": False,
        "elapsed_sec": round(time.time() - started, 2),
    }
    image_hero_pack = None
    try:
        image_hero_pack = build_image_hero_pack(
            url=url,
            video_id=video_id,
            metadata=metadata,
            analysis=analysis,
            variants=rendered,
            frames=frames,
            moments=moments,
            summary=summary,
            out_dir=out_dir,
            model=model if not skip_gemini else "fallback",
            image_model=image_model,
            size=size,
            ai_covers=bool(ai_covers),
            skip_gemini=bool(skip_gemini),
            submission_id=submission_id or None,
        )
        image_hero_pack_path = out_dir / "image_hero_pack.json"
        dump_json(image_hero_pack_path, image_hero_pack)
        summary["image_hero_pack"] = {
            "pack_id": image_hero_pack.get("pack_id"),
            "path": str(image_hero_pack_path),
        }
        summary["storage"] = maybe_store_image_hero_pack(image_hero_pack)
        log(f"mongo storage status: {summary['storage']}")
    except Exception as exc:
        err = short_error(exc, 1000)
        summary["storage"] = {"enabled": False, "error": f"{exc.__class__.__name__}: {err}"}
        log(f"mongo storage failed: {err}")


    if email_to:
        text_body, html_body = build_result_email_body(
            submitted_url=url,
            submission_id=submission_id or "manual",
            metadata=metadata,
            summary=summary,
            public_url_items=public_url_items,
            final_result=image_hero_pack if "image_hero_pack" in locals() else None,
        )
        try:
            send_email(to_email=email_to, subject="Your Indexframe result", text=text_body, html_body=html_body)
        except Exception as exc:
            summary["email_error"] = str(exc)
            dump_json(out_dir / "run_summary.json", summary)
            raise
        summary["email_sent"] = True
        log(f"result email sent to {email_to}")

    summary["elapsed_sec"] = round(time.time() - started, 2)
    dump_json(out_dir / "run_summary.json", summary)
    log("done: " + json.dumps(summary, indent=2))
    return RunResult(
        run_dir=str(out_dir),
        index_html=str(index),
        analysis_json=str(out_dir / "analysis.json"),
        variants_json=str(out_dir / "variants.json"),
        cover_paths=[item["cover_path"] for item in rendered],
        gcs_uri=gcs_uri,
        public_urls=public_urls,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Indexframe YouTube cover-variant PoC")
    p.add_argument("--url", default=os.getenv("SUBMITTED_URL", os.getenv("YOUTUBE_URL", "")), help="YouTube URL or video id. Defaults to SUBMITTED_URL or YOUTUBE_URL when set.")
    p.add_argument("--out-dir", default=os.getenv("OUT", ""))
    p.add_argument("--project", default=os.getenv("PROJECT_ID", ""))
    p.add_argument("--location", default=os.getenv("VERTEX_LOCATION", os.getenv("GOOGLE_CLOUD_LOCATION", "global")))
    p.add_argument("--youtube-api-key", default=os.getenv("YOUTUBE_API_KEY", ""))
    p.add_argument("--video-path", type=Path, help="Skip download and use an existing local video")
    p.add_argument("--transcript-file", type=Path, help="Optional .srt/.vtt transcript")
    p.add_argument("--heatmap-json", type=Path, help="Optional heatmap/most-replayed JSON from your downloader or analytics exporter")
    p.add_argument("--download-cmd", default=os.getenv("YT_DOWNLOAD_CMD", ""), help="Command template with {url}, {out}, {out_dir}, {out_base}")
    p.add_argument("--model", default=os.getenv("INDEXFRAME_MODEL", "gemini-3.5-flash"))
    p.add_argument("--variants", type=int, default=int(os.getenv("INDEXFRAME_VARIANTS", "2")))
    p.add_argument("--size", type=parse_size, default=parse_size(os.getenv("INDEXFRAME_SIZE", "1280x720")))
    p.add_argument("--output-gcs-uri", default=os.getenv("OUTPUT_GCS_URI", ""), help="Optional gs://bucket/prefix upload destination")
    p.add_argument("--skip-gemini", action="store_true", help="Use deterministic fallback for local smoke tests")
    p.add_argument(
        "--ai-covers",
        action=argparse.BooleanOptionalAction,
        default=env_flag("INDEXFRAME_AI_COVERS", True),
        help="Turn on/off expressive AI cover generation before deterministic text rendering. Defaults on; use --no-ai-covers to force source-frame rendering.",
    )
    p.add_argument("--image-model", default=os.getenv("INDEXFRAME_IMAGE_MODEL", "gemini-2.5-flash-image"), help="Image generation model used when --ai-covers is enabled")
    p.add_argument("--ai-cover-prompt", default=os.getenv("INDEXFRAME_AI_COVER_PROMPT", ""), help="Optional creative guidance for the AI cover planner")
    p.add_argument("--email-to", default=os.getenv("USER_EMAIL", os.getenv("RESULT_EMAIL_TO", os.getenv("EMAIL_TO", ""))), help="Optional result-email recipient. Defaults to USER_EMAIL, RESULT_EMAIL_TO, or EMAIL_TO when set.")
    p.add_argument("--submission-id", default=os.getenv("SUBMISSION_ID", "manual"), help="Optional submission id included in the result email.")
    return p


def main() -> None:
    args = build_arg_parser().parse_args()
    if not args.url:
        raise SystemExit("--url is required unless SUBMITTED_URL or YOUTUBE_URL is set")
    run_pipeline(
        url=args.url,
        out_dir=Path(args.out_dir),
        project=args.project or None,
        location=args.location,
        youtube_api_key=args.youtube_api_key or None,
        video_path=args.video_path,
        transcript_file=args.transcript_file,
        heatmap_json=args.heatmap_json,
        download_cmd=args.download_cmd or None,
        model=args.model,
        variants=args.variants,
        size=args.size,
        output_gcs_uri=args.output_gcs_uri or None,
        skip_gemini=args.skip_gemini,
        ai_covers=args.ai_covers,
        image_model=args.image_model or None,
        ai_cover_prompt=args.ai_cover_prompt or "",
        email_to=args.email_to or None,
        submission_id=args.submission_id or None,
    )


if __name__ == "__main__":
    try:
        dotenv.load_dotenv()
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)






