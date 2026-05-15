"""
============================================================
  enrich_users.py — Manually enrich one or many authors
============================================================
  Usage:
      python enrich_users.py u/Only-Sea-2741
      python enrich_users.py Only-Sea-2741 ratman_5991 Own-Primary-2081
      python enrich_users.py --stale     (re-enriches profiles older than cache window)
      python enrich_users.py --missing   (enriches authors in leads but not in user_profiles)

  Respects the 7-day cache by default (use --force to override).
============================================================
"""

import sys
from datetime import datetime, timezone, timedelta

from lib import (
    log, supabase, enrich_user, upsert_user_profile,
    USER_PROFILE_CACHE_DAYS,
)


def get_stale_authors() -> list[str]:
    """Profiles older than cache window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=USER_PROFILE_CACHE_DAYS)
    res = (
        supabase.table("user_profiles")
        .select("author")
        .lt("enriched_at", cutoff.isoformat())
        .execute()
    )
    return [r["author"] for r in res.data]


def get_missing_authors() -> list[str]:
    """Authors in `leads` but not in `user_profiles` yet."""
    leads_res = supabase.table("leads").select("author").execute()
    lead_authors = {r["author"] for r in leads_res.data if r["author"] != "[deleted]"}
    profile_res = supabase.table("user_profiles").select("author").execute()
    profile_authors = {r["author"] for r in profile_res.data}
    return sorted(lead_authors - profile_authors)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    if args[0] == "--stale":
        authors = get_stale_authors()
        log.info(f"Stale profiles: {len(authors)}")
    elif args[0] == "--missing":
        authors = get_missing_authors()
        log.info(f"Missing profiles: {len(authors)}")
    else:
        # Clean up u/ prefix if user pasted that style
        authors = [a.removeprefix("u/").removeprefix("/u/") for a in args]

    for i, author in enumerate(authors, start=1):
        log.info(f"[{i}/{len(authors)}] u/{author}")
        try:
            profile = enrich_user(author)
            if profile:
                upsert_user_profile(profile)
                log.info(f"  ✓ {profile.get('classification', '?')} "
                         f"({profile.get('classification_confidence', '?')}%)")
        except Exception as e:
            log.error(f"  Failed: {e}")


if __name__ == "__main__":
    main()
