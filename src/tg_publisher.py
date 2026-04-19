"""
tg_publisher.py — Публикация из tg_queue в Telegram-канал через Bot API.

Ограничения:
  - Не более 5 постов в день
  - Минимум 90 мин между постами
  - Рабочие часы: 09:00–21:00 по Ташкенту (UTC+5)

ENV:
  TG_BOT_TOKEN    — токен бота (@BotFather)
  TG_MY_CHANNEL   — @твойканал (куда постить)
  TG_NOTIFY_CHAT  — твой личный chat_id для уведомлений
"""

import os
import sqlite3
import logging
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

DB_PATH        = Path(os.environ.get("DB_PATH", str(BASE_DIR / "data" / "news.db")))
TG_BOT_TOKEN   = os.environ.get("TG_BOT_TOKEN", "")
TG_MY_CHANNEL  = os.environ.get("TG_MY_CHANNEL", "")   # @channel
TG_NOTIFY_CHAT = os.environ.get("TG_NOTIFY_CHAT", "")  # личный chat_id

MAX_PER_DAY  = 5
MIN_GAP_MIN  = 90
UZT          = timedelta(hours=5)

# Эмодзи подбирается по ключевым словам в тексте
TOPIC_EMOJI = [
    (["штраф", "мошенни", "взятк", "арест", "задержа", "тюрьм", "уголовн"],  "🚨"),
    (["погиб", "жертв", "пожар", "авари", "трагед", "землетряс"],             "⚠️"),
    (["цен", "подорожа", "инфляц", "курс", "доллар", "сум", "деньг"],        "💸"),
    (["налог", "бюджет", "эконом", "инвестиц", "миллиард", "миллион"],       "💰"),
    (["закон", "указ", "постановлен", "суд", "приговор", "реформ"],          "⚖️"),
    (["президент", "правительств", "министр", "парламент", "сенат"],         "🏛️"),
    (["транспорт", "дорог", "метро", "авиа", "поезд", "аэропорт"],           "🚌"),
    (["здоров", "больниц", "медицин", "лекарств", "врач"],                   "🏥"),
    (["образован", "школ", "универс", "студент", "учеб"],                    "📚"),
    (["технолог", "цифров", "интернет", "it ", "приложен", "ai "],           "💻"),
    (["туризм", "отдых", "визы", "путешеств", "отель"],                      "✈️"),
    (["строительств", "жильё", "квартир", "снос", "застройщ"],               "🏗️"),
    (["энергетик", "свет", "газ", "отключен", "электр"],                     "⚡"),
]

log = logging.getLogger("tg_publisher")


# ── BOT API ──────────────────────────────────────────────────────────────────

def _api(method: str, **kwargs) -> dict:
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}"
    r = requests.post(url, timeout=30, **kwargs)
    return r.json()


def _send_text(chat: str, text: str) -> dict:
    return _api("sendMessage", json={
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def _send_photo(chat: str, path: str, caption: str) -> dict:
    with open(path, "rb") as f:
        return _api("sendPhoto", data={
            "chat_id": chat,
            "caption": caption,
            "parse_mode": "HTML",
        }, files={"photo": f})


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _pick_emoji(text: str) -> str:
    t = (text or "").lower()
    for keywords, emoji in TOPIC_EMOJI:
        if any(k in t for k in keywords):
            return emoji
    return "📰"


def _posts_today(conn: sqlite3.Connection) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r = conn.execute(
        "SELECT COUNT(*) FROM tg_queue WHERE published=1 AND published_at LIKE ?",
        (f"{today}%",)
    ).fetchone()
    return r[0] if r else 0


def _gap_minutes(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT published_at FROM tg_queue WHERE published=1 "
        "ORDER BY published_at DESC LIMIT 1"
    ).fetchone()
    if not row or not row[0]:
        return 9999.0
    try:
        last = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() / 60
    except Exception:
        return 9999.0


def _is_working_hours() -> bool:
    """Разрешить публикацию только 09:00–21:00 по Ташкенту."""
    h = (datetime.now(timezone.utc) + UZT).hour
    return 9 <= h <= 21


def _next_item(conn: sqlite3.Connection) -> dict | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT * FROM tg_queue
        WHERE published = 0
        ORDER BY score DESC, created_at ASC
        LIMIT 1
    """).fetchone()
    conn.row_factory = None
    return dict(row) if row else None


def _mark_published(conn: sqlite3.Connection, item_id: int, msg_id: int | None):
    conn.execute("""
        UPDATE tg_queue
        SET published=1,
            published_at=datetime('now'),
            tg_msg_id=COALESCE(?, tg_msg_id)
        WHERE id=?
    """, (msg_id, item_id))
    conn.commit()


def _format_post(item: dict) -> str:
    """Собрать текст поста в формате HTML для Telegram."""
    headline = (item.get("headline") or "").strip()
    summary  = (item.get("summary") or "").strip()
    source   = (item.get("source_channel") or "").strip()
    combined = (item.get("translated_text") or "") + " " + headline
    emoji    = _pick_emoji(combined)

    lines = []
    if headline:
        lines.append(f"{emoji} <b>{headline}</b>")
        lines.append("")
    if summary:
        lines.append(summary)
        lines.append("")
    if source:
        lines.append(f"Источник: @{source}")

    return "\n".join(lines)


def _notify_self(item: dict, msg_id: int | None):
    """Отправить уведомление тебе в личку после публикации."""
    if not TG_NOTIFY_CHAT or not TG_BOT_TOKEN:
        return
    headline = item.get("headline", "—")
    score    = item.get("score", 0)
    source   = item.get("source_channel", "—")
    text = (
        f"✅ Опубликовано: <b>{headline}</b>\n"
        f"📊 Score: {score} | Источник: @{source}"
    )
    if msg_id and TG_MY_CHANNEL:
        ch = TG_MY_CHANNEL.lstrip("@")
        text += f"\n🔗 https://t.me/{ch}/{msg_id}"
    try:
        _send_text(TG_NOTIFY_CHAT, text)
    except Exception as e:
        log.error(f"Notify error: {e}")


# ── PUBLISH ───────────────────────────────────────────────────────────────────

def publish_one(item: dict) -> tuple[bool, int | None]:
    """
    Опубликовать один пост в канал.
    Возвращает (успех, telegram_message_id).
    """
    caption = _format_post(item)
    photo   = item.get("photo_path")

    try:
        if photo and Path(photo).exists():
            result = _send_photo(TG_MY_CHANNEL, photo, caption)
        else:
            result = _send_text(TG_MY_CHANNEL, caption)

        if result.get("ok"):
            msg_id = result.get("result", {}).get("message_id")
            log.info(f"✅ [{item['score']}] {item.get('headline', '')[:70]}")
            return True, msg_id
        else:
            log.error(f"Telegram API: {result.get('description', result)}")
            return False, None

    except Exception as e:
        log.error(f"publish_one error: {e}")
        return False, None


def maybe_publish() -> bool:
    """
    Проверить все условия и опубликовать следующий пост из очереди.
    Вызывается планировщиком каждый час.
    Возвращает True если пост опубликован.
    """
    if not TG_BOT_TOKEN or not TG_MY_CHANNEL:
        log.warning("TG_BOT_TOKEN / TG_MY_CHANNEL не заданы")
        return False

    conn = sqlite3.connect(DB_PATH)

    today = _posts_today(conn)
    if today >= MAX_PER_DAY:
        log.info(f"Лимит дня достигнут: {today}/{MAX_PER_DAY}")
        conn.close()
        return False

    gap = _gap_minutes(conn)
    if gap < MIN_GAP_MIN:
        log.info(f"Слишком рано: {gap:.0f} мин назад (мин {MIN_GAP_MIN})")
        conn.close()
        return False

    if not _is_working_hours():
        log.info("Нерабочие часы — публикация только 09:00–21:00 UTC+5")
        conn.close()
        return False

    item = _next_item(conn)
    if not item:
        log.info("Очередь пуста — нечего публиковать")
        conn.close()
        return False

    ok, msg_id = publish_one(item)
    if ok:
        _mark_published(conn, item["id"], msg_id)
        _notify_self(item, msg_id)

    conn.close()
    return ok


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )
    maybe_publish()
