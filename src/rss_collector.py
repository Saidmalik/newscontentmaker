"""
RSS Collector — fetches and parses news from configured RSS feeds.
Handles deduplication, keyword pre-filtering, and DB storage.
"""

import feedparser
import yaml
import re
import html
from pathlib import Path
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from database import save_news_items, get_connection


def load_config() -> dict:
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_preferences() -> dict:
    prefs_path = Path(__file__).parent.parent / "config" / "preferences.yaml"
    with open(prefs_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def clean_html(text: str) -> str:
    """Strip HTML tags and decode entities."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_date(entry) -> str:
    """Try to extract a clean ISO date string from feed entry."""
    for field in ("published_parsed", "updated_parsed"):
        val = getattr(entry, field, None)
        if val:
            try:
                return datetime(*val[:6]).isoformat()
            except Exception:
                pass
    for field in ("published", "updated"):
        val = getattr(entry, field, None)
        if val:
            try:
                return parsedate_to_datetime(val).isoformat()
            except Exception:
                pass
    return datetime.now().isoformat()


def get_summary(entry) -> str:
    """Extract the best available summary from feed entry."""
    summary = getattr(entry, "summary", "") or ""
    if not summary:
        content = getattr(entry, "content", None)
        if content and isinstance(content, list):
            summary = content[0].get("value", "")
    return clean_html(summary)[:1000]


def is_already_known(url: str) -> bool:
    """Check if URL already exists in DB."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM news_items WHERE url = ?", (url,))
    found = cursor.fetchone() is not None
    conn.close()
    return found


def passes_keyword_prefilter(item: dict, prefs: dict) -> tuple[bool, str]:
    """
    Fast keyword pre-filter BEFORE saving to DB.
    Returns (passes: bool, reason: str)

    This saves AI tokens — we don't send obvious garbage to Claude.
    """
    news_filter = prefs.get("news_filter", {})
    title_lower = item.get("title", "").lower()
    summary_lower = item.get("summary", "").lower()
    full_text = title_lower + " " + summary_lower

    # 1. Hard exclude — immediate rejection
    hard_exclude = news_filter.get("hard_exclude_keywords", [])
    for kw in hard_exclude:
        if kw.lower() in full_text:
            return False, f"hard_exclude: '{kw}'"

    # 2. Age filter
    max_age_hours = news_filter.get("max_age_hours", 0)
    if max_age_hours > 0:
        published_str = item.get("published_at", "")
        if published_str:
            try:
                pub_dt = datetime.fromisoformat(published_str)
                age = datetime.now() - pub_dt
                if age > timedelta(hours=max_age_hours):
                    return False, f"too_old: {age.total_seconds()/3600:.0f}h"
            except Exception:
                pass  # Can't parse date, allow it

    # 3. Require any keyword (optional)
    require_any = news_filter.get("require_any_keyword", [])
    if require_any:
        if not any(kw.lower() in full_text for kw in require_any):
            return False, "no_required_keyword"

    return True, "ok"


def fetch_source(source: dict, prefs: dict) -> tuple[list[dict], int]:
    """
    Fetch and parse a single RSS source.
    Returns (items_list, pre_filtered_count)
    """
    print(f"  📡 {source['name']} ({source['url']})")
    items = []
    pre_filtered = 0

    try:
        feed = feedparser.parse(source["url"])

        if feed.bozo and not feed.entries:
            print(f"  ⚠️  Feed error: {feed.bozo_exception}")
            return [], 0

        for entry in feed.entries:
            url = getattr(entry, "link", "") or getattr(entry, "id", "")
            if not url:
                continue

            title = clean_html(getattr(entry, "title", "")).strip()
            if not title:
                continue

            item = {
                "url": url,
                "title": title,
                "summary": get_summary(entry),
                "source_name": source["name"],
                "source_url": source["url"],
                "published_at": parse_date(entry),
            }

            # Pre-filter check
            passes, reason = passes_keyword_prefilter(item, prefs)
            if not passes:
                pre_filtered += 1
                continue

            items.append(item)

        status = f"✅ {source['name']}: {len(items)} прошло"
        if pre_filtered:
            status += f" (отсеяно: {pre_filtered})"
        print(f"  {status}")

    except Exception as e:
        print(f"  ❌ Ошибка: {e}")

    return items, pre_filtered


def collect_news(verbose: bool = True, silent: bool = False) -> list[dict]:
    if silent:
        verbose = False
    """
    Main function: fetch all sources, pre-filter, deduplicate, save to DB.
    Returns list of NEW items saved.
    """
    config = load_config()
    prefs = load_preferences()
    sources = config.get("sources", [])
    max_news = config["filter"]["max_news_per_run"]

    if verbose:
        print(f"\n{'='*55}")
        print(f"📰 Сбор новостей из {len(sources)} источников")
        print(f"{'='*55}")

    all_items = []
    total_pre_filtered = 0

    for source in sources:
        items, pre_filtered = fetch_source(source, prefs)
        all_items.extend(items)
        total_pre_filtered += pre_filtered

    # Deduplicate by URL within this batch
    seen_urls = set()
    unique_items = []
    for item in all_items:
        if item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            unique_items.append(item)

    # Filter out already known URLs
    new_items = [i for i in unique_items if not is_already_known(i["url"])]

    if verbose:
        print(f"\n📊 Статистика:")
        print(f"   Всего из RSS:       {len(all_items)}")
        print(f"   Отсеяно (ключевые): {total_pre_filtered}")
        print(f"   Уникальных новых:   {len(new_items)}")

    if not new_items:
        print("ℹ️  Нет новых новостей.")
        return []

    # Apply max limit
    if len(new_items) > max_news:
        print(f"   ⚠️  Лимит {max_news}, остальные {len(new_items)-max_news} пропущены")
        new_items = new_items[:max_news]

    saved_count = save_news_items(new_items)
    if verbose:
        print(f"   💾 Сохранено в БД:  {saved_count}")
        print(f"{'='*55}\n")

    return new_items


def print_news_list(items: list[dict]):
    """Pretty print a list of news items."""
    print(f"\n{'='*70}")
    print(f"{'#':<4} {'Источник':<12} {'Заголовок'}")
    print(f"{'='*70}")
    for i, item in enumerate(items, 1):
        source = item.get("source_name", "?")[:11]
        title = item.get("title", "")[:55]
        print(f"{i:<4} {source:<12} {title}")
    print(f"{'='*70}")


if __name__ == "__main__":
    from database import init_database
    init_database()
    items = collect_news()
    if items:
        print_news_list(items)
