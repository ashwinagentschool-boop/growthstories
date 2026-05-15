"""
============================================================
  Growthstories Leads — Shared Library
============================================================
  Used by fetch_leads.py, enrich_users.py, backfill_24h.py.

  Provides:
    - Reddit public JSON fetch (subreddit + user profile)
    - Claude prompts for classification, extraction, user enrichment
    - Supabase client
    - Helpers for upsert / cache checking
============================================================
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter

import requests
from anthropic import Anthropic
from supabase import create_client, Client
from dotenv import load_dotenv

# ─── ENV + LOGGING ────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

# Force UTF-8 stdout on Windows (charmap codec can't print ✓ otherwise)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(SCRIPT_DIR / "scraper.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ─── CONFIG ──────────────────────────────────────────────
SUBREDDITS = [
    {"name": "hyderabadrealestate",  "filter_hyd_re": False},
    {"name": "Hyderabad_highrises",  "filter_hyd_re": False},
    {"name": "WestHydrealestate",    "filter_hyd_re": False},
    #{"name": "hyderabad",            "filter_hyd_re": True},
    #{"name": "indianrealestate",     "filter_hyd_re": True},
]

# Real estate subs we consider when computing re_activity_pct
RE_SUBS_FOR_ACTIVITY = {
    "hyderabadrealestate", "Hyderabad_highrises", "WestHydrealestate",
    "indianrealestate", "RealEstateIndia", "IndianRealEstate",
    "BangaloreRealEstates", "MumbaiRealEstate", "DelhiNCRRealEstate",
    "ChennaiRealEstate", "PuneRealEstate", "realestate",
}

CLAUDE_MODEL = "claude-sonnet-4-5"

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "growthstories-leads/1.0 (Hyderabad real estate aggregator)",
)

# Cache user profiles for 7 days (configurable)
USER_PROFILE_CACHE_DAYS = int(os.environ.get("USER_PROFILE_CACHE_DAYS", "7"))

# Only enrich these classifications (agents auto-confirmed by post analysis)
ENRICH_CLASSIFICATIONS = {"end_user", "unclear"}

# Promotional phrases we count in user activity
PROMO_PHRASES = [
    "dm me", "dm for", "whatsapp me", "call me", "ping me",
    "best deal", "best price", "exclusive offer", "limited time",
    "site visit", "book now", "channel partner", "rera",
    "loan assistance", "0% emi", "spot booking",
]

# Reddit JSON rate limit politeness
DELAY_BETWEEN_REQUESTS = 1.5  # seconds between Reddit JSON calls
DELAY_AFTER_429 = 30          # seconds after a rate-limit response


# ─── CLIENTS ─────────────────────────────────────────────
anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_ROLE_KEY"],
)


# ─── REDDIT JSON ─────────────────────────────────────────

def reddit_get(url: str, timeout: int = 15) -> dict | None:
    """GET a Reddit public JSON URL with polite headers + rate-limit handling."""
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        log.error(f"Reddit GET failed: {url} | {e}")
        return None

    if r.status_code == 429:
        log.warning(f"Rate-limited (429), sleeping {DELAY_AFTER_429}s | {url}")
        time.sleep(DELAY_AFTER_429)
        return None
    if r.status_code == 404:
        log.info(f"Not found (404): {url}")
        return {"_status": "not_found"}
    if r.status_code == 403:
        log.info(f"Forbidden (403): {url}")
        return {"_status": "suspended"}
    if r.status_code != 200:
        log.error(f"Reddit HTTP {r.status_code}: {url}")
        return None

    try:
        return r.json()
    except Exception as e:
        log.error(f"Reddit JSON parse failed: {url} | {e}")
        return None


def fetch_subreddit_posts(sub_name: str, limit: int = 25) -> list[dict]:
    """Fetch latest posts via public JSON. Returns normalized post dicts."""
    url = f"https://www.reddit.com/r/{sub_name}/new.json?limit={limit}"
    data = reddit_get(url)
    if not data or data.get("_status"):
        return []

    posts = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        # Detect cross-posts: crosspost_parent_list has originals
        cross_post_subs = []
        if d.get("crosspost_parent_list"):
            for parent in d["crosspost_parent_list"]:
                cps = parent.get("subreddit")
                if cps and cps.lower() != sub_name.lower():
                    cross_post_subs.append(cps)

        # external link = the post's link target if it's not a self-post
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
            "upvote_ratio":   d.get("upvote_ratio"),     # 0.0 to 1.0
            "flair":          d.get("link_flair_text"),
            "external_link":  external_link,
            "cross_post_subs": ",".join(cross_post_subs) if cross_post_subs else None,
        })
    return posts


def fetch_user_about(author: str) -> dict | None:
    """Fetch /user/<name>/about.json. Returns dict or {'_status': 'not_found'/'suspended'}."""
    if not author or author == "[deleted]":
        return {"_status": "not_found"}
    url = f"https://www.reddit.com/user/{author}/about.json"
    return reddit_get(url)


def fetch_user_submitted(author: str, limit: int = 25) -> list[dict]:
    """Fetch /user/<name>/submitted.json — recent posts."""
    if not author or author == "[deleted]":
        return []
    url = f"https://www.reddit.com/user/{author}/submitted.json?limit={limit}"
    data = reddit_get(url)
    if not data or data.get("_status"):
        return []
    return [c.get("data", {}) for c in data.get("data", {}).get("children", [])]


def fetch_user_comments(author: str, limit: int = 50) -> list[dict]:
    """Fetch /user/<name>/comments.json — recent comments."""
    if not author or author == "[deleted]":
        return []
    url = f"https://www.reddit.com/user/{author}/comments.json?limit={limit}"
    data = reddit_get(url)
    if not data or data.get("_status"):
        return []
    return [c.get("data", {}) for c in data.get("data", {}).get("children", [])]


# ─── CLAUDE ──────────────────────────────────────────────

HYDERABAD_RE_FILTER_PROMPT = """You are filtering Reddit posts. Decide if this post is about Hyderabad real estate (buying, renting, investing, property news, locality discussions, builders, projects, prices).

Title: {title}
Body: {body}

Respond with ONLY a JSON object, no other text:
{{"is_hyderabad_re": true or false, "reason": "one short sentence"}}"""


CLASSIFY_AND_EXTRACT_PROMPT = """You are analyzing a Reddit post about Hyderabad real estate. Classify the poster, extract structured fields, and score lead quality.

Subreddit: r/{source}
Flair: {flair}
Title: {title}
Body: {body}
Author: u/{author}

Respond with ONLY a JSON object, no other text, no markdown fences:
{{
  "classification": "end_user" | "agent" | "unclear",
  "classification_confidence": 0-100 integer,
  "classification_reason": "one short sentence",
  "quality_score": 1-10 integer,
  "intent": "buying" | "renting" | "selling" | "investing" | "comparing" | "asking_advice" | "info",
  "budget_min": integer in INR or null,
  "budget_max": integer in INR or null,
  "budget_text": "raw budget mention like '25-35L' or null",
  "locality": "extracted Hyderabad locality like 'Kondapur' or null",
  "property_type": "flat" | "villa" | "plot" | "commercial" | "independent_house" | "others",
  "bhk": "1BHK" | "2BHK" | "3BHK" | "4BHK" | null
}}

Guidelines:
- end_user = buyer, renter, investor, or someone genuinely asking
- agent = real estate agent, broker, builder promoting projects, channel partner
- unclear = could be either
- quality_score (1-10): how actionable is this as a lead?
    10 = clear buyer, specific budget + locality + timeline
    7-9 = strong intent with most details
    4-6 = curious/researching, partial info
    1-3 = vague, opinion-only, or off-topic
- Convert "25L" to 2500000, "1.2Cr" to 12000000
- Locality should be a specific area, not "Hyderabad" itself
- property_type "others" = news, opinion, agent spam, general discussion
- If a field can't be determined, use null (except property_type which is "others")"""


USER_ENRICHMENT_PROMPT = """You are analyzing a Reddit user's profile to classify them as an end_user (genuine buyer/renter/investor) or an agent (broker, builder, channel partner, promoter).

Username: u/{author}
Account age: {age_days} days
Total karma: {total_karma} (link: {link_karma}, comment: {comment_karma})

Activity in last 90 days:
- Posts: {posts_90d}
- Comments: {comments_90d}
- Distinct subreddits: {subs_diversity}
- Real estate activity %: {re_activity_pct}%
- Top subreddits: {top_subs_str}
- Promotional phrase hits: {promo_hits}

Recent post titles:
{post_titles}

Recent comment snippets:
{comment_snippets}

Respond with ONLY a JSON object, no other text, no markdown fences:
{{
  "classification": "end_user" | "agent" | "unclear",
  "classification_confidence": 0-100 integer,
  "reasoning": "1-2 sentence summary of why",
  "red_flags": "pipe-separated list of concerning signals (or empty string)",
  "supporting_signals": "pipe-separated list of supporting signals"
}}

Guidelines:
- Agents tend to: promote specific projects, post identical content repeatedly, have 100% RE activity, use sales language ("DM me", "best deal"), low subreddit diversity
- End users tend to: ask questions, share personal context (family, job, timeline), critical of builders, diverse subreddits (life topics, work, hobbies), conversational tone
- New accounts (<30 days) with only RE posts are suspicious but not conclusive
- Very low karma but personal/conversational tone usually = end_user"""


def call_claude_json(prompt: str, max_tokens: int = 800) -> tuple[dict, int]:
    """Call Claude and parse JSON. Returns (parsed_dict, tokens_used)."""
    text = ""
    try:
        resp = anthropic.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        tokens = resp.usage.input_tokens + resp.usage.output_tokens
        return json.loads(text), tokens
    except json.JSONDecodeError:
        log.error(f"Claude returned non-JSON (first 200 chars): {text[:200]}")
        return {}, 0
    except Exception as e:
        log.error(f"Claude call failed: {e}")
        return {}, 0


def is_hyderabad_re(title: str, body: str) -> tuple[bool, int]:
    prompt = HYDERABAD_RE_FILTER_PROMPT.format(
        title=title[:500],
        body=(body or "")[:1500],
    )
    result, tokens = call_claude_json(prompt, max_tokens=150)
    return bool(result.get("is_hyderabad_re", False)), tokens


def classify_and_extract(source: str, title: str, body: str, author: str, flair: str | None) -> tuple[dict, int]:
    prompt = CLASSIFY_AND_EXTRACT_PROMPT.format(
        source=source,
        title=title[:500],
        body=(body or "")[:2000],
        author=author,
        flair=flair or "(none)",
    )
    return call_claude_json(prompt, max_tokens=600)


# ─── USER ENRICHMENT ─────────────────────────────────────

def user_profile_is_fresh(author: str) -> bool:
    """True if we have a cached profile from within the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=USER_PROFILE_CACHE_DAYS)
    res = (
        supabase.table("user_profiles")
        .select("enriched_at")
        .eq("author", author)
        .gte("enriched_at", cutoff.isoformat())
        .limit(1)
        .execute()
    )
    return len(res.data) > 0


def compute_user_stats(submitted: list[dict], comments: list[dict]) -> dict:
    """Compute activity stats from raw Reddit submitted/comments lists."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    cutoff_ts = cutoff.timestamp()

    posts_90d = [p for p in submitted if p.get("created_utc", 0) >= cutoff_ts]
    comments_90d = [c for c in comments if c.get("created_utc", 0) >= cutoff_ts]

    all_subs = (
        [p.get("subreddit", "") for p in posts_90d if p.get("subreddit")] +
        [c.get("subreddit", "") for c in comments_90d if c.get("subreddit")]
    )
    sub_counts = Counter(all_subs)

    total_activity = len(all_subs)
    re_activity = sum(1 for s in all_subs if s in RE_SUBS_FOR_ACTIVITY)
    re_pct = (re_activity / total_activity * 100) if total_activity else 0

    # Promo phrase detection
    all_text = " ".join([
        (p.get("title") or "") + " " + (p.get("selftext") or "") for p in posts_90d
    ] + [
        (c.get("body") or "") for c in comments_90d
    ]).lower()
    promo_hits = sum(all_text.count(phrase) for phrase in PROMO_PHRASES)

    return {
        "posts_90d": len(posts_90d),
        "comments_90d": len(comments_90d),
        "subs_diversity": len(sub_counts),
        "re_activity_pct": round(re_pct, 2),
        "promo_hits": promo_hits,
        "top_subreddits": [
            {"sub": s, "count": c} for s, c in sub_counts.most_common(8)
        ],
        "latest_post_titles": [p.get("title", "")[:200] for p in posts_90d[:5]],
        "latest_comment_snippets": [
            (c.get("body") or "")[:200] for c in comments_90d[:5]
        ],
    }


def enrich_user(author: str) -> dict | None:
    """
    Full enrichment pipeline for one author.
    Returns dict ready for Supabase upsert, or None on hard failure.
    Respects 7-day cache.
    """
    if not author or author == "[deleted]":
        return None

    if user_profile_is_fresh(author):
        log.info(f"  [cache hit] u/{author}")
        return None

    log.info(f"  [enriching] u/{author}")

    # 1. About
    about = fetch_user_about(author)
    time.sleep(DELAY_BETWEEN_REQUESTS)

    if not about:
        return None
    if about.get("_status") in ("not_found", "suspended"):
        # Save a stub row so we don't keep trying
        return {
            "author": author,
            "profile_url": f"https://reddit.com/user/{author}",
            "enrich_status": about["_status"],
            "enriched_at": datetime.now(timezone.utc).isoformat(),
        }

    about_data = about.get("data", {})
    created = about_data.get("created_utc", 0)
    age_days = int((datetime.now(timezone.utc).timestamp() - created) / 86400) if created else 0

    # 2. Submitted + comments
    submitted = fetch_user_submitted(author)
    time.sleep(DELAY_BETWEEN_REQUESTS)
    comments = fetch_user_comments(author)
    time.sleep(DELAY_BETWEEN_REQUESTS)

    stats = compute_user_stats(submitted, comments)

    # 3. Claude analysis
    top_subs_str = ", ".join(
        f"{s['sub']}({s['count']})" for s in stats["top_subreddits"]
    ) or "(none)"

    prompt = USER_ENRICHMENT_PROMPT.format(
        author=author,
        age_days=age_days,
        total_karma=about_data.get("total_karma", 0),
        link_karma=about_data.get("link_karma", 0),
        comment_karma=about_data.get("comment_karma", 0),
        posts_90d=stats["posts_90d"],
        comments_90d=stats["comments_90d"],
        subs_diversity=stats["subs_diversity"],
        re_activity_pct=stats["re_activity_pct"],
        top_subs_str=top_subs_str,
        promo_hits=stats["promo_hits"],
        post_titles="\n".join(f"- {t}" for t in stats["latest_post_titles"]) or "(none)",
        comment_snippets="\n".join(f"- {c}" for c in stats["latest_comment_snippets"]) or "(none)",
    )
    claude_result, _tokens = call_claude_json(prompt, max_tokens=600)

    return {
        "author": author,
        "profile_url": f"https://reddit.com/user/{author}",
        "account_age_days": age_days,
        "total_karma": about_data.get("total_karma", 0),
        "link_karma": about_data.get("link_karma", 0),
        "comment_karma": about_data.get("comment_karma", 0),
        "posts_90d": stats["posts_90d"],
        "comments_90d": stats["comments_90d"],
        "subs_diversity": stats["subs_diversity"],
        "re_activity_pct": stats["re_activity_pct"],
        "promo_hits": stats["promo_hits"],
        "top_subreddits": stats["top_subreddits"],
        "latest_post_titles": stats["latest_post_titles"],
        "latest_comment_snippets": stats["latest_comment_snippets"],
        "classification": claude_result.get("classification"),
        "classification_confidence": claude_result.get("classification_confidence"),
        "reasoning": claude_result.get("reasoning"),
        "red_flags": claude_result.get("red_flags", ""),
        "supporting_signals": claude_result.get("supporting_signals", ""),
        "enriched_at": datetime.now(timezone.utc).isoformat(),
        "enrich_status": "ok",
    }


def upsert_user_profile(profile: dict):
    """Insert or update a user_profile row (author is UNIQUE)."""
    if not profile:
        return
    supabase.table("user_profiles").upsert(profile, on_conflict="author").execute()


# ─── LEAD HELPERS ────────────────────────────────────────

def post_already_exists(reddit_post_id: str) -> bool:
    res = (
        supabase.table("leads")
        .select("id")
        .eq("reddit_post_id", reddit_post_id)
        .limit(1)
        .execute()
    )
    return len(res.data) > 0


def insert_lead(row: dict) -> bool:
    """Insert a lead, gracefully handling duplicates. Returns True if inserted."""
    try:
        supabase.table("leads").insert(row).execute()
        return True
    except Exception as e:
        if "duplicate key" in str(e).lower():
            return False
        log.error(f"Insert failed: {e}")
        return False


# ─── TELEGRAM ─────────────────────────────────────────────

def send_telegram_summary(source_counts: dict, totals: dict):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        log.warning("Telegram credentials missing — skipping notification")
        return

    dashboard_url = os.environ.get("DASHBOARD_URL", "https://example.com")
    total = totals.get("total", 0)
    end_users = totals.get("end_user", 0)
    agents = totals.get("agent", 0)

    if total == 0:
        log.info("No new leads — skipping Telegram message")
        return

    source_lines = "\n".join(
        f"  • r/{s}: {c}" for s, c in source_counts.items() if c > 0
    )
    message = (
        f"🏘️ *{total} new leads* from this hour\n"
        f"  🟢 End-users: {end_users}\n"
        f"  🟡 Agents: {agents}\n\n"
        f"*By source:*\n{source_lines}\n\n"
        f"[Open Dashboard]({dashboard_url})"
    )
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if r.status_code != 200:
            log.error(f"Telegram send failed: {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"Telegram error: {e}")


# ─── BATCH HELPERS ───────────────────────────────────────

def create_batch() -> int:
    """Create an hourly_batches row, return the id."""
    resp = supabase.table("hourly_batches").insert({
        "run_at": datetime.now(timezone.utc).isoformat(),
        "total_posts": 0,
        "source_counts": {},
        "claude_tokens": 0,
    }).execute()
    return resp.data[0]["id"]


def update_batch(batch_id: int, totals: dict, source_counts: dict, tokens: int):
    supabase.table("hourly_batches").update({
        "total_posts": totals.get("total", 0),
        "source_counts": source_counts,
        "claude_tokens": tokens,
    }).eq("id", batch_id).execute()