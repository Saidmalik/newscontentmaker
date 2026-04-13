"""
Content Generator — generates Instagram content and TTS scripts.

Структура генерируемого контента:
  preview_title  — 2-4 слова для ОБЛОЖКИ Reels
  tts_script     — полный скрипт для ElevenLabs (интро + тело + аутро)
  caption        — текст поста Instagram (без хэштегов)

Два режима:
  1. Генерация через API (локально, без памяти)
  2. Экспорт промпта для Claude.ai Projects (с памятью)
"""

import json
import os
import subprocess
import tempfile
import yaml
from pathlib import Path
from anthropic import Anthropic

from database import (
    get_selected_news, save_content, update_content,
    approve_tts, undo_tts_approval, update_tts_script
)


def load_config() -> dict:
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_preferences() -> dict:
    prefs_path = Path(__file__).parent.parent / "config" / "preferences.yaml"
    with open(prefs_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── NOTEPAD EDITOR ───────────────────────────────────────────────────────

def edit_in_notepad(text: str, hint: str = "") -> str:
    """
    Open text in Notepad, wait for user to save and close.
    Returns edited text. If user didn't change — returns original.
    """
    if hint:
        header = f"# {hint}\n# Редактируй текст ниже. Сохрани (Ctrl+S) и закрой окно.\n# Эту строку можно удалить.\n\n"
    else:
        header = ""

    full_text = header + text

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8", prefix="uz_news_"
    ) as f:
        f.write(full_text)
        tmp_path = f.name

    print(f"\n  📝 Открываю Notepad... (сохрани и закрой чтобы продолжить)")
    subprocess.run(["notepad.exe", tmp_path], check=False)

    try:
        with open(tmp_path, encoding="utf-8") as f:
            result = f.read()
        os.unlink(tmp_path)
    except Exception:
        return text

    # Strip helper comments if user left them
    lines = [l for l in result.split("\n") if not l.startswith("# ")]
    cleaned = "\n".join(lines).strip()
    return cleaned if cleaned else text


# ── PROMPT BUILDER ───────────────────────────────────────────────────────

def build_content_prompt(news_item: dict, config: dict, prefs: dict) -> str:
    style = prefs.get("content_style", {})
    tts_max_sec = config["content"]["tts_max_duration_seconds"]

    tts_intros = "\n".join(
        f"  - {v}" for v in style.get("tts_intro_variants", [])
    )

    return f"""Ты контент-менеджер новостного Instagram-аккаунта.

═══ О КАНАЛЕ ═══
{style.get("account_tone", "")}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ЭЛЕМЕНТ 1 — ПРЕВЬЮ (preview_title)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Текст на обложке Reels. Человек видит это до нажатия.

{style.get("preview_title_rules", "2-4 слова. Суть новости. Остановить скролл.")}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ЭЛЕМЕНТ 2 — TTS СКРИПТ (tts_script)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Произносится голосом. Длина: до {tts_max_sec} сек.

{style.get("tts_rules", "")}

Варианты начальной фразы (выбери по контексту):
{tts_intros}

Правила финального вопроса (ЧИТАЙ ВНИМАТЕЛЬНО):
{style.get("tts_outro_rules", "Задай вопрос по теме. Напишите в комментариях.")}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ЭЛЕМЕНТ 3 — CAPTION (caption)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Текст поста под Reels.

{style.get("caption_rules", "")}

ХЭШТЕГИ: не добавлять вообще — ни в caption, ни в tts_script.

═══ НОВОСТЬ ═══
Заголовок: {news_item["title"]}
Категория: {news_item.get("ai_category") or news_item.get("category") or "другое"}
Описание: {(news_item.get("summary") or "нет")[:500]}
Источник: {news_item.get("source_name", "")}

═══ ОТВЕТ — ТОЛЬКО JSON ═══
{{
  "preview_title": "2-4 слова",
  "tts_script": "полный скрипт без хэштегов",
  "caption": "текст поста без хэштегов"
}}"""


def build_claude_ai_prompt(news_item: dict, prefs: dict) -> str:
    """
    Generate a prompt to paste into Claude.ai Projects.
    Shorter — assumes your Project already has the instructions.
    """
    style = prefs.get("content_style", {})
    cat = news_item.get("ai_category") or news_item.get("category") or "другое"

    return f"""Создай контент для этой новости:

Заголовок: {news_item["title"]}
Категория: {cat}
Источник: {news_item.get("source_name", "")}
Описание: {(news_item.get("summary") or "нет")[:600]}
URL: {news_item.get("url", "")}

Нужно:
1. preview_title — 2-4 слова для обложки Reels
2. tts_script — скрипт для озвучки (интро + суть + аутро-вопрос по теме)
3. caption — текст поста без хэштегов"""


# ── AI GENERATION ────────────────────────────────────────────────────────

def generate_content(news_item: dict, config: dict, prefs: dict,
                     client: Anthropic) -> dict | None:
    """Generate content via Claude API. Returns dict or None."""
    model = config.get("models", {}).get("content_model", "claude-sonnet-4-5")
    print(f"\n  ✍️  Генерирую: {news_item['title'][:65]}...")

    prompt = build_content_prompt(news_item, config, prefs)

    try:
        message = client.messages.create(
            model=model,
            max_tokens=2500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = message.content[0].text.strip()

        # Extract JSON robustly
        import re
        code = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if code:
            raw = code.group(1)
        else:
            obj = re.search(r"\{.*\}", raw, re.DOTALL)
            if obj:
                raw = obj.group(0)

        result = json.loads(raw)

        # Strip any hashtags Claude snuck in anyway
        for key in ("caption", "tts_script", "preview_title"):
            if key in result and isinstance(result[key], str):
                result[key] = _strip_hashtags(result[key])

        return result

    except json.JSONDecodeError as e:
        print(f"  ❌ JSON ошибка: {e}")
        return None
    except Exception as e:
        print(f"  ❌ API ошибка: {e}")
        return None


def _strip_hashtags(text: str) -> str:
    """Remove hashtags from any text field."""
    import re
    # Remove #word patterns
    text = re.sub(r"#\w+", "", text)
    # Clean up extra spaces/newlines left behind
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── DISPLAY ──────────────────────────────────────────────────────────────

def display_content(news_item: dict, content: dict):
    """Pretty-print generated content for review."""
    print(f"\n{'═'*68}")
    print(f"📰 {news_item.get('title', '')[:65]}")
    print(f"{'═'*68}")

    # Preview title
    preview = content.get("preview_title", "")
    print(f"\n🖼️  ПРЕВЬЮ (обложка Reels):")
    print(f"   ┌{'─'*34}┐")
    print(f"   │  {preview:<32}│")
    print(f"   └{'─'*34}┘")

    # TTS script
    tts = content.get("tts_script", "")
    word_count = len(tts.split())
    est_dur = round(word_count / 2.3, 1)
    print(f"\n🎙️  TTS СКРИПТ (~{est_dur} сек, {word_count} слов):")
    print(f"{'─'*60}")
    # Highlight intro and outro
    sentences = [s.strip() for s in tts.replace("?", "?.").split(".") if s.strip()]
    if sentences:
        print(f"  [ИНТРО] {sentences[0]}.")
        for s in sentences[1:-1]:
            print(f"          {s}.")
        if len(sentences) > 1:
            last = sentences[-1]
            print(f"  [АУТРО] {last}{'?' if not last.endswith('?') else ''}")
    print(f"{'─'*60}")

    # Caption
    caption = content.get("caption", "")
    print(f"\n📸 CAPTION:")
    print(f"{'─'*60}")
    # Show first 300 chars clearly
    print(caption[:300])
    if len(caption) > 300:
        print(f"  ... [+{len(caption)-300} симв]")
    print(f"{'─'*60}")

    print(f"\n🔗 {news_item.get('url', '')}")


# ── CONFIRMATION LOOP ────────────────────────────────────────────────────

def confirm_content(news_item: dict, content: dict, content_id: int) -> bool | str:
    """
    Interactive review and confirmation.
    Returns: True (approved) | False (skip) | 'regenerate'
    """
    while True:
        display_content(news_item, content)

        print(f"\n{'═'*68}")
        print("[1] ✅ Одобрить — отправить на ElevenLabs")
        print("[2] ✏️  Редактировать превью  (откроется Notepad)")
        print("[3] ✏️  Редактировать TTS     (откроется Notepad)")
        print("[4] ✏️  Редактировать Caption (откроется Notepad)")
        print("[5] 🔄 Перегенерировать всё заново")
        print("[6] ⏭️  Пропустить эту новость")
        print(f"{'═'*68}")

        choice = input("\n👉 Выбор: ").strip()

        if choice == "1":
            approve_tts(content_id)
            print("\n✅ Одобрено! Аудио будет создано на шаге 4.")
            return True

        elif choice == "2":
            edited = edit_in_notepad(
                content.get("preview_title", ""),
                "ПРЕВЬЮ — 2-4 слова для обложки Reels"
            )
            if edited != content.get("preview_title"):
                content["preview_title"] = edited
                _save_to_db(content_id, content)
                print(f"  ✅ Превью обновлено: «{edited}»")

        elif choice == "3":
            edited = edit_in_notepad(
                content.get("tts_script", ""),
                "TTS СКРИПТ — текст для ElevenLabs (без хэштегов, без скобок)"
            )
            if edited != content.get("tts_script"):
                content["tts_script"] = _strip_hashtags(edited)
                update_tts_script(content_id, content["tts_script"])
                print("  ✅ TTS обновлён.")

        elif choice == "4":
            edited = edit_in_notepad(
                content.get("caption", ""),
                "CAPTION — текст поста Instagram (без хэштегов)"
            )
            if edited != content.get("caption"):
                content["caption"] = _strip_hashtags(edited)
                _save_to_db(content_id, content)
                print("  ✅ Caption обновлён.")

        elif choice == "5":
            return "regenerate"

        elif choice == "6":
            print("⏭️  Пропускаю.")
            return False

        else:
            print("⚠️  Введи 1–6")


# ── CLAUDE.AI EXPORT ─────────────────────────────────────────────────────

def export_to_claude_ai(news_items: list[dict], prefs: dict):
    """
    Export news as formatted prompts to paste in Claude.ai Projects.
    Copies to clipboard and saves to file.
    """
    print(f"\n{'═'*65}")
    print(f"📋 ЭКСПОРТ ДЛЯ CLAUDE.AI PROJECTS")
    print(f"   Генерирую промпты для {len(news_items)} новостей...")
    print(f"{'═'*65}")

    all_prompts = []
    for i, item in enumerate(news_items, 1):
        prompt = build_claude_ai_prompt(item, prefs)
        all_prompts.append(f"{'═'*60}\nНОВОСТЬ {i} из {len(news_items)}\n{'═'*60}\n{prompt}")

    combined = "\n\n".join(all_prompts)

    # Save to file
    out_path = Path(__file__).parent.parent / "output" / "claude_ai_prompts.txt"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(combined, encoding="utf-8")

    # Copy to clipboard (Windows)
    try:
        subprocess.run("clip", input=combined.encode("utf-8"), check=True)
        clipboard_ok = True
    except Exception:
        clipboard_ok = False

    print(f"\n✅ Готово!")
    print(f"   📄 Файл: {out_path}")
    if clipboard_ok:
        print(f"   📋 Скопировано в буфер обмена!")
    print(f"\n   Что делать:")
    print(f"   1. Открой Claude.ai → твой Project")
    print(f"   2. Вставь (Ctrl+V) — там все {len(news_items)} новостей")
    print(f"   3. Claude сгенерирует контент по твоим правилам из Project")
    print(f"   4. Скопируй результат обратно и вставь в шаг 4 бота")
    print(f"\n   Или открой файл: {out_path.name}")
    input("\n  Enter чтобы продолжить...")


# ── MAIN ENTRY POINTS ────────────────────────────────────────────────────

def _save_to_db(content_id: int, content: dict):
    update_content(
        content_id=content_id,
        preview_title=content.get("preview_title", ""),
        caption=content.get("caption", ""),
        hashtags="",
        tts_script=content.get("tts_script", ""),
    )


def run_content_generation() -> list[int]:
    """
    Full content generation step.
    Asks user: generate via API or export to Claude.ai.
    Returns list of approved content_ids.
    """
    config = load_config()
    prefs = load_preferences()

    news_items = get_selected_news()
    if not news_items:
        print("\nℹ️  Нет выбранных новостей. Сначала запусти шаг 2.")
        return []

    print(f"\n{'═'*60}")
    print(f"✍️  ГЕНЕРАЦИЯ КОНТЕНТА — {len(news_items)} новостей")
    print(f"{'═'*60}")
    print(f"\nКак генерировать?")
    print(f"  [1] 🤖 Через API (без памяти, автоматически)")
    print(f"  [2] 🌐 Экспорт для Claude.ai Projects (с твоей памятью)")
    print(f"  [q] Отмена")

    mode = input("\n👉 Режим: ").strip().lower()

    if mode == "q":
        return []

    if mode == "2":
        export_to_claude_ai(news_items, prefs)
        return []

    # Mode 1: API generation
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    model = config.get("models", {}).get("content_model", "claude-sonnet-4-5")
    print(f"\n  Модель: {model}")

    approved_ids = []

    for news_item in news_items:
        content = None
        content_id = None

        for attempt in range(1, 4):
            content = generate_content(news_item, config, prefs, client)

            if not content:
                print(f"  ⚠️  Попытка {attempt}/3 не удалась")
                if attempt == 3:
                    print("  ❌ Пропускаю")
                continue

            # Save/update DB
            if content_id is None:
                content_id = save_content(
                    news_id=news_item["id"],
                    preview_title=content.get("preview_title", ""),
                    caption=content.get("caption", ""),
                    hashtags="",
                    tts_script=content.get("tts_script", ""),
                )
            else:
                _save_to_db(content_id, content)

            result = confirm_content(news_item, content, content_id)

            if result is True:
                approved_ids.append(content_id)
                break
            elif result == "regenerate":
                print(f"\n  🔄 Перегенерирую (попытка {attempt + 1})...")
                continue
            else:
                break

    print(f"\n{'═'*60}")
    print(f"✅ Одобрено: {len(approved_ids)} из {len(news_items)}")
    if approved_ids:
        print(f"   Следующий шаг: меню [5] — Генерация аудио")
    print(f"{'═'*60}")

    return approved_ids


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
    from database import init_database
    init_database()
    run_content_generation()
