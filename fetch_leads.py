"""
============================================================
  GROWTHSTORIES Leads Scraper — Pi → Supabase → Telegram
============================================================
  Runs hourly via cron on Raspberry Pi.

  Flow per run:
    1. Create an hourly_batch row in Supabase
    2. For each of 5 subreddits, fetch latest posts (last ~70 min)
    3. Skip posts already in `leads` (reddit_post_id is UNIQUE)
    4. For r/hyderabad + r/indianrealestate: ask Claude if Hyderabad-RE-relevant
    5. For all kept posts: ask Claude to classify + extract structured fields
    6. INSERT each into Supabase `leads`
    7. UPDATE batch row with totals
    8. Send one Telegram summary message with deep link to dashboard

  Requires `.env` file in the same directory with:
    REDDIT_CLIENT_ID=
    REDDIT_CLIENT_SECRET=
    REDDIT_USER_AGENT=growthstories-leads-bot/1.0 by u/yourusername
    ANTHROPIC_API_KEY=
    SUPABASE_URL=https://xxxxx.supabase.co
    SUPABASE_SERVICE_ROLE_KEY=eyJ...
    TELEGRAM_BOT_TOKEN=
    TELEGRAM_CHAT_ID=
    DASHBOARD_URL=https://your-app.vercel.app   (after Session 2)
============================================================
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import praw
import requests
from anthropic import Anthropic
from supabase import create_client, Client
from dotenv import load_dotenv

# ─── CONFIG ──────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(SCRIPT_DIR / "scraper.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# Subreddits + whether they need a "is this Hyderabad real estate?" pre-filter
SUBREDDITS = [
    {"name": "hyderabadrealestate",  "filter_hyd_re": False},
    {"name": "Hyderabad_highrises",  "filter_hyd_re": False},
    {"name": "WestHydrealestate",    "filter_hyd_re": False},
    {"name": "hyderabad",            "filter_hyd_re": True},
    {"name": "indianrealestate",     "filter_hyd_re": True},
]

# Fetch posts from the last LOOKBACK_MINUTES (cron runs hourly, 70 gives buffer)
LOOKBACK_MINUTES = 70

# Posts per subreddit to scan (we filter by time, but cap the scan)
POSTS_PER_SUB = 25

CLAUDE_MODEL = "claude-sonnet-4-5"


# ─── CLIENTS ─────────────────────────────────────────────
reddit = praw.Reddit(
    client_id=os.environ["REDDIT_CLIENT_ID"],
    client_secret=os.environ["REDDIT_CLIENT_SECRET"],
    user_agent=os.environ["REDDIT_USER_AGENT"],
)

anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_ROLE_KEY"],
)


# ─── CLAUDE PROMPTS ──────────────────────────────────────

HYDERABAD_RE_FILTER_PROMPT = """You are filtering Reddit posts. Decide if this post is about Hyderabad real estate (buying, renting, investing, property news, locality discussions, builders, projects, prices).

Title: {title}
Body: {body}

Respond with ONLY a JSON object, no other text:
{{"is_hyderabad_re": true or false, "reason": "one short sentence"}}"""


CLASSIFY_AND_EXTRACT_PROMPT = """You are analyzing a Reddit post about Hyderabad real estate. Classify the poster and extract structured fields.

Subreddit: r/{source}
Title: {title}
Body: {body}
Author: u/{author}

Respond with ONLY a JSON object, no other text, no markdown fences:
{{
  "classification": "end_user" | "agent" | "unclear",
  "classification_confidence": 0-100 integer,
  "classification_reason": "one short sentence",
  "intent": "buy" | "rent" | "invest" | "info" | "unclear",
  "budget_min": integer in INR or null,
  "budget_max": integer in INR or null,
  "budget_text": "raw budget mention like '25-35L' or null",
  "locality": "extracted Hyderabad locality like 'Kondapur' or null",
  "property_type": "apartment" | "villa" | "plot" | "commercial" | "independent_house" | null,
  "bhk": "1BHK" | "2BHK" | "3BHK" | "4BHK" | null
}}

Guidelines:
- end_user = buyer, renter, investor, or someone genuinely asking
- agent = real estate agent, broker, builder promoting projects, channel partner
- unclear = could be either
- Convert "25L" to 2500000, "1.2Cr" to 12000000
- Locality should be a specific area, not "Hyderabad" itself
- If a field can't be determined, use null
"""


# ─── HELPERS ─────────────────────────────────────────────

def call_claude_json(prompt: str, max_tokens: int = 600) -> tuple[dict, int]:
    """Call Claude and parse JSON response. Returns (parsed_dict, tokens_used)."""
    try:
        resp = anthropic.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        # Strip markdown fences if Claude adds them
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        tokens = resp.usage.input_tokens + resp.usage.output_tokens
        return json.loads(text), tokens
    except json.JSONDecodeError as e:
        log.error(f"Claude returned non-JSON: {text[:200]}... | {e}")
        return {}, 0
    except Exception as e:
        log.error(f"Claude call failed: {e}")
        return {}, 0


def is_hyderabad_re(title: str, body: str) -> tuple[bool, int]:
    """For r/hyderabad and r/indianrealestate, check if post is Hyderabad-RE-related."""
    prompt = HYDERABAD_RE_FILTER_PROMPT.format(
        title=title[:500],
        body=(body or "")[:1500],
    )
    result, tokens = call_claude_json(prompt, max_tokens=150)
    return bool(result.get("is_hyderabad_re", False)), tokens


def classify_and_extract(source: str, title: str, body: str, author: str) -> tuple[dict, int]:
    """Get classification + extracted fields for a post."""
    prompt = CLASSIFY_AND_EXTRACT_PROMPT.format(
        source=source,
        title=title[:500],
        body=(body or "")[:2000],
        author=author,
    )
    return call_claude_json(prompt, max_tokens=600)


def post_already_exists(reddit_post_id: str) -> bool:
    """Check if we've already saved this post (Supabase UNIQUE constraint also protects us)."""
    res = supabase.table("leads").select("id").eq("reddit_post_id", reddit_post_id).limit(1).execute()
    return len(res.data) > 0


def send_telegram_summary(batch_id: int, source_counts: dict, totals: dict):
    """Send one summary message to Telegram with a link to the dashboard."""
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
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


# ─── MAIN ────────────────────────────────────────────────

def main():
    run_at = datetime.now(timezone.utc)
    cutoff = run_at - timedelta(minutes=LOOKBACK_MINUTES)
    log.info(f"=== Run start: {run_at.isoformat()} ===")

    # Create batch row
    batch_resp = supabase.table("hourly_batches").insert({
        "run_at": run_at.isoformat(),
        "total_posts": 0,
        "source_counts": {},
        "claude_tokens": 0,
    }).execute()
    batch_id = batch_resp.data[0]["id"]
    log.info(f"Created batch_id={batch_id}")

    source_counts = {}
    totals = {"total": 0, "end_user": 0, "agent": 0, "unclear": 0}
    total_tokens = 0

    for sub_cfg in SUBREDDITS:
        sub_name = sub_cfg["name"]
        needs_hyd_filter = sub_cfg["filter_hyd_re"]
        kept = 0

        try:
            for post in reddit.subreddit(sub_name).new(limit=POSTS_PER_SUB):
                posted_at = datetime.fromtimestamp(post.created_utc, tz=timezone.utc)

                # Skip if too old (older than lookback window)
                if posted_at < cutoff:
                    continue

                # Skip if already saved
                if post_already_exists(post.id):
                    continue

                title = post.title or ""
                body = post.selftext or ""
                author = str(post.author) if post.author else "[deleted]"

                # Step 1: For r/hyderabad + r/indianrealestate, filter to RE only
                if needs_hyd_filter:
                    is_re, tokens = is_hyderabad_re(title, body)
                    total_tokens += tokens
                    if not is_re:
                        log.info(f"  Skipped (not Hyd-RE): {title[:60]}")
                        continue

                # Step 2: Classify + extract
                extracted, tokens = classify_and_extract(sub_name, title, body, author)
                total_tokens += tokens

                if not extracted:
                    log.warning(f"  Empty Claude response for: {title[:60]}")
                    continue

                # Step 3: Insert into Supabase
                row = {
                    "batch_id": batch_id,
                    "reddit_post_id": post.id,
                    "source": sub_name,
                    "post_url": f"https://reddit.com{post.permalink}",
                    "title": title,
                    "body": body[:5000] if body else None,
                    "author": author,
                    "post_score": post.score,
                    "num_comments": post.num_comments,
                    "posted_at": posted_at.isoformat(),
                    "classification": extracted.get("classification"),
                    "classification_confidence": extracted.get("classification_confidence"),
                    "classification_reason": extracted.get("classification_reason"),
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

                try:
                    supabase.table("leads").insert(row).execute()
                    kept += 1
                    totals["total"] += 1
                    classif = extracted.get("classification", "unclear")
                    if classif in totals:
                        totals[classif] += 1
                    log.info(f"  ✓ Saved [{classif}]: {title[:60]}")
                except Exception as e:
                    if "duplicate key" in str(e).lower():
                        log.info(f"  Skipped duplicate: {post.id}")
                    else:
                        log.error(f"  Insert failed: {e}")

        except Exception as e:
            log.error(f"Subreddit r/{sub_name} failed: {e}")

        source_counts[sub_name] = kept
        log.info(f"r/{sub_name}: {kept} new leads")

    # Update batch with totals
    supabase.table("hourly_batches").update({
        "total_posts": totals["total"],
        "source_counts": source_counts,
        "claude_tokens": total_tokens,
    }).eq("id", batch_id).execute()

    log.info(
        f"=== Run done: {totals['total']} new leads "
        f"({totals['end_user']} end-users, {totals['agent']} agents) "
        f"| {total_tokens} Claude tokens ==="
    )

    # Telegram summary
    send_telegram_summary(batch_id, source_counts, totals)


if __name__ == "__main__":
    main()
