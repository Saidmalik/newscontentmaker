"""
instagram_worker.py — Instagram Reels/Stories publishing + insights snapshots.

ENV:
  META_ACCESS_TOKEN    — System user permanent token (no expiry)
  INSTAGRAM_ACCOUNT_ID — IG Business Account ID (default: 17841477793587741)
"""

import os
import json
import asyncio
import sqlite3
import time
import logging
from pathlib import Path
from datetime import datetime

import httpx

log = logging.getLogger("ig_worker")

GRAPH_URL   = "https://graph.facebook.com/v19.0"
RUPLOAD_URL = "https://rupload.facebook.com/video-upload/v19.0"

IG_POSTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS ig_posts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id          INTEGER,
    media_id         TEXT UNIQUE NOT NULL,
    permalink        TEXT,
    published_at     TEXT DEFAULT (datetime('now')),
    caption          TEXT,
    post_type        TEXT DEFAULT 'REELS',
    stats_views      INTEGER DEFAULT 0,
    stats_reach      INTEGER DEFAULT 0,
    stats_likes      INTEGER DEFAULT 0,
    stats_comments   INTEGER DEFAULT 0,
    stats_shares     INTEGER DEFAULT 0,
    stats_saves      INTEGER DEFAULT 0,
    stats_watch_time TEXT,
    stats_updated_at TEXT,
    stats_24h        TEXT,
    stats_48h        TEXT,
    stats_72h        TEXT
)"""


# ── DB helpers ────────────────────────────────────────────────────────────────

def init_table(conn: sqlite3.Connection) -> None:
    conn.execute(IG_POSTS_SCHEMA)
    conn.commit()


def save_post(conn: sqlite3.Connection, news_id: int, media_id: str,
              permalink: str, caption: str, post_type: str = "REELS") -> None:
    conn.execute(
        """INSERT INTO ig_posts (news_id, media_id, permalink, caption, post_type)
           VALUES (?,?,?,?,?)
           ON CONFLICT(media_id) DO UPDATE SET
               news_id=excluded.news_id, permalink=excluded.permalink""",
        (news_id, media_id, permalink, caption, post_type),
    )
    conn.commit()


# ── Auth ──────────────────────────────────────────────────────────────────────

def _token() -> str:
    return os.environ.get("META_ACCESS_TOKEN", "")

def _ig_id() -> str:
    """Return first non-empty INSTAGRAM_ACCOUNT_ID variant."""
    for key in ("INSTAGRAM_ACCOUNT_ID", "INSTAGRAM_ACCOUNT_ID_"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return v
    return "17841477793587741"


# ── Resumable upload helpers ──────────────────────────────────────────────────

async def _init_container(caption: str, post_type: str) -> tuple[str, str]:
    """Create upload container. Returns (container_id, upload_uri)."""
    params: dict = {
        "access_token": _token(),
        "media_type":   post_type,
        "upload_type":  "resumable",
    }
    if post_type == "REELS" and caption:
        params["caption"] = caption[:2200]  # IG caption limit

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{GRAPH_URL}/{_ig_id()}/media", data=params)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Container init error: {data['error']['message']}")
    container_id = data["id"]
    upload_uri   = data.get("uri", f"{RUPLOAD_URL}/{container_id}")
    return container_id, upload_uri


async def _upload_file(upload_uri: str, file_path: Path, mime: str) -> None:
    """POST raw bytes to the resumable upload URI."""
    size = file_path.stat().st_size
    with open(file_path, "rb") as fh:
        content = fh.read()
    async with httpx.AsyncClient(timeout=600) as c:  # 10 min for large videos
        r = await c.post(
            upload_uri,
            content=content,
            headers={
                "Authorization": f"OAuth {_token()}",
                "offset":        "0",
                "file_size":     str(size),
                "Content-Type":  mime,
            },
        )
    if not r.is_success:
        raise RuntimeError(f"Upload failed {r.status_code}: {r.text[:300]}")


async def _wait_container(container_id: str, timeout_s: int = 300) -> None:
    """Poll container status until FINISHED (or error/timeout)."""
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=15) as c:
        while time.monotonic() < deadline:
            r  = await c.get(
                f"{GRAPH_URL}/{container_id}",
                params={"fields": "status_code", "access_token": _token()},
            )
            sc = r.json().get("status_code", "")
            if sc == "FINISHED":
                return
            if sc in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Container {container_id} status: {sc}")
            await asyncio.sleep(5)
    raise TimeoutError(f"Container not ready in {timeout_s}s")


async def _publish_container(container_id: str) -> str:
    """Publish container → returns live media_id."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{GRAPH_URL}/{_ig_id()}/media_publish",
            data={"creation_id": container_id, "access_token": _token()},
        )
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Publish error: {data['error']['message']}")
    return data["id"]


async def _get_permalink(media_id: str) -> str:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"{GRAPH_URL}/{media_id}",
            params={"fields": "permalink", "access_token": _token()},
        )
    return r.json().get("permalink", "")


# ── Public API ────────────────────────────────────────────────────────────────

async def publish_reel(
    news_id: int,
    video_path: str | Path,
    caption: str,
    db_conn: sqlite3.Connection | None = None,
) -> str:
    """Upload a local video and publish as an Instagram Reel. Returns media_id."""
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    container_id, uri = await _init_container(caption, "REELS")
    await _upload_file(uri, video_path, "video/mp4")
    await _wait_container(container_id)
    media_id  = await _publish_container(container_id)
    permalink = await _get_permalink(media_id)

    if db_conn is not None:
        save_post(db_conn, news_id, media_id, permalink, caption, "REELS")

    log.info("Reel published: %s  %s", media_id, permalink)
    return media_id


async def publish_story(
    image_path: str | Path,
    db_conn: sqlite3.Connection | None = None,
) -> str:
    """Upload a local image and publish as an Instagram Story. Returns media_id."""
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    ext  = image_path.suffix.lower()
    mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"

    container_id, uri = await _init_container("", "STORIES")
    await _upload_file(uri, image_path, mime)
    await _wait_container(container_id)
    media_id  = await _publish_container(container_id)
    permalink = await _get_permalink(media_id)

    if db_conn is not None:
        save_post(db_conn, 0, media_id, permalink, "", "STORIES")

    log.info("Story published: %s", media_id)
    return media_id


async def get_insights(media_id: str) -> dict:
    """Fetch live insights for a published Reel. Returns unified metrics dict."""
    metrics = "plays,reach,saved,shares,ig_reels_avg_watch_time,total_interactions"
    async with httpx.AsyncClient(timeout=30) as c:
        r_ins = await c.get(
            f"{GRAPH_URL}/{media_id}/insights",
            params={"metric": metrics, "access_token": _token()},
        )
        r_med = await c.get(
            f"{GRAPH_URL}/{media_id}",
            params={"fields": "like_count,comments_count", "access_token": _token()},
        )
    ins    = {i["name"]: i.get("value", 0) for i in r_ins.json().get("data", [])}
    med    = r_med.json()
    avg_ms = ins.get("ig_reels_avg_watch_time", 0)
    return {
        "plays":         ins.get("plays", 0),
        "reach":         ins.get("reach", 0),
        "saved":         ins.get("saved", 0),
        "shares":        ins.get("shares", 0),
        "likes":         med.get("like_count", 0),
        "comments":      med.get("comments_count", 0),
        "watch_time_ms": avg_ms,
        "watch_time":    f"{avg_ms / 1000:.1f}с" if avg_ms else "",
    }


# ── Hourly snapshot job ───────────────────────────────────────────────────────

async def run_snapshot_jobs(db_path: Path) -> int:
    """
    Check ig_posts for posts that have passed 24/48/72h since publication
    but have not yet had their stats snapshot saved. Fetches and stores each.
    Returns count of snapshots saved.
    """
    conn = sqlite3.connect(str(db_path))
    init_table(conn)
    rows = conn.execute(
        "SELECT id, media_id, published_at, stats_24h, stats_48h, stats_72h FROM ig_posts"
    ).fetchall()

    now   = datetime.utcnow()
    saved = 0

    for (row_id, media_id, pub_str, s24, s48, s72) in rows:
        try:
            pub = datetime.fromisoformat(pub_str) if pub_str else None
            if not pub:
                continue
            age_h = (now - pub).total_seconds() / 3600

            # Take each snapshot exactly once, in order: 24 → 48 → 72
            which: str | None = None
            if   age_h >= 72 and not s72: which = "stats_72h"
            elif age_h >= 48 and not s48: which = "stats_48h"
            elif age_h >= 24 and not s24: which = "stats_24h"

            if not which:
                continue

            m = await get_insights(media_id)

            conn.execute(
                f"""UPDATE ig_posts SET {which}=?,
                        stats_views=?,     stats_reach=?,   stats_likes=?,
                        stats_comments=?,  stats_shares=?,  stats_saves=?,
                        stats_watch_time=?, stats_updated_at=?
                    WHERE id=?""",
                (
                    json.dumps(m),
                    m["plays"], m["reach"],   m["likes"],
                    m["comments"], m["shares"], m["saved"],
                    m["watch_time"], datetime.utcnow().isoformat(),
                    row_id,
                ),
            )
            conn.commit()
            saved += 1
            log.info("Snapshot %s saved for %s", which, media_id)

        except Exception as e:
            log.error("Snapshot error %s: %s", media_id, e)

    conn.close()
    return saved
