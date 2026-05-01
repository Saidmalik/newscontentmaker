"""
tg_auto_worker.py — Автономный Telegram-постинг из таблицы news.

РЕЖИМ 1 (авто): выбирает лучшую новость score >= 5 за последние 12ч,
генерирует короткий пост через Claude Haiku, публикует в канал.

РЕЖИМ 2 (ручной): вызывается из дашборда, генерирует детальный пост
через Claude Sonnet и возвращает текст для предпросмотра.

Расписание: 09:00, 12:00, 17:00, 20:00 UZT (04:00, 07:00, 12:00, 15:00 UTC)
Лимиты: макс 4 поста/день, мин 2ч между постами.

ENV:
  TG_BOT_TOKEN      — токен бота (@BotFather)
  TG_MY_CHANNEL     — @канал или числовой ID
  TG_NOTIFY_CHAT    — твой личный chat_id для уведомлений
  TG_AUTO_ENABLED   — "1" чтобы включить (по умолчанию выключен)
  TG_AUTO_MAX_DAY   — макс постов в день (default: 4)
  TG_AUTO_MIN_SCORE — мин score (default: 5)
  ANTHROPIC_API_KEY — для генерации текста
"""

import os
import re
import json
import hashlib
import sqlite3
import logging
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = Path(os.environ.get("DB_PATH", str(BASE_DIR / "data" / "news.db")))


def _tok() -> str:
    """Bot token: TG_BOT_TOKEN → fallback TELEGRAM_BOT_TOKEN."""
    return (os.environ.get("TG_BOT_TOKEN") or
            os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()

def _channel() -> str:
    """Channel to post: TG_MY_CHANNEL."""
    return (os.environ.get("TG_MY_CHANNEL") or "").strip()

def _notify_chat() -> str:
    """Personal chat for notifications: TG_NOTIFY_CHAT → fallback TELEGRAM_CHAT_ID."""
    return (os.environ.get("TG_NOTIFY_CHAT") or
            os.environ.get("TELEGRAM_CHAT_ID") or "").strip()

def _anthropic_key() -> str:
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()

UZT = timedelta(hours=5)

log = logging.getLogger("tg_auto")


# ── НАСТРОЙКИ (читаем из ENV, можно переопределить через preferences.yaml) ──

def _cfg_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _settings() -> dict:
    return {
        "enabled":   os.environ.get("TG_AUTO_ENABLED", "0") == "1",
        "max_day":   _cfg_int("TG_AUTO_MAX_DAY", 4),
        "min_score": _cfg_int("TG_AUTO_MIN_SCORE", 5),
        "min_gap_h": _cfg_int("TG_AUTO_MIN_GAP_H", 2),
        # Часы публикации UTC (= UZT - 5)
        "hours_utc": [4, 7, 12, 15],   # = 09, 12, 17, 20 по Ташкенту
    }


# ── ЭМОДЗИ ───────────────────────────────────────────────────────────────────

TOPIC_EMOJI = [
    (["штраф", "мошенни", "взятк", "арест", "задержа", "тюрьм", "уголовн"],    "🚨"),
    (["погиб", "жертв", "пожар", "авари", "трагед", "землетряс"],               "⚠️"),
    (["цен", "подорожа", "инфляц", "курс", "доллар", "сум", "деньг"],          "💸"),
    (["налог", "бюджет", "эконом", "инвестиц", "миллиард", "миллион"],         "💰"),
    (["закон", "указ", "постановлен", "суд", "приговор", "реформ"],            "⚖️"),
    (["президент", "правительств", "министр", "парламент", "сенат"],           "🏛️"),
    (["транспорт", "дорог", "метро", "авиа", "поезд", "аэропорт"],             "🚌"),
    (["здоров", "больниц", "медицин", "лекарств", "врач"],                     "🏥"),
    (["образован", "школ", "универс", "студент", "учеб"],                      "📚"),
    (["технолог", "цифров", "интернет", "it ", "приложен"],                    "💻"),
    (["туризм", "отдых", "виз", "путешеств"],                                  "✈️"),
    (["строительств", "жильё", "квартир", "снос"],                             "🏗️"),
    (["энергетик", "коммунал", "жкх", "свет", "газ", "отключен"],             "⚡"),
]


def _pick_emoji(text: str) -> str:
    t = (text or "").lower()
    for keywords, emoji in TOPIC_EMOJI:
        if any(k in t for k in keywords):
            return emoji
    return "📰"


# ── BOT API ──────────────────────────────────────────────────────────────────

def _bot(method: str, **kwargs) -> dict:
    tok = _tok()
    if not tok:
        raise RuntimeError("TG_BOT_TOKEN / TELEGRAM_BOT_TOKEN не задан")
    url = f"https://api.telegram.org/bot{tok}/{method}"
    r = requests.post(url, timeout=30, **kwargs)
    return r.json()


def _send_text(chat: str, text: str) -> dict:
    return _bot("sendMessage", json={
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    })


def _notify(text: str):
    nc = _notify_chat()
    if nc and _tok():
        try:
            _send_text(nc, text)
        except Exception as e:
            log.error(f"Notify error: {e}")


# ── БД HELPERS ────────────────────────────────────────────────────────────────

def _posts_today(conn: sqlite3.Connection) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r = conn.execute(
        "SELECT COUNT(*) FROM news WHERE tg_auto_published=1 AND tg_published_at LIKE ?",
        (f"{today}%",)
    ).fetchone()
    return r[0] if r else 0


def _gap_hours(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT tg_published_at FROM news "
        "WHERE tg_auto_published=1 OR tg_manual_published=1 "
        "ORDER BY tg_published_at DESC LIMIT 1"
    ).fetchone()
    if not row or not row[0]:
        return 9999.0
    try:
        last = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() / 3600
    except Exception:
        return 9999.0


def _topic_hash(title: str) -> str:
    """Хэш из первых 4 значимых слов заголовка для антидубля."""
    words = re.findall(r"[а-яёa-z]{4,}", (title or "").lower())
    key = " ".join(words[:4])
    return hashlib.md5(key.encode()).hexdigest()


def _is_topic_duplicate(conn: sqlite3.Connection, topic_hash: str) -> bool:
    """Проверить что похожая тема не публиковалась за последние 24ч."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM news WHERE tg_topic_hash=? AND "
        "(tg_auto_published=1 OR tg_manual_published=1) AND "
        "tg_published_at > ?",
        (topic_hash, since)
    ).fetchone()
    return row is not None


def _pick_best_news(conn: sqlite3.Connection, min_score: int,
                    lookback_hours: int = 12) -> dict | None:
    """Выбрать лучшую непубликовавшуюся новость за последние N часов."""
    since = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, title, summary, source, url, score, published
        FROM news
        WHERE tg_auto_published = 0
          AND score >= ?
          AND (published > ? OR collected > ?)
        ORDER BY score DESC, published DESC
        LIMIT 20
    """, (min_score, since, since)).fetchall()
    conn.row_factory = None

    for row in rows:
        d = dict(row)
        th = _topic_hash(d["title"])
        if not _is_topic_duplicate(conn, th):
            d["topic_hash"] = th
            return d
    return None


def _mark_auto_published(conn: sqlite3.Connection, news_id: int,
                         msg_id: int | None, post_text: str, topic_hash: str):
    conn.execute("""
        UPDATE news SET
            tg_auto_published = 1,
            tg_published_at   = datetime('now'),
            tg_message_id     = ?,
            tg_short_post     = ?,
            tg_topic_hash     = ?
        WHERE id = ?
    """, (msg_id, post_text, topic_hash, news_id))
    conn.commit()


def _mark_manual_published(conn: sqlite3.Connection, news_id: int,
                           msg_id: int | None, post_text: str):
    conn.execute("""
        UPDATE news SET
            tg_manual_published = 1,
            tg_published_at     = datetime('now'),
            tg_message_id       = ?,
            tg_long_post        = ?
        WHERE id = ?
    """, (msg_id, post_text, news_id))
    conn.commit()


# ── CLAUDE ГЕНЕРАЦИЯ ──────────────────────────────────────────────────────────

def _generate_short_post(title: str, summary: str, source: str, url: str) -> str:
    """
    Режим 1 — короткий автопост через Claude Haiku (дёшево).
    Если API недоступен — форматируем сами.
    """
    if not _anthropic_key():
        return _format_short_fallback(title, summary, source, url)

    import anthropic
    akey = _anthropic_key()
    emoji = _pick_emoji(title + " " + (summary or ""))
    prompt = f"""Ты редактор Telegram-канала «Новости Узбекистана». Пиши кратко и цепляюще.

Заголовок: {title}
Краткое описание: {summary or "нет"}
Источник: {source}

Напиши короткий Telegram-пост. Формат СТРОГО:

HEADER: {emoji} [цепляющий заголовок 5-8 слов, без кавычек]
BODY: [3-4 предложения. Суть + ключевой факт + последствие для людей. Без воды.]
FACT: [самый важный факт одной строкой, выделить жирным через HTML <b>...</b>]

Требования:
- Без официоза и канцелярита
- Язык живой, разговорный
- Только факты, без оценок"""

    try:
        client = anthropic.Anthropic(api_key=akey)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        return _parse_short_post(raw, title, summary, source, url, emoji)
    except Exception as e:
        log.error(f"Claude short post error: {e}")
        return _format_short_fallback(title, summary, source, url)


def _parse_short_post(raw: str, title: str, summary: str,
                      source: str, url: str, emoji: str) -> str:
    header = body = fact = ""
    for line in raw.splitlines():
        s = line.strip()
        if s.upper().startswith("HEADER:"):
            header = s[7:].strip()
        elif s.upper().startswith("BODY:"):
            body = s[5:].strip()
        elif s.upper().startswith("FACT:"):
            fact = s[5:].strip()
        elif body and s:
            body += " " + s

    if not header:
        header = f"{emoji} {title[:60]}"
    if not body:
        body = (summary or title)[:300]

    ch = _channel().lstrip("@")
    parts = [f"<b>{header}</b>", "", body]
    if fact:
        parts += ["", fact]
    parts += ["", f"📎 Источник: {source}"]
    if ch:
        parts += ["", f"👉 @{ch}"]

    return "\n".join(parts)


def _format_short_fallback(title: str, summary: str, source: str, url: str) -> str:
    """Fallback без Claude."""
    emoji = _pick_emoji(title + " " + (summary or ""))
    ch = _channel().lstrip("@")
    parts = [
        f"<b>{emoji} {title}</b>",
        "",
        (summary or "")[:400],
        "",
        f"📎 Источник: {source}",
    ]
    if url:
        parts += [f"🔗 {url}"]
    if ch:
        parts += ["", f"👉 @{ch}"]
    return "\n".join(p for p in parts if p is not None)


def generate_long_post(news_item: dict) -> str:
    """
    Режим 2 — детальный пост через Claude Sonnet.
    Возвращает готовый HTML-текст для Telegram.
    """
    title       = news_item.get("title", "")
    description = news_item.get("description", "")
    tts_script  = news_item.get("tts_script", "")
    summary     = news_item.get("summary", "")
    source      = news_item.get("source", "")
    url         = news_item.get("url", "")

    akey = _anthropic_key()
    if not akey:
        return _format_short_fallback(title, summary or description, source, url)

    import anthropic
    prompt = f"""Ты журналист новостного Telegram-канала про Узбекистан.
Напиши детальный разбор новости для Telegram.

Новость: {title}
Описание: {description or summary or "нет"}
Скрипт: {tts_script[:1000] if tts_script else "нет"}
Источник: {source}

Ответь СТРОГО в JSON:
{{
  "header": "эмодзи + цепляющий заголовок (без кавычек)",
  "intro": "первый абзац — главный факт, 2 предложения",
  "context": "абзац с контекстом — почему важно, предыстория, 2-3 предложения",
  "details": "абзац с деталями — цифры, имена, даты, 2-3 предложения",
  "impact": "что это значит для жителей Ташкента, 1-2 предложения",
  "conclusion": "финальная мысль или вовлекающий вопрос",
  "hashtags": "#узбекистан + 3-4 тематических тега через пробел"
}}

Требования:
- Живой язык, без канцелярита
- Только факты из текста, без домыслов
- Если данных для раздела нет — пропусти (пустая строка "")"""

    try:
        client = anthropic.Anthropic(api_key=akey)
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        # Вытащить JSON из ответа
        m = re.search(r"\{[\s\S]+\}", raw)
        if not m:
            raise ValueError("JSON not found in response")
        data = json.loads(m.group())
        return _format_long_post(data, source, url)
    except Exception as e:
        log.error(f"Claude long post error: {e}")
        return _format_short_fallback(title, summary or description, source, url)


def _format_long_post(data: dict, source: str, url: str) -> str:
    ch = _channel().lstrip("@")
    parts = []

    if data.get("header"):
        parts += [f"<b>{data['header']}</b>", ""]
    if data.get("intro"):
        parts += [data["intro"], ""]
    if data.get("context"):
        parts += [data["context"], ""]
    if data.get("details"):
        parts += [data["details"], ""]
    if data.get("impact"):
        parts += [f"🎯 {data['impact']}", ""]
    if data.get("conclusion"):
        parts += [f"💬 {data['conclusion']}", ""]
    if data.get("hashtags"):
        parts += [data["hashtags"], ""]

    parts += [f"📎 Источник: {source}"]
    if url:
        parts += [f"🔗 {url}"]
    if ch:
        parts += ["", f"👉 @{ch}"]

    return "\n".join(parts)


# ── ПУБЛИКАЦИЯ ────────────────────────────────────────────────────────────────

def _publish_to_channel(text: str) -> tuple[bool, int | None]:
    ch = _channel()
    if not _tok() or not ch:
        log.warning(f"TG_BOT_TOKEN={bool(_tok())} TG_MY_CHANNEL={repr(ch)} — не заданы")
        return False, None
    try:
        result = _send_text(ch, text)
        if result.get("ok"):
            msg_id = result.get("result", {}).get("message_id")
            return True, msg_id
        log.error(f"Telegram API: {result.get('description', result)}")
        return False, None
    except Exception as e:
        log.error(f"Publish error: {e}")
        return False, None


# ── ГЛАВНАЯ ФУНКЦИЯ (Режим 1 — авто) ─────────────────────────────────────────

def diagnose(db_path: Path | None = None) -> dict:
    """Диагностика: проверить все условия без публикации."""
    cfg = _settings()
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))
    today_count = _posts_today(conn)
    gap_h = _gap_hours(conn)
    item = _pick_best_news(conn, cfg["min_score"])
    conn.close()

    # Test token by calling getMe
    bot_info = None
    tok = _tok()
    if tok:
        try:
            r = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10)
            bot_info = r.json().get("result", {}).get("username")
        except Exception as e:
            bot_info = f"error: {e}"

    return {
        "enabled":       cfg["enabled"],
        "token_set":     bool(tok),
        "bot_username":  bot_info,
        "channel":       _channel(),
        "notify_chat":   _notify_chat(),
        "min_score":     cfg["min_score"],
        "max_day":       cfg["max_day"],
        "min_gap_h":     cfg["min_gap_h"],
        "today_posts":   today_count,
        "gap_hours":     round(gap_h, 1),
        "best_news":     {"id": item["id"], "title": item["title"][:80], "score": item["score"]} if item else None,
        "will_post":     cfg["enabled"] and bool(tok) and bool(_channel()) and today_count < cfg["max_day"] and gap_h >= cfg["min_gap_h"] and item is not None,
    }


def run_auto_post(db_path: Path | None = None, force: bool = False) -> dict:
    """
    Проверить очередь и опубликовать один пост если настало время.
    force=True — игнорировать enabled, лимит дня и интервал (для теста).
    """
    cfg = _settings()

    if not cfg["enabled"] and not force:
        return {"status": "disabled", "hint": "Поставь TG_AUTO_ENABLED=1 или используй force=true для теста"}

    if not _tok() or not _channel():
        return {"status": "no_token", "detail": f"token={bool(_tok())} channel={repr(_channel())}"}

    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))

    # Лимит дня
    today_count = _posts_today(conn)
    if not force and today_count >= cfg["max_day"]:
        conn.close()
        log.info(f"TG авто: лимит дня {today_count}/{cfg['max_day']}")
        return {"status": "limit_reached", "today": today_count}

    # Минимальный интервал
    gap_h = _gap_hours(conn)
    if not force and gap_h < cfg["min_gap_h"]:
        conn.close()
        log.info(f"TG авто: слишком рано, прошло {gap_h:.1f}ч (мин {cfg['min_gap_h']}ч)")
        return {"status": "too_soon", "gap_h": gap_h}

    # Выбрать новость (при force берём за последние 72ч с мин score=1)
    item = _pick_best_news(conn,
                           min_score=cfg["min_score"] if not force else 1,
                           lookback_hours=12 if not force else 72)
    if not item:
        conn.close()
        log.info("TG авто: нет подходящих новостей")
        return {"status": "no_news"}

    # Генерировать пост
    post_text = _generate_short_post(
        title=item["title"],
        summary=item.get("summary", ""),
        source=item.get("source", ""),
        url=item.get("url", ""),
    )

    # Публиковать
    ok, msg_id = _publish_to_channel(post_text)
    if not ok:
        conn.close()
        return {"status": "publish_failed"}

    # Сохранить в БД
    _mark_auto_published(conn, item["id"], msg_id, post_text, item["topic_hash"])
    conn.close()

    # Уведомить в личку
    now_uzt = (datetime.now(timezone.utc) + UZT).strftime("%H:%M")
    ch = _channel().lstrip("@")
    msg_url = f"\n🔗 https://t.me/{ch}/{msg_id}" if msg_id and ch else ""
    _notify(
        f"✅ Авто: <b>{item['title'][:70]}</b>\n"
        f"⭐ Score: {item['score']} | 📡 {item.get('source','')}\n"
        f"🕐 {now_uzt}{msg_url}"
    )

    log.info(f"TG авто опубликовано: [{item['score']}] {item['title'][:60]}")
    return {"status": "published", "news_id": item["id"], "msg_id": msg_id}


# ── РУЧНАЯ ПУБЛИКАЦИЯ (Режим 2) ───────────────────────────────────────────────

def run_manual_post(news_id: int, db_path: Path | None = None,
                    custom_text: str | None = None) -> dict:
    """
    Сгенерировать детальный пост и опубликовать.
    custom_text: если передан — использовать вместо генерации.
    Возвращает dict: {status, post_text, msg_id}.
    """
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, title, summary, description, tts_script, source, url "
        "FROM news WHERE id=?", (news_id,)
    ).fetchone()
    conn.row_factory = None

    if not row:
        conn.close()
        return {"status": "not_found"}

    item = dict(row)
    post_text = custom_text if custom_text else generate_long_post(item)

    ok, msg_id = _publish_to_channel(post_text)
    if ok:
        _mark_manual_published(conn, news_id, msg_id, post_text)

    conn.close()

    if ok:
        ch = _channel().lstrip("@")
        msg_url = f"\n🔗 https://t.me/{ch}/{msg_id}" if msg_id and ch else ""
        _notify(
            f"📤 Ручная публикация: <b>{item['title'][:70]}</b>{msg_url}"
        )
        log.info(f"TG ручная публикация: {news_id}")
        return {"status": "published", "post_text": post_text, "msg_id": msg_id}

    return {"status": "publish_failed", "post_text": post_text}


# ── PREVIEW (без публикации) ──────────────────────────────────────────────────

def preview_long_post(news_id: int, db_path: Path | None = None) -> dict:
    """Сгенерировать детальный пост но НЕ публиковать. Для предпросмотра."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, title, summary, description, tts_script, source, url "
        "FROM news WHERE id=?", (news_id,)
    ).fetchone()
    conn.row_factory = None
    conn.close()

    if not row:
        return {"status": "not_found"}

    post_text = generate_long_post(dict(row))
    return {"status": "ok", "post_text": post_text}


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-16s %(levelname)s %(message)s"
    )
    if "--preview" in sys.argv:
        # python src/tg_auto_worker.py --preview <news_id>
        idx = sys.argv.index("--preview")
        nid = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 1
        r = preview_long_post(nid)
        print(r.get("post_text", r))
    elif "--manual" in sys.argv:
        idx = sys.argv.index("--manual")
        nid = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 1
        r = run_manual_post(nid)
        print(r)
    else:
        r = run_auto_post()
        print(r)
