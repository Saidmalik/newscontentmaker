"""
tg_source_reader.py — Чтение новостей из Telegram-каналов.

Использует Telethon (MTProto user client) для чтения публичных каналов.
Сессия хранится в /data/tg_session.session (Railway Volume).

ПЕРВЫЙ ЗАПУСК (один раз локально):
    python src/tg_source_reader.py --auth
    → введи номер телефона + код из SMS
    → tg_session.session сохранится в data/
    → скопируй файл на Railway Volume (/data/)
"""

import os
import asyncio
import hashlib
import sqlite3
import logging
import base64
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
import anthropic

BASE_DIR   = Path(__file__).parent.parent
MEDIA_DIR  = BASE_DIR / "media"
SESSION    = str(BASE_DIR / "data" / "tg_session")

load_dotenv(BASE_DIR / ".env")

DB_PATH       = Path(os.environ.get("DB_PATH", str(BASE_DIR / "data" / "news.db")))
TG_API_ID     = int(os.environ.get("TG_API_ID", "0") or "0")
TG_API_HASH   = os.environ.get("TG_API_HASH", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MIN_SCORE    = 5    # ниже — не сохранять
MIN_TEXT_LEN = 20   # короче — пропустить
LOOKBACK_HRS = 2    # смотреть назад N часов при каждом запуске

TG_SOURCES = [
    "pressservice_uz",  # Пресс-служба президента
    "uzdaily_news",     # UzDaily
    "kun_uz",           # Kun.uz
    "gazeta_uz",        # Gazeta.uz
    "mininfouz",        # Мининформ
    "daryo_uz",         # Daryo.uz
    "uzreport_news",    # UzReport
    "podrobnosti_uz",   # Podrobnosti.uz
    "anhor_uz",         # Anhor.uz
    "nuz_uz",           # Nuz.uz
]

log = logging.getLogger("tg_reader")


# ── БАЗА ДАННЫХ ──────────────────────────────────────────────────────────────

def init_tg_db():
    """Создать таблицу tg_queue если не существует."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tg_queue (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_channel  TEXT NOT NULL,
            original_text   TEXT,
            translated_text TEXT,
            summary         TEXT,
            headline        TEXT,
            score           INTEGER DEFAULT 0,
            has_photo       INTEGER DEFAULT 0,
            photo_path      TEXT,
            published       INTEGER DEFAULT 0,
            published_at    TEXT,
            text_hash       TEXT UNIQUE,
            tg_msg_id       INTEGER,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def text_hash(text: str) -> str:
    return hashlib.md5(text.strip().lower().encode()).hexdigest()


def is_duplicate(conn: sqlite3.Connection, h: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM tg_queue WHERE text_hash=?", (h,)
    ).fetchone() is not None


def save_to_queue(conn: sqlite3.Connection, item: dict) -> bool:
    try:
        conn.execute("""
            INSERT INTO tg_queue
              (source_channel, original_text, translated_text, summary,
               headline, score, has_photo, photo_path, text_hash, tg_msg_id)
            VALUES
              (:source_channel, :original_text, :translated_text, :summary,
               :headline, :score, :has_photo, :photo_path, :text_hash, :tg_msg_id)
        """, item)
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # дубликат


# ── CLAUDE ───────────────────────────────────────────────────────────────────

def analyze_text(text: str) -> dict:
    """Оценить, перевести (если нужно), резюмировать, дать заголовок."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    prompt = f"""Ты редактор Telegram-канала об Узбекистане. Аудитория — русскоязычные.

ТЕКСТ НОВОСТИ:
{text[:3000]}

Выполни 4 задачи и ответь СТРОГО в этом формате:

SCORE: [1-10]
  10 = срочно, касается всех (цены, законы, ЧП, указы президента)
  7-9 = важно и интересно большинству
  5-6 = интересно части аудитории
  1-4 = официоз, скучно, пропустить

TRANSLATE: [текст на русском — переведи если узбекский, иначе оригинал]

SUMMARY: [2-3 предложения. Суть + факты + последствие для людей. Без воды.]

HEADLINE: [5-7 слов. Цепляющий. Без "В Узбекистане", без официоза.]"""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        return _parse_response(resp.content[0].text.strip(), text)
    except Exception as e:
        log.error(f"Claude analyze error: {e}")
        return {"score": 0, "translated_text": text, "summary": "", "headline": ""}


def describe_image(image_bytes: bytes) -> str:
    """Claude Vision: описать фото (для постов без подписи)."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=200,
            messages=[{"role": "user", "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(image_bytes).decode()
                    }
                },
                {
                    "type": "text",
                    "text": (
                        "Опиши что на фото в 1-2 предложениях. "
                        "Контекст: новостной канал об Узбекистане. "
                        "Только факты: кто, где, что происходит. Без оценок."
                    )
                }
            ]}]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        log.error(f"Claude Vision error: {e}")
        return ""


def _parse_response(raw: str, fallback: str) -> dict:
    """Разобрать структурированный ответ Claude."""
    result = {
        "score": 0,
        "translated_text": fallback,
        "summary": "",
        "headline": ""
    }
    current_key = None
    buffer: list[str] = []

    def flush():
        if current_key and buffer:
            val = " ".join(buffer).strip()
            if current_key == "score":
                try:
                    result["score"] = int(val.split()[0])
                except Exception:
                    pass
            elif current_key == "translate":
                result["translated_text"] = val
            elif current_key == "summary":
                result["summary"] = val
            elif current_key == "headline":
                result["headline"] = val

    for line in raw.splitlines():
        s = line.strip()
        u = s.upper()
        if u.startswith("SCORE:"):
            flush(); buffer = [s[6:].strip()]; current_key = "score"
        elif u.startswith("TRANSLATE:"):
            flush(); buffer = [s[10:].strip()]; current_key = "translate"
        elif u.startswith("SUMMARY:"):
            flush(); buffer = [s[8:].strip()]; current_key = "summary"
        elif u.startswith("HEADLINE:"):
            flush(); buffer = [s[9:].strip()]; current_key = "headline"
        elif current_key and s:
            buffer.append(s)
    flush()
    return result


# ── ОБРАБОТКА СООБЩЕНИЯ ──────────────────────────────────────────────────────

async def process_message(
    message, client: TelegramClient, source: str
) -> dict | None:
    """
    Обработать одно сообщение из канала.
    Вернуть dict для tg_queue или None если нужно пропустить.
    """
    text = ""
    photo_path = None
    has_photo = False
    media = message.media

    # 1. Чистый текст
    if not media and message.text:
        text = message.text.strip()

    # 2. Фото + подпись
    elif isinstance(media, MessageMediaPhoto) and message.text:
        text = message.text.strip()
        has_photo = True
        photo_path = await _save_photo(message, client)

    # 3. Фото без подписи → Claude Vision
    elif isinstance(media, MessageMediaPhoto) and not message.text:
        has_photo = True
        photo_path = await _save_photo(message, client)
        if photo_path:
            text = describe_image(Path(photo_path).read_bytes())
        if not text:
            log.debug(f"[{source}] Фото без текста — Vision вернул пусто, пропуск")
            return None

    # 4. Видео/документ + подпись
    elif isinstance(media, MessageMediaDocument) and message.text:
        text = message.text.strip()

    # 5. Видео без подписи → пропуск
    elif isinstance(media, MessageMediaDocument) and not message.text:
        log.debug(f"[{source}] Видео без текста — пропуск")
        return None

    # 6. Пустой/короткий текст
    if not text or len(text) < MIN_TEXT_LEN:
        return None

    # Проверка дублей
    h = text_hash(text)
    conn = sqlite3.connect(DB_PATH)
    dup = is_duplicate(conn, h)
    conn.close()
    if dup:
        log.debug(f"[{source}] Дубликат — пропуск")
        return None

    log.info(f"  [{source}] Анализирую: {text[:70]}…")
    result = analyze_text(text)

    if result["score"] < MIN_SCORE:
        log.info(f"  [{source}] Score {result['score']} < {MIN_SCORE} — отброшен")
        return None

    return {
        "source_channel":  source,
        "original_text":   text[:4000],
        "translated_text": result["translated_text"][:4000],
        "summary":         result["summary"],
        "headline":        result["headline"],
        "score":           result["score"],
        "has_photo":       1 if has_photo else 0,
        "photo_path":      photo_path,
        "text_hash":       h,
        "tg_msg_id":       message.id,
    }


async def _save_photo(message, client: TelegramClient) -> str | None:
    try:
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = str(MEDIA_DIR / f"tg_{ts}_{message.id}.jpg")
        await client.download_media(message, file=dst)
        return dst
    except Exception as e:
        log.error(f"Ошибка скачивания фото: {e}")
        return None


# ── ГЛАВНАЯ ФУНКЦИЯ ──────────────────────────────────────────────────────────

async def read_all_sources(lookback_hours: int = LOOKBACK_HRS) -> int:
    """
    Прочитать все источники за последние N часов.
    Вернуть количество новых записей в tg_queue.
    """
    if not TG_API_ID or not TG_API_HASH:
        log.error("TG_API_ID / TG_API_HASH не заданы — пропуск")
        return 0

    init_tg_db()
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    total = 0

    async with TelegramClient(SESSION, TG_API_ID, TG_API_HASH) as client:
        for source in TG_SOURCES:
            try:
                log.info(f"Читаю @{source}…")
                saved = 0
                async for msg in client.iter_messages(
                    source, limit=50, reverse=False
                ):
                    # iter_messages идёт от новых к старым — останавливаемся
                    if msg.date.replace(tzinfo=timezone.utc) < since:
                        break
                    item = await process_message(msg, client, source)
                    if item:
                        conn = sqlite3.connect(DB_PATH)
                        if save_to_queue(conn, item):
                            saved += 1
                            total += 1
                            log.info(f"  ✅ [{item['score']}] {item['headline']}")
                        conn.close()
                log.info(f"  @{source}: +{saved} сохранено")
            except Exception as e:
                log.error(f"  ❌ @{source}: {e}")

    log.info(f"Итого новых в tg_queue: {total}")
    return total


# ── АВТОРИЗАЦИЯ (первый запуск) ───────────────────────────────────────────────

async def auth_interactive():
    """
    Запустить один раз локально для создания сессии Telethon.
    После — скопировать data/tg_session.session на Railway Volume.
    """
    print(f"Создаю сессию: {SESSION}.session")
    print(f"TG_API_ID={TG_API_ID}, TG_API_HASH={'***' if TG_API_HASH else 'НЕ ЗАДАН'}")
    async with TelegramClient(SESSION, TG_API_ID, TG_API_HASH) as client:
        await client.start()
        me = await client.get_me()
        print(f"\n✅ Авторизован: {me.first_name} (@{me.username or 'нет username'})")
        print(f"   Сессия сохранена: {SESSION}.session")
        print("   Следующий шаг: скопируй файл на Railway Volume в /data/")


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )
    if "--auth" in sys.argv:
        asyncio.run(auth_interactive())
    else:
        asyncio.run(read_all_sources())
