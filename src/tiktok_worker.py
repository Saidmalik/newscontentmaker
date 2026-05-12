"""
tiktok_worker.py — TikTok Content Posting API v2.

ПЕРВЫЙ ЗАПУСК (один раз локально):
    python src/tiktok_worker.py --auth
    → Откроет браузер для TikTok OAuth
    → Скопируй TIKTOK_ACCESS_TOKEN + TIKTOK_REFRESH_TOKEN в Railway Variables

ТРЕБОВАНИЯ:
    1. Создай аккаунт на developers.tiktok.com (через VPN если нужно)
    2. Создай App → включи "Content Posting API"
    3. Добавь redirect URI: http://localhost:8766
    4. Скопируй Client Key и Client Secret в .env
    5. Запусти --auth один раз

ENV:
    TIKTOK_CLIENT_KEY       — из TikTok for Developers
    TIKTOK_CLIENT_SECRET    — из TikTok for Developers
    TIKTOK_ACCESS_TOKEN     — получается после --auth (истекает через 24ч)
    TIKTOK_REFRESH_TOKEN    — для обновления access token (90 дней)
    TIKTOK_OPEN_ID          — ID аккаунта (получается при авторизации)

ОГРАНИЧЕНИЯ TikTok API:
    - Видео: mp4, mov, webm
    - Длина: 3 секунды — 10 минут
    - Размер: до 4 GB
    - Aspect ratio: 9:16, 1:1, или 16:9
    - Первые 24ч после публикации: видео в "processing" (ждём)

ИСПОЛЬЗОВАНИЕ:
    from src.tiktok_worker import upload_video
    video_id = upload_video(
        video_path="data/videos/clip.mp4",
        title="Заголовок видео #узбекистан",
        news_id=42,
    )
"""

import os
import sys
import json
import time
import sqlite3
import logging
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

DB_PATH    = Path(os.environ.get("DB_PATH", str(BASE_DIR / "data" / "news.db")))
TOKEN_PATH = BASE_DIR / "data" / "tiktok_token.json"

# TikTok API v2 endpoints
AUTH_URL    = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL   = "https://open.tiktokapis.com/v2/oauth/token/"
UPLOAD_URL  = "https://open.tiktokapis.com/v2/post/video/init/"
PUBLISH_URL = "https://open.tiktokapis.com/v2/post/video/publish/"
STATUS_URL  = "https://open.tiktokapis.com/v2/post/video/status/fetch/"
SCOPE       = "video.upload,video.publish"

log = logging.getLogger("tt_worker")


# ── ENV HELPERS ───────────────────────────────────────────────────────────────

def _client_key() -> str:
    return (os.environ.get("TIKTOK_CLIENT_KEY") or "").strip()

def _client_secret() -> str:
    return (os.environ.get("TIKTOK_CLIENT_SECRET") or "").strip()

def _open_id() -> str:
    return (os.environ.get("TIKTOK_OPEN_ID") or "").strip()


# ── DATABASE ──────────────────────────────────────────────────────────────────

TT_POSTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tt_posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id         INTEGER,
    publish_id      TEXT UNIQUE,
    video_id        TEXT,
    title           TEXT,
    language        TEXT DEFAULT 'ru',
    status          TEXT DEFAULT 'processing',
    url             TEXT,
    views           INTEGER DEFAULT 0,
    likes           INTEGER DEFAULT 0,
    comments        INTEGER DEFAULT 0,
    shares          INTEGER DEFAULT 0,
    published_at    TEXT DEFAULT (datetime('now')),
    stats_updated   TEXT
)"""


def _init_table(conn: sqlite3.Connection) -> None:
    conn.execute(TT_POSTS_SCHEMA)
    conn.commit()


def _save_post(conn: sqlite3.Connection, news_id: int, publish_id: str,
               title: str, language: str) -> None:
    conn.execute(
        """INSERT INTO tt_posts (news_id, publish_id, title, language)
           VALUES (?,?,?,?)
           ON CONFLICT(publish_id) DO NOTHING""",
        (news_id, publish_id, title, language),
    )
    conn.commit()


# ── TOKEN MANAGEMENT ─────────────────────────────────────────────────────────

def _load_token() -> dict | None:
    """Load token from env vars (Railway) or local file."""
    access  = (os.environ.get("TIKTOK_ACCESS_TOKEN") or "").strip()
    refresh = (os.environ.get("TIKTOK_REFRESH_TOKEN") or "").strip()
    open_id = _open_id()

    if access and refresh and open_id:
        return {
            "access_token":  access,
            "refresh_token": refresh,
            "open_id":       open_id,
            "source":        "env",
        }

    if TOKEN_PATH.exists():
        try:
            return json.loads(TOKEN_PATH.read_text())
        except Exception:
            pass
    return None


def _refresh_access_token(refresh_token: str) -> dict:
    """Use refresh_token to get new access_token."""
    r = requests.post(TOKEN_URL, data={
        "client_key":    _client_key(),
        "client_secret": _client_secret(),
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
    }, timeout=15)
    data = r.json().get("data", {})
    if not data.get("access_token"):
        raise RuntimeError(f"TikTok token refresh failed: {r.text[:300]}")
    return data


def _get_access_token() -> tuple[str, str]:
    """Returns (access_token, open_id). Auto-refreshes if expired."""
    token = _load_token()
    if not token:
        raise RuntimeError(
            "TikTok token not found. Run: python src/tiktok_worker.py --auth"
        )

    access  = token.get("access_token", "")
    refresh = token.get("refresh_token", "")
    open_id = token.get("open_id", "")

    # If token from env — try to use as-is, refresh if needed
    if not access or not open_id:
        new = _refresh_access_token(refresh)
        access  = new["access_token"]
        open_id = new.get("open_id", open_id)

    return access, open_id


# ── UPLOAD ────────────────────────────────────────────────────────────────────

def upload_video(
    video_path: str | Path,
    title: str,
    language: str = "ru",
    news_id: int = 0,
    db_conn: sqlite3.Connection | None = None,
    privacy: str = "PUBLIC_TO_EVERYONE",
) -> str:
    """
    Upload video to TikTok using Content Posting API.
    Returns publish_id.

    privacy: PUBLIC_TO_EVERYONE | MUTUAL_FOLLOW_FRIENDS | FOLLOWER_OF_CREATOR | SELF_ONLY
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    access_token, open_id = _get_access_token()
    file_size = video_path.stat().st_size
    headers   = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/json; charset=UTF-8",
    }

    log.info("TikTok upload start: %s (%d MB)", video_path.name, file_size // 1_048_576)

    # ── Step 1: Init upload ──
    init_payload = {
        "post_info": {
            "title":           title[:2200],
            "privacy_level":   privacy,
            "disable_duet":    False,
            "disable_comment": False,
            "disable_stitch":  False,
        },
        "source_info": {
            "source":         "FILE_UPLOAD",
            "video_size":     file_size,
            "chunk_size":     10 * 1024 * 1024,  # 10 MB
            "total_chunk_count": max(1, (file_size + 10*1024*1024 - 1) // (10*1024*1024)),
        },
    }
    r = requests.post(UPLOAD_URL, headers=headers, json=init_payload, timeout=30)
    resp = r.json()
    if resp.get("error", {}).get("code") not in ("ok", ""):
        err = resp.get("error", {})
        raise RuntimeError(f"TikTok init error: {err.get('message','unknown')} ({err.get('code','')})")

    data       = resp.get("data", {})
    publish_id = data.get("publish_id")
    upload_url = data.get("upload_url")

    if not publish_id or not upload_url:
        raise RuntimeError(f"TikTok init: missing publish_id or upload_url: {resp}")

    log.info("TikTok publish_id: %s", publish_id)

    # ── Step 2: Upload chunks ──
    CHUNK_SIZE = 10 * 1024 * 1024
    chunk_num  = 0

    with open(video_path, "rb") as fh:
        offset = 0
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            end = offset + len(chunk) - 1
            upload_headers = {
                "Content-Range":   f"bytes {offset}-{end}/{file_size}",
                "Content-Length":  str(len(chunk)),
                "Content-Type":    "video/mp4",
            }
            resp_up = requests.put(
                upload_url, headers=upload_headers,
                data=chunk, timeout=120,
            )
            if resp_up.status_code not in (200, 201, 206):
                raise RuntimeError(
                    f"TikTok chunk {chunk_num} failed: {resp_up.status_code} {resp_up.text[:200]}"
                )
            offset   += len(chunk)
            chunk_num += 1
            pct = offset / file_size * 100
            log.info("TikTok upload: %.0f%%", pct)

    log.info("TikTok upload complete, publish_id=%s", publish_id)

    # ── Step 3: Save to DB ──
    if db_conn is not None:
        _init_table(db_conn)
        _save_post(db_conn, news_id, publish_id, title, language)
    elif DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        _init_table(conn)
        _save_post(conn, news_id, publish_id, title, language)
        conn.close()

    return publish_id


def check_status(publish_id: str) -> dict:
    """Check publishing status of an uploaded video."""
    access_token, _ = _get_access_token()
    r = requests.post(
        STATUS_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type":  "application/json",
        },
        json={"publish_id": publish_id},
        timeout=15,
    )
    return r.json().get("data", {})


# ── DIAGNOSTICS ───────────────────────────────────────────────────────────────

def diagnose() -> dict:
    """Check TikTok config status."""
    token = _load_token()
    result = {
        "client_key_set":    bool(_client_key()),
        "client_secret_set": bool(_client_secret()),
        "token_found":       token is not None,
        "token_source":      token.get("source", "file") if token else None,
        "open_id":           (token or {}).get("open_id", ""),
        "ready":             False,
    }
    if token:
        try:
            access_token, open_id = _get_access_token()
            # Test: get creator info
            r = requests.post(
                "https://open.tiktokapis.com/v2/post/video/creator_info/query/",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type":  "application/json",
                },
                timeout=10,
            )
            data = r.json().get("data", {})
            result["creator_username"] = data.get("creator_username", "")
            result["creator_nickname"] = data.get("creator_nickname", "")
            result["ready"] = True
        except Exception as e:
            result["error"] = str(e)
    return result


# ── AUTH FLOW ─────────────────────────────────────────────────────────────────

def auth_interactive() -> None:
    """
    One-time OAuth flow. Run locally with VPN if needed.
    Saves tokens to data/tiktok_token.json and prints Railway vars.
    """
    if not _client_key() or not _client_secret():
        print("❌ TIKTOK_CLIENT_KEY и TIKTOK_CLIENT_SECRET не заданы в .env")
        print("   1. Зайди на developers.tiktok.com (через VPN)")
        print("   2. Создай App → включи Content Posting API")
        print("   3. Добавь redirect URI: http://localhost:8766")
        print("   4. Скопируй Client Key и Secret в .env")
        sys.exit(1)

    import urllib.parse, webbrowser, http.server, threading

    code_holder: list[str] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            params = dict(urllib.parse.parse_qsl(
                urllib.parse.urlparse(self.path).query
            ))
            code_holder.append(params.get("code", ""))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>TikTok authorized! Return to terminal.</h2>")
        def log_message(self, *a): pass

    server = http.server.HTTPServer(("localhost", 8766), _Handler)
    t = threading.Thread(target=server.handle_request)
    t.start()

    redirect_uri = "http://localhost:8766"
    code_verifier  = os.urandom(32).hex()  # simple PKCE verifier

    auth_url = (
        f"{AUTH_URL}"
        f"?client_key={_client_key()}"
        f"&response_type=code"
        f"&scope={urllib.parse.quote(SCOPE)}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
        f"&state=mediahub"
    )
    print(f"\n🔗 Opening browser for TikTok OAuth...\n{auth_url}\n")
    webbrowser.open(auth_url)
    t.join(timeout=120)

    code = code_holder[0] if code_holder else ""
    if not code:
        print("❌ No auth code received.")
        sys.exit(1)

    # Exchange code for tokens
    r = requests.post(TOKEN_URL, data={
        "client_key":    _client_key(),
        "client_secret": _client_secret(),
        "code":          code,
        "grant_type":    "authorization_code",
        "redirect_uri":  redirect_uri,
    }, timeout=15)
    resp = r.json()
    data = resp.get("data", {})

    if not data.get("access_token"):
        print(f"❌ Token exchange failed: {resp}")
        sys.exit(1)

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(data, indent=2))

    access_token  = data["access_token"]
    refresh_token = data.get("refresh_token", "")
    open_id       = data.get("open_id", "")

    print("\n" + "="*60)
    print("✅ TikTok авторизация успешна!")
    print("\n📋 Добавь в Railway Variables:")
    print(f"   TIKTOK_ACCESS_TOKEN  = {access_token}")
    print(f"   TIKTOK_REFRESH_TOKEN = {refresh_token}")
    print(f"   TIKTOK_OPEN_ID       = {open_id}")
    print("="*60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-16s %(levelname)s %(message)s"
    )
    if "--auth" in sys.argv:
        auth_interactive()
    elif "--diagnose" in sys.argv:
        import pprint
        pprint.pprint(diagnose())
    elif "--status" in sys.argv:
        idx = sys.argv.index("--status")
        pid = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        if pid:
            import pprint
            pprint.pprint(check_status(pid))
    else:
        print("Usage:")
        print("  python src/tiktok_worker.py --auth             # первая авторизация")
        print("  python src/tiktok_worker.py --diagnose         # проверить конфиг")
        print("  python src/tiktok_worker.py --status <pub_id>  # статус публикации")
