"""
Main orchestrator — CLI entry point for the news pipeline.

Запуск:
  python main.py             — Интерактивное меню
  python main.py --collect   — Только сбор новостей
  python main.py --filter    — Только AI фильтрация
  python main.py --content   — Только генерация контента
  python main.py --audio     — Только генерация аудио
  python main.py --status    — Статистика БД
  python main.py --test      — Тест подключений
  python main.py --cleanup   — Очистка старых записей в БД
  python main.py --undo-tts  — Отменить последнее одобрение TTS
"""

import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env
load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

from database import init_database, get_db_stats, cleanup_filtered_news, cleanup_old_records


BANNER = """
╔═══════════════════════════════════════════════════╗
║        🇺🇿 UZ NEWS BOT — Автоматизация новостей    ║
╚═══════════════════════════════════════════════════╝"""


def show_status():
    stats = get_db_stats()
    total_mb = stats["audio_total_bytes"] / (1024 * 1024)

    print(f"""
📊 СТАТИСТИКА:
{'─'*45}
📥 Новые (не обработаны):    {stats['news_collected']}
🤖 Прошли AI фильтр:         {stats['news_filtered']}
❌ Отсеяны AI:               {stats['news_filtered_out']}
✅ Выбраны тобой:            {stats['news_selected']}
✍️  Контент готов:            {stats['news_content_generated']}
🎙️  TTS одобрен:             {stats['news_tts_approved']}
🔊 Аудио создано:            {stats['news_audio_generated']}
{'─'*45}
📁 Всего аудио файлов:       {stats['audio_total']}
💾 Размер аудио:             {total_mb:.1f} MB
📲 Отправлено в Telegram:    {stats['audio_sent_telegram']}
{'─'*45}""")


def explain_pipeline():
    """Объяснение пайплайна для понимания."""
    print("""
📖 КАК РАБОТАЕТ ПАЙПЛАЙН:
{'─'*55}
ШАГ 1 — СБОР [--collect]
  RSS → предфильтр по ключевым словам → БД
  Что получаешь: новые новости в БД (без дублей)

ШАГ 2 — AI ФИЛЬТРАЦИЯ [--filter]
  БД → Claude оценивает важность (1-10) → показывает список
  Ты выбираешь какие берём → остальные УДАЛЯЮТСЯ из БД
  URL каждой новости видны — можешь проверить источник
  Что получаешь: чистый список важных новостей

ШАГ 3 — КОНТЕНТ [--content]
  ← Это и есть "генерация контента"
  Для каждой выбранной новости Claude пишет:
    📌 Заголовок (для Instagram)
    📸 Caption с хэштегами (текст поста)
    🎙️ TTS скрипт (текст для озвучки голосом)
  Ты читаешь TTS → одобряешь / редактируешь / перегенеришь
  Что получаешь: готовые тексты для поста + аудиоскрипт

ШАГ 4 — АУДИО [--audio]
  ← Отдельный шаг после одобрения TTS
  Одобренный TTS → ElevenLabs API → MP3 файл
  Сохраняется локально в output/audio/
  Опционально: отправляется в Telegram
  Что получаешь: готовый аудио файл для reels/stories

Почему шаги 3 и 4 разделены?
  → Чтобы ты мог РЕДАКТИРОВАТЬ TTS перед генерацией аудио.
    Аудио стоит токены ElevenLabs — не хочешь тратить на плохой скрипт.
{'─'*55}""")


def test_connections():
    print("\n🔌 Тестирование подключений...\n")

    # Claude API
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        client.messages.create(
            model="claude-haiku-4-5", max_tokens=5,
            messages=[{"role": "user", "content": "hi"}]
        )
        print("✅ Claude API — OK")
    except Exception as e:
        err = str(e)
        if "401" in err or "authentication" in err.lower():
            print("❌ Claude API — Неверный API ключ (ANTHROPIC_API_KEY)")
        else:
            print(f"❌ Claude API — {err[:80]}")

    # ElevenLabs
    el_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
    if not el_key:
        print("⚠️  ElevenLabs — ELEVENLABS_API_KEY не задан")
    else:
        import requests
        try:
            r = requests.get(
                "https://api.elevenlabs.io/v1/user",
                headers={"xi-api-key": el_key},
                timeout=10
            )
            if r.status_code == 200:
                info = r.json()
                tier = info.get("subscription", {}).get("tier", "?")
                chars = info.get("subscription", {}).get("character_count", 0)
                limit = info.get("subscription", {}).get("character_limit", 0)
                print(f"✅ ElevenLabs — OK (план: {tier}, символов: {chars:,}/{limit:,})")
            elif r.status_code == 401:
                print("❌ ElevenLabs — Неверный API ключ (401)")
                print("   → Проверь ELEVENLABS_API_KEY в .env (без пробелов)")
            else:
                print(f"❌ ElevenLabs — HTTP {r.status_code}")
        except Exception as e:
            print(f"❌ ElevenLabs — {e}")

    # ElevenLabs Voice
    if el_key and voice_id:
        import requests
        try:
            r = requests.get(
                f"https://api.elevenlabs.io/v1/voices/{voice_id}",
                headers={"xi-api-key": el_key},
                timeout=10
            )
            if r.status_code == 200:
                vname = r.json().get("name", "?")
                vsettings = r.json().get("settings", {})
                print(f"✅ Voice ID — OK ('{vname}')")
                print(f"   Настройки голоса: {vsettings}")
            else:
                print(f"❌ Voice ID — не найден ({voice_id[:8]}...)")
        except Exception as e:
            print(f"❌ Voice — {e}")

    # Telegram
    from telegram_sender import test_connection
    test_connection()

    # Telegram advice
    print("""
💡 Если Telegram выдаёт 'chat not found':
   1. Открой Telegram → найди @{bot_username}
   2. Нажми СТАРТ (/start) — это обязательно!
   3. После этого бот знает твой chat_id
   4. Убедись что TELEGRAM_CHAT_ID = твой числовой ID (от @userinfobot)
""")


def undo_last_tts():
    """Отменить последнее одобрение TTS."""
    from database import get_recent_approved_tts, undo_tts_approval

    recent = get_recent_approved_tts(limit=5)
    if not recent:
        print("ℹ️  Нет одобренных TTS для отмены.")
        return

    print("\n📋 Последние одобренные TTS:")
    for i, item in enumerate(recent, 1):
        print(f"[{i}] ID={item['id']} | {item['title'][:50]}")
        print(f"     Одобрено: {item['tts_approved_at']}")

    choice = input("\nКакой отменить? (номер или q): ").strip()
    if choice.lower() == "q":
        return

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(recent):
            content_id = recent[idx]["id"]
            if undo_tts_approval(content_id):
                print(f"✅ Одобрение TTS ID={content_id} отменено.")
                print("   Теперь можешь заново редактировать и одобрить.")
        else:
            print("⚠️  Неверный номер")
    except ValueError:
        print("⚠️  Введи число")


def run_full_pipeline():
    print(BANNER)
    print("\n🚀 Полный пайплайн: Сбор → Фильтрация → Контент → Аудио\n")
    input("Enter чтобы начать (Ctrl+C для отмены)...")

    # Step 1
    print("\n" + "="*55 + "\nШАГ 1: СБОР НОВОСТЕЙ\n" + "="*55)
    from rss_collector import collect_news
    new_items = collect_news()
    if not new_items:
        print("⚠️  Нет новых новостей. Пайплайн завершён.")
        return

    # Step 2
    print("\n" + "="*55 + "\nШАГ 2: AI ФИЛЬТРАЦИЯ\n" + "="*55)
    from ai_filter import run_filter_step
    selected = run_filter_step()
    if not selected:
        print("⚠️  Новости не выбраны. Пайплайн завершён.")
        return

    # Step 3
    print("\n" + "="*55 + "\nШАГ 3: ГЕНЕРАЦИЯ КОНТЕНТА + TTS\n" + "="*55)
    from content_generator import run_content_generation
    approved_ids = run_content_generation()
    if not approved_ids:
        print("⚠️  TTS не одобрен. Пайплайн завершён.")
        return

    # Step 4
    print("\n" + "="*55 + "\nШАГ 4: ГЕНЕРАЦИЯ АУДИО\n" + "="*55)
    send_tg = input("\n📲 Отправить в Telegram? (y/n): ").strip().lower() == "y"
    from elevenlabs_client import run_audio_generation
    run_audio_generation(send_to_telegram=send_tg)

    print("\n" + "="*55 + "\n✅ ПАЙПЛАЙН ЗАВЕРШЁН\n" + "="*55)
    show_status()


def interactive_menu():
    print(BANNER)
    show_status()

    while True:
        # Show count of ready-to-pick news in menu
        from database import get_connection as _gc
        _c = _gc(); _cur = _c.cursor()
        _cur.execute("SELECT COUNT(*) FROM news_items WHERE status='filtered' AND is_filtered_out=0 AND is_selected=0")
        _ready = _cur.fetchone()[0]; _c.close()
        _ready_str = f" ({_ready} готово)" if _ready else ""

        print(f"""
МЕНЮ:
  [1] 🚀 Полный пайплайн (все 4 шага)
  [2] 📡 Шаг 1: Сбор + AI фильтрация (вручную)
  [3] 📋 Шаг 2: Выбрать новости из готового списка{_ready_str}
  [4] ✍️  Шаг 3: Генерация контента и TTS
  [5] 🔊 Шаг 4: Генерация аудио (ElevenLabs)
  ─────────────────────────────────────────────
  [6] 📊 Статистика БД
  [7] 📋 Очередь — что сделано / что ждёт
  [8] 🔌 Тест подключений
  ─────────────────────────────────────────────
  [u] ↩️  Отменить последнее одобрение TTS
  [c] 🗑️  Очистить старые записи в БД
  [q] Выход
  (Авто-сбор работает каждые 12ч в фоне)
""")
        choice = input("👉 Выбор: ").strip().lower()

        if choice == "q":
            print("Пока!")
            break
        elif choice == "1":
            run_full_pipeline()
        elif choice == "2":
            # Manual: collect fresh + AI filter right now
            from rss_collector import collect_news, print_news_list
            from ai_filter import run_filter_step
            items = collect_news()
            if items:
                print_news_list(items)
            run_filter_step()
        elif choice == "3":
            # Pick from already auto-filtered list
            from ai_filter import show_filtered_for_selection
            show_filtered_for_selection()
        elif choice == "4":
            from content_generator import run_content_generation
            run_content_generation()
        elif choice == "5":
            send_tg = input("📲 Отправить в Telegram? (y/n): ").strip().lower() == "y"
            from elevenlabs_client import run_audio_generation
            run_audio_generation(send_to_telegram=send_tg)
        elif choice == "6":
            show_status()
        elif choice == "7":
            from ai_filter import show_pending_news
            show_pending_news()
        elif choice == "8":
            explain_pipeline()
        elif choice == "9":
            test_connections()
        elif choice == "u":
            undo_last_tts()
        elif choice == "c":
            from database import cleanup_filtered_news, cleanup_old_records
            import yaml
            with open(Path(__file__).parent.parent / "config" / "config.yaml", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            days = cfg["storage"].get("cleanup_filtered_after_days", 3)
            n1 = cleanup_filtered_news(days=days)
            n2 = cleanup_old_records(cfg["storage"]["retention_days"])
            print(f"✅ Удалено: {n1} отфильтрованных + {n2} старых записей")
        else:
            print("⚠️  Неверный выбор")


if __name__ == "__main__":
    init_database()
    args = sys.argv[1:]

    if not args:
        interactive_menu()
    elif "--auto-collect" in args:
        # Silent mode for Windows Task Scheduler — no user input needed
        from ai_filter import run_auto_filter
        count = run_auto_filter()
        print(f"[AUTO] Завершено. {count} важных новостей готовы к выбору.")
    elif "--collect" in args:
        from rss_collector import collect_news, print_news_list
        items = collect_news()
        if items:
            print_news_list(items)
    elif "--filter" in args:
        from ai_filter import run_filter_step
        run_filter_step()
    elif "--content" in args:
        from content_generator import run_content_generation
        run_content_generation()
    elif "--audio" in args:
        from elevenlabs_client import run_audio_generation
        run_audio_generation(send_to_telegram="--telegram" in args)
    elif "--status" in args:
        show_status()
    elif "--test" in args:
        test_connections()
    elif "--cleanup" in args:
        import yaml
        with open(Path(__file__).parent.parent / "config" / "config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        n1 = cleanup_filtered_news(days=cfg["storage"].get("cleanup_filtered_after_days", 3))
        n2 = cleanup_old_records(cfg["storage"]["retention_days"])
        print(f"✅ Удалено: {n1} + {n2} записей")
    elif "--undo-tts" in args:
        undo_last_tts()
    elif "--help" in args or "-h" in args:
        print(__doc__)
    else:
        print(f"Неизвестный аргумент: {args}")
        print("Используй --help для справки")
