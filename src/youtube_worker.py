"""
youtube_worker.py — YouTube video upload via YouTube Data API v3.

ПЕРВЫЙ ЗАПУСК (один раз локально):
    python src/youtube_worker.py --auth
    → Откроется браузер для Google OAuth
    → После авторизации скопируй YOUTUBE_REFRESH_TOKEN в Railway Variables

ТРЕБОВАНИЯ в Google Cloud Console:
    1. Создай проект
    2. Включи YouTube Data API v3
    3. Создай OAuth 2.0 Credentials → Desktop App
    4. Скачай client_secret.json → добавь ID/SECRET в .env

ENV:
    YOUTUBE_CLIENT_ID       — из Google Cloud Console
    YOUTUBE_CLIENT_SECRET   — из Google Cloud Console
    YOUTUBE_REFRESH_TOKEN   — получается после --auth (сохраняется в data/)
    YOUTUBE_CHANNEL_ID      — ID твоего канала (UC...), для проверки
    YOUTUBE_DEFAULT_LANG    — ru | uz (default: ru)

ИСПОЛЬЗОВАНИЕ:
    from src.youtube_worker import upload_video
    media_id = upload_video(
        video_path="data/videos/clip.mp4",
        title="Заголовок видео",
        description="Описание\\n\\n#узбекистан",
        tags=["узбекистан", "новости"],
        language="ru",       # ru или uz
        news_id=42,          # ссылка на новость в БД
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
from datetime import datetime, timezone
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

DB_PATH      = Path(os.environ.get("DB_PATH", str(BASE_DIR / "data" / "news.db")))
TOKEN_PATH   = BASE_DIR / "data" / "youtube_token.json"
TOKEN_URL    = "https://oauth2.googleapis.com/token"
UPLOAD_URL   = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_API  = "https://www.googleapis.com/youtube/v3"
SCOPE        = "https://www.googleapis.com/auth/youtube.upload"

log = logging.getLogger("yt_worker")


# ── ENV HELPERS ───────────────────────────────────────────────────────────────

def _client_id() -> str:
    return (os.environ.get("YOUTUBE_CLIENT_ID") or "").strip()

def _client_secret() -> str:
    return (os.environ.get("YOUTUBE_CLIENT_SECRET") or "").strip()

def _channel_id() -> str:
    return (os.environ.get("YOUTUBE_CHANNEL_ID") or "").strip()


# ── DATABASE ──────────────────────────────────────────────────────────────────

YT_POSTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS yt_posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id         INTEGER,
    video_id        TEXT UNIQUE NOT NULL,
    title           TEXT,
    language        TEXT DEFAULT 'ru',
    status          TEXT DEFAULT 'published',
    url             TEXT,
    views           INTEGER DEFAULT 0,
    likes           INTEGER DEFAULT 0,
    comments        INTEGER DEFAULT 0,
    published_at    TEXT DEFAULT (datetime('now')),
    stats_updated   TEXT
)"""


def _init_table(conn: sqlite3.Connection) -> None:
    conn.execute(YT_POSTS_SCHEMA)
    conn.commit()


def _save_post(conn: sqlite3.Connection, news_id: int, video_id: str,
               title: str, language: str) -> None:
    url = f"https://youtu.be/{video_id}"
    conn.execute(
        """INSERT INTO yt_posts (news_id, video_id, title, language, url)
           VALUES (?,?,?,?,?)
           ON CONFLICT(video_id) DO NOTHING""",
        (news_id, video_id, title, language, url),
    )
    conn.commit()


# ── TOKEN MANAGEMENT ─────────────────────────────────────────────────────────

def _load_token() -> dict | None:
    """Load token from env var (Railway) or local file."""
    # 1. Try env var first (Railway production)
    raw = (os.environ.get("YOUTUBE_REFRESH_TOKEN") or "").strip()
    if raw:
        return {"refresh_token": raw, "source": "env"}

    # 2. Try local file (dev / first-time auth)
    if TOKEN_PATH.exists():
        try:
            return json.loads(TOKEN_PATH.read_text())
        except Exception:
            pass
    return None


def _refresh_access_token(refresh_token: str) -> str:
    """Exchange refresh token for a fresh access token."""
    r = requests.post(TOKEN_URL, data={
        "client_id":     _client_id(),
        "client_secret": _client_secret(),
        "refresh_token": refresh_token,
        "grant_type":    "refresh_token",
    }, timeout=15)
    data = r.json()
    if "access_token" not in data:
        raise RuntimeError(f"Token refresh failed: {data}")
    return data["access_token"]


def _get_access_token() -> str:
    """Get a valid access token, refreshing if needed."""
    token = _load_token()
    if not token:
        raise RuntimeError(
            "YouTube token not found. Run: python src/youtube_worker.py --auth"
        )
    return _refresh_access_token(token["refresh_token"])


# ── UPLOAD ────────────────────────────────────────────────────────────────────

def _build_metadata(title: str, description: str, tags: list[str],
                    language: str, category_id: str = "25") -> dict:
    """
    category_id:
      25 = News & Politics  (новости)
      22 = People & Blogs
      24 = Entertainment
    """
    snippet: dict = {
        "title":       title[:100],
        "description": description[:5000],
        "tags":        tags[:500],
        "categoryId":  category_id,
    }
    # YouTube defaultLanguage codes
    lang_map = {"ru": "ru", "uz": "uz"}
    if language in lang_map:
        snippet["defaultLanguage"] = lang_map[language]
        snippet["defaultAudioLanguage"] = lang_map[language]

    return {
        "snippet": snippet,
        "status": {
            "privacyStatus":            "public",
            "selfDeclaredMadeForKids":  False,
        },
    }


def upload_video(
    video_path: str | Path,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    language: str = "ru",
    category_id: str = "25",
    news_id: int = 0,
    db_conn: sqlite3.Connection | None = None,
) -> str:
    """
    Upload a local video to YouTube as a public video.
    Returns the YouTube video_id (e.g. 'dQw4w9WgXcQ').

    Raises RuntimeError on auth/upload failure.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    access_token = _get_access_token()
    metadata     = _build_metadata(title, description, tags or [], language, category_id)
    file_size    = video_path.stat().st_size

    log.info("Starting YouTube upload: %s (%d MB)", video_path.name, file_size // 1_048_576)

    # ── Step 1: Initiate resumable upload session ──
    init_r = requests.post(
        UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization":           f"Bearer {access_token}",
            "Content-Type":            "application/json; charset=UTF-8",
            "X-Upload-Content-Type":   "video/*",
            "X-Upload-Content-Length": str(file_size),
        },
        json=metadata,
        timeout=30,
    )
    if init_r.status_code != 200:
        raise RuntimeError(
            f"Upload init failed {init_r.status_code}: {init_r.text[:300]}"
        )
    upload_uri = init_r.headers.get("Location")
    if not upload_uri:
        raise RuntimeError("No upload URI in response headers")

    # ── Step 2: Upload the file ──
    CHUNK = 10 * 1024 * 1024  # 10 MB chunks
    video_id = None
    uploaded = 0

    with open(video_path, "rb") as fh:
        while uploaded < file_size:
            chunk = fh.read(CHUNK)
            end   = uploaded + len(chunk) - 1
            headers = {
                "Authorization":  f"Bearer {access_token}",
                "Content-Length": str(len(chunk)),
                "Content-Range":  f"bytes {uploaded}-{end}/{file_size}",
            }
            resp = requests.put(upload_uri, headers=headers,
                                data=chunk, timeout=120)

            if resp.status_code in (200, 201):
                data     = resp.json()
                video_id = data.get("id")
                log.info("Upload complete. video_id=%s", video_id)
                break
            elif resp.status_code == 308:
                # Resumable incomplete — update range
                rng = resp.headers.get("Range", f"bytes=0-{uploaded - 1}")
                uploaded = int(rng.split("-")[1]) + 1
                pct = uploaded / file_size * 100
                log.info("Upload progress: %.0f%%", pct)
            else:
                raise RuntimeError(
                    f"Chunk upload failed {resp.status_code}: {resp.text[:300]}"
                )

    if not video_id:
        raise RuntimeError("Upload finished but no video_id returned")

    # ── Step 3: Save to DB ──
    if db_conn is not None:
        _init_table(db_conn)
        _save_post(db_conn, news_id, video_id, title, language)
    elif DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        _init_table(conn)
        _save_post(conn, news_id, video_id, title, language)
        conn.close()

    url = f"https://youtu.be/{video_id}"
    log.info("YouTube video live: %s", url)
    return video_id


# ── DIAGNOSTICS ───────────────────────────────────────────────────────────────

def diagnose() -> dict:
    """Check YouTube config. Returns dict with status."""
    token = _load_token()
    client_ok = bool(_client_id() and _client_secret())

    result = {
        "client_id_set":     bool(_client_id()),
        "client_secret_set": bool(_client_secret()),
        "token_found":       token is not None,
        "token_source":      token.get("source", "file") if token else None,
        "channel_id":        _channel_id() or "(not set)",
        "ready":             False,
    }

    if token and client_ok:
        try:
            access_token = _get_access_token()
            r = requests.get(
                f"{YOUTUBE_API}/channels",
                params={"part": "snippet", "mine": "true",
                        "access_token": access_token},
                timeout=10,
            )
            data = r.json()
            items = data.get("items", [])
            if items:
                ch = items[0]["snippet"]
                result["channel_title"] = ch.get("title", "")
                result["ready"] = True
            else:
                result["error"] = "No channel found for this account"
        except Exception as e:
            result["error"] = str(e)

    return result


# ── AUTH FLOW (first-time setup) ──────────────────────────────────────────────

def auth_interactive() -> None:
    """
    One-time OAuth flow. Run locally to get refresh token.
    Then add YOUTUBE_REFRESH_TOKEN to Railway Variables.
    """
    if not _client_id() or not _client_secret():
        print("❌ YOUTUBE_CLIENT_ID и YOUTUBE_CLIENT_SECRET не заданы в .env")
        print("   Создай OAuth 2.0 credentials в Google Cloud Console:")
        print("   console.cloud.google.com → APIs → YouTube Data API v3 → Credentials")
        sys.exit(1)

    import urllib.parse, webbrowser, http.server, threading

    auth_code_holder: list[str] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            params = dict(urllib.parse.parse_qsl(
                urllib.parse.urlparse(self.path).query
            ))
            auth_code_holder.append(params.get("code", ""))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Authorized! Return to terminal.</h2>")
        def log_message(self, *a): pass

    server = http.server.HTTPServer(("localhost", 8765), _Handler)
    t = threading.Thread(target=server.handle_request)
    t.start()

    redirect_uri = "http://localhost:8765"
    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={_client_id()}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
        f"&response_type=code"
        f"&scope={urllib.parse.quote(SCOPE)}"
        "&access_type=offline"
        "&prompt=consent"
    )
    print(f"\n🔗 Opening browser for Google OAuth...\n{auth_url}\n")
    webbrowser.open(auth_url)
    t.join(timeout=120)

    code = auth_code_holder[0] if auth_code_holder else ""
    if not code:
        print("❌ No auth code received.")
        sys.exit(1)

    # Exchange code for tokens
    r = requests.post(TOKEN_URL, data={
        "code":          code,
        "client_id":     _client_id(),
        "client_secret": _client_secret(),
        "redirect_uri":  redirect_uri,
        "grant_type":    "authorization_code",
    }, timeout=15)
    token_data = r.json()

    if "refresh_token" not in token_data:
        print(f"❌ Token exchange failed: {token_data}")
        sys.exit(1)

    refresh_token = token_data["refresh_token"]

    # Save locally
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(token_data, indent=2))

    print("\n" + "="*60)
    print("✅ Авторизация успешна!")
    print("\n📋 Добавь в Railway Variables:")
    print(f"   YOUTUBE_REFRESH_TOKEN = {refresh_token}")
    print("="*60)
    print(f"\n(токен сохранён локально: {TOKEN_PATH})")


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
    elif "--test" in sys.argv:
        # python src/youtube_worker.py --test path/to/video.mp4
        idx = sys.argv.index("--test")
        path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        if not path:
            print("Usage: --test path/to/video.mp4")
            sys.exit(1)
        vid_id = upload_video(
            video_path=path,
            title="Test upload — MediaHub",
            description="Тестовая загрузка",
            tags=["тест"],
            language="ru",
        )
        print(f"✅ Uploaded: https://youtu.be/{vid_id}")
    else:
        print("Usage:")
        print("  python src/youtube_worker.py --auth       # первая авторизация")
        print("  python src/youtube_worker.py --diagnose   # проверить конфиг")
        print("  python src/youtube_worker.py --test video.mp4")
