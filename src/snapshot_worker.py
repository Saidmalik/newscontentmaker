"""
snapshot_worker.py — 24/48/72ч снапшоты метрик для Instagram-постов.

Каждый час проверяет news.instagram_media_id, берёт снапшот
когда наступает порог 24h/48h/72h и сохраняет в post_snapshots.
Отправляет алерт в Telegram если ролик идёт вирусно (24ч views > 2× среднее).
"""

import os
import sqlite3
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger("snapshot_worker")

SNAPSHOTS = [("24h", 24), ("48h", 48), ("72h", 72)]


def _avg_72h_views(conn: sqlite3.Connection) -> float:
    r = conn.execute(
        "SELECT AVG(views) FROM post_snapshots WHERE snapshot_at='72h' AND views > 0"
    ).fetchone()
    return float(r[0] or 0)


def _tg_alert(text: str) -> None:
    token   = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_NOTIFY_CHAT", "")
    if not token or not chat_id:
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


async def run_snapshots(db_path: Path) -> int:
    """
    Check all news posts with instagram_media_id and collect due snapshots.
    Returns count of snapshots saved.
    """
    from src.instagram_worker import get_insights

    conn   = sqlite3.connect(str(db_path))
    rows   = conn.execute("""
        SELECT id, title, instagram_media_id, published_at
        FROM news
        WHERE instagram_media_id IS NOT NULL AND instagram_media_id != ''
          AND published_at IS NOT NULL
    """).fetchall()

    now   = datetime.now(timezone.utc)
    saved = 0
    avg_v = _avg_72h_views(conn)

    for post_id, title, media_id, pub_str in rows:
        try:
            pub = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        age_h = (now - pub).total_seconds() / 3600

        for snap_type, threshold in SNAPSHOTS:
            if age_h < threshold:
                continue

            exists = conn.execute(
                "SELECT 1 FROM post_snapshots WHERE post_id=? AND snapshot_at=?",
                (post_id, snap_type),
            ).fetchone()
            if exists:
                continue

            try:
                m = await get_insights(media_id)
            except Exception as e:
                log.error(f"Insights {media_id}: {e}")
                continue

            views        = m.get("plays", 0)
            reach        = m.get("reach", 0)
            likes        = m.get("likes", 0)
            comments     = m.get("comments", 0)
            saves        = m.get("saved", 0)
            shares       = m.get("shares", 0)
            watch_ms     = m.get("watch_time_ms", 0)
            avg_watch    = round(watch_ms / 1000, 1) if watch_ms else None

            conn.execute("""
                INSERT OR IGNORE INTO post_snapshots
                  (post_id, snapshot_at, views, reach, likes, comments,
                   saves, shares, avg_watch_time, recorded_at)
                VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))
            """, (post_id, snap_type, views, reach, likes, comments,
                  saves, shares, avg_watch))
            conn.commit()
            saved += 1
            log.info(f"Snapshot {snap_type}: post #{post_id}, views={views}, reach={reach}")

            if snap_type == "24h" and avg_v > 0 and views > avg_v * 2:
                _tg_alert(
                    f"🚀 <b>Вирусный ролик!</b>\n"
                    f"<b>{title}</b>\n"
                    f"24ч: {views:,} просмотров (средн. по каналу: {int(avg_v):,})"
                )

            # Trigger Claude analysis after 72h snapshot
            if snap_type == "72h":
                try:
                    from src import analysis_worker
                    analyzed = await analysis_worker.analyze_post(post_id, db_path)
                    if analyzed:
                        log.info(f"Claude analysis triggered for post #{post_id}")
                except Exception as e:
                    log.error(f"Analysis trigger error for post #{post_id}: {e}")

    conn.close()
    log.info(f"Snapshots done: {saved} saved / {len(rows)} posts checked")
    return saved
