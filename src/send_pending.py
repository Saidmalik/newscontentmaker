"""
send_pending.py — Отправить в Telegram новости которые уже в БД но ещё не отправлены.
Используй если автоматическая отправка не сработала.
"""

import os
import sqlite3
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

# Import everything from auto_collect
import sys
sys.path.insert(0, str(BASE_DIR / "src"))

from auto_collect import (
    DB_PATH, get_unsent_news, format_news_for_telegram,
    send_telegram, mark_sent, log
)


def send_pending():
    if not DB_PATH.exists():
        print("БД не найдена. Сначала запусти collect.bat")
        return

    conn = sqlite3.connect(DB_PATH)
    unsent = get_unsent_news(conn, limit=25)

    if not unsent:
        print("Нет новостей для отправки (все уже отправлены или БД пуста).")
        print("Запусти collect.bat чтобы собрать новые новости.")
        conn.close()
        return

    print(f"Найдено {len(unsent)} неотправленных новостей")
    print("Отправляю в Telegram...\n")

    chunks = format_news_for_telegram(unsent)
    success = 0

    for i, chunk in enumerate(chunks, 1):
        print(f"  Сообщение {i}/{len(chunks)} ({len(chunk)} симв)...")
        ok = send_telegram(chunk)
        if ok:
            success += 1
            print(f"  OK")
        else:
            print(f"  ОШИБКА — проверь TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")
            break

    if success == len(chunks):
        sent_ids = [item["id"] for item in unsent]
        mark_sent(conn, sent_ids)
        print(f"\nУспешно отправлено {len(unsent)} новостей ({len(chunks)} сообщений).")
    else:
        print(f"\nОтправлено {success} из {len(chunks)} сообщений. Остальные ждут следующего запуска.")

    conn.close()


if __name__ == "__main__":
    send_pending()
