"""
============================================================
  fetch_leads.py — Hourly lead scraper
============================================================
  Pulls last ~70 min of posts from 5 subs, classifies via Claude,
  enriches new end_user/unclear authors, writes to Supabase,
  sends Telegram summary.

  Run manually:
      python fetch_leads.py
  Or via cron (hourly).
============================================================
"""

import time
from datetime import datetime, timezone, timedelta

from lib import (
    log, SUBREDDITS, ENRICH_CLASSIFICATIONS, DELAY_BETWEEN_REQUESTS,
    fetch_subreddit_posts, classify_and_extract, is_hyderabad_re,
    post_already_exists, insert_lead, enrich_user, upsert_user_profile,
    create_batch, update_batch, send_telegram_summary,
)

LOOKBACK_MINUTES = 70
POSTS_PER_SUB = 25


def main():
    run_at = datetime.now(timezone.utc)
    cutoff = run_at - timedelta(minutes=LOOKBACK_MINUTES)
    log.info(f"=== Run start: {run_at.isoformat()} ===")

    batch_id = create_batch()
    log.info(f"Created batch_id={batch_id}")

    source_counts = {}
    totals = {"total": 0, "end_user": 0, "agent": 0, "unclear": 0}
    total_tokens = 0
    new_authors_to_enrich: set[tuple[str, str]] = set()  # (author, classification)

    for i, sub_cfg in enumerate(SUBREDDITS):
        sub_name = sub_cfg["name"]
        needs_hyd_filter = sub_cfg["filter_hyd_re"]
        kept = 0

        if i > 0:
            time.sleep(DELAY_BETWEEN_REQUESTS)

        posts = fetch_subreddit_posts(sub_name, limit=POSTS_PER_SUB)
        log.info(f"r/{sub_name}: fetched {len(posts)} posts")

        for post in posts:
            posted_at = datetime.fromtimestamp(post["created_utc"], tz=timezone.utc)
            if posted_at < cutoff:
                continue
            if post_already_exists(post["id"]):
                continue

            title, body, author, flair = post["title"], post["body"], post["author"], post["flair"]

            if needs_hyd_filter:
                is_re, _ = is_hyderabad_re(title, body)
                if not is_re:
                    log.info(f"  Skipped (not Hyd-RE): {title[:60]}")
                    continue

            extracted, _ = classify_and_extract(sub_name, title, body, author, flair)
            if not extracted:
                log.warning(f"  Empty Claude response for: {title[:60]}")
                continue

            row = {
                "batch_id": batch_id,
                "reddit_post_id": post["id"],
                "source": sub_name,
                "post_url": f"https://reddit.com{post['permalink']}",
                "title": title,
                "body": body[:5000] if body else None,
                "author": author,
                "post_score": post["score"],
                "num_comments": post["num_comments"],
                "upvote_ratio": post["upvote_ratio"],
                "flair": flair,
                "external_link": post["external_link"],
                "cross_posted_subs": post["cross_post_subs"],
                "posted_at": posted_at.isoformat(),
                "classification": extracted.get("classification"),
                "classification_confidence": extracted.get("classification_confidence"),
                "classification_reason": extracted.get("classification_reason"),
                "quality_score": extracted.get("quality_score"),
                "intent": extracted.get("intent"),
                "budget_min": extracted.get("budget_min"),
                "budget_max": extracted.get("budget_max"),
                "budget_text": extracted.get("budget_text"),
                "locality": extracted.get("locality"),
                "property_type": extracted.get("property_type"),
                "bhk": extracted.get("bhk"),
                "is_hyderabad_re": True,
                "status": "new",
            }

            if insert_lead(row):
                kept += 1
                totals["total"] += 1
                classif = extracted.get("classification", "unclear")
                if classif in totals:
                    totals[classif] += 1

                quality = extracted.get("quality_score", "?")
                log.info(f"  ✓ Saved [{classif} q{quality}]: {title[:60]}")

                if classif in ENRICH_CLASSIFICATIONS and author != "[deleted]":
                    new_authors_to_enrich.add((author, classif))

        source_counts[sub_name] = kept
        log.info(f"r/{sub_name}: {kept} new leads kept")

    log.info(f"Enriching {len(new_authors_to_enrich)} unique authors...")
    for author, _classif in new_authors_to_enrich:
        try:
            profile = enrich_user(author)
            if profile:
                upsert_user_profile(profile)
        except Exception as e:
            log.error(f"  Enrich failed for u/{author}: {e}")

    update_batch(batch_id, totals, source_counts, total_tokens)
    log.info(
        f"=== Run done: {totals['total']} new leads "
        f"({totals['end_user']} end-users, {totals['agent']} agents) ==="
    )

    send_telegram_summary(source_counts, totals)


if __name__ == "__main__":
    main()
