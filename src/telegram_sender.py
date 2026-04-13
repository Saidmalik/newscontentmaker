"""
Telegram Sender — sends files and messages to Telegram.
Foundation for the future full Telegram Bot interface.
"""

import os
import requests
from pathlib import Path


def get_bot_credentials() -> tuple[str, str]:
    """Get bot token and chat ID from environment."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    return token, chat_id


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Send a text message to the configured chat."""
    token, chat_id = get_bot_credentials()
    if not token or not chat_id:
        print("⚠️  Telegram не настроен (нет BOT_TOKEN или CHAT_ID)")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }

    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"❌ Ошибка отправки сообщения в Telegram: {e}")
        return False


def send_audio_file(file_path: str, caption: str = "") -> str | None:
    """
    Send an audio/voice file to the configured chat.
    Returns telegram file_id for future re-use, or None on failure.
    """
    token, chat_id = get_bot_credentials()
    if not token or not chat_id:
        print("⚠️  Telegram не настроен (нет BOT_TOKEN или CHAT_ID)")
        return None

    file_path = Path(file_path)
    if not file_path.exists():
        print(f"❌ Файл не найден: {file_path}")
        return None

    url = f"https://api.telegram.org/bot{token}/sendAudio"

    try:
        with open(file_path, "rb") as audio_file:
            response = requests.post(
                url,
                data={
                    "chat_id": chat_id,
                    "caption": caption[:1024],  # Telegram caption limit
                    "parse_mode": "HTML",
                },
                files={"audio": (file_path.name, audio_file, "audio/mpeg")},
                timeout=120,
            )
        response.raise_for_status()
        result = response.json()
        # Extract file_id for storage
        audio = result.get("result", {}).get("audio", {})
        return audio.get("file_id")
    except requests.HTTPError as e:
        print(f"❌ Telegram HTTP ошибка: {e.response.status_code} — {e.response.text[:200]}")
        return None
    except requests.RequestException as e:
        print(f"❌ Ошибка отправки аудио в Telegram: {e}")
        return None


def send_news_summary(news_items: list[dict]) -> bool:
    """Send a formatted news summary list to Telegram."""
    if not news_items:
        return False

    lines = ["<b>📋 Отфильтрованные новости:</b>\n"]
    for i, item in enumerate(news_items, 1):
        score = item.get("ai_score", "?")
        cat = item.get("ai_category", "").upper()
        title = item.get("title", "")[:80]
        reason = item.get("ai_reason", "")[:100]
        lines.append(f"<b>[{i}] ⭐{score}/10 | {cat}</b>")
        lines.append(f"📰 {title}")
        lines.append(f"💬 {reason}\n")

    text = "\n".join(lines)[:4096]
    return send_message(text)


def notify_audio_ready(audio_info: dict) -> bool:
    """Send notification that audio file is ready."""
    text = (
        f"✅ <b>Аудио готово!</b>\n\n"
        f"📰 {audio_info.get('news_title', '')[:100]}\n"
        f"📁 <code>{audio_info.get('file_name', '')}</code>\n"
        f"⏱️ ~{audio_info.get('duration', 0):.0f} сек. | "
        f"{audio_info.get('file_size', 0) // 1024} KB"
    )
    return send_message(text)


def test_connection() -> bool:
    """Test Telegram bot connection."""
    token, chat_id = get_bot_credentials()
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN не задан")
        return False
    if not chat_id:
        print("❌ TELEGRAM_CHAT_ID не задан")
        return False

    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        bot_info = response.json().get("result", {})
        print(f"✅ Telegram OK: @{bot_info.get('username')} — {bot_info.get('first_name')}")
        return True
    except Exception as e:
        print(f"❌ Telegram ошибка: {e}")
        return False


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
    test_connection()
