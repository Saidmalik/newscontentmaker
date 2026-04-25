"""
analysis_worker.py — Анализ ролика через Claude после 72ч снапшота.

Сохраняет в post_analysis:
  hook_score (strong/medium/weak), hook_feedback,
  watch_time_verdict, cta_verdict,
  main_recommendation, predicted_next
"""

import os
import json
import sqlite3
import logging
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

DB_PATH           = Path(os.environ.get("DB_PATH", str(BASE_DIR / "data" / "news.db")))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANALYSIS_MODEL    = "claude-haiku-4-5-20251001"

log = logging.getLogger("analysis_worker")


def _channel_avgs(conn: sqlite3.Connection) -> dict:
    r = conn.execute(
        "SELECT AVG(views), AVG(reach), AVG(avg_watch_time) "
        "FROM post_snapshots WHERE snapshot_at='72h' AND views > 0"
    ).fetchone()
    return {
        "avg_views": int(r[0] or 0),
        "avg_reach": int(r[1] or 0),
        "avg_watch": round(float(r[2] or 0), 1),
    }


def _est_duration(script: str) -> int:
    """Estimate TTS duration in seconds (~130 words/min for Russian)."""
    return max(15, round(len((script or "").split()) / 130 * 60))


def _extract_cta(tts: str) -> str:
    """Return last 1-2 sentences as estimated CTA."""
    sentences = [s.strip() for s in tts.replace("!", ".").replace("?", ".").split(".") if s.strip()]
    return ". ".join(sentences[-2:]) if len(sentences) >= 2 else tts[-120:]


def _parse_json(raw: str) -> dict:
    """Extract JSON from Claude response, handling ```json blocks."""
    text = raw.strip()
    if "```" in text:
        parts = text.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            try:
                return json.loads(p)
            except Exception:
                pass
    return json.loads(text)


async def analyze_post(post_id: int, db_path: Path, force: bool = False) -> bool:
    """
    Run Claude analysis for post_id.
    force=True re-analyzes even if analysis already exists.
    Returns True if analysis was saved.
    """
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — skipping analysis")
        return False

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    if not force:
        exists = conn.execute(
            "SELECT 1 FROM post_analysis WHERE post_id=?", (post_id,)
        ).fetchone()
        if exists:
            conn.close()
            return False

    # Try snapshot first (prefer 72h); fall back to news.stats_* from PDF upload
    snap_row = conn.execute(
        """SELECT views, reach, saves, comments, avg_watch_time
           FROM post_snapshots WHERE post_id=?
           ORDER BY CASE snapshot_at WHEN '72h' THEN 0 WHEN '48h' THEN 1 ELSE 2 END LIMIT 1""",
        (post_id,),
    ).fetchone()

    post = conn.execute(
        """SELECT title, tts_script, description,
                  stats_views, stats_reach, stats_saves, stats_comments,
                  stats_likes, stats_shares, stats_watch_time
           FROM news WHERE id=?""",
        (post_id,),
    ).fetchone()
    if not post:
        conn.close()
        return False

    # Build unified snap dict from snapshot or news stats
    if snap_row:
        snap = dict(snap_row)
    else:
        sv = post["stats_views"] or 0
        sr = post["stats_reach"] or 0
        if not any([sv, sr, post["stats_saves"], post["stats_comments"]]):
            conn.close()
            return False  # no stats at all
        # Parse watch time string (e.g. "12.3с") to float
        wt_str = post["stats_watch_time"] or ""
        wt = 0.0
        import re as _re
        m = _re.search(r"[\d,\.]+", wt_str)
        if m:
            try:
                wt = float(m.group().replace(",", "."))
            except Exception:
                pass
        snap = {
            "views": sv,
            "reach": sr,
            "saves": post["stats_saves"] or 0,
            "comments": post["stats_comments"] or 0,
            "avg_watch_time": wt or None,
        }

    avgs = _channel_avgs(conn)
    conn.close()

    tts   = (post["tts_script"] or post["description"] or "").strip()
    title = (post["title"] or "").strip()
    hook  = tts.split(".")[0].strip() if tts else title
    cta   = _extract_cta(tts) if tts else ""
    dur   = _est_duration(tts)

    watch_s   = snap.get("avg_watch_time") or 0
    watch_pct = round(watch_s / dur * 100) if dur > 0 else 0

    prompt = f"""Ты аналитик контента. Проанализируй ролик новостного канала про Узбекистан.

Скрипт:
{tts[:1500] if tts else title}

Хук (первое предложение): {hook}
CTA (конец ролика): {cta}
Тема: {title}

Метрики 72ч:
- Просмотры: {snap['views']:,}
- Охват: {snap['reach']:,}
- Avg watch time: {watch_s}с из {dur}с ({watch_pct}%)
- Сохранения: {snap['saves']}
- Комменты: {snap['comments']}

Исторические данные канала:
- Средний охват (72ч): {avgs['avg_reach']:,}
- Средний watch time: {avgs['avg_watch']}с

Дай анализ строго в JSON без пояснений вне JSON:
{{
  "hook_score": "strong" или "medium" или "weak",
  "hook_feedback": "1-2 предложения почему хук сработал или не сработал",
  "watch_time_verdict": "хорошо/средне/плохо + 1 предложение с причиной",
  "cta_verdict": "что сработало или не сработало в завершении ролика",
  "main_recommendation": "одна конкретная вещь для следующего ролика на эту тему",
  "predicted_next": "конкретная идея для следующего ролика"
}}"""

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        msg = await client.messages.create(
            model=ANALYSIS_MODEL,
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        raw  = msg.content[0].text
        data = _parse_json(raw)
    except json.JSONDecodeError as e:
        log.error(f"JSON parse error for post #{post_id}: {e}")
        return False
    except Exception as e:
        log.error(f"Claude API error for post #{post_id}: {e}")
        return False

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT OR REPLACE INTO post_analysis
             (post_id, hook_score, hook_feedback, watch_time_verdict,
              cta_verdict, main_recommendation, predicted_next,
              raw_response, analyzed_at)
           VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
        (
            post_id,
            data.get("hook_score", ""),
            data.get("hook_feedback", ""),
            data.get("watch_time_verdict", ""),
            data.get("cta_verdict", ""),
            data.get("main_recommendation", ""),
            data.get("predicted_next", ""),
            json.dumps(data, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()

    log.info(f"Analysis saved for post #{post_id}: hook={data.get('hook_score')}")
    return True
