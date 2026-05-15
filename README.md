# Growthstories Leads

Hourly scraper that pulls Hyderabad real-estate posts from Reddit, classifies and extracts structured fields via Claude, writes to Supabase, and notifies via Telegram.

## Architecture

- **Scraper (Raspberry Pi):** `fetch_leads.py` runs hourly via cron. Residential IP avoids Reddit datacenter blocks.
- **Database (Supabase):** Postgres + Auth, free tier.
- **Dashboard (Vercel):** Next.js team UI (Session 2 — coming).
- **Notifier (Telegram):** one summary message per hourly batch.

## Sources

- r/hyderabadrealestate
- r/Hyderabad_highrises
- r/WestHydrealestate
- r/hyderabad (filtered to real-estate posts via Claude)
- r/indianrealestate (filtered to Hyderabad posts via Claude)

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.template .env
# Fill in keys in .env
python fetch_leads.py
```

## Cron

```
# Every hour from 7am to 9pm IST
0 7-21 * * * cd /home/pi/growthstories-leads && ./venv/bin/python fetch_leads.py
```
