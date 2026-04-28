"""
auto_collect.py — Основной скрипт авто-сбора.

Запускается Task Scheduler каждые 12 часов.
Логика:
  1. Собрать новости из RSS
  2. Отфильтровать по ключевым словам (БЕСПЛАТНО, без Claude API)
  3. Сохранить в БД (без дублей)
  4. Отправить сводку в Telegram

Токены НЕ тратятся. Всё бесплатно.
"""

import os
import re
import sys
import html
import yaml
import sqlite3
import requests
import feedparser
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = Path(os.environ.get("DB_PATH", str(BASE_DIR / "data" / "news.db")))
CONFIG_PATH = BASE_DIR / "config" / "config.yaml"
LOG_PATH = BASE_DIR / "data" / "auto_collect.log"


# ── CONFIG ───────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── DATABASE ─────────────────────────────────────────────────────────────

def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url         TEXT UNIQUE NOT NULL,
            title       TEXT NOT NULL,
            summary     TEXT,
            source      TEXT,
            published   TEXT,
            score       INTEGER DEFAULT 0,
            collected   TEXT DEFAULT (datetime('now')),
            sent_tg     INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def url_exists(conn: sqlite3.Connection, url: str) -> bool:
    r = conn.execute("SELECT 1 FROM news WHERE url=?", (url,)).fetchone()
    return r is not None


def save_news(conn: sqlite3.Connection, item: dict) -> bool:
    """Save one item. Returns True if it was new."""
    try:
        conn.execute("""
            INSERT INTO news (url, title, summary, source, published, score)
            VALUES (:url, :title, :summary, :source, :published, :score)
        """, item)
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # duplicate URL


def get_unsent_news(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT * FROM news
        WHERE sent_tg = 0
        ORDER BY score DESC, collected DESC
        LIMIT ?
    """, (limit,)).fetchall()
    result = [dict(r) for r in rows]
    conn.row_factory = None
    return result


def mark_sent(conn: sqlite3.Connection, news_ids: list[int]):
    for nid in news_ids:
        conn.execute("UPDATE news SET sent_tg=1 WHERE id=?", (nid,))
    conn.commit()


def cleanup_old_news(conn: sqlite3.Connection, days: int = 7) -> int:
    """Delete news older than N days. Returns count deleted."""
    cursor = conn.execute(
        "DELETE FROM news WHERE collected < datetime('now', ? || ' days')",
        (f"-{days}",)
    )
    conn.commit()
    return cursor.rowcount


# ── KEYWORD SCORING (FREE, no Claude) ────────────────────────────────────

# Вирусные триггеры — эмоция, конфликт, угроза (+2 за каждое)
VIRAL_KEYWORDS = [
    "штраф", "конфискация", "мошенничество", "мошенник", "обман",
    "поддельный", "подделка", "угроза", "опасно", "опасный",
    "уволили", "уволен", "запретили", "запрет",
    "арестован", "задержан", "задержали", "приговор", "тюрьма",
    "взятка", "коррупция", "хищение", "украли", "украл",
    "рост цен", "подорожание", "обрушение", "трагедия",
    "погиб", "погибли", "пострадали", "жертв",
    "нелегальн", "нарушен", "долг", "банкрот",
    "отказали", "скандал", "авария", "пожар",
]

# Важные темы — власть, экономика, регионы (+1 за каждое)
PRIORITY_KEYWORDS = [
    # Власть
    "президент", "мирзиёев", "правительство", "министр", "сенат", "парламент",
    "закон", "указ", "постановление", "реформа",
    # Экономика
    "миллиард", "миллион", "бюджет", "налог", "инфляция", "цены",
    "инвестиции", "курс", "сум", "доллар",
    # Общество
    "суд", "уголовное",
    # Регионы
    "ташкент", "самарканд", "бухара", "фергана", "андижан", "наманган",
    # Темы
    "повышение", "снижение", "отключение", "нехватка",
]

# Убийцы просмотров — скучно, не берём
EXCLUDE_KEYWORDS = [
    "реклама", "акция", "скидка", "конкурс", "промо", "розыгрыш",
    "sponsored", "партнёрский", "advertisement",
    "обсудили", "рассмотрели", "планируется",
    "конференция", "форум", "саммит",
    "визит", "переговоры", "встреча прошла",
    "церемония", "награждение", "поздравил",
    "провели встречу", "состоялась встреча",
]


def clean_text(text: str) -> str:
    """Strip HTML tags and decode entities for safe display."""
    if not text:
        return ""
    # Decode HTML entities (&nbsp; &amp; etc.)
    text = html.unescape(text)
    # Remove HTML tags (<br />, <b>, etc.)
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def keyword_score(title: str, summary: str) -> int:
    """
    Score news by keyword matches.
    VIRAL_KEYWORDS  = +2 (триггеры: штраф, мошенничество, погиб...)
    PRIORITY_KEYWORDS = +1 (важные темы: министр, бюджет...)
    EXCLUDE_KEYWORDS  = 0  (конференция, визит, поздравил...)
    """
    text = (title + " " + (summary or "")).lower()

    for kw in EXCLUDE_KEYWORDS:
        if kw in text:
            return 0

    score = 0
    for kw in VIRAL_KEYWORDS:
        if kw in text:
            score += 2
    for kw in PRIORITY_KEYWORDS:
        if kw in text:
            score += 1

    return score


# ── RSS FETCHER ───────────────────────────────────────────────────────────

def fetch_source(source: dict, conn: sqlite3.Connection,
                 min_score: int) -> tuple[int, int]:
    """
    Fetch one RSS source, save new items to DB.
    Returns (new_count, skipped_count).
    """
    new_count = 0
    skipped = 0

    try:
        feed = feedparser.parse(source["url"])
    except Exception as e:
        log(f"  ERROR parsing {source['name']}: {e}")
        return 0, 0

    for entry in feed.entries:
        url = getattr(entry, "link", "")
        if not url:
            continue

        if url_exists(conn, url):
            skipped += 1
            continue

        title = clean_text(getattr(entry, "title", ""))
        summary = clean_text(getattr(entry, "summary", ""))[:400]

        # Normalize publication time to UTC ISO (feedparser gives UTC struct_time)
        pub_parsed = getattr(entry, "published_parsed", None)
        if pub_parsed:
            try:
                published = datetime(*pub_parsed[:6], tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                published = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            published = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        score = keyword_score(title, summary)
        if score < min_score:
            continue  # Not relevant — don't save at all

        item = {
            "url": url,
            "title": title,
            "summary": summary,
            "source": source["name"],
            "published": published,
            "score": score,
        }

        if save_news(conn, item):
            new_count += 1

    return new_count, skipped


# ── TELEGRAM SENDER ───────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    """Send message to Telegram. Returns True if OK."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        log("  ⚠️ TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            return True
        else:
            log(f"  ❌ Telegram {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        log(f"  ❌ Telegram ошибка: {e}")
        return False


def tg_escape(text: str) -> str:
    """Escape < > & for Telegram HTML mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_published(published_str: str) -> str:
    """Extract readable date/time from RSS, converted to Uzbekistan time (UTC+5)."""
    if not published_str:
        return ""
    import email.utils
    from datetime import timezone, timedelta
    UZT = timezone(timedelta(hours=5))
    try:
        parsed = email.utils.parsedate_to_datetime(published_str)
        # Convert to UTC+5 (Uzbekistan)
        local = parsed.astimezone(UZT)
        return local.strftime("%d.%m %H:%M")
    except Exception:
        pass
    try:
        return published_str[:16].replace("T", " ")
    except Exception:
        return ""


def make_news_block(i: int, item: dict) -> str:
    title = tg_escape(clean_text(item.get("title", "")))
    url = item.get("url", "")
    pub = format_published(item.get("published", ""))
    date_str = f" <i>{pub}</i>" if pub else ""
    return f'{i}.{date_str} <a href="{url}">{title}</a>\n'


def format_news_for_telegram(news_list: list[dict]) -> list[str]:
    """
    Format news into compact Telegram messages: number + title as link.
    Splits into chunks under 4000 chars.
    """
    header = (
        f"🗞 <b>НОВОСТИ — {datetime.now().strftime('%d.%m %H:%M')}</b>\n\n"
    )
    footer = "\n💡 Копируй нужные → вставь в Claude.ai"

    chunks = []
    current = header

    for i, item in enumerate(news_list, 1):
        block = make_news_block(i, item)

        # If adding this block exceeds limit — flush current chunk, start new
        if len(current + block) > 3900:
            chunks.append(current)
            current = block
        else:
            current += block

    # Last chunk + footer
    if current.strip():
        current += footer
        chunks.append(current)

    return chunks


# ── LOGGING ───────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        # Windows console may not support some chars — strip them
        print(line.encode("ascii", errors="replace").decode("ascii"))
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── MAIN ─────────────────────────────────────────────────────────────────

def run(notify: bool = True, show_local: bool = False):
    """
    Main auto-collect function.
    notify=True: send to Telegram
    show_local=True: print news to console
    """
    config = load_config()
    sources = config.get("sources", [])
    min_score = config.get("keyword_filter", {}).get("min_score", 1)

    log(f"=== Auto collect ({len(sources)} sources) ===")

    init_db()
    conn = sqlite3.connect(DB_PATH)

    # Cleanup news older than 7 days
    deleted = cleanup_old_news(conn, days=7)
    if deleted:
        log(f"  Deleted {deleted} news older than 7 days")

    total_new = 0
    for source in sources:
        log(f"  >> {source['name']} ({source['url']})")
        new, skip = fetch_source(source, conn, min_score)
        log(f"     New: {new} | Duplicates: {skip}")
        total_new += new

    log(f"  Total new: {total_new}")

    if total_new == 0:
        log("  No new news. Skipping Telegram.")
        conn.close()
        return

    # Get unsent news to notify
    unsent = get_unsent_news(conn, limit=20)

    if show_local:
        print("\n" + "="*60)
        print(f"NEWS ({len(unsent)}):")
        print("="*60)
        for i, item in enumerate(unsent, 1):
            url = item['url']
            score = item['score']
            try:
                print(f"\n[{i}] score={score}  {item['title']}")
                print(f"    {url}")
            except UnicodeEncodeError:
                print(f"\n[{i}] score={score}  {url}")

    if notify and unsent:
        log(f"  Sending {len(unsent)} news to Telegram...")
        chunks = format_news_for_telegram(unsent)
        all_ok = True
        for chunk in chunks:
            ok = send_telegram(chunk)
            if not ok:
                all_ok = False
                break

        if all_ok:
            sent_ids = [item["id"] for item in unsent]
            mark_sent(conn, sent_ids)
            log(f"  Sent to Telegram ({len(chunks)} messages)")
        else:
            log("  ERROR: Telegram send failed")

    conn.close()
    log("=== Done ===")


if __name__ == "__main__":
    silent = "--silent" in sys.argv
    local = "--local" in sys.argv or not silent
    notify = "--no-telegram" not in sys.argv
    run(notify=notify, show_local=local)
