"""
Database module — SQLite storage for all bot data.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import yaml


def get_db_path() -> str:
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    db_path = Path(__file__).parent.parent / config["storage"]["database_path"]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return str(db_path)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_database():
    """Create all tables if they don't exist."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS news_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            url             TEXT UNIQUE NOT NULL,
            title           TEXT NOT NULL,
            summary         TEXT,
            source_name     TEXT,
            source_url      TEXT,
            published_at    TEXT,
            collected_at    TEXT DEFAULT (datetime('now')),

            importance_score    INTEGER,
            importance_reason   TEXT,
            category            TEXT,
            is_selected         BOOLEAN DEFAULT 0,
            is_filtered_out     BOOLEAN DEFAULT 0,

            status  TEXT DEFAULT 'collected'
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS content_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id         INTEGER NOT NULL REFERENCES news_items(id),
            preview_title   TEXT,   -- 3-4 слова для обложки Reels
            headline        TEXT,   -- устарело, оставлено для совместимости
            caption         TEXT,
            hashtags        TEXT,
            tts_script      TEXT,
            tts_approved    BOOLEAN DEFAULT 0,
            tts_approved_at TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        )
    """)

    # Migration: add preview_title column if DB already exists without it
    try:
        cursor.execute("ALTER TABLE content_items ADD COLUMN preview_title TEXT")
    except Exception:
        pass  # Column already exists — OK

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audio_files (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id      INTEGER NOT NULL REFERENCES content_items(id),
            news_id         INTEGER NOT NULL REFERENCES news_items(id),
            file_path       TEXT NOT NULL,
            file_name       TEXT NOT NULL,
            file_size_bytes INTEGER,
            duration_seconds REAL,
            voice_id        TEXT,
            model_id        TEXT,
            sent_to_telegram BOOLEAN DEFAULT 0,
            telegram_file_id TEXT,
            generated_at    TEXT DEFAULT (datetime('now'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processing_sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at      TEXT DEFAULT (datetime('now')),
            finished_at     TEXT,
            news_collected  INTEGER DEFAULT 0,
            news_filtered   INTEGER DEFAULT 0,
            news_selected   INTEGER DEFAULT 0,
            audio_generated INTEGER DEFAULT 0,
            status          TEXT DEFAULT 'running'
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS instagram_stats (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id         TEXT,
            news_id         INTEGER REFERENCES news_items(id),
            likes           INTEGER DEFAULT 0,
            comments        INTEGER DEFAULT 0,
            reach           INTEGER DEFAULT 0,
            impressions     INTEGER DEFAULT 0,
            saves           INTEGER DEFAULT 0,
            shares          INTEGER DEFAULT 0,
            collected_at    TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.commit()
    conn.close()
    print("✅ База данных инициализирована")


# ── NEWS ITEMS ──────────────────────────────────────────────────────────

def save_news_items(items: list[dict]) -> int:
    """Save news items, skip duplicates. Returns count saved."""
    conn = get_connection()
    cursor = conn.cursor()
    saved = 0
    for item in items:
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO news_items
                    (url, title, summary, source_name, source_url, published_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                item.get("url"),
                item.get("title"),
                item.get("summary", ""),
                item.get("source_name"),
                item.get("source_url"),
                item.get("published_at"),
            ))
            if cursor.rowcount > 0:
                saved += 1
        except sqlite3.Error as e:
            print(f"⚠️ Ошибка сохранения: {e}")
    conn.commit()
    conn.close()
    return saved


def get_unfiltered_news(limit: int = 100) -> list[dict]:
    """Get news with status 'collected' (not yet AI-filtered)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM news_items
        WHERE status = 'collected'
        ORDER BY collected_at DESC
        LIMIT ?
    """, (limit,))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def update_news_filter_result(news_id: int, score: int, reason: str,
                               category: str, filtered_out: bool):
    """Save AI filtering result."""
    conn = get_connection()
    conn.execute("""
        UPDATE news_items SET
            importance_score = ?,
            importance_reason = ?,
            category = ?,
            is_filtered_out = ?,
            status = 'filtered'
        WHERE id = ?
    """, (score, reason, category, filtered_out, news_id))
    conn.commit()
    conn.close()


def mark_news_selected(news_ids: list[int]):
    """Mark news as selected by user."""
    conn = get_connection()
    for nid in news_ids:
        conn.execute("""
            UPDATE news_items SET is_selected = 1, status = 'selected'
            WHERE id = ?
        """, (nid,))
    conn.commit()
    conn.close()


def delete_rejected_news(news_ids: list[int]) -> int:
    """
    Delete rejected news from DB to keep it clean.
    Only deletes if they don't have associated content/audio.
    Returns count deleted.
    """
    if not news_ids:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    deleted = 0
    for nid in news_ids:
        # Don't delete if has content
        cursor.execute(
            "SELECT COUNT(*) FROM content_items WHERE news_id = ?", (nid,)
        )
        if cursor.fetchone()[0] > 0:
            continue
        cursor.execute("DELETE FROM news_items WHERE id = ?", (nid,))
        deleted += cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def get_selected_news() -> list[dict]:
    """Get user-selected news not yet content-generated."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM news_items
        WHERE is_selected = 1 AND status = 'selected'
        ORDER BY importance_score DESC
    """)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


# ── CONTENT ITEMS ───────────────────────────────────────────────────────

def save_content(news_id: int, preview_title: str, caption: str,
                  hashtags: str, tts_script: str,
                  headline: str = "") -> int:
    """Save generated content. Returns content_id."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO content_items
            (news_id, preview_title, headline, caption, hashtags, tts_script)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (news_id, preview_title, headline, caption, hashtags, tts_script))
    content_id = cursor.lastrowid
    conn.execute(
        "UPDATE news_items SET status = 'content_generated' WHERE id = ?",
        (news_id,)
    )
    conn.commit()
    conn.close()
    return content_id


def update_content(content_id: int, preview_title: str, caption: str,
                    hashtags: str, tts_script: str, headline: str = ""):
    """Update existing content (on regenerate)."""
    conn = get_connection()
    conn.execute("""
        UPDATE content_items SET
            preview_title = ?,
            headline = ?,
            caption = ?,
            hashtags = ?,
            tts_script = ?,
            tts_approved = 0,
            tts_approved_at = NULL,
            updated_at = datetime('now')
        WHERE id = ?
    """, (preview_title, headline, caption, hashtags, tts_script, content_id))
    conn.commit()
    conn.close()


def update_tts_script(content_id: int, new_script: str):
    """Update only the TTS script (after manual edit)."""
    conn = get_connection()
    conn.execute("""
        UPDATE content_items SET
            tts_script = ?,
            updated_at = datetime('now')
        WHERE id = ?
    """, (new_script, content_id))
    conn.commit()
    conn.close()


def approve_tts(content_id: int, final_script: str = None):
    """Mark TTS as approved."""
    conn = get_connection()
    if final_script:
        conn.execute("""
            UPDATE content_items SET
                tts_approved = 1,
                tts_script = ?,
                tts_approved_at = datetime('now'),
                updated_at = datetime('now')
            WHERE id = ?
        """, (final_script, content_id))
    else:
        conn.execute("""
            UPDATE content_items SET
                tts_approved = 1,
                tts_approved_at = datetime('now'),
                updated_at = datetime('now')
            WHERE id = ?
        """, (content_id,))
    conn.execute("""
        UPDATE news_items SET status = 'tts_approved'
        WHERE id = (SELECT news_id FROM content_items WHERE id = ?)
    """, (content_id,))
    conn.commit()
    conn.close()


def undo_tts_approval(content_id: int) -> bool:
    """
    Undo TTS approval — set back to content_generated.
    Useful if you approved by mistake.
    Returns True if undone, False if audio already generated.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Check if audio already generated
    cursor.execute("""
        SELECT COUNT(*) FROM audio_files WHERE content_id = ?
    """, (content_id,))
    if cursor.fetchone()[0] > 0:
        conn.close()
        print("⚠️  Нельзя отменить — аудио уже сгенерировано")
        return False

    conn.execute("""
        UPDATE content_items SET
            tts_approved = 0,
            tts_approved_at = NULL,
            updated_at = datetime('now')
        WHERE id = ?
    """, (content_id,))
    conn.execute("""
        UPDATE news_items SET status = 'content_generated'
        WHERE id = (SELECT news_id FROM content_items WHERE id = ?)
    """, (content_id,))
    conn.commit()
    conn.close()
    return True


def get_approved_tts() -> list[dict]:
    """Get content with approved TTS not yet audio-generated."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.*, n.title as news_title, n.url as news_url, n.category as news_category
        FROM content_items c
        JOIN news_items n ON c.news_id = n.id
        WHERE c.tts_approved = 1 AND n.status = 'tts_approved'
    """)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def get_recent_approved_tts(limit: int = 5) -> list[dict]:
    """Get recently approved TTS items (for undo functionality)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.id, c.tts_approved_at, c.tts_script, n.title
        FROM content_items c
        JOIN news_items n ON c.news_id = n.id
        WHERE c.tts_approved = 1
        ORDER BY c.tts_approved_at DESC
        LIMIT ?
    """, (limit,))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


# ── AUDIO FILES ─────────────────────────────────────────────────────────

def save_audio_file(content_id: int, news_id: int, file_path: str,
                     file_name: str, file_size: int, duration: float,
                     voice_id: str, model_id: str) -> int:
    """Save audio file metadata. Returns audio_id."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO audio_files
            (content_id, news_id, file_path, file_name, file_size_bytes,
             duration_seconds, voice_id, model_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (content_id, news_id, file_path, file_name,
          file_size, duration, voice_id, model_id))
    audio_id = cursor.lastrowid
    conn.execute(
        "UPDATE news_items SET status = 'audio_generated' WHERE id = ?",
        (news_id,)
    )
    conn.commit()
    conn.close()
    return audio_id


def mark_audio_sent_to_telegram(audio_id: int, telegram_file_id: str = None):
    """Mark audio as sent to Telegram."""
    conn = get_connection()
    conn.execute("""
        UPDATE audio_files SET
            sent_to_telegram = 1,
            telegram_file_id = ?
        WHERE id = ?
    """, (telegram_file_id, audio_id))
    conn.commit()
    conn.close()


# ── CLEANUP ─────────────────────────────────────────────────────────────

def cleanup_old_records(retention_days: int = 30):
    """Remove old processed news."""
    conn = get_connection()
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
    result = conn.execute("""
        DELETE FROM news_items
        WHERE status IN ('done', 'audio_generated')
        AND collected_at < ?
    """, (cutoff,))
    deleted = result.rowcount
    conn.commit()
    conn.close()
    return deleted


def cleanup_filtered_news(days: int = 3) -> int:
    """
    Delete AI-filtered-out news older than N days.
    These are news Claude rejected — no point keeping them.
    """
    conn = get_connection()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    result = conn.execute("""
        DELETE FROM news_items
        WHERE is_filtered_out = 1
        AND collected_at < ?
    """, (cutoff,))
    deleted = result.rowcount
    conn.commit()
    conn.close()
    return deleted


def get_db_stats() -> dict:
    """Get database statistics."""
    conn = get_connection()
    cursor = conn.cursor()
    stats = {}

    for status in ["collected", "filtered", "selected",
                   "content_generated", "tts_approved", "audio_generated"]:
        cursor.execute("SELECT COUNT(*) FROM news_items WHERE status = ?", (status,))
        stats[f"news_{status}"] = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM news_items WHERE is_filtered_out = 1")
    stats["news_filtered_out"] = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM content_items WHERE tts_approved = 1")
    stats["tts_approved"] = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM audio_files")
    stats["audio_total"] = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM audio_files WHERE sent_to_telegram = 1")
    stats["audio_sent_telegram"] = cursor.fetchone()[0]

    cursor.execute("SELECT COALESCE(SUM(file_size_bytes), 0) FROM audio_files")
    stats["audio_total_bytes"] = cursor.fetchone()[0]

    conn.close()
    return stats


if __name__ == "__main__":
    init_database()
