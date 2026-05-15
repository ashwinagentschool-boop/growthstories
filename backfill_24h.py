"""
============================================================
  backfill_24h.py — One-time backfill of last 24 hours
============================================================
  Pulls last 24h from all 5 subs, classifies, enriches end_user/unclear
  authors, writes everything to Supabase.

  Run ONCE after initial setup:
      python backfill_24h.py

  Safe to re-run — duplicates are skipped via reddit_post_id UNIQUE constraint
  and user_profiles use a 7-day cache.

  Difference from fetch_leads.py:
    - Lookback window is 24h instead of 70 min
    - Uses pagination ('after' cursor) to pull >25 posts per sub
============================================================
"""

import time
from datetime import datetime, timezone, timedelta

from lib import (
    log, SUBREDDITS, ENRICH_CLASSIFICATIONS, DELAY_BETWEEN_REQUESTS,
    reddit_get, classify_and_extract, is_hyderabad_re,
    post_already_exists, insert_lead, enrich_user, upsert_user_profile,
    create_batch, update_batch,
)

LOOKBACK_HOURS = 24
PAGE_LIMIT = 100   # Reddit JSON max per page
MAX_PAGES = 5      # safety cap (500 posts/sub max)


def fetch_subreddit_paginated(sub_name: str, cutoff_ts: float) -> list[dict]:
    """Fetch posts back to cutoff, paginating with 'after' cursor."""
    posts = []
    after = None

    for page in range(MAX_PAGES):
        url = f"https://www.reddit.com/r/{sub_name}/new.json?limit={PAGE_LIMIT}"
        if after:
            url += f"&after={after}"
        data = reddit_get(url)
        if not data or data.get("_status"):
            break

        children = data.get("data", {}).get("children", [])
        if not children:
            break

        oldest_ts = None
        for child in children:
            d = child.get("data", {})
            cross_post_subs = []
            if d.get("crosspost_parent_list"):
                for parent in d["crosspost_parent_list"]:
                    cps = parent.get("subreddit")
                    if cps and cps.lower() != sub_name.lower():
                        cross_post_subs.append(cps)

            external_link = None
            if not d.get("is_self") and d.get("url"):
                external_link = d["url"]

            posts.append({
                "id":             d.get("id"),
                "title":          d.get("title", ""),
                "body":           d.get("selftext", "") or "",
                "author":         d.get("author", "[deleted]") or "[deleted]",
                "score":          d.get("score", 0),
                "num_comments":   d.get("num_comments", 0),
                "permalink":      d.get("permalink", ""),
                "created_utc":    d.get("created_utc", 0),
                "upvote_ratio":   d.get("upvote_ratio"),
                "flair":          d.get("link_flair_text"),
                "external_link":  external_link,
                "cross_post_subs": ",".join(cross_post_subs) if cross_post_subs else None,
            })
            oldest_ts = d.get("created_utc", 0)

        # Stop once oldest post in page is older than cutoff
        if oldest_ts and oldest_ts < cutoff_ts:
            break

        after = data.get("data", {}).get("after")
        if not after:
            break
        time.sleep(DELAY_BETWEEN_REQUESTS)

    # Filter to those within cutoff window
    posts = [p for p in posts if p["created_utc"] >= cutoff_ts]
    return posts


def main():
    run_at = datetime.now(timezone.utc)
    cutoff = run_at - timedelta(hours=LOOKBACK_HOURS)
    cutoff_ts = cutoff.timestamp()

    log.info(f"=== Backfill start: lookback {LOOKBACK_HOURS}h ===")
    log.info(f"Cutoff: {cutoff.isoformat()}")

    batch_id = create_batch()
    log.info(f"Created batch_id={batch_id} (backfill)")

    source_counts = {}
    totals = {"total": 0, "end_user": 0, "agent": 0, "unclear": 0}
    new_authors_to_enrich: set[str] = set()

    for i, sub_cfg in enumerate(SUBREDDITS):
        sub_name = sub_cfg["name"]
        needs_hyd_filter = sub_cfg["filter_hyd_re"]
        kept = 0

        if i > 0:
            time.sleep(DELAY_BETWEEN_REQUESTS)

        posts = fetch_subreddit_paginated(sub_name, cutoff_ts)
        log.info(f"r/{sub_name}: fetched {len(posts)} posts (24h window)")

        for post in posts:
            if post_already_exists(post["id"]):
                continue

            title, body, author, flair = post["title"], post["body"], post["author"], post["flair"]
            posted_at = datetime.fromtimestamp(post["created_utc"], tz=timezone.utc)

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
                    new_authors_to_enrich.add(author)

        source_counts[sub_name] = kept
        log.info(f"r/{sub_name}: {kept} new leads kept")

    log.info(f"Enriching {len(new_authors_to_enrich)} unique authors (this takes a while)...")
    for j, author in enumerate(sorted(new_authors_to_enrich), start=1):
        log.info(f"  [{j}/{len(new_authors_to_enrich)}] u/{author}")
        try:
            profile = enrich_user(author)
            if profile:
                upsert_user_profile(profile)
        except Exception as e:
            log.error(f"  Enrich failed for u/{author}: {e}")

    update_batch(batch_id, totals, source_counts, 0)
    log.info(
        f"=== Backfill done: {totals['total']} new leads "
        f"({totals['end_user']} end-users, {totals['agent']} agents, "
        f"{totals['unclear']} unclear) ==="
    )


if __name__ == "__main__":
    main()
