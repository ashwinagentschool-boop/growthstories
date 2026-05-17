"""
============================================================
  Growthstories — Twitter Helpers (lib_twitter.py)
============================================================
  Shares clients (supabase, anthropic, log) and core helpers
  with lib.py. Adds:
    - GetXAPI client (fetch a handle's recent tweets)
    - Tweet classifier prompt + caller
    - Skip-Claude rules for short/retweet tweets
    - Telegram alert for high-signal tweets
    - Tracked-handles helpers (active, cache, recency)
============================================================
"""

import os
import time
import re
from datetime import datetime, timezone, timedelta

import requests

from lib import (
    log, supabase, call_claude_json, CLAUDE_MODEL,
)

# ─── CONFIG ──────────────────────────────────────────────
GETXAPI_KEY = os.environ.get("GETXAPI_KEY", "")
GETXAPI_BASE = "https://api.getxapi.com"

# How many tweets to request per call (GetXAPI returns ~20 per page)
TWEETS_PER_CALL = 20

# Skip Claude classification when the tweet is too thin to be worth analyzing
MIN_TEXT_LENGTH_FOR_CLAUDE = 50

# Don't bother re-fetching a handle that hasn't tweeted in this many days
SILENT_HANDLE_DAYS = 7

# Polite delay between GetXAPI calls (they don't enforce strict limits but be nice)
DELAY_BETWEEN_TWEET_CALLS = 1.0

# Which classifications count as "high signal" → Telegram alert
HIGH_SIGNAL_CLASSIFICATIONS = {"project_launch"}


# ─── GetXAPI CLIENT ─────────────────────────────────────

def getxapi_get(path: str, params: dict | None = None, timeout: int = 20) -> dict | None:
    """GET a GetXAPI endpoint with auth + simple error handling."""
    if not GETXAPI_KEY:
        log.error("GETXAPI_KEY missing in .env")
        return None

    url = f"{GETXAPI_BASE}{path}"
    headers = {"Authorization": f"Bearer {GETXAPI_KEY}"}

    try:
        r = requests.get(url, headers=headers, params=params or {}, timeout=timeout)
    except Exception as e:
        log.error(f"GetXAPI request failed: {url} | {e}")
        return None

    if r.status_code == 401:
        log.error("GetXAPI 401 unauthorized — check GETXAPI_KEY")
        return None
    if r.status_code == 402:
        log.error("GetXAPI 402 — out of credits! Top up at getxapi.com")
        return None
    if r.status_code == 429:
        log.warning("GetXAPI rate-limited, sleeping 30s")
        time.sleep(30)
        return None
    if r.status_code != 200:
        log.error(f"GetXAPI HTTP {r.status_code}: {url} | {r.text[:200]}")
        return None

    try:
        return r.json()
    except Exception as e:
        log.error(f"GetXAPI JSON parse failed: {e}")
        return None


def fetch_user_tweets(handle: str) -> list[dict]:
    """Fetch the most recent tweets from a handle's timeline.

    Returns a list of normalized tweet dicts:
        id, text, created_at_iso, url, lang,
        retweet_count, like_count, reply_count, quote_count, view_count,
        is_retweet, is_quote, in_reply_to_handle,
        media_urls, external_urls,
        author_name, author_avatar_url
    """
    handle = handle.lstrip("@")
    data = getxapi_get(
        "/twitter/user/tweets",
        params={"userName": handle},
    )
    if not data:
        return []

    # GetXAPI returns tweets in a "tweets" key (response shape per docs)
    tweets_raw = data.get("tweets") or data.get("data", {}).get("tweets") or []
    if not tweets_raw and isinstance(data.get("data"), list):
        tweets_raw = data["data"]

    if not tweets_raw:
        # Either user has no tweets, doesn't exist, or response shape changed —
        # log the top-level keys so we can debug.
        keys = list(data.keys())[:10]
        log.warning(f"  @{handle}: no tweets in response. keys={keys}")
        # If GetXAPI returned an error message, show it
        if data.get("error"):
            log.warning(f"  @{handle}: GetXAPI error message: {data.get('error')}")

    out = []
    for t in tweets_raw[:TWEETS_PER_CALL]:
        normalized = _normalize_tweet(t, handle)
        if normalized:
            out.append(normalized)
    return out


def _normalize_tweet(t: dict, expected_handle: str) -> dict | None:
    """Convert GetXAPI's tweet shape into our internal shape.

    GetXAPI response shape (per docs.getxapi.com/docs/users/user-tweets):
      id, url, text, createdAt, lang
      retweetCount, replyCount, likeCount, quoteCount, viewCount, bookmarkCount
      isReply, inReplyToId, conversationId
      media: [{type, url, expanded_url, video_url}]
      entities.urls: [{url, expanded_url, display_url}]
      author: {userName, name, profilePicture, ...}
      quoted_tweet: object or null
    """
    try:
        tid = str(t.get("id") or "")
        if not tid:
            return None

        text = t.get("text") or ""

        # createdAt — Twitter classic format: "Mon Jan 12 13:44:55 +0000 2026"
        created_at_iso = _parse_twitter_date(t.get("createdAt"))

        # Author block
        author = t.get("author") or {}
        author_handle = (author.get("userName") or expected_handle).lstrip("@")
        author_name = author.get("name") or ""
        author_avatar = author.get("profilePicture") or ""

        url = t.get("url") or t.get("twitterUrl") or f"https://twitter.com/{author_handle}/status/{tid}"

        # Metrics — note GetXAPI uses camelCase consistently
        retweet_count = int(t.get("retweetCount") or 0)
        like_count    = int(t.get("likeCount") or 0)
        reply_count   = int(t.get("replyCount") or 0)
        quote_count   = int(t.get("quoteCount") or 0)
        view_count    = t.get("viewCount")
        view_count    = int(view_count) if view_count is not None else None

        # Type flags
        # GetXAPI doesn't flag retweets explicitly in this endpoint —
        # the user/tweets endpoint returns posts authored by user, but RT'd ones
        # still appear with "RT @..." prefix. We detect by text prefix.
        is_retweet = text.startswith("RT @")
        is_quote   = t.get("quoted_tweet") is not None
        is_reply   = bool(t.get("isReply"))

        # When it's a reply we don't have the screen_name directly, only inReplyToId.
        # Skip for now; could enrich later with a separate lookup.
        in_reply_to_handle = None

        # Media — top-level "media" array per docs
        media_urls = []
        for m in (t.get("media") or []):
            mu = m.get("video_url") or m.get("url")
            if mu:
                media_urls.append(mu)

        # External URLs from entities.urls (already expanded — no t.co resolution needed)
        external_urls = []
        entities = t.get("entities") or {}
        for u in (entities.get("urls") or []):
            ext = u.get("expanded_url") or u.get("url")
            if ext:
                external_urls.append(ext)

        return {
            "tweet_id": tid,
            "handle": author_handle,
            "author_name": author_name,
            "author_avatar_url": author_avatar,
            "text": text,
            "created_at_iso": created_at_iso,
            "url": url,
            "lang": t.get("lang") or None,
            "retweet_count": retweet_count,
            "like_count": like_count,
            "reply_count": reply_count,
            "quote_count": quote_count,
            "view_count": view_count,
            "is_retweet": is_retweet,
            "is_quote": is_quote,
            "in_reply_to_handle": in_reply_to_handle,
            "media_urls": media_urls,
            "external_urls": external_urls,
        }
    except Exception as e:
        log.error(f"Failed to normalize tweet: {e}")
        return None


def _parse_twitter_date(s: str | None) -> str | None:
    """Convert various Twitter date formats to ISO 8601 UTC."""
    if not s:
        return None
    # Try ISO 8601 first (some GetXAPI endpoints return this)
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except Exception:
        pass
    # Twitter's classic format: "Wed Oct 18 16:32:01 +0000 2023"
    try:
        dt = datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    log.warning(f"Could not parse twitter date: {s!r}")
    return None


# ─── SKIP RULES ─────────────────────────────────────────
def should_skip_tweet_entirely(tweet: dict) -> tuple[bool, str]:
    """Decide whether to skip a tweet completely (don't even save to DB).
    Currently: skip retweets — we want what the user said, not what they amplified.
    Quote-tweets are kept because the user added their own commentary.
    Returns (skip, reason)."""
    text = (tweet.get("text") or "").strip()

    # Explicit retweet flag (text starts with "RT @")
    if tweet.get("is_retweet") and not tweet.get("is_quote"):
        return True, "pure_retweet"
    # Belt-and-suspenders: catch retweets that slipped past the flag
    if text.startswith("RT @"):
        return True, "RT_prefix"

    return False, ""

def should_skip_claude(tweet: dict) -> tuple[bool, str]:
    """Decide whether to skip Claude classification on this tweet.
    Returns (skip, reason)."""
    text = (tweet.get("text") or "").strip()

    if tweet.get("is_retweet") and not tweet.get("is_quote"):
        return True, "pure_retweet"

    if len(text) < MIN_TEXT_LENGTH_FOR_CLAUDE:
        return True, "too_short"

    # Pure URL / image-only tweets
    if text.startswith("http") and len(text.split()) <= 2:
        return True, "url_only"

    return False, ""


# ─── CLAUDE TWEET CLASSIFIER ────────────────────────────



TWEET_CLASSIFY_PROMPT = """You are analyzing a tweet from a Hyderabad real estate handle. Classify it and extract structured fields.

Handle: @{handle}
Handle category: {category}
Tweet text:
\"\"\"{text}\"\"\"

Respond with ONLY a JSON object, no other text, no markdown fences:
{{
  "classification": "project_launch" | "listing" | "news" | "market_intel" | "opinion" | "spam" | "other",
  "classification_confidence": 0-100 integer,
  "classification_reason": "one short sentence",
  "locality": "extracted Hyderabad locality like 'Tellapur' or null",
  "price_text": "extracted price/budget mention or null",
  "property_type": "flat" | "villa" | "plot" | "commercial" | "independent_house" | null,
  "builder_name": "name of builder/developer mentioned, or null",
  "is_high_signal": true or false
}}

Classification guide:
- project_launch: a builder/developer announcing or promoting a new/upcoming project (RERA approval, launch event, pre-launch, phase 2, etc.)
- listing: a specific property for sale/rent with details
- news: market news, regulatory news, infrastructure updates affecting RE
- market_intel: market trends, price movements, expert commentary, data
- opinion: personal take, advice, generic commentary without specific actionability
- spam: promotional fluff, generic ads, irrelevant
- other: anything else (events, off-topic, etc.)

is_high_signal = true ONLY for project_launch or specific listings worth alerting the team about.
For news, market_intel, opinions — is_high_signal = false.

Locality should be a specific Hyderabad area (Tellapur, Kondapur, Gachibowli, etc.) — not "Hyderabad" itself.
If a field can't be determined, use null."""


def classify_tweet(text: str, handle: str, category: str) -> tuple[dict, int]:
    """Run Claude classification + extraction on a tweet."""
    prompt = TWEET_CLASSIFY_PROMPT.format(
        handle=handle,
        category=category or "unknown",
        text=text[:1500],
    )
    return call_claude_json(prompt, max_tokens=500)


# ─── TRACKED HANDLES ────────────────────────────────────

def get_active_handles() -> list[dict]:
    """All tracked handles where active=true, with their cache fields."""
    res = (
        supabase.table("tracked_handles")
        .select("*")
        .eq("active", True)
        .execute()
    )
    return res.data or []


def update_handle_cache(handle: str, last_tweeted_at: str | None, last_seen_tweet_id: str | None):
    """Update cache fields after a successful fetch."""
    patch = {"last_fetched_at": datetime.now(timezone.utc).isoformat()}
    if last_tweeted_at:
        patch["last_tweeted_at"] = last_tweeted_at
    if last_seen_tweet_id:
        patch["last_seen_tweet_id"] = last_seen_tweet_id
    supabase.table("tracked_handles").update(patch).eq("handle", handle).execute()


def handle_is_silent(h: dict) -> bool:
    """True if handle hasn't tweeted in SILENT_HANDLE_DAYS days (skip to save credits)."""
    last = h.get("last_tweeted_at")
    if not last:
        return False  # never fetched — try it
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last_dt) > timedelta(days=SILENT_HANDLE_DAYS)
    except Exception:
        return False


# ─── TWEET HELPERS ──────────────────────────────────────

def tweet_already_exists(tweet_id: str) -> bool:
    res = supabase.table("tweets").select("id").eq("tweet_id", tweet_id).limit(1).execute()
    return len(res.data) > 0


def insert_tweet(row: dict) -> bool:
    """Insert a tweet, gracefully handling duplicates."""
    try:
        supabase.table("tweets").insert(row).execute()
        return True
    except Exception as e:
        if "duplicate key" in str(e).lower():
            return False
        log.error(f"Insert tweet failed: {e}")
        return False


# ─── TELEGRAM HIGH-SIGNAL ALERT ─────────────────────────

def send_high_signal_alert(tweet: dict, classification: str, reason: str):
    """One Telegram message per high-signal tweet."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        return

    dashboard_url = os.environ.get("DASHBOARD_URL", "")
    emoji = "🚀" if classification == "project_launch" else "🏷️"
    handle = tweet.get("handle", "?")
    text_preview = (tweet.get("text") or "")[:240]
    if len(tweet.get("text") or "") > 240:
        text_preview += "…"

    msg = (
        f"{emoji} *{classification.replace('_', ' ').title()}* — @{handle}\n\n"
        f"{text_preview}\n\n"
        f"_{reason}_\n\n"
        f"[↗ Tweet]({tweet['url']})"
    )
    if dashboard_url:
        msg += f" · [Dashboard]({dashboard_url})"

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if r.status_code != 200:
            log.error(f"Telegram high-signal alert failed: {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"Telegram alert error: {e}")