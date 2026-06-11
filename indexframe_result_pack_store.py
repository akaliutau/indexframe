
#!/usr/bin/env python3
"""MongoDB persistence for IndexFrame Image Hero runs.

Mirrors AI Salad's result_pack_store.py pattern:
- MongoDB is optional and enabled only when MONGODB_URI / INDEXFRAME_MONGODB_URI is set.
- Persistence failures are returned in a small status object instead of crashing the pipeline.
- Images are not stored in MongoDB; store local paths, GCS URIs, and signed URLs only.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

_MONGO_CLIENT: Any | None = None
_INDEXED_COLLECTIONS: set[tuple[str, str]] = set()

DEFAULT_DB = os.getenv("INDEXFRAME_MONGODB_DB", os.getenv("MONGODB_DB", "indexframe"))
DEFAULT_COLLECTION = os.getenv(
    "INDEXFRAME_MONGODB_COLLECTION",
    os.getenv("MONGODB_COLLECTION", "image_hero_packs"),
)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_plain_json(value: Any, *, max_string: int = 50_000) -> Any:
    """Convert dataclasses/Path/SDK-ish objects into Mongo-safe JSON-ish data."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= max_string else value[: max_string - 3] + "..."
    if isinstance(value, (bytes, bytearray)):
        return {"bytes_len": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return to_plain_json(dataclasses.asdict(value), max_string=max_string)
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
    return repr(value)[:max_string]


def sha256_json(value: Any) -> str:
    data = json.dumps(to_plain_json(value), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def video_id_from_url(raw_url: str) -> str:
    raw = (raw_url or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw):
        return raw
    parsed = urlparse(raw)
    if "youtu.be" in parsed.netloc.lower():
        candidate = parsed.path.strip("/").split("/")[0]
        if candidate:
            return candidate
    query = parse_qs(parsed.query)
    if query.get("v"):
        return str(query["v"][0])
    match = re.search(r"/shorts/([A-Za-z0-9_-]{11})", parsed.path)
    return match.group(1) if match else "unknown-video"


def mongodb_uri_from_env() -> str | None:
    """Resolve MongoDB URI from env or a mounted Secret Manager file."""
    for name in ("INDEXFRAME_MONGODB_URI", "MONGODB_URI"):
        value = os.getenv(name)
        if value:
            return value.strip()

    for name in ("INDEXFRAME_MONGODB_URI_FILE", "MONGODB_URI_FILE"):
        path = os.getenv(name)
        if not path:
            continue
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return None
    return None


def mongo_collection(
    uri: str | None = None,
    db_name: str | None = None,
    collection_name: str | None = None,
) -> Any:
    from pymongo import ASCENDING, DESCENDING, MongoClient
    from pymongo.server_api import ServerApi

    resolved_uri = uri or mongodb_uri_from_env()
    if not resolved_uri:
        raise RuntimeError("MONGODB_URI / INDEXFRAME_MONGODB_URI is not set")

    global _MONGO_CLIENT
    if _MONGO_CLIENT is None:
        _MONGO_CLIENT = MongoClient(
            resolved_uri,
            server_api=ServerApi("1"),
            serverSelectionTimeoutMS=int(os.getenv("MONGODB_TIMEOUT_MS", "5000")),
        )

    dbn = db_name or DEFAULT_DB
    coln = collection_name or DEFAULT_COLLECTION
    collection = _MONGO_CLIENT[dbn][coln]

    index_key = (dbn, coln)
    if index_key not in _INDEXED_COLLECTIONS:
        collection.create_index("pack_id", unique=True)
        collection.create_index([("video.video_id", ASCENDING), ("created_at", DESCENDING)])
        collection.create_index([("submission_id", ASCENDING), ("created_at", DESCENDING)])
        collection.create_index([("best_variant.score_0_to_100", DESCENDING), ("created_at", DESCENDING)])
        collection.create_index([("artifacts.gcs_uri", ASCENDING)])
        _INDEXED_COLLECTIONS.add(index_key)

    return collection


def ping_mongodb() -> dict[str, Any]:
    collection = mongo_collection()
    collection.database.client.admin.command("ping")
    return {"ok": True, "db": collection.database.name, "collection": collection.name}


def _public_item_maps(summary: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_local: dict[str, dict[str, Any]] = {}
    by_relative: dict[str, dict[str, Any]] = {}

    for item in summary.get("public_url_items") or []:
        if not isinstance(item, dict):
            continue
        local_path = str(item.get("local_path") or "")
        relative_path = str(item.get("relative_path") or "")
        if local_path:
            try:
                by_local[str(Path(local_path).resolve(strict=False))] = item
            except Exception:
                by_local[local_path] = item
        if relative_path:
            by_relative[relative_path] = item

    return by_local, by_relative


def _artifact_for_path(path_value: Any, summary: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    path_text = str(path_value or "")
    local_resolved = ""
    relative_path = ""
    if path_text:
        path = Path(path_text)
        try:
            local_resolved = str(path.resolve(strict=False))
        except Exception:
            local_resolved = path_text
        try:
            relative_path = str(path.resolve(strict=False).relative_to(out_dir.resolve(strict=False))).replace("\\", "/")
        except Exception:
            relative_path = path.name

    by_local, by_relative = _public_item_maps(summary)
    public_item = by_local.get(local_resolved) or by_relative.get(relative_path) or {}

    return {
        "local_path": path_text,
        "relative_path": relative_path,
        "gcs_uri": public_item.get("gcs_uri"),
        "signed_url": public_item.get("signed_url"),
        "content_type": public_item.get("content_type"),
        "bytes": public_item.get("bytes"),
    }


def _metadata_for_mongo(metadata: dict[str, Any]) -> dict[str, Any]:
    """Keep useful video metadata without accidentally bloating every document."""
    allowed = {
        "video_id",
        "title",
        "description",
        "channel_title",
        "published_at",
        "duration_sec",
        "statistics",
        "content_details",
        "metadata_source",
        "thumbnails",
        "source_video_path",
    }
    out = {key: metadata.get(key) for key in allowed if key in metadata}
    comments = metadata.get("comments") or []
    out["comment_count"] = len(comments) if isinstance(comments, list) else 0
    out["comments_sample"] = comments[:20] if isinstance(comments, list) else []
    return to_plain_json(out)


def build_image_hero_pack(
    *,
    url: str,
    video_id: str | None,
    metadata: dict[str, Any],
    analysis: dict[str, Any],
    variants: list[dict[str, Any]],
    frames: list[Any],
    moments: list[Any],
    summary: dict[str, Any],
    out_dir: str | Path,
    model: str,
    image_model: str | None,
    size: tuple[int, int],
    ai_covers: bool,
    skip_gemini: bool,
    submission_id: str | None = None,
) -> dict[str, Any]:
    """Build the durable MongoDB document for one Image Hero pipeline run."""
    out_path = Path(out_dir)
    created_at = utc_now_iso()
    resolved_video_id = video_id or str(metadata.get("video_id") or video_id_from_url(url))
    clean_variants = to_plain_json(variants)
    clean_analysis = to_plain_json(analysis)
    best_variant = max(
        [v for v in clean_variants if isinstance(v, dict)],
        key=lambda v: int(v.get("score_0_to_100") or 0),
        default={},
    )

    artifacts = {
        "run_dir": str(out_path),
        "index_html": _artifact_for_path(summary.get("index_html"), summary, out_path),
        "analysis_json": _artifact_for_path(summary.get("analysis_json"), summary, out_path),
        "variants_json": _artifact_for_path(summary.get("variants_json"), summary, out_path),
        "gcs_uri": summary.get("gcs_uri"),
        "public_urls": summary.get("public_urls") or [],
        "signed_url_errors": summary.get("signed_url_errors") or [],
        "covers": [_artifact_for_path(v.get("cover_path"), summary, out_path) for v in clean_variants if isinstance(v, dict)],
    }

    content_hash = sha256_json(
        {
            "url": url,
            "video_id": resolved_video_id,
            "analysis": clean_analysis,
            "variants": clean_variants,
            "cover_paths": summary.get("cover_paths"),
        }
    )
    identity = {
        "pipeline": "indexframe.image_hero",
        "submission_id": submission_id or "",
        "video_id": resolved_video_id,
        # For production submit IDs, retries upsert into the same doc. For manual runs, keep each run separate.
        "created_at": "" if submission_id and submission_id != "manual" else created_at,
        "content_hash": content_hash,
    }
    pack_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:24]

    pack = {
        "pack_id": pack_id,
        "project": "IndexFrame",
        "pipeline": "indexframe.image_hero",
        "schema_version": 1,
        "created_at": created_at,
        "submission_id": submission_id,
        "url": url,
        "video": {
            "video_id": resolved_video_id,
            "title": metadata.get("title"),
            "channel_title": metadata.get("channel_title"),
            "published_at": metadata.get("published_at"),
            "duration_sec": metadata.get("duration_sec"),
        },
        "input": {
            "size": {"width": int(size[0]), "height": int(size[1])},
            "requested_variants": len(clean_variants),
            "model": model,
            "image_model": image_model,
            "ai_covers": bool(ai_covers),
            "skip_gemini": bool(skip_gemini),
        },
        "metadata": _metadata_for_mongo(metadata),
        "analysis": clean_analysis,
        "variants": clean_variants,
        "frames": to_plain_json(frames),
        "moments": to_plain_json(moments),
        "best_variant": best_variant,
        "artifacts": artifacts,
        "summary": to_plain_json(summary),
        "hashes": {
            "content_sha256": content_hash,
            "analysis_sha256": sha256_json(clean_analysis),
            "variants_sha256": sha256_json(clean_variants),
        },
    }
    return pack


def store_image_hero_pack(pack: dict[str, Any], *, uri: str | None = None) -> dict[str, Any]:
    collection = mongo_collection(uri=uri)
    doc = dict(pack)
    doc["_id"] = pack["pack_id"]
    result = collection.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    return {
        "enabled": True,
        "db": collection.database.name,
        "collection": collection.name,
        "pack_id": pack["pack_id"],
        "video_id": (pack.get("video") or {}).get("video_id"),
        "submission_id": pack.get("submission_id"),
        "upserted_id": str(result.upserted_id) if result.upserted_id is not None else None,
        "matched_count": result.matched_count,
        "modified_count": result.modified_count,
    }


def maybe_store_image_hero_pack(pack: dict[str, Any]) -> dict[str, Any]:
    if env_flag("MONGODB_DISABLED", False) or env_flag("INDEXFRAME_MONGODB_DISABLED", False):
        return {"enabled": False, "reason": "MongoDB disabled by env"}
    if not mongodb_uri_from_env():
        return {"enabled": False, "reason": "MONGODB_URI / INDEXFRAME_MONGODB_URI not set"}
    try:
        return store_image_hero_pack(pack)
    except Exception as exc:
        return {"enabled": False, "error": f"{exc.__class__.__name__}: {exc}"}


def list_image_hero_packs(video_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    cursor = mongo_collection().find(
        {"video.video_id": video_id},
        {
            "metadata.description": 0,
            "metadata.comments_sample": 0,
            "analysis": 0,
            "variants.generation_prompt": 0,
        },
    ).sort([("created_at", -1)]).limit(limit)

    docs: list[dict[str, Any]] = []
    for doc in cursor:
        doc.pop("_id", None)
        docs.append(doc)
    return docs


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Query stored IndexFrame Image Hero packs.")
    parser.add_argument("video_id", nargs="?", help="YouTube video id")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--ping", action="store_true")
    ns = parser.parse_args()

    if ns.ping:
        print(json.dumps(ping_mongodb(), indent=2, ensure_ascii=False))
    else:
        if not ns.video_id:
            parser.error("video_id is required unless --ping is used")
        print(json.dumps(list_image_hero_packs(ns.video_id, limit=ns.limit), indent=2, ensure_ascii=False))
