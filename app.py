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
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
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
PREFS_PATH     = BASE_DIR / "config" / "preferences.yaml"
SYSPROMPT_PATH = BASE_DIR / "config" / "system_prompt.md"
APP_PASSWORD   = os.environ.get("APP_PASSWORD", "")


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
    ]:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass
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


# ── SCHEDULER ─────────────────────────────────────────────────────────────

async def scheduled_collect():
    log("=== Scheduled collect ===")
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
        _tg_notify(f"❌ Ошибка сбора: {str(e)[:120]}")


scheduler = AsyncIOScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    run_migrations(conn)
    conn.close()

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
    rows = conn.execute("SELECT score FROM news WHERE score >= 1").fetchall()
    conn.close()
    counts = {"all": 0, "hot": 0, "good": 0, "reserve": 0}
    for (score,) in rows:
        p = _score_to_priority(score)
        counts[p] += 1
        counts["all"] += 1
    return counts


@app.get("/api/news")
async def api_news(
    request: Request,
    min_score: int = 1,
    limit: int = 150,
    tab: str = "all",    # all | hot | good | reserve | approved
    sort: str = "score", # score | date
):
    require_auth(request)

    where = ["score >= ?"]
    params: list = [min_score]

    if tab == "approved":
        where.append("approved = 1")
    elif tab == "hot":
        where.append("score >= 6")
    elif tab == "good":
        where.append("score >= 3 AND score < 6")
    elif tab == "reserve":
        where.append("score >= 1 AND score < 3")

    order = "score DESC, collected DESC" if sort == "score" else "collected DESC"

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""SELECT id, title, url, source, published, score,
                   tts_script, approved, generated_at, collected, sent_tg,
                   reviewed_script, gpt_comment, description, preview_titles
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
    background_tasks.add_task(scheduled_collect)
    return {"status": "started"}


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


@app.delete("/api/news/{news_id}")
async def api_delete(news_id: int, request: Request):
    require_auth(request)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM news WHERE id=?", (news_id,))
    conn.commit()
    conn.close()
    return {"id": news_id, "deleted": True}


# ── API: ELEVENLABS CREDITS ───────────────────────────────────────────────

def _get_el_credits_sync(api_key: str) -> int:
    """Returns remaining characters for an ElevenLabs account. -1 on error."""
    try:
        r = req_lib.get(
            "https://api.elevenlabs.io/v1/user",
            headers={"xi-api-key": api_key},
            timeout=10,
        )
        if r.ok:
            sub = r.json().get("subscription", {})
            return sub.get("character_limit", 0) - sub.get("character_count", 0)
    except Exception:
        pass
    return -1


async def _pick_el_account() -> tuple[str, str, int]:
    """Auto-select ElevenLabs account with most remaining credits.
    Returns (api_key, voice_id, account_number).
    """
    best = (-2, "", "", 0)
    for acc in [1, 2]:
        api_key  = os.environ.get(f"ELEVENLABS_API_KEY_{acc}", "")
        voice_id = os.environ.get(f"ELEVENLABS_VOICE_ID_{acc}", "")
        if not api_key or not voice_id:
            continue
        credits = await asyncio.to_thread(_get_el_credits_sync, api_key)
        log(f"ElevenLabs acc{acc}: {credits} chars remaining")
        if credits > best[0]:
            best = (credits, api_key, voice_id, acc)

    if not best[1]:
        raise HTTPException(
            status_code=500,
            detail="Нет настроенных ElevenLabs аккаунтов. Добавьте ELEVENLABS_API_KEY_1 и ELEVENLABS_VOICE_ID_1."
        )
    if best[0] == 0:
        raise HTTPException(
            status_code=402,
            detail="На обоих аккаунтах ElevenLabs закончились кредиты. Пополните баланс."
        )
    return best[1], best[2], best[3]


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
        "[Описание для поста — детальнее TTS, абзацы с отступами, цифры, почему важно, вопрос]\n\n"
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
         datetime.now().isoformat(), news_id),
    )
    conn.commit()
    conn.close()

    return {
        "id": news_id,
        "tts_script": script,
        "description": description,
        "preview_titles": previews,
    }
