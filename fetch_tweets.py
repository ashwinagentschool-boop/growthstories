"""
============================================================
  fetch_tweets.py — Twitter scraper for tracked handles
============================================================
  Runs every N hours via cron.

  Flow:
    1. Pull active handles from `tracked_handles`
    2. Skip silent handles (last_tweeted_at > 7 days ago)
    3. For each handle: fetch last 20 tweets via GetXAPI
    4. Skip tweets we've already saved (dedup on tweet_id)
    5. Skip Claude for retweets / short tweets
    6. Classify remaining via Claude
    7. Insert into `tweets`
    8. Fire Telegram alert for any high-signal tweets
    9. Update handle cache (last_fetched_at, last_tweeted_at)

  Run manually:
      python fetch_tweets.py
  Or via cron (every 4 hours).
============================================================
"""

import time
from datetime import datetime, timezone

from lib import log, create_batch, update_batch
from lib_twitter import (
    DELAY_BETWEEN_TWEET_CALLS, HIGH_SIGNAL_CLASSIFICATIONS,
    fetch_user_tweets, classify_tweet, should_skip_claude, should_skip_tweet_entirely,
    get_active_handles, update_handle_cache, handle_is_silent,
    tweet_already_exists, insert_tweet, send_high_signal_alert,
)


def main():
    run_at = datetime.now(timezone.utc)
    log.info(f"=== Tweet run start: {run_at.isoformat()} ===")

    batch_id = create_batch()
    log.info(f"Created batch_id={batch_id}")

    handles = get_active_handles()
    log.info(f"Tracking {len(handles)} active handles")

    source_counts = {}
    totals = {"total": 0, "classified": 0, "skipped": 0, "high_signal": 0}
    total_tokens = 0

    for i, h in enumerate(handles):
        handle = h["handle"]
        category = h.get("category") or "other"

        if handle_is_silent(h):
            log.info(f"  Skipping silent handle @{handle} (last tweet > 7d ago)")
            source_counts[handle] = 0
            continue

        if i > 0:
            time.sleep(DELAY_BETWEEN_TWEET_CALLS)

        log.info(f"@{handle} ({category}): fetching...")
        tweets = fetch_user_tweets(handle)
        log.info(f"@{handle}: got {len(tweets)} tweets from GetXAPI")

        kept = 0
        newest_seen_id = h.get("last_seen_tweet_id")
        newest_seen_at = h.get("last_tweeted_at")

        # GetXAPI returns newest-first; iterate accordingly
        for tw in tweets:
            tid = tw["tweet_id"]
            created_iso = tw.get("created_at_iso")

            # Track the absolute newest tweet for cache (whether new or seen)
            if created_iso and (not newest_seen_at or created_iso > newest_seen_at):
                newest_seen_at = created_iso
                newest_seen_id = tid

            if tweet_already_exists(tid):
                continue

            # Skip retweets entirely — we want what the user said, not amplified
            skip_entire, skip_reason = should_skip_tweet_entirely(tw)
            if skip_entire:
                log.info(f"  - Skipping retweet ({skip_reason}): {(tw.get('text') or '')[:60]}")
                continue

            # Skip Claude on retweets / short tweets
            skip, reason = should_skip_claude(tw)
            if skip:
                row = _build_row(tw, category, batch_id, classification=None,
                                 confidence=None, reason_text=None,
                                 locality=None, price=None, prop_type=None,
                                 builder=None, high_signal=False,
                                 claude_skipped=True, skip_reason=reason)
                if insert_tweet(row):
                    kept += 1
                    totals["total"] += 1
                    totals["skipped"] += 1
                    log.info(f"  [skipped:{reason}] {tw['text'][:60]}")
                continue

            # Claude classify
            extracted, _ = classify_tweet(tw["text"], handle, category)
            if not extracted:
                log.warning(f"  Empty Claude response for tweet {tid}")
                continue

            classif = extracted.get("classification") or "other"
            is_high = bool(extracted.get("is_high_signal")) and classif in HIGH_SIGNAL_CLASSIFICATIONS

            row = _build_row(
                tw, category, batch_id,
                classification=classif,
                confidence=extracted.get("classification_confidence"),
                reason_text=extracted.get("classification_reason"),
                locality=extracted.get("locality"),
                price=extracted.get("price_text"),
                prop_type=extracted.get("property_type"),
                builder=extracted.get("builder_name"),
                high_signal=is_high,
                claude_skipped=False,
                skip_reason=None,
            )

            if insert_tweet(row):
                kept += 1
                totals["total"] += 1
                totals["classified"] += 1
                if is_high:
                    totals["high_signal"] += 1
                    send_high_signal_alert(tw, classif, extracted.get("classification_reason") or "")
                log.info(f"  [{classif}{' HIGH' if is_high else ''}] {tw['text'][:60]}")

        source_counts[f"@{handle}"] = kept

        # Update cache for this handle
        update_handle_cache(handle, newest_seen_at, newest_seen_id)
        log.info(f"@{handle}: {kept} new tweets kept")

    update_batch(batch_id, {"total": totals["total"]}, source_counts, total_tokens)
    log.info(
        f"=== Tweet run done: {totals['total']} new "
        f"({totals['classified']} classified, "
        f"{totals['skipped']} skipped, "
        f"{totals['high_signal']} high-signal alerts) ==="
    )


def _build_row(tw, category, batch_id,
               classification, confidence, reason_text,
               locality, price, prop_type, builder,
               high_signal, claude_skipped, skip_reason):
    return {
        "tweet_id": tw["tweet_id"],
        "handle": tw["handle"],
        "author_name": tw.get("author_name"),
        "author_avatar_url": tw.get("author_avatar_url"),
        "text": tw["text"],
        "created_at": tw["created_at_iso"],
        "lang": tw.get("lang"),
        "url": tw["url"],
        "retweet_count": tw["retweet_count"],
        "like_count": tw["like_count"],
        "reply_count": tw["reply_count"],
        "quote_count": tw["quote_count"],
        "view_count": tw.get("view_count"),
        "is_retweet": tw["is_retweet"],
        "is_quote": tw["is_quote"],
        "in_reply_to_handle": tw.get("in_reply_to_handle"),
        "media_urls": tw.get("media_urls"),
        "external_urls": tw.get("external_urls"),
        "classification": classification,
        "classification_confidence": confidence,
        "classification_reason": reason_text,
        "locality": locality,
        "price_text": price,
        "property_type": prop_type,
        "builder_name": builder,
        "is_high_signal": high_signal,
        "claude_skipped": claude_skipped,
        "claude_skip_reason": skip_reason,
        "batch_id": batch_id,
    }


if __name__ == "__main__":
    main()
