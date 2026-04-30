"""
app.py — FastAPI web application for UZ News Bot.

Railway env vars needed:
  DB_PATH=/data/news.db
  APP_PASSWORD=...
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=...
  ANTHROPIC_API_KEY=...
  OPENAI_API_KEY=...            (для GPT-review, опционально)
  ELEVENLABS_API_KEY_1=...
  ELEVENLABS_VOICE_ID_1=...
  ELEVENLABS_API_KEY_2=...      (второй аккаунт, опционально)
  ELEVENLABS_VOICE_ID_2=...

Schedule UTC: 01:00, 05:00, 09:00, 13:00, 16:00
= Узбекистан: 06:00, 10:00, 14:00, 18:00, 21:00
"""

import os
import re
import sys
import json
import hashlib
import sqlite3
import asyncio
import yaml
import requests as req_lib
from pathlib import Path
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

sys.path.insert(0, str(BASE_DIR / "src"))
from auto_collect import (
    init_db, log, run as collect_run,
)

DB_PATH        = Path(os.environ.get("DB_PATH", str(BASE_DIR / "data" / "news.db")))
VIDEOS_DIR     = DB_PATH.parent / "videos"
PREFS_PATH     = BASE_DIR / "config" / "preferences.yaml"
SYSPROMPT_PATH = BASE_DIR / "config" / "system_prompt.md"
APP_PASSWORD   = os.environ.get("APP_PASSWORD", "")
CAPCUT_PATH    = os.environ.get("CAPCUT_EXPORT_PATH", "")


def _load_system_prompt() -> str:
    """Load content instructions from config/system_prompt.md.
    Falls back to claude_system_prompt in preferences.yaml."""
    if SYSPROMPT_PATH.exists():
        return SYSPROMPT_PATH.read_text(encoding="utf-8").strip()
    try:
        with open(PREFS_PATH, encoding="utf-8") as f:
            prefs = yaml.safe_load(f)
        return prefs.get("claude_system_prompt", "").strip()
    except Exception:
        return ""


# ── MIGRATIONS ────────────────────────────────────────────────────────────

def run_migrations(conn: sqlite3.Connection):
    for sql in [
        "ALTER TABLE news ADD COLUMN tts_script TEXT",
        "ALTER TABLE news ADD COLUMN approved INTEGER DEFAULT 0",
        "ALTER TABLE news ADD COLUMN generated_at TEXT",
        "ALTER TABLE news ADD COLUMN reviewed_script TEXT",
        "ALTER TABLE news ADD COLUMN gpt_comment TEXT",
        "ALTER TABLE news ADD COLUMN description TEXT",
        "ALTER TABLE news ADD COLUMN preview_titles TEXT",
        "ALTER TABLE news ADD COLUMN published_at TEXT",
        "ALTER TABLE news ADD COLUMN stats_views INTEGER DEFAULT 0",
        "ALTER TABLE news ADD COLUMN stats_likes INTEGER DEFAULT 0",
        "ALTER TABLE news ADD COLUMN stats_comments INTEGER DEFAULT 0",
        "ALTER TABLE news ADD COLUMN stats_shares INTEGER DEFAULT 0",
        "ALTER TABLE news ADD COLUMN stats_saves INTEGER DEFAULT 0",
        "ALTER TABLE news ADD COLUMN stats_reach INTEGER DEFAULT 0",
        "ALTER TABLE news ADD COLUMN stats_watch_time TEXT",
        "ALTER TABLE news ADD COLUMN stats_platform TEXT DEFAULT 'instagram'",
        "ALTER TABLE news ADD COLUMN instagram_media_id TEXT",
        "ALTER TABLE news ADD COLUMN instagram_permalink TEXT",
        "ALTER TABLE news ADD COLUMN starred INTEGER DEFAULT 0",
        "ALTER TABLE news ADD COLUMN preselected INTEGER DEFAULT 0",
        # Telegram auto-channel fields
        "ALTER TABLE news ADD COLUMN tg_auto_published INTEGER DEFAULT 0",
        "ALTER TABLE news ADD COLUMN tg_manual_published INTEGER DEFAULT 0",
        "ALTER TABLE news ADD COLUMN tg_published_at TEXT",
        "ALTER TABLE news ADD COLUMN tg_message_id INTEGER",
        "ALTER TABLE news ADD COLUMN tg_short_post TEXT",
        "ALTER TABLE news ADD COLUMN tg_long_post TEXT",
        "ALTER TABLE news ADD COLUMN tg_topic_hash TEXT",
    ]:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass

    conn.execute("""
        CREATE TABLE IF NOT EXISTS post_snapshots (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id        INTEGER NOT NULL,
            snapshot_at    TEXT NOT NULL,
            views          INTEGER DEFAULT 0,
            reach          INTEGER DEFAULT 0,
            likes          INTEGER DEFAULT 0,
            comments       INTEGER DEFAULT 0,
            saves          INTEGER DEFAULT 0,
            shares         INTEGER DEFAULT 0,
            avg_watch_time REAL,
            recorded_at    TEXT DEFAULT (datetime('now')),
            UNIQUE(post_id, snapshot_at)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ps_post ON post_snapshots(post_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS post_analysis (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id              INTEGER UNIQUE NOT NULL,
            hook_score           TEXT,
            hook_feedback        TEXT,
            watch_time_verdict   TEXT,
            cta_verdict          TEXT,
            main_recommendation  TEXT,
            predicted_next       TEXT,
            raw_response         TEXT,
            analyzed_at          TEXT DEFAULT (datetime('now'))
        )
    """)

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


# ── TELEGRAM NOTIFY ───────────────────────────────────────────────────────

def _tg_notify(text: str):
    """Send a plain-text message to TELEGRAM_CHAT_ID."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        req_lib.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        log(f"TG notify error: {e}")


# ── SCHEDULER STATE ───────────────────────────────────────────────────────

_collect_state: dict = {
    "last_run":    None,   # ISO string UTC
    "last_count":  None,   # int — how many new items last run found
    "last_error":  None,   # str — last error message if any
    "running":     False,  # is a collect currently in progress
}

# ── SCHEDULER ─────────────────────────────────────────────────────────────

async def scheduled_collect():
    log("=== Scheduled collect ===")
    _collect_state["running"] = True
    try:
        # Snapshot existing IDs before run
        conn = sqlite3.connect(DB_PATH)
        before_ids = {r[0] for r in conn.execute("SELECT id FROM news").fetchall()}
        conn.close()

        await asyncio.to_thread(collect_run, notify=True, show_local=False)

        # Find new items
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        if before_ids:
            placeholders = ",".join("?" * len(before_ids))
            new_rows = conn.execute(
                f"SELECT id, title, score FROM news WHERE id NOT IN ({placeholders}) ORDER BY score DESC",
                tuple(before_ids),
            ).fetchall()
        else:
            new_rows = conn.execute(
                "SELECT id, title, score FROM news ORDER BY score DESC"
            ).fetchall()
        conn.close()

        new_items = [dict(r) for r in new_rows]
        new_count = len(new_items)
        now_uzt   = datetime.utcnow() + timedelta(hours=5)

        _collect_state["last_run"]   = datetime.utcnow().isoformat()
        _collect_state["last_count"] = new_count
        _collect_state["last_error"] = None

        if new_count > 0:
            top = new_items[:3]
            msg = f"✅ {now_uzt.strftime('%H:%M')} | Собрано: +{new_count} новостей\n\n"
            for item in top:
                t = item["title"][:55] + ("…" if len(item["title"]) > 55 else "")
                msg += f"★{item['score']}  {t}\n"
        else:
            msg = f"🔁 {now_uzt.strftime('%H:%M')} | Новых новостей нет"

        _tg_notify(msg)

    except Exception as e:
        log(f"Scheduler error: {e}")
        _collect_state["last_error"] = str(e)[:200]
        if not _collect_state["last_run"]:
            _collect_state["last_run"] = datetime.utcnow().isoformat()
        _tg_notify(f"❌ Ошибка сбора: {str(e)[:120]}")
    finally:
        _collect_state["running"] = False


scheduler = AsyncIOScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    run_migrations(conn)
    from src.instagram_worker import init_table as _ig_init
    _ig_init(conn)
    conn.close()

    for hour in [1, 5, 9, 13, 16]:
        scheduler.add_job(scheduled_collect, "cron", hour=hour, minute=0)
    # Every 6h: auto-link new IG reels to news posts by timestamp
    scheduler.add_job(_scheduled_auto_sync, "cron", hour="*/6", minute=45)
    # Daily Instagram live-stats refresh at 02:00 UTC (07:00 UZT)
    scheduler.add_job(_scheduled_ig_refresh, "cron", hour=2, minute=0)
    # Hourly 24/48/72h snapshot check (at :15 to avoid overlap with other jobs)
    scheduler.add_job(_scheduled_ig_snapshots, "cron", minute=15)
    # Daily cleanup of old unreviewed news at 00:30 UTC (05:30 UZT)
    scheduler.add_job(_scheduled_cleanup, "cron", hour=0, minute=30)
    # TG auto-post: 09:00, 12:00, 17:00, 20:00 UZT = 04:00, 07:00, 12:00, 15:00 UTC
    scheduler.add_job(_scheduled_tg_auto, "cron", hour="4,7,12,15", minute=0)
    scheduler.start()
    log(f"Scheduler started. DB: {DB_PATH}")

    # Telegram auto-channel scheduler (запускается если TG_API_ID задан)
    tg_sched = None
    if os.environ.get("TG_API_ID"):
        try:
            from src.tg_scheduler import start_tg_scheduler
            tg_sched = start_tg_scheduler()
            log("TG Scheduler started ✓")
        except Exception as e:
            log(f"TG Scheduler init failed (OK if session not set up): {e}")

    yield

    scheduler.shutdown()
    if tg_sched:
        tg_sched.shutdown()


# ── APP ───────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan, title="UZ News Bot")

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ── AUTH PAGES ────────────────────────────────────────────────────────────

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>UZ News Bot</title>
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
  <h2>UZ News Bot</h2>{error}
  <form method="POST" action="/login">
    <input type="password" name="password" placeholder="Password" autofocus>
    <button type="submit">Enter</button>
  </form>
</div></body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page(error: str = ""):
    err = '<div class="err">Неверный пароль</div>' if error else ""
    return HTMLResponse(_LOGIN_HTML.replace("{error}", err))


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    pwd  = str(form.get("password", ""))
    if hashlib.sha256(pwd.encode()).hexdigest() == _token():
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("auth_token", _token(), max_age=30*24*3600, httponly=True, samesite="lax")
        return resp
    return RedirectResponse("/login?error=1", status_code=302)


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("auth_token")
    return resp


# ── PAGES ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if APP_PASSWORD and not check_auth(request):
        return RedirectResponse("/login")
    with open(static_dir / "index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/health")
async def health():
    return {"status": "ok", "db": str(DB_PATH)}


# ── API: NEWS ─────────────────────────────────────────────────────────────

def _score_to_priority(score: int) -> str:
    if score >= 6: return "hot"
    if score >= 3: return "good"
    return "reserve"


@app.get("/api/news/counts")
async def api_news_counts(request: Request):
    require_auth(request)
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT score, starred, preselected FROM news WHERE score >= 1").fetchall()
    conn.close()
    counts = {"all": 0, "hot": 0, "good": 0, "reserve": 0, "starred": 0, "preselected": 0}
    for (score, starred, preselected) in rows:
        p = _score_to_priority(score)
        counts[p] += 1
        counts["all"] += 1
        if starred:
            counts["starred"] += 1
        if preselected:
            counts["preselected"] += 1
    return counts


@app.get("/api/news")
async def api_news(
    request: Request,
    min_score: int = 1,
    limit: int = 150,
    tab: str = "all",       # all | hot | good | reserve | approved | preselected | starred
    sort: str = "score",    # score | date
    date_from: str = "",    # YYYY-MM-DD
    date_to: str = "",      # YYYY-MM-DD
):
    require_auth(request)

    where = ["score >= ?"]
    params: list = [min_score]

    if tab == "starred":
        where.append("starred = 1 AND published_at IS NULL")
    elif tab == "preselected":
        where.append("preselected = 1 AND published_at IS NULL")
    elif tab == "approved":
        where.append("approved = 1 AND published_at IS NULL")
    elif tab == "published":
        where.append("published_at IS NOT NULL")
    elif tab == "hot":
        where.append("score >= 6")
    elif tab == "good":
        where.append("score >= 3 AND score < 6")
    elif tab == "reserve":
        where.append("score >= 1 AND score < 3")

    if date_from:
        where.append("date(collected) >= ?")
        params.append(date_from)
    if date_to:
        where.append("date(collected) <= ?")
        params.append(date_to)

    if tab == "published":
        order = "published_at DESC"
    else:
        order = "score DESC, collected DESC" if sort == "score" else "collected DESC"

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""SELECT id, title, url, source, published, score,
                   tts_script, approved, generated_at, collected, sent_tg,
                   reviewed_script, gpt_comment, description, preview_titles,
                   published_at,
                   stats_views, stats_likes, stats_comments, stats_shares, stats_saves,
                   stats_reach, stats_watch_time, stats_platform,
                   instagram_media_id, instagram_permalink, starred, preselected
            FROM news
            WHERE {' AND '.join(where)}
            ORDER BY {order}
            LIMIT ?""",
        (*params, limit),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["preview_titles"] = json.loads(d["preview_titles"]) if d.get("preview_titles") else []
        except Exception:
            d["preview_titles"] = []
        d["priority"] = _score_to_priority(d["score"])
        result.append(d)
    return result


@app.post("/api/collect")
async def api_collect(request: Request, background_tasks: BackgroundTasks):
    require_auth(request)
    if _collect_state["running"]:
        return {"status": "already_running"}
    background_tasks.add_task(scheduled_collect)
    return {"status": "started"}


@app.get("/api/collect/status")
async def api_collect_status(request: Request):
    require_auth(request)
    # Compute next scheduled run times (UTC)
    now_utc = datetime.utcnow()
    collect_hours_utc = [1, 5, 9, 13, 16]
    next_run = None
    for h in collect_hours_utc:
        candidate = now_utc.replace(hour=h, minute=0, second=0, microsecond=0)
        if candidate > now_utc:
            next_run = candidate
            break
    if next_run is None:
        # Next day first slot
        from datetime import timedelta as _td
        next_run = (now_utc + _td(days=1)).replace(
            hour=collect_hours_utc[0], minute=0, second=0, microsecond=0
        )
    return {
        "running":     _collect_state["running"],
        "last_run":    _collect_state["last_run"],
        "last_count":  _collect_state["last_count"],
        "last_error":  _collect_state["last_error"],
        "next_run":    next_run.isoformat(),
        "schedule_utc": collect_hours_utc,
        "schedule_uzt": [h + 5 for h in collect_hours_utc],
    }


@app.post("/api/news/{news_id}/tg-preview")
async def api_tg_preview(news_id: int, request: Request):
    """Generate detailed TG post for preview (no publish)."""
    require_auth(request)
    try:
        from src.tg_auto_worker import preview_long_post
        result = await asyncio.to_thread(preview_long_post, news_id, DB_PATH)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/news/{news_id}/tg-publish")
async def api_tg_publish(news_id: int, request: Request):
    """Generate detailed TG post and publish to channel. Accepts optional custom_text."""
    require_auth(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    custom_text = (body.get("custom_text") or "").strip() or None
    try:
        from src.tg_auto_worker import run_manual_post
        result = await asyncio.to_thread(run_manual_post, news_id, DB_PATH, custom_text)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tg/auto-now")
async def api_tg_auto_now(request: Request, background_tasks: BackgroundTasks):
    """Manually trigger auto TG post check."""
    require_auth(request)
    background_tasks.add_task(_scheduled_tg_auto)
    return {"status": "triggered"}


@app.get("/api/tg/status")
async def api_tg_status(request: Request):
    """TG channel status: today's post count, last published, config."""
    require_auth(request)
    conn = sqlite3.connect(DB_PATH)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    today_count = conn.execute(
        "SELECT COUNT(*) FROM news WHERE tg_auto_published=1 AND tg_published_at LIKE ?",
        (f"{today}%",)
    ).fetchone()[0]
    last_row = conn.execute(
        "SELECT title, tg_published_at, tg_message_id FROM news "
        "WHERE tg_auto_published=1 OR tg_manual_published=1 "
        "ORDER BY tg_published_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return {
        "enabled":      os.environ.get("TG_AUTO_ENABLED", "0") == "1",
        "channel":      os.environ.get("TG_MY_CHANNEL", ""),
        "today_count":  today_count,
        "max_day":      int(os.environ.get("TG_AUTO_MAX_DAY", "4")),
        "last_title":   last_row[0] if last_row else None,
        "last_pub":     last_row[1] if last_row else None,
        "schedule_uzt": "09:00, 12:00, 17:00, 20:00",
    }


@app.patch("/api/news/{news_id}/tts")
async def api_update_tts(news_id: int, request: Request):
    """Save manually edited TTS script."""
    require_auth(request)
    body = await request.json()
    script = (body.get("tts_script") or "").strip()
    if not script:
        raise HTTPException(status_code=400, detail="tts_script is empty")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE news SET tts_script=? WHERE id=?", (script, news_id))
    conn.commit()
    conn.close()
    return {"id": news_id, "saved": True}


@app.post("/api/news/{news_id}/generate")
async def api_generate(news_id: int, request: Request):
    require_auth(request)
    return await _do_generate(news_id)


@app.post("/api/news/{news_id}/regenerate")
async def api_regenerate(news_id: int, request: Request):
    require_auth(request)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE news SET approved=0, reviewed_script=NULL, gpt_comment=NULL WHERE id=?", (news_id,))
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


@app.post("/api/news/{news_id}/publish")
async def api_publish(news_id: int, request: Request):
    """Mark news as manually published."""
    require_auth(request)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE news SET published_at=?, starred=0 WHERE id=?",
        (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), news_id),
    )
    conn.commit()
    conn.close()
    return {"id": news_id, "published": True}


@app.patch("/api/news/{news_id}/stats")
async def api_update_stats(news_id: int, request: Request):
    """Save post statistics from PDF upload. Also writes to post_snapshots."""
    require_auth(request)
    body = await request.json()

    views    = int(body.get("views",    0) or 0)
    likes    = int(body.get("likes",    0) or 0)
    comments = int(body.get("comments", 0) or 0)
    shares   = int(body.get("shares",   0) or 0)
    saves    = int(body.get("saves",    0) or 0)
    reach    = int(body.get("reach",    0) or 0)
    wt_str   = str(body.get("watch_time", "") or "")
    platform = str(body.get("platform", "instagram") or "instagram")

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """UPDATE news SET
           stats_views=?, stats_likes=?, stats_comments=?, stats_shares=?, stats_saves=?,
           stats_reach=?, stats_watch_time=?, stats_platform=?
           WHERE id=?""",
        (views, likes, comments, shares, saves, reach, wt_str, platform, news_id),
    )

    # Determine snapshot period from PDF pub/download dates and save to post_snapshots
    pub_raw  = str(body.get("pub_date_raw",      "") or "")
    dl_raw   = str(body.get("download_date_raw", "") or "")
    snap_at  = _compute_snapshot_label(pub_raw, dl_raw)
    if snap_at:
        avg_watch = None
        m = re.search(r"([\d,\.]+)", wt_str)
        if m:
            try:
                avg_watch = float(m.group(1).replace(",", "."))
            except Exception:
                pass
        conn.execute(
            """INSERT OR REPLACE INTO post_snapshots
                 (post_id, snapshot_at, views, reach, likes, comments,
                  saves, shares, avg_watch_time, recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))""",
            (news_id, snap_at, views, reach, likes, comments, saves, shares, avg_watch),
        )

    conn.commit()
    conn.close()
    return {"id": news_id, "saved": True, "snapshot_at": snap_at or None}


# ── PDF STATS PARSER ─────────────────────────────────────────────────────

_RU_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}

def _parse_ru_datetime(s: str):
    """Parse '23 апр 2026 г. в 18:30' → datetime or None."""
    from datetime import datetime as _dt
    m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4}).*?(\d{1,2}):(\d{2})", s)
    if not m:
        return None
    day, mon, year, hr, mn = m.groups()
    month = _RU_MONTHS.get(mon[:3].lower())
    if not month:
        return None
    try:
        return _dt(int(year), month, int(day), int(hr), int(mn))
    except Exception:
        return None


def _compute_snapshot_label(pub_raw: str, dl_raw: str) -> str:
    """Return '24h', '48h', or '72h' based on hours between pub and download."""
    pub = _parse_ru_datetime(pub_raw)
    dl  = _parse_ru_datetime(dl_raw)
    if not pub or not dl:
        return ""
    hours = (dl - pub).total_seconds() / 3600
    if hours <= 36:
        return "24h"
    elif hours <= 60:
        return "48h"
    else:
        return "72h"

def _parse_reels_pdf(file_bytes: bytes) -> dict:
    """Parse Instagram Reels stats PDF exported from Meta Edits app.

    The Meta Edits PDF layout has labels and values on *separate lines*:
      Просмотры Охваченные аккаунты Среднее время просмотра   ← label line
      21 тыс.   14 тыс.             13,50 с.                  ← value line
      "Нравится" Комментарии Репосты Поделились Сохранения     ← label line
      238        54          16      333        43             ← value line
    """
    import io
    try:
        import pdfplumber
    except ImportError:
        raise HTTPException(status_code=500, detail="pdfplumber не установлен. Запустите: pip install pdfplumber")

    result: dict = {
        "platform": "instagram",
        "views": 0, "reach": 0, "watch_time": "",
        "likes": 0, "comments": 0, "reposts": 0, "shares": 0, "saves": 0,
        "caption_snippet": "", "pub_date_raw": "", "download_date_raw": "",
        "_raw_preview": "",
    }

    def parse_metric(s: str) -> int:
        """'21 тыс.' → 21000, '1,2 тыс.' → 1200, '238' → 238"""
        s = (s or "").strip().replace("\xa0", "").replace("\u202f", "")
        mult = 1
        if re.search(r"млн", s, re.IGNORECASE):
            mult = 1_000_000
        elif re.search(r"тыс", s, re.IGNORECASE):
            mult = 1_000
        m = re.search(r"([\d]+(?:[,\.][\d]+)?)", s)
        if not m:
            return 0
        return int(float(m.group(1).replace(",", ".")) * mult)

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    result["_raw_preview"] = full_text[:600]
    lines = [ln.strip() for ln in full_text.split("\n")]

    for i, line in enumerate(lines):
        next_line = lines[i + 1] if i + 1 < len(lines) else ""

        # ── Main metrics row ────────────────────────────────────────────────
        # Label: "Просмотры Охваченные аккаунты Среднее время просмотра"
        # Value: "21 тыс. 14 тыс. 13,50 с."
        if "Просмотры" in line and ("Охваченные" in line or "Reach" in line or "Views" in line):
            # Watch time — ends with "с." or "s"
            m_wt = re.search(r"([\d,\.]+)\s*с\.?", next_line)
            if m_wt:
                result["watch_time"] = m_wt.group(1).replace(",", ".") + " с."

            # Views & reach — strip watch-time portion then extract тыс/млн or plain ints
            vals_no_wt = re.sub(r"[\d,\.]+\s*с\.?\s*", "", next_line).strip()
            metrics = re.findall(r"[\d,\.]+(?:\s*(?:тыс\.?|млн\.?))?", vals_no_wt)
            metrics = [m.strip() for m in metrics if m.strip()]
            if len(metrics) >= 1:
                result["views"] = parse_metric(metrics[0])
            if len(metrics) >= 2:
                result["reach"] = parse_metric(metrics[1])

        # ── Engagement row ──────────────────────────────────────────────────
        # Label: '"Нравится" Комментарии Репосты Поделились Сохранения'
        # Value: "238 54 16 333 43"  (positional, same order as labels)
        if "Нравится" in line and "Комментарии" in line:
            nums = re.findall(r"\d+", next_line)
            # Positional: likes, comments, reposts, shares(Поделились), saves(Сохранения)
            if len(nums) >= 1: result["likes"]    = int(nums[0])
            if len(nums) >= 2: result["comments"] = int(nums[1])
            if len(nums) >= 3: result["reposts"]  = int(nums[2])
            if len(nums) >= 4: result["shares"]   = int(nums[3])
            if len(nums) >= 5: result["saves"]    = int(nums[4])

        # ── Publish + download dates ─────────────────────────────────────────
        # PDF layout: "23 апр 2026 г. 26 апр 2026 г."  ← date line
        #             "в 18:30 в 02:43"                 ← time line
        # First date/time = published; second = when stats were downloaded
        dates_on_line = re.findall(r"\d{1,2}\s+\w+\s+\d{4}\s*г\.?", line)
        if dates_on_line and not result["pub_date_raw"]:
            times_on_next = re.findall(r"\d{1,2}:\d{2}", next_line)
            if times_on_next:
                result["pub_date_raw"] = f"{dates_on_line[0].strip()} в {times_on_next[0]}"
                if len(dates_on_line) >= 2 and len(times_on_next) >= 2:
                    result["download_date_raw"] = f"{dates_on_line[1].strip()} в {times_on_next[1]}"
            else:
                result["pub_date_raw"] = dates_on_line[0].strip()

    # Caption snippet (text between "Подпись" and the stats block)
    m = re.search(r"Подпись\s+(.+?)(?:\n\d|\d:\d\d|\nПросмотры|\Z)", full_text, re.DOTALL)
    if m:
        result["caption_snippet"] = m.group(1).strip()[:300]

    return result


@app.post("/api/stats/upload-pdf")
async def api_upload_stats_pdf(request: Request, file: UploadFile = File(...)):
    """Upload Reels PDF from Meta Edits, parse stats, return + candidate posts."""
    require_auth(request)
    content = await file.read()
    parsed = await asyncio.to_thread(_parse_reels_pdf, content)

    # Find candidate posts: published items within ±2 days of pub_date_raw
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    candidates = conn.execute(
        """SELECT id, title, published_at, score FROM news
           WHERE published_at IS NOT NULL
           ORDER BY published_at DESC LIMIT 30"""
    ).fetchall()
    conn.close()

    return {
        "parsed": parsed,
        "candidates": [dict(r) for r in candidates],
    }


# ── ANALYTICS ────────────────────────────────────────────────────────────

@app.get("/api/analytics")
async def api_analytics(request: Request):
    """Return aggregated analytics data for all published posts."""
    require_auth(request)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, title, score, published_at, stats_platform,
                  stats_views, stats_reach, stats_likes, stats_comments,
                  stats_shares, stats_saves, stats_watch_time,
                  instagram_media_id
           FROM news
           WHERE published_at IS NOT NULL
           ORDER BY published_at DESC
           LIMIT 100"""
    ).fetchall()

    # Load snapshots
    snaps_rows = []
    try:
        snaps_rows = conn.execute(
            "SELECT post_id, snapshot_at, views, reach, avg_watch_time FROM post_snapshots"
        ).fetchall()
    except Exception:
        pass

    # Load analyses
    analysis_rows = []
    try:
        analysis_rows = conn.execute(
            """SELECT post_id, hook_score, hook_feedback, watch_time_verdict,
                      cta_verdict, main_recommendation, predicted_next, analyzed_at
               FROM post_analysis"""
        ).fetchall()
    except Exception:
        pass

    conn.close()

    snaps: dict[int, dict] = {}
    for s in snaps_rows:
        pid, stype, sv, sr, sw = s
        snaps.setdefault(pid, {})[stype] = {"views": sv, "reach": sr, "watch": sw}

    analyses: dict[int, dict] = {}
    for a in analysis_rows:
        pid = a[0]
        analyses[pid] = {
            "hook_score":          a[1],
            "hook_feedback":       a[2],
            "watch_time_verdict":  a[3],
            "cta_verdict":         a[4],
            "main_recommendation": a[5],
            "predicted_next":      a[6],
            "analyzed_at":         a[7],
        }

    posts = []
    for r in rows:
        d = dict(r)
        v  = d.get("stats_views") or 0
        li = d.get("stats_likes") or 0
        c  = d.get("stats_comments") or 0
        sh = d.get("stats_shares") or 0
        sv = d.get("stats_saves") or 0
        er = round((li + c + sh + sv) / v * 100, 2) if v > 0 else 0
        d["engagement_rate"] = er
        d["has_stats"]       = any([v > 0, li > 0, c > 0, sh > 0, sv > 0,
                                    (d.get("stats_reach") or 0) > 0])
        d["snapshots"]       = snaps.get(d["id"], {})
        d["analysis"]        = analyses.get(d["id"])
        posts.append(d)

    with_stats = [p for p in posts if p["has_stats"]]
    agg = {}
    if with_stats:
        agg["total_posts"]  = len(with_stats)
        agg["total_views"]  = sum(p["stats_views"] or 0 for p in with_stats)
        agg["avg_views"]    = round(agg["total_views"] / len(with_stats))
        agg["avg_likes"]    = round(sum(p["stats_likes"] or 0 for p in with_stats) / len(with_stats))
        agg["avg_er"]       = round(sum(p["engagement_rate"] for p in with_stats) / len(with_stats), 2)
        agg["best_post"]    = max(with_stats, key=lambda p: p["stats_views"] or 0)
        agg["best_er_post"] = max(with_stats, key=lambda p: p["engagement_rate"])

    return {"posts": posts, "aggregate": agg}


@app.post("/api/analytics/insights")
async def api_analytics_insights(request: Request):
    """Ask Claude to analyze performance data and give content recommendations."""
    require_auth(request)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY не задан.")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT title, score, published_at, stats_views, stats_reach,
                  stats_likes, stats_comments, stats_shares, stats_saves,
                  stats_watch_time, tts_script
           FROM news WHERE published_at IS NOT NULL AND stats_views > 0
           ORDER BY published_at DESC LIMIT 20"""
    ).fetchall()
    conn.close()

    if not rows:
        raise HTTPException(status_code=400, detail="Нет опубликованных постов со статистикой.")

    posts_data = []
    for r in rows:
        d = dict(r)
        v = d.get("stats_views") or 0
        er = round(((d.get("stats_likes") or 0) + (d.get("stats_comments") or 0) +
                    (d.get("stats_shares") or 0) + (d.get("stats_saves") or 0)) / v * 100, 1) if v > 0 else 0
        posts_data.append(
            f"• «{d['title'][:60]}» | Views: {v} | Reach: {d.get('stats_reach',0)} | "
            f"Likes: {d.get('stats_likes',0)} | Comments: {d.get('stats_comments',0)} | "
            f"Shares: {d.get('stats_shares',0)} | Saves: {d.get('stats_saves',0)} | "
            f"Watch: {d.get('stats_watch_time','?')} | ER: {er}% | Score: {d.get('score',0)}"
        )

    prompt = (
        "Проанализируй статистику Reels-постов Instagram-канала UzbekNows (новости Узбекистана).\n\n"
        "ДАННЫЕ ПОСТОВ:\n" + "\n".join(posts_data) + "\n\n"
        "Дай конкретные инсайты по этим пунктам:\n"
        "1. ЛУЧШИЕ ПОСТЫ — что общего у топ-3 по просмотрам и ER?\n"
        "2. ТЕМЫ — какие темы работают лучше всего?\n"
        "3. УДЕРЖАНИЕ — у каких постов лучшее среднее время просмотра и почему?\n"
        "4. СЛАБЫЕ МЕСТА — что тянет вниз: ER, watch time, охват?\n"
        "5. КОНКРЕТНЫЕ РЕКОМЕНДАЦИИ — 3 действия которые нужно сделать прямо сейчас.\n\n"
        "Отвечай кратко, конкретно, без воды. Используй цифры из данных."
    )

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)
    msg = await client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return {"insights": msg.content[0].text.strip()}


@app.post("/api/news/{news_id}/analyze")
async def api_analyze_post(news_id: int, request: Request):
    """Trigger Claude analysis for a single post. Uses post_analysis table."""
    require_auth(request)
    from src import analysis_worker
    try:
        ok = await analysis_worker.analyze_post(news_id, DB_PATH, force=True)
        if not ok:
            raise HTTPException(status_code=400, detail="Анализ не выполнен (нет данных или ключа API)")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT hook_score, hook_feedback, watch_time_verdict,
                  cta_verdict, main_recommendation, predicted_next, analyzed_at
           FROM post_analysis WHERE post_id=?""",
        (news_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else {"ok": True}


@app.post("/api/news/{news_id}/approve-reviewed")
async def api_approve_reviewed(news_id: int, request: Request):
    """Replace tts_script with GPT-reviewed version and approve."""
    require_auth(request)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE news SET tts_script=reviewed_script, approved=1 WHERE id=?",
        (news_id,),
    )
    conn.commit()
    conn.close()
    return {"id": news_id, "approved": True, "used": "reviewed"}


@app.post("/api/news/{news_id}/gpt-review")
async def api_gpt_review(news_id: int, request: Request):
    """Claude review of TTS only. Uses description + URL as context."""
    require_auth(request)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=400, detail="ANTHROPIC_API_KEY не задан")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM news WHERE id=?", (news_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Новость не найдена")

    item = dict(row)
    tts         = (item.get("tts_script") or "").strip()
    description = (item.get("description") or "").strip()
    url         = (item.get("url") or "").strip()

    if not tts:
        raise HTTPException(status_code=400, detail="TTS скрипт пустой — сначала сгенерируй")

    system = _load_system_prompt()

    context_parts = [f"TTS СКРИПТ (оригинал):\n{tts}"]
    if description:
        context_parts.append(f"ОПИСАНИЕ (для проверки фактов):\n{description[:700]}")
    if url:
        context_parts.append(f"ИСТОЧНИК: {url}")

    prompt = (
        "ЗАДАЧА: Улучши TTS-скрипт. Два главных приоритета:\n\n"
        "1. ХУК (первое предложение) — самое важное.\n"
        "   Должно бить в лоб: страх / потеря / запрет / несправедливость.\n"
        "   Формула: [Угроза/выгода] + [Вы/Вас] + [недосказанность].\n"
        "   Примеры сильных хуков:\n"
        "   - «Вас могут не принять в больнице в выходные.»\n"
        "   - «Теперь развод может затянуться на год.»\n"
        "   - «Наличные теперь не везде примут.»\n"
        "   Первое слово: «Теперь…» / «Вас…» / «Они…» / «Скоро…» / «Если вы…»\n"
        "   НЕЛЬЗЯ начинать с: «В Узбекистане…», «Сегодня…», «Хорошая новость…»\n\n"
        "2. ДЛИНА — цель 45-55 слов (20-25 секунд).\n"
        "   Если длиннее — режь безжалостно. Каждое лишнее предложение убирай.\n"
        "   Максимум 55 слов — жёсткий потолок.\n\n"
        "Описание и источник — только для проверки точности фактов.\n\n"
        + "\n\n".join(context_parts)
        + "\n\nОтветь СТРОГО в этом формате (без лишних слов):\n"
        "СКРИПТ: [готовый TTS — только текст]\n"
        "КОММЕНТАРИЙ: [1-2 предложения: что изменил в хуке и/или что сократил]"
    )

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)
    try:
        msg = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=500,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Claude ошибка: {e}")

    raw = msg.content[0].text.strip()

    # Parse СКРИПТ: / КОММЕНТАРИЙ:
    script, comment = "", ""
    current = None
    for line in raw.splitlines():
        if line.startswith("СКРИПТ:"):
            current = "s"; script = line[7:].strip()
        elif line.startswith("КОММЕНТАРИЙ:"):
            current = "c"; comment = line[12:].strip()
        elif current == "s" and line.strip():
            script += "\n" + line
        elif current == "c" and line.strip():
            comment += " " + line.strip()

    if not script:
        script = raw  # fallback

    script  = script.strip()
    comment = comment.strip()

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE news SET reviewed_script=?, gpt_comment=? WHERE id=?",
        (script, comment, news_id)
    )
    conn.commit()
    conn.close()

    return {"id": news_id, "reviewed_script": script, "gpt_comment": comment}


@app.delete("/api/news/{news_id}")
async def api_delete(news_id: int, request: Request):
    require_auth(request)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM news WHERE id=?", (news_id,))
    conn.commit()
    conn.close()
    return {"id": news_id, "deleted": True}


@app.post("/api/news/{news_id}/star")
async def api_star(news_id: int, request: Request):
    """Toggle starred status for a news item."""
    require_auth(request)
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT starred FROM news WHERE id=?", (news_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404)
    new_val = 0 if row[0] else 1
    conn.execute("UPDATE news SET starred=? WHERE id=?", (new_val, news_id))
    conn.commit()
    conn.close()
    return {"id": news_id, "starred": bool(new_val)}


@app.post("/api/news/{news_id}/preselect")
async def api_preselect(news_id: int, request: Request):
    """Toggle preselected status (Предотбор)."""
    require_auth(request)
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT preselected FROM news WHERE id=?", (news_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404)
    new_val = 0 if row[0] else 1
    conn.execute("UPDATE news SET preselected=? WHERE id=?", (new_val, news_id))
    conn.commit()
    conn.close()
    return {"id": news_id, "preselected": bool(new_val)}


# ── API: ELEVENLABS CREDITS ───────────────────────────────────────────────

def _get_el_credits_sync(api_key: str) -> int:
    """Returns remaining characters for an ElevenLabs account.
    Returns -1 if credits can't be fetched (treat as 'unknown, assume available')."""
    try:
        r = req_lib.get(
            "https://api.elevenlabs.io/v1/user",
            headers={"xi-api-key": api_key},
            timeout=10,
        )
        if r.ok:
            sub = r.json().get("subscription", {})
            limit = sub.get("character_limit", 0)
            count = sub.get("character_count", 0)
            # If limit==0 — API returned no subscription data, assume funded
            if limit == 0:
                return -1
            return max(0, limit - count)
    except Exception:
        pass
    return -1  # network error — assume available, let EL API decide


EL_MIN_CHARS = 200  # минимум символов для одной озвучки

async def _pick_el_account() -> tuple[str, str, int]:
    """Auto-select ElevenLabs account with FEWEST credits that still has enough.
    -1 means 'unknown credits' (API error) — treated as viable.
    Logic: use the account that's running lower first (save the fuller one).
    Returns (api_key, voice_id, account_number).
    """
    accounts = []
    for acc in [1, 2]:
        api_key  = os.environ.get(f"ELEVENLABS_API_KEY_{acc}", "")
        voice_id = os.environ.get(f"ELEVENLABS_VOICE_ID_{acc}", "")
        if not api_key or not voice_id:
            continue
        credits = await asyncio.to_thread(_get_el_credits_sync, api_key)
        log(f"ElevenLabs acc{acc}: {credits} chars remaining")
        accounts.append((credits, api_key, voice_id, acc))

    if not accounts:
        raise HTTPException(
            status_code=500,
            detail="Нет настроенных ElevenLabs аккаунтов. Добавьте ELEVENLABS_API_KEY_1 и ELEVENLABS_VOICE_ID_1."
        )

    # -1 = unknown credits (API error) → treat as viable (assume funded)
    # viable = accounts with confirmed enough credits OR unknown
    viable = [a for a in accounts if a[0] < 0 or a[0] >= EL_MIN_CHARS]
    if viable:
        # Among viable: prefer known ones first (lowest confirmed credits)
        # fall back to unknown (-1) if all are unknown
        known = [a for a in viable if a[0] >= 0]
        chosen = min(known, key=lambda a: a[0]) if known else viable[0]
    else:
        # All accounts confirmed at 0 credits — truly empty
        chosen = max(accounts, key=lambda a: a[0])
        if chosen[0] == 0:
            raise HTTPException(
                status_code=402,
                detail="На всех аккаунтах ElevenLabs 0 кредитов. Пополните баланс на elevenlabs.io."
            )

    return chosen[1], chosen[2], chosen[3]


@app.get("/api/elevenlabs/credits")
async def api_el_credits(request: Request):
    """Return remaining characters for each configured ElevenLabs account."""
    require_auth(request)
    result = {}
    for acc in [1, 2]:
        api_key = os.environ.get(f"ELEVENLABS_API_KEY_{acc}", "")
        if not api_key:
            continue
        credits = await asyncio.to_thread(_get_el_credits_sync, api_key)
        result[str(acc)] = credits
    return result


# ── API: ELEVENLABS SPEAK ─────────────────────────────────────────────────

@app.get("/api/news/{news_id}/speak")
async def api_speak(news_id: int, request: Request, account: int = 0):
    """Generate MP3 via ElevenLabs.
    account=0 (default) → auto-pick account with most credits.
    account=1 or 2      → force specific account.
    """
    require_auth(request)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT tts_script FROM news WHERE id=?", (news_id,)).fetchone()
    conn.close()

    if not row or not row["tts_script"]:
        raise HTTPException(status_code=404, detail="Нет TTS скрипта. Сначала нажмите Generate.")

    script = row["tts_script"]

    # Resolve account
    if account == 0:
        api_key, voice_id, used_acc = await _pick_el_account()
    else:
        api_key  = os.environ.get(f"ELEVENLABS_API_KEY_{account}", "")
        voice_id = os.environ.get(f"ELEVENLABS_VOICE_ID_{account}", "")
        used_acc = account
        if not api_key or not voice_id:
            raise HTTPException(
                status_code=500,
                detail=f"ELEVENLABS_API_KEY_{account} или ELEVENLABS_VOICE_ID_{account} не заданы."
            )

    # Load voice settings
    with open(PREFS_PATH, encoding="utf-8") as f:
        prefs = yaml.safe_load(f)
    el = prefs.get("elevenlabs", {})
    voice_settings = el.get("voice_settings", {
        "stability": 0.45, "similarity_boost": 0.80,
        "style": 0.35, "speed": 1.05, "use_speaker_boost": True,
    })
    model_id = el.get("model_id", "eleven_multilingual_v2")

    audio = await asyncio.to_thread(
        _elevenlabs_sync, script, api_key, voice_id, voice_settings, model_id
    )
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": f'attachment; filename="tts_{news_id}_acc{used_acc}.mp3"',
            "X-EL-Account": str(used_acc),
        },
    )


def _elevenlabs_sync(
    text: str, api_key: str, voice_id: str, voice_settings: dict, model_id: str
) -> bytes:
    r = req_lib.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        json={"text": text, "model_id": model_id, "voice_settings": voice_settings},
        timeout=60,
    )
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"ElevenLabs error {r.status_code}: {r.text[:200]}")
    return r.content


@app.patch("/api/news/{news_id}/description")
async def api_update_description(news_id: int, request: Request):
    """Save manually edited description."""
    require_auth(request)
    body = await request.json()
    description = (body.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description is empty")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE news SET description=? WHERE id=?", (description, news_id))
    conn.commit()
    conn.close()
    return {"id": news_id, "saved": True}


# ── TTS + DESCRIPTION + PREVIEW GENERATION (Claude) ──────────────────────

def _parse_generate_response(text: str) -> tuple[str, str, list[str]]:
    """Parse Claude's structured response into (tts, description, previews)."""
    tts = description = ""
    previews: list[str] = []

    m_tts  = re.search(r"===TTS===(.*?)(?====ОПИСАНИЕ===)", text, re.DOTALL)
    m_desc = re.search(r"===ОПИСАНИЕ===(.*?)(?====ПРЕВЬЮ===)", text, re.DOTALL)
    m_prev = re.search(r"===ПРЕВЬЮ===(.*?)$", text, re.DOTALL)

    tts         = m_tts.group(1).strip()  if m_tts  else text.strip()
    description = m_desc.group(1).strip() if m_desc else ""

    if m_prev:
        for line in m_prev.group(1).strip().splitlines():
            line = re.sub(r"^\d+[\.\)]\s*", "", line.strip()).strip()
            if line and not line.startswith("["):
                previews.append(line)

    return tts, description, previews[:3]


async def _do_generate(news_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM news WHERE id=?", (news_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Новость не найдена")
    item = dict(row)

    system = _load_system_prompt()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY не задан.")

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)

    prompt = (
        "Напиши для этой новости три части строго по инструкции выше.\n\n"
        "НОВОСТЬ:\n"
        f"Заголовок: {item['title']}\n"
        f"Источник: {item.get('source', '')}\n"
        f"Описание: {item.get('summary', '') or ''}\n\n"
        "Ответь ТОЛЬКО в этом формате (без пояснений):\n\n"
        "===TTS===\n"
        "[TTS скрипт — ЦЕЛЬ 20-25 секунд (~45-55 слов). 18 сек — отлично. "
        "Короче = лучше если всё сказано. Макс 35-45 сек только для сложных новостей.]\n\n"
        "===ОПИСАНИЕ===\n"
        "[Описание для поста — 150-250 слов. Строго соблюдай структуру:\n"
        "1. Суть: кто, что, где, когда — конкретные цифры и факты. НЕ повторяй первое предложение TTS. Сразу детали. (2-3 абзаца)\n"
        "2. Почему это важно для обычного человека — личное последствие (1 абзац)\n"
        "3. Контекст которого нет в TTS: история вопроса, сравнения, как это работает (1-2 абзаца)\n"
        "4. Дополнительная деталь или факт из источника которого нет выше (1 абзац)\n"
        "Абзацы разделяй пустой строкой. БЕЗ эмодзи. БЕЗ заголовков. БЕЗ вопросов. Пиши как живой информативный текст, не как СМИ.]\n\n"
        "===ПРЕВЬЮ===\n"
        "1. [3-5 слов КАПСОМ — вариант 1]\n"
        "2. [3-5 слов КАПСОМ — вариант 2]\n"
        "3. [3-5 слов КАПСОМ — вариант 3]"
    )

    msg = await client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1200,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    script, description, previews = _parse_generate_response(raw)

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE news SET tts_script=?, description=?, preview_titles=?, "
        "generated_at=?, reviewed_script=NULL, gpt_comment=NULL WHERE id=?",
        (script, description, json.dumps(previews, ensure_ascii=False),
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), news_id),
    )
    conn.commit()
    conn.close()

    return {
        "id": news_id,
        "tts_script": script,
        "description": description,
        "preview_titles": previews,
    }


# ── META / INSTAGRAM OAUTH ────────────────────────────────────────────────────
#
# ENV vars needed (add to Railway Variables):
#   META_APP_SECRET=dd3c70868143795c8036b6758253ee21
#
# Flow:
#   1. Open /api/meta/auth  → redirects to Meta login
#   2. Meta calls back /api/meta/auth/callback → saves long-lived token to DB
#   3. Check /api/meta/status → shows saved token + Instagram account ID
#
# Token is saved in news.db table `meta_config` (key-value store).

META_APP_ID     = "785663281089923"
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
META_REDIRECT   = "https://web-production-6394a.up.railway.app/api/meta/auth/callback"
META_SCOPES     = ",".join([
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_insights",
    "pages_show_list",
    "pages_read_engagement",
])
GRAPH_URL = "https://graph.facebook.com/v19.0"


def _meta_db_init(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta_config (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()


def _meta_set(conn: sqlite3.Connection, key: str, value: str):
    conn.execute(
        "INSERT INTO meta_config(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def _meta_get(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM meta_config WHERE key=?", (key,)).fetchone()
    return row[0] if row else ""


@app.get("/api/meta/auth")
async def meta_auth_start():
    """Redirect to Meta OAuth login. Open this URL in browser."""
    oauth_url = (
        f"https://www.facebook.com/v19.0/dialog/oauth"
        f"?client_id={META_APP_ID}"
        f"&redirect_uri={META_REDIRECT}"
        f"&scope={META_SCOPES}"
        f"&response_type=code"
    )
    return RedirectResponse(url=oauth_url)


@app.get("/api/meta/auth/callback")
async def meta_auth_callback(request: Request):
    """Meta redirects here after login. Exchanges code → long-lived token, saves to DB."""
    code  = request.query_params.get("code")
    error = request.query_params.get("error_description")

    if error or not code:
        return HTMLResponse(
            f"<h2>Ошибка авторизации</h2><p>{error or 'Код не получен'}</p>",
            status_code=400,
        )

    if not META_APP_SECRET:
        return HTMLResponse(
            "<h2>Ошибка</h2><p>META_APP_SECRET не задан в Railway Variables.</p>",
            status_code=500,
        )

    async with httpx.AsyncClient() as client:
        # 1. Exchange code → short-lived token
        r1 = await client.get(f"{GRAPH_URL}/oauth/access_token", params={
            "client_id": META_APP_ID,
            "client_secret": META_APP_SECRET,
            "redirect_uri": META_REDIRECT,
            "code": code,
        })
        data1 = r1.json()
        if "error" in data1:
            return HTMLResponse(f"<h2>Ошибка токена</h2><pre>{data1}</pre>", status_code=400)
        short_token = data1["access_token"]

        # 2. Exchange short-lived → long-lived (60 days)
        r2 = await client.get(f"{GRAPH_URL}/oauth/access_token", params={
            "grant_type": "fb_exchange_token",
            "client_id": META_APP_ID,
            "client_secret": META_APP_SECRET,
            "fb_exchange_token": short_token,
        })
        data2 = r2.json()
        if "error" in data2:
            return HTMLResponse(f"<h2>Ошибка long-lived токена</h2><pre>{data2}</pre>", status_code=400)
        long_token = data2["access_token"]

        # 3. Get Facebook Pages list
        r3 = await client.get(f"{GRAPH_URL}/me/accounts", params={"access_token": long_token})
        pages_data = r3.json()

        # 4. Find Instagram Business Account
        ig_account_id = None
        ig_username   = None
        for page in pages_data.get("data", []):
            r4 = await client.get(f"{GRAPH_URL}/{page['id']}", params={
                "fields": "instagram_business_account",
                "access_token": long_token,
            })
            ig = r4.json().get("instagram_business_account")
            if ig:
                ig_account_id = ig["id"]
                r5 = await client.get(f"{GRAPH_URL}/{ig_account_id}", params={
                    "fields": "username",
                    "access_token": long_token,
                })
                ig_username = r5.json().get("username", ig_account_id)
                break

    # 5. Save to DB
    conn = sqlite3.connect(DB_PATH)
    _meta_db_init(conn)
    _meta_set(conn, "access_token", long_token)
    _meta_set(conn, "token_saved_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    if ig_account_id:
        _meta_set(conn, "instagram_account_id", ig_account_id)
    if ig_username:
        _meta_set(conn, "instagram_username", ig_username)
    conn.close()

    ig_info = (
        f"<p><b>Instagram:</b> @{ig_username} (ID: {ig_account_id})</p>"
        if ig_account_id else
        "<p style='color:orange'>Instagram Business Account не найден — убедись что страница Facebook привязана к Instagram.</p>"
    )

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>Meta подключён</title>
  <style>
    body{{font-family:sans-serif;display:flex;justify-content:center;align-items:center;
         min-height:100vh;margin:0;background:#0a0a0b;color:#fff}}
    .card{{background:#1a1a1b;padding:40px 48px;border-radius:12px;
           max-width:500px;text-align:center;border:1px solid #2a2a2b}}
    .check{{font-size:64px}}
    h1{{color:#22cc88;margin:16px 0 8px}}
    p{{color:#aaa;line-height:1.6}}
    .token{{font-size:11px;color:#555;word-break:break-all;margin-top:16px;
            background:#111;padding:8px;border-radius:6px}}
    a{{color:#ff5533;text-decoration:none}}
  </style>
</head>
<body>
  <div class="card">
    <div class="check">✅</div>
    <h1>Meta подключён!</h1>
    <p>Long-lived токен (60 дней) сохранён в базе данных.</p>
    {ig_info}
    <p class="token">Токен: {long_token[:32]}…</p>
    <p style="margin-top:24px"><a href="/">← Вернуться в MediaHub</a></p>
  </div>
</body>
</html>""")


@app.get("/api/meta/status")
async def meta_status(request: Request):
    """Check if Meta token is saved. Returns token info without exposing the full token."""
    require_auth(request)
    conn = sqlite3.connect(DB_PATH)
    _meta_db_init(conn)
    token      = _meta_get(conn, "access_token")
    saved_at   = _meta_get(conn, "token_saved_at")
    ig_id      = _meta_get(conn, "instagram_account_id")
    ig_user    = _meta_get(conn, "instagram_username")
    conn.close()

    return {
        "connected": bool(token),
        "token_preview": token[:16] + "…" if token else None,
        "saved_at": saved_at,
        "instagram_account_id": ig_id,
        "instagram_username": ig_user,
    }


# ── INSTAGRAM STATS SYNC ─────────────────────────────────────────────────────
#
# Uses META_ACCESS_TOKEN (system user token, permanent) from Railway Variables.
# INSTAGRAM_ACCOUNT_ID = 17841477793587741  (@uzbekknows)
#
# Endpoints:
#   GET  /api/meta/account-stats   — followers, posts count
#   GET  /api/meta/ig-reels        — recent reels + live insights
#   POST /api/meta/link-reel       — {news_id, media_id} → save stats to DB
#   POST /api/meta/refresh-stats   — re-fetch stats for all linked reels


def _ig_ids() -> list[str]:
    """Return all configured IG account IDs to try (primary first, then fallback)."""
    ids: list[str] = []
    for key in ("INSTAGRAM_ACCOUNT_ID_2", "INSTAGRAM_ACCOUNT_ID"):
        v = (os.environ.get(key) or "").strip()
        if v and v not in ids:
            ids.append(v)
    return ids or ["17841477793587741"]


async def _ig_get(path: str, params: dict = None) -> dict:
    """Make an authenticated Instagram Graph API request (system user token)."""
    token = os.environ.get("META_ACCESS_TOKEN", "")
    if not token:
        raise HTTPException(
            status_code=500,
            detail="META_ACCESS_TOKEN не задан в Railway Variables. "
                   "Добавь через Railway Dashboard → Variables.",
        )
    if params is None:
        params = {}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{GRAPH_URL}/{path}",
            params={"access_token": token, **params},
        )
    return r.json()


@app.get("/api/meta/account-stats")
async def api_meta_account_stats(request: Request):
    """Return Instagram account info: followers, posts count, username."""
    require_auth(request)
    data, last_err = {}, {}
    for ig_id in _ig_ids():
        data = await _ig_get(ig_id, {
            "fields": "username,followers_count,media_count,profile_picture_url",
        })
        if "error" not in data:
            break
        last_err = data
    if "error" in data:
        raise HTTPException(status_code=502, detail=str(last_err.get("error")))
    return data


@app.get("/api/meta/ig-reels")
async def api_ig_reels(request: Request, limit: int = 30):
    """Fetch recent Instagram Reels with live insights from Meta Graph API."""
    require_auth(request)

    # 1. Get media list — try each configured account ID
    media_data: dict = {}
    for ig_id in _ig_ids():
        media_data = await _ig_get(f"{ig_id}/media", {
            "fields": "id,media_type,timestamp,permalink,like_count,comments_count,caption",
            "limit": limit,
        })
        if "error" not in media_data:
            break
    if "error" in media_data:
        raise HTTPException(status_code=502, detail=str(media_data["error"]))

    # Reels only (media_type == REELS or VIDEO)
    reels = [m for m in media_data.get("data", [])
             if m.get("media_type") in ("REELS", "VIDEO")]

    # 2. Fetch insights for each reel, check DB link
    conn = sqlite3.connect(DB_PATH)
    linked_map = {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT instagram_media_id, id, title FROM news "
            "WHERE instagram_media_id IS NOT NULL AND instagram_media_id != ''"
        ).fetchall()
    }
    conn.close()

    result = []
    for reel in reels:
        media_id = reel["id"]
        ins_data = await _ig_get(f"{media_id}/insights", {
            "metric": "plays,reach,saved,shares,ig_reels_avg_watch_time,total_interactions",
        })
        insights = {
            i["name"]: i.get("value", 0)
            for i in ins_data.get("data", [])
        }
        avg_ms    = insights.get("ig_reels_avg_watch_time", 0)
        watch_str = f"{avg_ms / 1000:.1f}с" if avg_ms else ""

        linked = linked_map.get(media_id)
        result.append({
            "media_id":          media_id,
            "timestamp":         reel.get("timestamp", ""),
            "permalink":         reel.get("permalink", ""),
            "caption_snippet":   (reel.get("caption") or "")[:120],
            "likes":             reel.get("like_count", 0),
            "comments":          reel.get("comments_count", 0),
            "views":             insights.get("plays", 0),
            "reach":             insights.get("reach", 0),
            "saves":             insights.get("saved", 0),
            "shares":            insights.get("shares", 0),
            "avg_watch_time_ms": avg_ms,
            "watch_time":        watch_str,
            "linked_news_id":    linked[0] if linked else None,
            "linked_news_title": linked[1] if linked else None,
        })

    return result


@app.post("/api/meta/link-reel")
async def api_link_reel(request: Request):
    """Link an Instagram reel to a news entry and immediately save its stats to DB.

    Body: {"news_id": 42, "media_id": "17846368219941196"}
    """
    require_auth(request)
    body     = await request.json()
    news_id  = int(body.get("news_id", 0))
    media_id = str(body.get("media_id", "")).strip()

    if not news_id or not media_id:
        raise HTTPException(status_code=400, detail="news_id и media_id обязательны")

    # Fetch insights
    ins_data = await _ig_get(f"{media_id}/insights", {
        "metric": "plays,reach,saved,shares,ig_reels_avg_watch_time",
    })
    # Fetch media meta (likes, comments, timestamp)
    med_data = await _ig_get(media_id, {
        "fields": "like_count,comments_count,timestamp",
    })

    if "error" in ins_data:
        raise HTTPException(status_code=502, detail=str(ins_data["error"]))
    if "error" in med_data:
        raise HTTPException(status_code=502, detail=str(med_data["error"]))

    insights  = {i["name"]: i.get("value", 0) for i in ins_data.get("data", [])}
    avg_ms    = insights.get("ig_reels_avg_watch_time", 0)
    watch_str = f"{avg_ms / 1000:.1f}с" if avg_ms else ""

    views    = insights.get("plays", 0)
    reach    = insights.get("reach", 0)
    saves    = insights.get("saved", 0)
    shares   = insights.get("shares", 0)
    likes    = med_data.get("like_count", 0)
    comments = med_data.get("comments_count", 0)
    pub_ts   = med_data.get("timestamp", "")

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """UPDATE news SET
               stats_views=?,     stats_likes=?,    stats_comments=?,
               stats_shares=?,    stats_saves=?,    stats_reach=?,
               stats_watch_time=?, stats_platform='instagram',
               instagram_media_id=?,
               published_at=COALESCE(published_at, ?)
           WHERE id=?""",
        (views, likes, comments, shares, saves, reach,
         watch_str, media_id, pub_ts, news_id),
    )
    conn.commit()
    conn.close()

    return {
        "news_id": news_id, "media_id": media_id, "saved": True,
        "views": views, "reach": reach, "likes": likes,
        "comments": comments, "saves": saves, "shares": shares,
        "watch_time": watch_str,
    }


@app.post("/api/meta/refresh-stats")
async def api_refresh_stats(request: Request):
    """Re-fetch live stats for all news entries that have instagram_media_id set."""
    require_auth(request)

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, instagram_media_id FROM news "
        "WHERE instagram_media_id IS NOT NULL AND instagram_media_id != ''"
    ).fetchall()
    conn.close()

    if not rows:
        return {"updated": 0, "message": "Нет привязанных Reels. Сначала привяжи через /api/meta/ig-reels."}

    updated = 0
    errors: list[str] = []

    for (news_id, media_id) in rows:
        try:
            ins_data = await _ig_get(f"{media_id}/insights", {
                "metric": "plays,reach,saved,shares,ig_reels_avg_watch_time",
            })
            med_data = await _ig_get(media_id, {
                "fields": "like_count,comments_count",
            })

            if "error" in ins_data:
                errors.append(f"#{news_id} ({media_id}): {ins_data['error']}")
                continue

            insights  = {i["name"]: i.get("value", 0) for i in ins_data.get("data", [])}
            avg_ms    = insights.get("ig_reels_avg_watch_time", 0)
            watch_str = f"{avg_ms / 1000:.1f}с" if avg_ms else ""

            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                """UPDATE news SET
                       stats_views=?,     stats_likes=?,    stats_comments=?,
                       stats_shares=?,    stats_saves=?,    stats_reach=?,
                       stats_watch_time=?
                   WHERE id=?""",
                (
                    insights.get("plays", 0),
                    med_data.get("like_count", 0),
                    med_data.get("comments_count", 0),
                    insights.get("shares", 0),
                    insights.get("saved", 0),
                    insights.get("reach", 0),
                    watch_str,
                    news_id,
                ),
            )
            conn.commit()
            conn.close()
            updated += 1

        except Exception as e:
            errors.append(f"#{news_id}: {e}")

    log(f"IG stats refresh: {updated} updated, {len(errors)} errors")
    return {"updated": updated, "errors": errors}


# ── INSTAGRAM PUBLISHING ─────────────────────────────────────────────────────
#
# Flow:
#   1. POST /api/news/{id}/upload-video  → saves .mp4 to /data/videos/{id}.mp4
#   2. POST /api/news/{id}/publish-reel  → uploads to Meta + publishes, returns permalink
#
# CapCut path (local only):
#   Set CAPCUT_EXPORT_PATH env var to the folder where CapCut exports .mp4 files.
#   GET /api/capcut/videos scans that folder and returns available files.
#   On Railway this env var won't be set, so the endpoint returns [].


@app.get("/api/capcut/videos")
async def api_capcut_videos(request: Request):
    """List .mp4 files from CAPCUT_EXPORT_PATH (local use only).
    Returns [] if path not configured or not accessible from the server."""
    require_auth(request)
    if not CAPCUT_PATH:
        return []
    folder = Path(CAPCUT_PATH)
    if not folder.exists():
        return []
    files = sorted(
        [f for f in folder.glob("*.mp4")],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )[:20]
    return [
        {
            "name":     f.name,
            "path":     str(f),
            "size_mb":  round(f.stat().st_size / 1024 / 1024, 1),
            "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
        }
        for f in files
    ]


@app.post("/api/news/{news_id}/upload-video")
async def api_upload_video(news_id: int, request: Request, file: UploadFile = File(...)):
    """Upload a video file for a news item. Saved to /data/videos/{news_id}.mp4."""
    require_auth(request)
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    dest = VIDEOS_DIR / f"{news_id}.mp4"
    content = await file.read()
    dest.write_bytes(content)
    size_mb = round(len(content) / 1024 / 1024, 1)
    log(f"Video uploaded for news #{news_id}: {size_mb} MB → {dest}")
    return {"news_id": news_id, "size_mb": size_mb, "ready": True}


@app.post("/api/news/{news_id}/publish-reel")
async def api_publish_reel(news_id: int, request: Request):
    """Publish uploaded video as an Instagram Reel.
    Video must be uploaded via upload-video first."""
    require_auth(request)

    video_path = VIDEOS_DIR / f"{news_id}.mp4"
    if not video_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Видео не найдено. Сначала загрузи видео.",
        )

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT description, title FROM news WHERE id=?", (news_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Новость не найдена")

    caption = (body.get("caption") or row["description"] or row["title"] or "").strip()

    from src import instagram_worker as ig_worker

    try:
        ig_conn = sqlite3.connect(DB_PATH)
        ig_worker.init_table(ig_conn)

        media_id = await ig_worker.publish_reel(
            news_id=news_id,
            video_path=video_path,
            caption=caption,
            db_conn=ig_conn,
        )

        now_iso   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        permalink = await ig_worker._get_permalink(media_id)
        ig_conn.execute(
            """UPDATE news SET instagram_media_id=?, instagram_permalink=?,
               published_at=COALESCE(published_at,?), starred=0 WHERE id=?""",
            (media_id, permalink, now_iso, news_id),
        )
        ig_conn.commit()
        ig_conn.close()

    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Instagram ошибка: {e}")

    log(f"Reel published news #{news_id} → {media_id}  {permalink}")

    return {
        "news_id":   news_id,
        "media_id":  media_id,
        "permalink": permalink,
        "published": True,
    }


@app.get("/api/ig-posts")
async def api_ig_posts(request: Request):
    """List posts published to Instagram via MediaHub (ig_posts table)."""
    require_auth(request)
    from src.instagram_worker import init_table as _ig_init

    conn = sqlite3.connect(DB_PATH)
    _ig_init(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT p.id, p.news_id, p.media_id, p.permalink, p.published_at,
                  p.caption, p.post_type,
                  p.stats_views, p.stats_reach, p.stats_likes, p.stats_comments,
                  p.stats_shares, p.stats_saves, p.stats_watch_time, p.stats_updated_at,
                  p.stats_24h, p.stats_48h, p.stats_72h,
                  n.title AS news_title, n.score
           FROM ig_posts p
           LEFT JOIN news n ON p.news_id = n.id
           ORDER BY p.published_at DESC LIMIT 50"""
    ).fetchall()
    conn.close()

    result = []
    for r in rows:
        d = dict(r)
        for col in ("stats_24h", "stats_48h", "stats_72h"):
            if d.get(col):
                try:
                    d[col] = json.loads(d[col])
                except Exception:
                    pass
        result.append(d)
    return result


async def _scheduled_cleanup():
    """Daily job: delete unpublished/unapproved news older than NEWS_RETENTION_DAYS (default 5).
    Starred news are also deleted. Only approved and published posts are kept."""
    days = int(os.environ.get("NEWS_RETENTION_DAYS", "5"))
    try:
        conn = sqlite3.connect(DB_PATH)
        result = conn.execute(
            """DELETE FROM news
               WHERE approved = 0
                 AND published_at IS NULL
                 AND date(collected) <= date('now', ? || ' days')""",
            (f"-{days}",),
        )
        deleted = result.rowcount
        conn.commit()
        conn.close()
        if deleted > 0:
            log(f"[Cleanup] Удалено {deleted} новостей старше {days} дней")
            _tg_notify(f"🗑 Автоочистка: удалено {deleted} новостей старше {days} дней")
        else:
            log(f"[Cleanup] Нечего удалять (старше {days} дней)")
    except Exception as e:
        log(f"[Cleanup] Error: {e}")


async def _scheduled_tg_auto():
    """Scheduled TG auto-post at 09/12/17/20 UZT."""
    try:
        from src.tg_auto_worker import run_auto_post
        result = await asyncio.to_thread(run_auto_post, DB_PATH)
        log(f"[TG Auto] {result}")
    except Exception as e:
        log(f"[TG Auto] Error: {e}")


async def _scheduled_ig_snapshots():
    """Hourly job: save 24/48/72h stats snapshots for posts published from MediaHub."""
    from src import instagram_worker as ig_worker
    from src import snapshot_worker
    try:
        saved_ig = await ig_worker.run_snapshot_jobs(DB_PATH)
        saved_sn = await snapshot_worker.run_snapshots(DB_PATH)
        total = saved_ig + saved_sn
        if total > 0:
            log(f"[IG Snapshots] {total} snapshots saved (ig_posts:{saved_ig} news:{saved_sn})")
    except Exception as e:
        log(f"[IG Snapshots] Error: {e}")


async def auto_link_reels(db_path: Path) -> dict:
    """
    Fetch recent Reels from the IG account, match each to an unlinked news post
    by publication timestamp (±4h window), save instagram_media_id + current stats.
    Returns {"linked": N, "skipped": M, "errors": [...]}
    """
    from datetime import timezone as _tz
    token = os.environ.get("META_ACCESS_TOKEN", "")
    if not token:
        return {"linked": 0, "skipped": 0, "errors": ["META_ACCESS_TOKEN не задан"]}

    # 1. Fetch recent media — try each configured account ID
    media_data: dict = {"error": "no account ID configured"}
    async with httpx.AsyncClient(timeout=30) as c:
        for ig_id in _ig_ids():
            r = await c.get(
                f"{GRAPH_URL}/{ig_id}/media",
                params={"access_token": token,
                        "fields": "id,media_type,timestamp,caption",
                        "limit": 50},
            )
            media_data = r.json()
            if "error" not in media_data:
                break

    if "error" in media_data:
        return {"linked": 0, "skipped": 0, "errors": [str(media_data["error"])]}

    reels = [m for m in media_data.get("data", [])
             if m.get("media_type") in ("REELS", "VIDEO")]

    # 2. Already-linked media IDs + unlinked news posts
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    linked_ids = {
        row[0] for row in conn.execute(
            "SELECT instagram_media_id FROM news "
            "WHERE instagram_media_id IS NOT NULL AND instagram_media_id != ''"
        ).fetchall()
    }
    unlinked = conn.execute(
        """SELECT id, title, published_at FROM news
           WHERE (instagram_media_id IS NULL OR instagram_media_id = '')
             AND published_at IS NOT NULL
           ORDER BY published_at DESC LIMIT 200"""
    ).fetchall()
    conn.close()

    linked_count = 0
    skipped = 0
    errors: list[str] = []

    for reel in reels:
        media_id = reel["id"]
        if media_id in linked_ids:
            skipped += 1
            continue

        # Parse IG timestamp (UTC)
        try:
            reel_dt = datetime.fromisoformat(
                reel.get("timestamp", "").replace("Z", "+00:00")
            ).astimezone(_tz.utc).replace(tzinfo=None)
        except Exception:
            skipped += 1
            continue

        # Find closest unlinked news post within ±4 hours
        best_id, best_title, best_delta = None, "", timedelta(hours=4)
        for row in unlinked:
            try:
                pub_dt = datetime.fromisoformat(
                    str(row["published_at"]).replace("Z", "").split("+")[0]
                )
            except Exception:
                continue
            d = abs(reel_dt - pub_dt)
            if d < best_delta:
                best_delta = d
                best_id    = row["id"]
                best_title = row["title"]

        if not best_id:
            skipped += 1
            continue

        # Fetch insights + link
        try:
            ins_data = await _ig_get(f"{media_id}/insights", {
                "metric": "plays,reach,saved,shares,ig_reels_avg_watch_time",
            })
            med_data = await _ig_get(media_id, {"fields": "like_count,comments_count"})
            if "error" in ins_data or "error" in med_data:
                errors.append(f"{media_id}: {ins_data.get('error') or med_data.get('error')}")
                continue

            ins     = {i["name"]: i.get("value", 0) for i in ins_data.get("data", [])}
            avg_ms  = ins.get("ig_reels_avg_watch_time", 0)
            wt      = f"{avg_ms / 1000:.1f}с" if avg_ms else ""

            conn = sqlite3.connect(str(db_path))
            conn.execute(
                """UPDATE news SET
                       instagram_media_id=?,
                       stats_views=?,  stats_likes=?,    stats_comments=?,
                       stats_shares=?, stats_saves=?,    stats_reach=?,
                       stats_watch_time=?, stats_platform='instagram'
                   WHERE id=?""",
                (media_id,
                 ins.get("plays", 0), med_data.get("like_count", 0),
                 med_data.get("comments_count", 0),
                 ins.get("shares", 0), ins.get("saved", 0), ins.get("reach", 0),
                 wt, best_id),
            )
            conn.commit()
            conn.close()

            linked_ids.add(media_id)
            linked_count += 1
            log(f"[AutoSync] Reel {media_id} → #{best_id} «{best_title[:40]}» Δ={best_delta}")

        except Exception as e:
            errors.append(f"{media_id}: {e}")

    return {"linked": linked_count, "skipped": skipped, "errors": errors}


async def _scheduled_auto_sync():
    """Every-6h job: auto-link unmatched IG reels to news posts."""
    try:
        res = await auto_link_reels(DB_PATH)
        if res["linked"] > 0:
            log(f"[AutoSync] Linked {res['linked']} reels, skipped {res['skipped']}")
    except Exception as e:
        log(f"[AutoSync] Error: {e}")


@app.post("/api/meta/auto-sync")
async def api_auto_sync(request: Request):
    """Manually trigger auto-link of Instagram Reels to news posts."""
    require_auth(request)
    result = await auto_link_reels(DB_PATH)
    return result


async def _scheduled_ig_refresh():
    """Daily job: silently refresh Instagram stats for all linked reels."""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT id, instagram_media_id FROM news "
            "WHERE instagram_media_id IS NOT NULL AND instagram_media_id != ''"
        ).fetchall()
        conn.close()

        if not rows:
            return

        updated = 0
        for (news_id, media_id) in rows:
            try:
                ins_data = await _ig_get(f"{media_id}/insights", {
                    "metric": "plays,reach,saved,shares,ig_reels_avg_watch_time",
                })
                med_data = await _ig_get(media_id, {"fields": "like_count,comments_count"})

                if "error" in ins_data:
                    continue

                insights  = {i["name"]: i.get("value", 0) for i in ins_data.get("data", [])}
                avg_ms    = insights.get("ig_reels_avg_watch_time", 0)
                watch_str = f"{avg_ms / 1000:.1f}с" if avg_ms else ""

                conn = sqlite3.connect(DB_PATH)
                conn.execute(
                    """UPDATE news SET
                           stats_views=?, stats_likes=?, stats_comments=?,
                           stats_shares=?, stats_saves=?, stats_reach=?,
                           stats_watch_time=?
                       WHERE id=?""",
                    (
                        insights.get("plays", 0),
                        med_data.get("like_count", 0),
                        med_data.get("comments_count", 0),
                        insights.get("shares", 0),
                        insights.get("saved", 0),
                        insights.get("reach", 0),
                        watch_str,
                        news_id,
                    ),
                )
                conn.commit()
                conn.close()
                updated += 1
            except Exception:
                pass

        log(f"[IG Auto-refresh] Updated {updated}/{len(rows)} reels stats")
        if updated > 0:
            _tg_notify(f"📊 Instagram статистика обновлена: {updated} рилс")

    except Exception as e:
        log(f"[IG Auto-refresh] Error: {e}")


# ── TELEGRAM DIRECT PUBLISH ───────────────────────────────────────────────────
#
# Публикует одобренную новость из news-таблицы прямо в Telegram-канал.
# ENV: TG_BOT_TOKEN, TG_MY_CHANNEL


_TG_TOPIC_EMOJI = [
    (["штраф","мошенни","взятк","арест","задержа","уголовн"],             "🚨"),
    (["погиб","жертв","пожар","авари","трагед","землетряс"],              "⚠️"),
    (["цен","подорожа","инфляц","курс","доллар","сум","деньг"],           "💸"),
    (["налог","бюджет","эконом","инвестиц","миллиард"],                   "💰"),
    (["закон","указ","постановлен","суд","реформ"],                       "⚖️"),
    (["президент","правительств","министр","парламент"],                  "🏛️"),
    (["транспорт","дорог","метро","авиа","аэропорт"],                     "🚌"),
    (["здоров","больниц","медицин","врач","лекарств"],                    "🏥"),
    (["образован","школ","универс","учеб","студент"],                     "📚"),
    (["технолог","цифров","интернет","it ","ai ","приложен"],             "💻"),
    (["туризм","отдых","визы","отель","путешеств"],                       "✈️"),
    (["строительств","жильё","квартир","снос","застройщ"],                "🏗️"),
    (["энергетик","коммунал","жкх","газ","свет","электр","отключен"],     "⚡️"),
]


def _tg_two_emoji(text: str) -> str:
    t = (text or "").lower()
    matched = [e for kws, e in _TG_TOPIC_EMOJI if any(k in t for k in kws)]
    if len(matched) >= 2:
        return matched[0] + matched[1]
    if matched:
        return "📢" + matched[0]
    return "📰💬"


def _tg_format_news(item: dict) -> str:
    """Форматировать пост для Telegram из новости MediaHub."""
    tts   = (item.get("tts_script") or "").strip()
    title = (item.get("title") or "").strip()
    desc  = (item.get("description") or "").strip()

    prefix  = _tg_two_emoji(title + " " + tts + " " + desc)
    body    = tts or desc[:700]
    channel = os.environ.get("TG_MY_CHANNEL", "@uzbekknows").lstrip("@")
    tagline = os.environ.get("TG_CHANNEL_TAGLINE", "подпишись на свежие новости!")

    parts = [f"{prefix} <b>{title}</b>", ""]
    if body:
        parts.append(body)
    parts += ["", "Распространите сообщение", "", f"👉 @{channel} — {tagline}"]
    return "\n".join(parts)


@app.post("/api/news/{news_id}/publish-tg")
async def api_publish_tg(news_id: int, request: Request):
    """Опубликовать новость в Telegram-канал напрямую через Bot API."""
    require_auth(request)

    bot_token = os.environ.get("TG_BOT_TOKEN", "")
    channel   = os.environ.get("TG_MY_CHANNEL", "")
    if not bot_token or not channel:
        raise HTTPException(
            status_code=500,
            detail="TG_BOT_TOKEN / TG_MY_CHANNEL не заданы в Railway Variables",
        )

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM news WHERE id=?", (news_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Новость не найдена")

    text = _tg_format_news(dict(row))

    r = req_lib.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={
            "chat_id": channel,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    result = r.json()
    if not result.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=f"Telegram API: {result.get('description', str(result))}",
        )

    msg_id    = result.get("result", {}).get("message_id")
    ch_clean  = channel.lstrip("@")
    permalink = f"https://t.me/{ch_clean}/{msg_id}" if msg_id else ""

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE news SET published_at=COALESCE(published_at,?), starred=0 WHERE id=?",
        (now_iso, news_id),
    )
    conn.commit()
    conn.close()

    log(f"TG published news #{news_id} → {permalink}")
    return {"published": True, "msg_id": msg_id, "permalink": permalink, "channel": channel}


@app.get("/api/tg/status")
async def api_tg_status(request: Request):
    """Статус Telegram-бота: переменные окружения + очередь."""
    require_auth(request)

    queue_pending = 0
    queue_published = 0
    last_pub = None
    tg_queue_exists = False

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("SELECT 1 FROM tg_queue LIMIT 1")
        tg_queue_exists = True
        r1 = conn.execute("SELECT COUNT(*) FROM tg_queue WHERE published=0").fetchone()
        r2 = conn.execute("SELECT COUNT(*) FROM tg_queue WHERE published=1").fetchone()
        r3 = conn.execute(
            "SELECT published_at FROM tg_queue WHERE published=1 ORDER BY published_at DESC LIMIT 1"
        ).fetchone()
        queue_pending   = r1[0] if r1 else 0
        queue_published = r2[0] if r2 else 0
        last_pub        = r3[0] if r3 else None
        conn.close()
    except Exception:
        pass

    return {
        "has_bot_token":   bool(os.environ.get("TG_BOT_TOKEN")),
        "has_channel":     bool(os.environ.get("TG_MY_CHANNEL")),
        "channel":         os.environ.get("TG_MY_CHANNEL", ""),
        "has_notify_chat": bool(os.environ.get("TG_NOTIFY_CHAT")),
        "has_api_id":      bool(os.environ.get("TG_API_ID")),
        "has_api_hash":    bool(os.environ.get("TG_API_HASH")),
        "has_session":     bool(os.environ.get("TG_SESSION_B64")),
        "tg_queue_exists": tg_queue_exists,
        "queue_pending":   queue_pending,
        "queue_published": queue_published,
        "last_published":  last_pub,
    }


@app.post("/api/tg/publish-now")
async def api_tg_publish_now(request: Request):
    """Принудительно запустить публикацию из tg_queue (тест)."""
    require_auth(request)
    try:
        from src.tg_publisher import maybe_publish
        result = await asyncio.to_thread(maybe_publish)
        return {"published": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
