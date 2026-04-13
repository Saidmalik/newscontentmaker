"""
ElevenLabs Client — generates audio from approved TTS scripts.

Важно про настройки голоса:
- stability: 0-1 (выше = стабильнее, меньше вариаций)
- similarity_boost: 0-1 (выше = ближе к оригинальному голосу)
- style: 0-1 (стиль/экспрессивность, только для v2)
- use_speaker_boost: true/false
- speed: 0.7-1.2 (1.0 = нормальный темп)

Меняй эти параметры в config/config.yaml → elevenlabs → voice_settings
"""

import os
import yaml
import requests
from pathlib import Path
from datetime import datetime

from database import get_approved_tts, save_audio_file


def load_config() -> dict:
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_audio_output_dir(config: dict) -> Path:
    output_dir = Path(__file__).parent.parent / config["storage"]["audio_output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def generate_audio(tts_script: str, voice_id: str, api_key: str,
                   config: dict) -> bytes | None:
    """Call ElevenLabs API. All voice settings from config."""
    el_config = config["elevenlabs"]
    model_id = el_config["model_id"]
    output_format = el_config["output_format"]

    # Voice settings — все параметры из config включая speed
    voice_settings = dict(el_config["voice_settings"])

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": api_key,
    }
    payload = {
        "text": tts_script,
        "model_id": model_id,
        "voice_settings": voice_settings,
        "output_format": output_format,
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=90)
        response.raise_for_status()
        return response.content
    except requests.HTTPError as e:
        status = e.response.status_code
        text = e.response.text[:300]
        print(f"  ❌ ElevenLabs HTTP {status}: {text}")
        if status == 401:
            print("     → Проверь ELEVENLABS_API_KEY в .env")
        elif status == 422:
            print("     → Возможно неверный voice_id или model_id")
        return None
    except requests.RequestException as e:
        print(f"  ❌ Ошибка запроса: {e}")
        return None


def estimate_duration(audio_bytes: bytes) -> float:
    """MP3 duration estimate (128kbps = 16000 bytes/sec)."""
    return len(audio_bytes) / 16000


def save_audio_locally(audio_bytes: bytes, news_title: str,
                       output_dir: Path) -> tuple[str, str]:
    """Save audio bytes to mp3 file."""
    safe_title = "".join(
        c if c.isalnum() or c in " _-" else "_"
        for c in news_title[:40]
    ).strip()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"{timestamp}_{safe_title}.mp3"
    file_path = output_dir / file_name

    with open(file_path, "wb") as f:
        f.write(audio_bytes)

    return str(file_path), file_name


def check_voice_settings(api_key: str, voice_id: str) -> dict | None:
    """Get current voice settings from ElevenLabs (for verification)."""
    url = f"https://api.elevenlabs.io/v1/voices/{voice_id}"
    headers = {"xi-api-key": api_key}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        return {
            "name": data.get("name"),
            "settings": data.get("settings", {}),
            "labels": data.get("labels", {}),
        }
    except Exception as e:
        print(f"  ⚠️  Не могу получить настройки голоса: {e}")
        return None


def run_audio_generation(send_to_telegram: bool = False) -> list[dict]:
    """
    Full audio generation:
    - Get approved TTS from DB
    - Generate via ElevenLabs with settings from config
    - Save locally
    - Optional: send to Telegram
    """
    config = load_config()
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()

    if not api_key:
        print("❌ ELEVENLABS_API_KEY не задан в .env")
        return []
    if not voice_id:
        print("❌ ELEVENLABS_VOICE_ID не задан в .env")
        return []

    approved_items = get_approved_tts()
    if not approved_items:
        print("ℹ️  Нет одобренных TTS. Сначала одобри скрипты в шаге 3.")
        return []

    output_dir = get_audio_output_dir(config)
    voice_cfg = config["elevenlabs"]["voice_settings"]

    print(f"\n{'='*55}")
    print(f"🎙️  Генерация аудио: {len(approved_items)} файлов")
    print(f"   Voice ID:    {voice_id}")
    print(f"   Model:       {config['elevenlabs']['model_id']}")
    print(f"   Stability:   {voice_cfg.get('stability', '?')}")
    print(f"   Similarity:  {voice_cfg.get('similarity_boost', '?')}")
    print(f"   Speed:       {voice_cfg.get('speed', 1.0)}")
    print(f"   Папка:       {output_dir}")
    print(f"{'='*55}")

    generated = []

    for item in approved_items:
        news_title = item.get("news_title", "news")
        tts_script = item.get("tts_script", "")
        content_id = item["id"]
        news_id = item["news_id"]

        word_count = len(tts_script.split())
        print(f"\n  🔊 {news_title[:55]}...")
        print(f"     Скрипт: {word_count} слов")

        audio_bytes = generate_audio(tts_script, voice_id, api_key, config)

        if not audio_bytes:
            print(f"  ⚠️  Пропускаю (ошибка генерации)")
            continue

        file_path, file_name = save_audio_locally(audio_bytes, news_title, output_dir)
        file_size = len(audio_bytes)
        duration = estimate_duration(audio_bytes)

        audio_id = save_audio_file(
            content_id=content_id,
            news_id=news_id,
            file_path=file_path,
            file_name=file_name,
            file_size=file_size,
            duration=duration,
            voice_id=voice_id,
            model_id=config["elevenlabs"]["model_id"],
        )

        print(f"  ✅ Сохранено: {file_name}")
        print(f"     {file_size // 1024} KB | ~{duration:.0f} сек.")

        audio_info = {
            "audio_id": audio_id,
            "content_id": content_id,
            "news_id": news_id,
            "file_path": file_path,
            "file_name": file_name,
            "file_size": file_size,
            "duration": duration,
            "news_title": news_title,
        }
        generated.append(audio_info)

        if send_to_telegram:
            _send_audio_to_telegram(audio_info, config)

    print(f"\n{'='*55}")
    print(f"✅ Готово: {len(generated)} аудио файлов")
    if generated:
        print(f"   📁 {output_dir}")
    print(f"{'='*55}")

    return generated


def _send_audio_to_telegram(audio_info: dict, config: dict):
    """Send audio file to Telegram."""
    from telegram_sender import send_audio_file
    from database import mark_audio_sent_to_telegram
    try:
        tg_file_id = send_audio_file(
            file_path=audio_info["file_path"],
            caption=(
                f"🎙️ <b>{audio_info['news_title'][:100]}</b>\n"
                f"⏱️ ~{audio_info['duration']:.0f} сек."
            )
        )
        if tg_file_id:
            mark_audio_sent_to_telegram(audio_info["audio_id"], tg_file_id)
            print(f"  📲 Отправлено в Telegram!")
        else:
            print(f"  ⚠️  Telegram: не удалось получить file_id")
    except Exception as e:
        print(f"  ⚠️  Ошибка Telegram: {e}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
    from database import init_database
    init_database()
    generated = run_audio_generation(send_to_telegram=False)
    print(f"\nИтог: {len(generated)} аудио файлов.")
