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
import re
import shlex
import subprocess
import sys
import textwrap
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import dotenv
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

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

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_SIZE = (1280, 720)
MAX_COMMENT_CHARS = 8000
MAX_TRANSCRIPT_CHARS = 14000
MAX_DESCRIPTION_CHARS = 2500


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


def run_cmd(cmd: List[str], *, cwd: Optional[Path] = None, timeout: Optional[int] = None) -> str:
    log("$ " + " ".join(shlex.quote(part) for part in cmd))
    completed = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
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
) -> Dict[str, Any]:
    contents: List[Any] = [prompt]
    if media_parts:
        contents.extend(media_parts)
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config={
            "temperature": temperature,
            "response_mime_type": "application/json",
            "response_schema": schema,
        },
    )
    return json.loads(response.text)


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

    dl_dir = ensure_dir(out_dir / "download")
    target = dl_dir / "source.mp4"
    cmd_template = download_cmd or os.getenv("YT_DOWNLOAD_CMD", "")

    if not cmd_template:
        # Fallback for local dev. The hackathon repo can replace this with the existing downloader CLI.
        cmd_template = "yt-dlp -f bv*+ba/best --merge-output-format mp4 --write-info-json --write-auto-subs --sub-lang en --convert-subs srt -o {out_base}.%(ext)s {url}"

    values = {
        "url": url,
        "out": str(target),
        "out_dir": str(dl_dir),
        "out_base": str(dl_dir / "source"),
    }
    cmd_str = cmd_template.format(**values)
    run_cmd(shlex.split(cmd_str), cwd=dl_dir, timeout=900)

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
) -> Dict[str, Any]:
    frame_table = "\n".join(
        f"- {f.frame_id}: {f.ts:.1f}s, source={f.source}, reason={f.reason}, local_visual_score={f.visual_score:.0f}, path={Path(f.path).name}"
        for f in frames
    )
    prompt = textwrap.dedent(
        f"""
        You are Indexframe, an elite video packaging strategist.
        Goal: choose the best video frames and generate {variants} YouTube cover/thumbnail hero variants.

        Constraints:
        - Final image text must be short: headline <= 5 words, subheadline <= 7 words.
        - Do not invent unsupported claims. Prefer concrete numbers, mistakes, strong visual moments, audience questions.
        - Avoid generic titles like "watch this" or "amazing story".
        - Use comments as audience evidence, not as truth.
        - Choose frame_id from the contact sheet only.
        - Prefer variants that are visually distinct and emotionally distinct.
        - layout must be one of: left_text, right_text, bottom_bar, big_number, split_claim.

        YouTube metadata:
        title: {metadata.get('title', '')}
        channel: {metadata.get('channel_title', '')}
        description: {str(metadata.get('description', ''))[:MAX_DESCRIPTION_CHARS]}
        stats: {json.dumps(metadata.get('statistics') or {}, ensure_ascii=False)}

        Candidate frames:
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
        temperature=0.35,
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
                "subheadline": "Generated from video evidence",
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
    tag = f"{angle}  ·  {score}"
    tb = draw.textbbox((0, 0), tag, font=tagfont)
    pill = (text_x, max(24, text_y - 58), text_x + tb[2] - tb[0] + 28, max(24, text_y - 58) + 38)
    draw.rounded_rectangle(pill, radius=18, fill=(255, 255, 255, 232))
    draw.text((pill[0] + 14, pill[1] + 6), tag, font=tagfont, fill=(10, 10, 12))

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


# ------------------------- output/GCS -------------------------


def split_gs_uri(uri: str) -> Tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError("Expected gs://bucket/prefix")
    rest = uri[5:]
    bucket, _, prefix = rest.partition("/")
    if not bucket:
        raise ValueError("Missing bucket")
    return bucket, prefix.rstrip("/")


def upload_dir_to_gcs(local_dir: Path, output_uri: str, project: Optional[str]) -> Dict[str, Any]:
    if storage is None:
        raise RuntimeError(f"google-cloud-storage not installed: {STORAGE_IMPORT_ERROR}")
    bucket_name, prefix = split_gs_uri(output_uri)
    client = storage.Client(project=project or None)
    bucket = client.bucket(bucket_name)
    uploaded: List[str] = []
    public_urls: List[str] = []
    for path in sorted(local_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(local_dir).as_posix()
        blob_name = f"{prefix}/{rel}" if prefix else rel
        blob = bucket.blob(blob_name)
        content_type, _ = mimetypes.guess_type(str(path))
        blob.upload_from_filename(str(path), content_type=content_type or "application/octet-stream")
        uploaded.append(f"gs://{bucket_name}/{blob_name}")
        if path.suffix.lower() in IMAGE_EXTS or path.name.endswith(".html"):
            try:
                public_urls.append(
                    blob.generate_signed_url(version="v4", expiration=60 * 60 * 24, method="GET", response_disposition="inline")
                )
            except Exception:
                pass
    return {"output_uri": output_uri, "uploaded": uploaded, "signed_urls": public_urls}


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
) -> RunResult:
    started = time.time()
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
    log(f"skip_gemini? {skip_gemini}")
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

    rendered = render_cover_variants(analysis, frames, out_dir, size)
    dump_json(out_dir / "variants.json", rendered)
    index = write_index_html(out_dir, metadata, analysis, rendered)

    public_urls: Optional[List[str]] = None
    gcs_uri: Optional[str] = None
    if output_gcs_uri:
        upload = upload_dir_to_gcs(out_dir, output_gcs_uri, project or os.getenv("PROJECT_ID") or None)
        dump_json(out_dir / "gcs_upload.json", upload)
        gcs_uri = output_gcs_uri
        public_urls = upload.get("signed_urls") or []

    summary = {
        "ok": True,
        "run_dir": str(out_dir),
        "index_html": str(index),
        "analysis_json": str(out_dir / "analysis.json"),
        "variants_json": str(out_dir / "variants.json"),
        "cover_paths": [item["cover_path"] for item in rendered],
        "gcs_uri": gcs_uri,
        "public_urls": public_urls,
        "elapsed_sec": round(time.time() - started, 2),
    }
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
    p.add_argument("--url", required=True, help="YouTube URL or video id")
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--project", default=os.getenv("PROJECT_ID", ""))
    p.add_argument("--location", default=os.getenv("VERTEX_LOCATION", os.getenv("GOOGLE_CLOUD_LOCATION", "global")))
    p.add_argument("--youtube-api-key", default=os.getenv("YOUTUBE_API_KEY", ""))
    p.add_argument("--video-path", type=Path, help="Skip download and use an existing local video")
    p.add_argument("--transcript-file", type=Path, help="Optional .srt/.vtt transcript")
    p.add_argument("--heatmap-json", type=Path, help="Optional heatmap/most-replayed JSON from your downloader or analytics exporter")
    p.add_argument("--download-cmd", default=os.getenv("YT_DOWNLOAD_CMD", ""), help="Command template with {url}, {out}, {out_dir}, {out_base}")
    p.add_argument("--model", default=os.getenv("INDEXFRAME_MODEL", "gemini-2.5-flash"))
    p.add_argument("--variants", type=int, default=int(os.getenv("INDEXFRAME_VARIANTS", "6")))
    p.add_argument("--size", type=parse_size, default=parse_size(os.getenv("INDEXFRAME_SIZE", "1280x720")))
    p.add_argument("--output-gcs-uri", default=os.getenv("OUTPUT_GCS_URI", ""), help="Optional gs://bucket/prefix upload destination")
    p.add_argument("--skip-gemini", action="store_true", help="Use deterministic fallback for local smoke tests")
    return p


def main() -> None:
    args = build_arg_parser().parse_args()
    run_pipeline(
        url=args.url,
        out_dir=args.out_dir,
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
    )


if __name__ == "__main__":
    try:
        dotenv.load_dotenv()
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
