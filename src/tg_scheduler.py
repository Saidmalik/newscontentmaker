"""
tg_scheduler.py — Планировщик задач для Telegram-автоканала.

Задачи:
  - Каждые 15 мин:         читать новые посты из источников
  - Каждый час в :30 (9-21): проверить очередь и опубликовать
  - При старте:             уведомить тебя в личку

Интеграция с app.py:
    from src.tg_scheduler import start_tg_scheduler
    tg_scheduler = start_tg_scheduler()

Standalone:
    python src/tg_scheduler.py
"""

import os
import asyncio
import logging
import requests
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

TG_BOT_TOKEN   = os.environ.get("TG_BOT_TOKEN", "")
TG_NOTIFY_CHAT = os.environ.get("TG_NOTIFY_CHAT", "")

log = logging.getLogger("tg_scheduler")


# ── ЗАДАЧИ ───────────────────────────────────────────────────────────────────

async def task_read_sources():
    """Каждые 15 мин: тянуть новые посты из Telegram-каналов."""
    log.info("[task] Читаю источники…")
    try:
        from src.tg_source_reader import read_all_sources
        count = await read_all_sources(lookback_hours=1)
        if count:
            log.info(f"  → +{count} новых в очереди")
        else:
            log.info("  → Новых постов нет")
    except Exception as e:
        log.error(f"task_read_sources: {e}", exc_info=True)


def task_check_queue():
    """Каждый час в :30: проверить очередь и опубликовать если пришло время."""
    log.info("[task] Проверяю очередь публикаций…")
    try:
        from src.tg_publisher import maybe_publish
        if maybe_publish():
            log.info("  → Пост опубликован ✅")
        else:
            log.info("  → Публикация отложена")
    except Exception as e:
        log.error(f"task_check_queue: {e}", exc_info=True)


def _notify_startup():
    """Уведомить тебя в личку что бот запущен."""
    if not TG_BOT_TOKEN or not TG_NOTIFY_CHAT:
        log.warning("TG_NOTIFY_CHAT не задан — startup notification пропущен")
        return
    uzt_now = (datetime.utcnow() + timedelta(hours=5)).strftime("%d.%m.%Y %H:%M")
    text = (
        "🚀 <b>MediaHub TG-бот запущен</b>\n\n"
        f"📅 {uzt_now} (Ташкент)\n"
        "📡 Читаю каналы каждые 15 мин\n"
        "📤 Публикую в 09:30–21:30 (каждый час в :30)\n"
        "📊 Максимум 5 постов в день · мин. интервал 90 мин"
    )
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_NOTIFY_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code == 200:
            log.info("Startup notification sent ✓")
        else:
            log.warning(f"Startup notification failed: {r.text[:100]}")
    except Exception as e:
        log.warning(f"Startup notify error: {e}")


# ── ПЛАНИРОВЩИК ───────────────────────────────────────────────────────────────

def start_tg_scheduler() -> AsyncIOScheduler:
    """
    Создать и запустить APScheduler для Telegram-модуля.
    Вызвать один раз при старте FastAPI (в lifespan).
    """
    scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")

    # Читать Telegram-источники каждые 15 минут
    scheduler.add_job(
        task_read_sources,
        trigger=IntervalTrigger(minutes=15),
        id="tg_read_sources",
        name="Читать TG источники",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=120,
    )

    # Публиковать каждый час в :30 с 9 до 21 по Ташкенту
    scheduler.add_job(
        task_check_queue,
        trigger=CronTrigger(
            minute=30,
            hour="9-21",
            timezone="Asia/Tashkent"
        ),
        id="tg_check_queue",
        name="Проверить очередь публикаций",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )

    scheduler.start()
    _notify_startup()
    log.info(
        "TG Scheduler started ✓  "
        "read: каждые 15 мин | publish: :30 каждый час 9-21 UTC+5"
    )
    return scheduler


# ── STANDALONE РЕЖИМ ─────────────────────────────────────────────────────────

async def _run_standalone():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-16s %(levelname)s %(message)s"
    )
    log.info("Запуск в standalone режиме…")

    scheduler = start_tg_scheduler()

    # Первый прогон сразу после старта
    log.info("Первый прогон источников…")
    await task_read_sources()

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановка планировщика…")
        scheduler.shutdown()
        log.info("Остановлен.")


if __name__ == "__main__":
    asyncio.run(_run_standalone())
