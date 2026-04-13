"""
app.py — FastAPI web application for UZ News Bot.

Deploy on Railway:
  - Set DB_PATH=/data/news.db (Railway Volume mounted at /data)
  - Set APP_PASSWORD=your_password
  - Set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY

Schedule (UTC): 01:00, 05:00, 09:00, 13:00, 16:00
= Uzbekistan time: 06:00, 10:00, 14:00, 18:00, 21:00
"""

import os
import sys
import hashlib
import sqlite3
import asyncio
import yaml
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# Import core functions from existing module
sys.path.insert(0, str(BASE_DIR / "src"))
from auto_collect import (
    init_db, load_config, fetch_source, cleanup_old_news,
    get_unsent_news, mark_sent, format_news_for_telegram,
    send_telegram, log, run as collect_run,
)

DB_PATH = Path(os.environ.get("DB_PATH", str(BASE_DIR / "data" / "news.db")))
PREFS_PATH = BASE_DIR / "config" / "preferences.yaml"
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


# ── MIGRATIONS ────────────────────────────────────────────────────────────

def run_migrations(conn: sqlite3.Connection):
    """Add new columns if they don't exist yet."""
    for sql in [
        "ALTER TABLE news ADD COLUMN tts_script TEXT",
        "ALTER TABLE news ADD COLUMN approved INTEGER DEFAULT 0",
        "ALTER TABLE news ADD COLUMN generated_at TEXT",
    ]:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # Column already exists
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()


# ── AUTH ──────────────────────────────────────────────────────────────────

def _token() -> str:
    return hashlib.sha256(APP_PASSWORD.encode()).hexdigest()

def check_auth(request: Request) -> bool:
    if not APP_PASSWORD:
        return True
    return request.cookies.get("auth_token", "") == _token()

def require_auth(request: Request):
    if not check_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── SCHEDULER ─────────────────────────────────────────────────────────────

async def scheduled_collect():
    log("=== Scheduled collect ===")
    try:
        await asyncio.to_thread(collect_run, notify=True, show_local=False)
    except Exception as e:
        log(f"Scheduler error: {e}")


scheduler = AsyncIOScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Init DB + migrate
    init_db()
    conn = sqlite3.connect(DB_PATH)
    run_migrations(conn)
    conn.close()

    # 06:00, 10:00, 14:00, 18:00, 21:00 Uzbekistan = 01:00, 05:00, 09:00, 13:00, 16:00 UTC
    for hour in [1, 5, 9, 13, 16]:
        scheduler.add_job(scheduled_collect, "cron", hour=hour, minute=0)
    scheduler.start()
    log(f"Scheduler started. DB: {DB_PATH}")

    yield

    scheduler.shutdown()


# ── APP ───────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan, title="UZ News Bot")

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ── AUTH PAGES ────────────────────────────────────────────────────────────

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UZ News Bot — Login</title>
<style>
  *{box-sizing:border-box}
  body{font-family:system-ui,sans-serif;display:flex;align-items:center;
       justify-content:center;min-height:100vh;margin:0;background:#f1f5f9}
  .card{background:#fff;padding:2rem;border-radius:14px;
        box-shadow:0 4px 20px rgba(0,0,0,.08);width:340px}
  h2{margin:0 0 1.5rem;font-size:1.2rem;color:#0f172a}
  input{width:100%;padding:.7rem .9rem;border:1px solid #e2e8f0;border-radius:8px;
        font-size:.95rem;margin-bottom:1rem;outline:none}
  input:focus{border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.15)}
  button{width:100%;padding:.75rem;background:#2563eb;color:#fff;border:none;
         border-radius:8px;font-size:.95rem;font-weight:600;cursor:pointer}
  button:hover{background:#1d4ed8}
  .err{color:#dc2626;font-size:.85rem;margin-bottom:.75rem}
</style>
</head>
<body>
<div class="card">
  <h2>UZ News Bot</h2>
  {error}
  <form method="POST" action="/login">
    <input type="password" name="password" placeholder="Password" autofocus>
    <button type="submit">Enter</button>
  </form>
</div>
</body>
</html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page(error: str = ""):
    err_html = '<div class="err">Wrong password</div>' if error else ""
    return HTMLResponse(_LOGIN_HTML.replace("{error}", err_html))


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    password = str(form.get("password", ""))
    if hashlib.sha256(password.encode()).hexdigest() == _token():
        response = RedirectResponse("/", status_code=302)
        response.set_cookie(
            "auth_token", _token(),
            max_age=30 * 24 * 3600,
            httponly=True,
            samesite="lax",
        )
        return response
    return RedirectResponse("/login?error=1", status_code=302)


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("auth_token")
    return response


# ── MAIN PAGE ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if APP_PASSWORD and not check_auth(request):
        return RedirectResponse("/login")
    index_path = static_dir / "index.html"
    with open(index_path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ── HEALTH ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "db": str(DB_PATH)}


# ── API: NEWS ─────────────────────────────────────────────────────────────

@app.get("/api/news")
async def api_news(request: Request, min_score: int = 0, limit: int = 100):
    require_auth(request)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, title, url, source, published, score,
               tts_script, approved, generated_at, collected, sent_tg
        FROM news
        WHERE score >= ?
        ORDER BY score DESC, collected DESC
        LIMIT ?
        """,
        (min_score, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/collect")
async def api_collect(request: Request, background_tasks: BackgroundTasks):
    require_auth(request)
    background_tasks.add_task(scheduled_collect)
    return {"status": "started"}


@app.post("/api/news/{news_id}/generate")
async def api_generate(news_id: int, request: Request):
    require_auth(request)
    return await _do_generate(news_id)


@app.post("/api/news/{news_id}/regenerate")
async def api_regenerate(news_id: int, request: Request):
    require_auth(request)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE news SET approved=0 WHERE id=?", (news_id,))
    conn.commit()
    conn.close()
    return await _do_generate(news_id)


@app.post("/api/news/{news_id}/approve")
async def api_approve(news_id: int, request: Request):
    require_auth(request)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE news SET approved=1 WHERE id=?", (news_id,))
    conn.commit()
    conn.close()
    return {"id": news_id, "approved": True}


@app.delete("/api/news/{news_id}")
async def api_delete(news_id: int, request: Request):
    require_auth(request)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM news WHERE id=?", (news_id,))
    conn.commit()
    conn.close()
    return {"id": news_id, "deleted": True}


# ── TTS GENERATION ────────────────────────────────────────────────────────

async def _do_generate(news_id: int) -> dict:
    # Load news item
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM news WHERE id=?", (news_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="News not found")
    item = dict(row)

    # Load TTS rules from preferences.yaml
    with open(PREFS_PATH, encoding="utf-8") as f:
        prefs = yaml.safe_load(f)
    style = prefs.get("content_style", {})
    tts_rules = style.get("tts_rules", "")
    tts_outro = style.get("tts_outro_rules", "")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not set")

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)

    prompt = (
        "Напиши TTS скрипт для этой новости строго по правилам ниже.\n\n"
        f"ПРАВИЛА TTS:\n{tts_rules}\n\n"
        f"ПРАВИЛА АУТРО:\n{tts_outro}\n\n"
        "НОВОСТЬ:\n"
        f"Заголовок: {item['title']}\n"
        f"Источник: {item.get('source', '')}\n"
        f"Описание: {item.get('summary', '') or ''}\n\n"
        "Верни ТОЛЬКО готовый скрипт — без пояснений, без заголовков. "
        "Только текст который будет произнесён голосом."
    )

    message = await client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    script = message.content[0].text.strip()

    # Save to DB
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE news SET tts_script=?, generated_at=? WHERE id=?",
        (script, datetime.now().isoformat(), news_id),
    )
    conn.commit()
    conn.close()

    return {"id": news_id, "tts_script": script}
