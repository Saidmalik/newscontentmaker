"""
AI Filter — uses Claude to score and filter news by importance.
Uses preferences.yaml for user-defined criteria.
Presents results with full URLs for source inspection.

Fix: batching (max 15 per request) to prevent JSON truncation.
"""

import json
import re
import yaml
import os
from pathlib import Path
from anthropic import Anthropic

from database import (
    get_unfiltered_news, update_news_filter_result,
    mark_news_selected, delete_rejected_news
)

BATCH_SIZE = 15   # Max news per Claude request — prevents JSON truncation


def load_config() -> dict:
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_preferences() -> dict:
    prefs_path = Path(__file__).parent.parent / "config" / "preferences.yaml"
    with open(prefs_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def extract_json_array(text: str) -> list | None:
    """
    Robustly extract a JSON array from Claude's response.
    Handles markdown code blocks, extra text before/after JSON.
    """
    # 1. Try to extract from ```json ... ``` block
    code_block = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if code_block:
        try:
            return json.loads(code_block.group(1))
        except json.JSONDecodeError:
            pass

    # 2. Try to find first [...] in text
    bracket_match = re.search(r"\[.*\]", text, re.DOTALL)
    if bracket_match:
        try:
            return json.loads(bracket_match.group(0))
        except json.JSONDecodeError:
            # 3. Try to fix truncated JSON by finding last complete object
            raw = bracket_match.group(0)
            last_brace = raw.rfind("},")
            if last_brace > 0:
                trimmed = raw[:last_brace + 1] + "]"
                try:
                    return json.loads(trimmed)
                except json.JSONDecodeError:
                    pass

    # 4. Last resort — try parsing the whole text
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None


def build_filter_prompt(news_batch: list[dict], min_score: int,
                         priority_topics: str, exclude_topics: str,
                         account_tone: str) -> str:
    news_list = ""
    for i, item in enumerate(news_batch, 1):
        summary = (item.get("summary") or "")[:200]
        news_list += (
            f"\n[{i}] {item['title']}\n"
            f"    Источник: {item['source_name']}\n"
        )
        if summary:
            news_list += f"    Описание: {summary}\n"

    return f"""Ты редактор новостного Instagram-аккаунта про Узбекистан.

О КАНАЛЕ:
{account_tone}

ПРИОРИТЕТНЫЕ ТЕМЫ (высокий балл):
{priority_topics}

НЕ НУЖНЫ (низкий балл):
{exclude_topics}

КРИТЕРИИ оценки (1-10):
- 8-10: Прямо влияет на жизнь людей, скандал, важное решение власти, крупные цифры
- 5-7: Полезно знать, умеренный интерес
- 1-4: Реклама, PR, спорт без скандала, культура, незначимые события
Балл {min_score}+ = достойна публикации (filtered_out: false)
Балл < {min_score} = filtered_out: true

НОВОСТИ ДЛЯ ОЦЕНКИ:
{news_list}

Верни ТОЛЬКО JSON массив, без пояснений, без markdown:
[{{"index": 1, "score": 8, "category": "экономика", "reason": "...", "filtered_out": false}},
 {{"index": 2, "score": 3, "category": "культура", "reason": "...", "filtered_out": true}}]

category — одно из: экономика, политика, общество, происшествия, технологии, здоровье, другое"""


def filter_batch(batch: list[dict], batch_num: int, total_batches: int,
                  config: dict, prefs: dict, client: Anthropic,
                  filter_model: str, min_score: int) -> list[dict]:
    """Filter one batch of news. Returns list of important items."""
    news_filter = prefs.get("news_filter", {})
    priority_topics = "\n".join(
        f"  - {t}" for t in news_filter.get("priority_topics", [])
    )
    exclude_topics = "\n".join(
        f"  - {t}" for t in news_filter.get("exclude_topics", [])
    )
    account_tone = prefs.get("content_style", {}).get("account_tone", "")

    prompt = build_filter_prompt(
        batch, min_score, priority_topics, exclude_topics, account_tone
    )

    print(f"  📦 Батч {batch_num}/{total_batches} ({len(batch)} новостей)...", end=" ", flush=True)

    for attempt in range(1, 4):
        try:
            message = client.messages.create(
                model=filter_model,
                max_tokens=3000,   # Enough for 15 items
                messages=[{"role": "user", "content": prompt}]
            )
            response_text = message.content[0].text.strip()
            ai_results = extract_json_array(response_text)

            if ai_results is None:
                print(f"⚠️  Попытка {attempt}: не удалось извлечь JSON")
                if attempt == 3:
                    print(f"\n  Ответ (первые 500 симв):\n  {response_text[:500]}")
                    return []
                continue

            print(f"✅ OK ({len(ai_results)} результатов)")
            break

        except Exception as e:
            print(f"❌ Ошибка API (попытка {attempt}): {e}")
            if attempt == 3:
                return []

    # Process results
    important_in_batch = []
    for result in ai_results:
        idx = result.get("index", 0) - 1
        if idx < 0 or idx >= len(batch):
            continue

        item = batch[idx]
        score = result.get("score", 0)
        filtered_out = result.get("filtered_out", score < min_score)

        update_news_filter_result(
            news_id=item["id"],
            score=score,
            reason=result.get("reason", ""),
            category=result.get("category", "другое"),
            filtered_out=filtered_out
        )

        if not filtered_out:
            item["ai_score"] = score
            item["ai_category"] = result.get("category", "")
            item["ai_reason"] = result.get("reason", "")
            important_in_batch.append(item)

    return important_in_batch


def filter_news_with_ai(verbose: bool = True) -> list[dict]:
    """Fetch unfiltered news, send to Claude in batches, save results to DB."""
    config = load_config()
    prefs = load_preferences()
    filter_model = config.get("models", {}).get("filter_model", "claude-haiku-4-5")
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    min_score = prefs.get("news_filter", {}).get(
        "min_importance_score", config["filter"]["min_importance_score"]
    )

    news_items = get_unfiltered_news(limit=config["filter"]["max_news_per_run"])

    if not news_items:
        print("ℹ️  Нет новостей для фильтрации.")
        return []

    if verbose:
        print(f"\n{'='*55}")
        print(f"🤖 AI фильтрация ({filter_model})")
        print(f"   Анализирую {len(news_items)} новостей (батчами по {BATCH_SIZE})")
        print(f"{'='*55}")

    # Split into batches
    batches = [
        news_items[i:i + BATCH_SIZE]
        for i in range(0, len(news_items), BATCH_SIZE)
    ]
    total_batches = len(batches)
    all_important = []

    for batch_num, batch in enumerate(batches, 1):
        important = filter_batch(
            batch, batch_num, total_batches,
            config, prefs, client, filter_model, min_score
        )
        all_important.extend(important)

    # Sort by score
    all_important.sort(key=lambda x: x.get("ai_score", 0), reverse=True)

    if verbose:
        filtered_count = len(news_items) - len(all_important)
        print(f"\n✅ Прошло фильтр: {len(all_important)} | Отсеяно: {filtered_count}")

    return all_important


def present_and_confirm(important_news: list[dict]) -> list[dict]:
    """
    Show filtered news to user with FULL URLs.
    User selects which to proceed with.
    Rejected news are DELETED from DB to keep it clean.
    """
    if not important_news:
        print("❌ Нет важных новостей после фильтрации.")
        return []

    top_count = min(len(important_news), 20)
    display_news = important_news[:top_count]

    _print_news_list(display_news)

    print(f"{'='*70}")
    print("Введи номера через запятую:  1,3,5")
    print("'all' = все  |  'q' = выход  |  'open N' = открыть ссылку N")
    print(f"{'='*70}")

    while True:
        user_input = input("\n👉 Выбор: ").strip()

        if user_input.lower().startswith("open "):
            try:
                n = int(user_input.split()[1]) - 1
                if 0 <= n < len(display_news):
                    print(f"\n🔗 [{n+1}]: {display_news[n].get('url', '')}\n")
                else:
                    print("⚠️  Номер вне диапазона")
            except (ValueError, IndexError):
                print("⚠️  Формат: open 3")
            continue

        if user_input.lower() == "q":
            print("Отмена.")
            return []

        if user_input.lower() == "all":
            selected = display_news
            break

        try:
            indices = [int(x.strip()) - 1 for x in user_input.split(",")]
            selected = [display_news[i] for i in indices
                        if 0 <= i < len(display_news)]
            if selected:
                break
            print("⚠️  Неверные номера.")
        except (ValueError, IndexError):
            print("⚠️  Введи числа через запятую, например: 1,2,3")

    # Mark selected in DB
    selected_ids = [item["id"] for item in selected]
    mark_news_selected(selected_ids)

    # Delete rejected news from DB to keep it clean
    all_ids = [item["id"] for item in display_news]
    rejected_ids = [nid for nid in all_ids if nid not in selected_ids]
    if rejected_ids:
        deleted = delete_rejected_news(rejected_ids)
        if deleted:
            print(f"🗑️  Удалено из БД: {deleted} отклонённых")

    print(f"\n✅ Выбрано {len(selected)} новостей:")
    for item in selected:
        print(f"   • {item['title'][:70]}")

    return selected


def _print_news_list(news_list: list[dict]):
    """Print news list with scores, categories, URLs."""
    print(f"\n{'='*70}")
    print(f"📋 ВАЖНЫЕ НОВОСТИ ({len(news_list)}) — выбери для публикации:")
    print(f"{'='*70}")

    for i, item in enumerate(news_list, 1):
        score = item.get("ai_score", "?")
        cat = (item.get("ai_category") or "").upper()
        reason = item.get("ai_reason", "")
        title = item.get("title", "")
        url = item.get("url", "")
        source = item.get("source_name", "")

        # Score indicator
        if isinstance(score, int):
            bar = "█" * score + "░" * (10 - score)
        else:
            bar = "?"

        print(f"\n[{i:>2}]  {bar} {score}/10  [{cat}]  {source}")
        print(f"       {title}")
        print(f"       💬 {reason}")
        print(f"       🔗 {url}")


def show_pending_news():
    """
    Show news that were selected but haven't been processed yet
    (no content generated). Lets user see what's still in the queue.
    """
    from database import get_selected_news, get_connection

    conn = get_connection()
    cursor = conn.cursor()

    # News selected but no content yet
    cursor.execute("""
        SELECT n.*
        FROM news_items n
        LEFT JOIN content_items c ON c.news_id = n.id
        WHERE n.is_selected = 1 AND c.id IS NULL
        ORDER BY n.importance_score DESC
    """)
    pending_content = [dict(r) for r in cursor.fetchall()]

    # News with content but TTS not approved
    cursor.execute("""
        SELECT n.*, c.id as content_id, c.preview_title, c.tts_approved
        FROM news_items n
        JOIN content_items c ON c.news_id = n.id
        WHERE c.tts_approved = 0
        ORDER BY n.importance_score DESC
    """)
    pending_tts = [dict(r) for r in cursor.fetchall()]

    # News with TTS approved but no audio
    cursor.execute("""
        SELECT n.*, c.id as content_id, c.preview_title
        FROM news_items n
        JOIN content_items c ON c.news_id = n.id
        LEFT JOIN audio_files a ON a.news_id = n.id
        WHERE c.tts_approved = 1 AND a.id IS NULL
        ORDER BY n.importance_score DESC
    """)
    pending_audio = [dict(r) for r in cursor.fetchall()]

    # Completed (audio generated)
    cursor.execute("""
        SELECT n.title, n.source_name, a.generated_at, a.file_name,
               c.preview_title
        FROM news_items n
        JOIN audio_files a ON a.news_id = n.id
        JOIN content_items c ON c.news_id = n.id
        ORDER BY a.generated_at DESC
        LIMIT 10
    """)
    completed = [dict(r) for r in cursor.fetchall()]
    conn.close()

    print(f"\n{'═'*65}")
    print(f"📋 ОЧЕРЕДЬ НОВОСТЕЙ")
    print(f"{'═'*65}")

    # --- Waiting for content generation
    if pending_content:
        print(f"\n⏳ Ждут генерации контента ({len(pending_content)}):")
        print(f"{'─'*60}")
        for i, item in enumerate(pending_content, 1):
            score = item.get("importance_score") or "?"
            print(f"  [{i}] ⭐{score}/10  {item['title'][:60]}")
            print(f"       🔗 {item.get('url', '')}")
    else:
        print(f"\n✅ Очередь контента пуста")

    # --- Waiting for TTS approval
    if pending_tts:
        print(f"\n✏️  Контент готов, ждут одобрения TTS ({len(pending_tts)}):")
        print(f"{'─'*60}")
        for i, item in enumerate(pending_tts, 1):
            preview = item.get("preview_title") or "—"
            print(f"  [{i}] «{preview}»  |  {item['title'][:45]}")
    else:
        print(f"\n✅ Нет ожидающих TTS")

    # --- Waiting for audio
    if pending_audio:
        print(f"\n🎙️  TTS одобрен, ждут генерации аудио ({len(pending_audio)}):")
        print(f"{'─'*60}")
        for i, item in enumerate(pending_audio, 1):
            preview = item.get("preview_title") or "—"
            print(f"  [{i}] «{preview}»  |  {item['title'][:45]}")
    else:
        print(f"\n✅ Нет ожидающих аудио")

    # --- Completed
    if completed:
        print(f"\n✅ Последние завершённые (аудио готово):")
        print(f"{'─'*60}")
        for item in completed:
            preview = item.get("preview_title") or "—"
            gen_at = (item.get("generated_at") or "")[:16]
            fname = item.get("file_name") or "?"
            print(f"  🎵 «{preview}»  |  {item['title'][:40]}")
            print(f"      {gen_at}  →  {fname}")

    print(f"\n{'═'*65}")


def run_filter_step() -> list[dict]:
    """Full filter step: AI filtering + user confirmation."""
    important_news = filter_news_with_ai()
    if not important_news:
        return []
    return present_and_confirm(important_news)


def run_auto_filter() -> int:
    """
    Auto mode: collect + AI filter silently, NO user interaction.
    Saves scored news to DB. User selects later via show_filtered_for_selection().
    Returns count of important news saved.
    """
    from rss_collector import collect_news
    print(f"[AUTO] Сбор новостей...")
    new_items = collect_news(silent=True)
    print(f"[AUTO] Новых в БД: {len(new_items)}")

    if not new_items:
        print("[AUTO] Нет новых новостей.")
        return 0

    important = filter_news_with_ai(verbose=False)
    print(f"[AUTO] Прошло фильтр: {len(important)} важных")
    return len(important)


def show_filtered_for_selection() -> list[dict]:
    """
    Show already AI-filtered news (from auto-collect run) for user to select.
    Use this instead of run_filter_step() when auto-collect already ran.
    """
    from database import get_connection
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM news_items
        WHERE status = 'filtered' AND is_filtered_out = 0 AND is_selected = 0
        ORDER BY importance_score DESC
        LIMIT 20
    """)
    already_filtered = [dict(r) for r in cursor.fetchall()]
    conn.close()

    if not already_filtered:
        print("\nℹ️  Нет отфильтрованных новостей.")
        print("   Запусти [2] Сбор новостей, или подожди авто-сбора (каждые 12ч).")
        # Check if there are unfiltered ones to process now
        from database import get_unfiltered_news
        unfiltered = get_unfiltered_news(limit=5)
        if unfiltered:
            print(f"\n   Найдено {len(unfiltered)} необработанных — запускаю AI фильтр...")
            return run_filter_step()
        return []

    # Add ai_ keys for display compatibility
    for item in already_filtered:
        item.setdefault("ai_score", item.get("importance_score", 0))
        item.setdefault("ai_category", item.get("category", ""))
        item.setdefault("ai_reason", item.get("importance_reason", ""))

    print(f"\n✅ Найдено {len(already_filtered)} новостей из авто-сбора.")
    return present_and_confirm(already_filtered)


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
    from database import init_database
    init_database()

    if "--auto" in sys.argv:
        count = run_auto_filter()
        print(f"\n[AUTO] Готово. {count} важных новостей в БД.")
    else:
        selected = run_filter_step()
        print(f"\nИтог: {len(selected)} новостей выбрано.")
