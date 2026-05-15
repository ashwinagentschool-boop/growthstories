-- ============================================================
--  Growthstories Leads — Schema v2
--  Run in Supabase SQL Editor. Safe to re-run (uses IF NOT EXISTS / DO blocks).
-- ============================================================

-- ─── 1. HOURLY BATCHES (existing — no change) ──────────────
CREATE TABLE IF NOT EXISTS hourly_batches (
    id              BIGSERIAL PRIMARY KEY,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_posts     INT NOT NULL DEFAULT 0,
    source_counts   JSONB,
    claude_tokens   INT DEFAULT 0,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_run_at ON hourly_batches(run_at DESC);


-- ─── 2. LEADS — add columns to match old bot ───────────────
-- We use ALTER TABLE ... ADD COLUMN IF NOT EXISTS so reruns are safe.
CREATE TABLE IF NOT EXISTS leads (
    id                       BIGSERIAL PRIMARY KEY,
    batch_id                 BIGINT REFERENCES hourly_batches(id) ON DELETE SET NULL,
    reddit_post_id           TEXT UNIQUE NOT NULL,
    source                   TEXT NOT NULL,
    post_url                 TEXT NOT NULL,
    title                    TEXT NOT NULL,
    body                     TEXT,
    author                   TEXT NOT NULL,
    post_score               INT DEFAULT 0,
    num_comments             INT DEFAULT 0,
    posted_at                TIMESTAMPTZ NOT NULL,
    fetched_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    classification           TEXT,
    classification_confidence INT,
    classification_reason    TEXT,
    intent                   TEXT,
    budget_min               BIGINT,
    budget_max               BIGINT,
    budget_text              TEXT,
    locality                 TEXT,
    property_type            TEXT,
    bhk                      TEXT,
    status                   TEXT NOT NULL DEFAULT 'new',
    comment                  TEXT,
    comment_by               TEXT,
    comment_at               TIMESTAMPTZ,
    is_hyderabad_re          BOOLEAN NOT NULL DEFAULT TRUE
);

-- New columns (added if missing)
ALTER TABLE leads ADD COLUMN IF NOT EXISTS quality_score INT;          -- 1-10
ALTER TABLE leads ADD COLUMN IF NOT EXISTS upvote_ratio NUMERIC(4,3);  -- 0.00 to 1.00
ALTER TABLE leads ADD COLUMN IF NOT EXISTS flair TEXT;                 -- "Agent", "Investor", etc.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS external_link TEXT;         -- cross-post URL or i.redd.it
ALTER TABLE leads ADD COLUMN IF NOT EXISTS cross_posted_subs TEXT;     -- comma-separated subs

-- Indexes for the dashboard's filters
CREATE INDEX IF NOT EXISTS idx_leads_posted_at      ON leads(posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_batch          ON leads(batch_id);
CREATE INDEX IF NOT EXISTS idx_leads_status         ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_source         ON leads(source);
CREATE INDEX IF NOT EXISTS idx_leads_classification ON leads(classification);
CREATE INDEX IF NOT EXISTS idx_leads_locality       ON leads(locality);
CREATE INDEX IF NOT EXISTS idx_leads_quality        ON leads(quality_score DESC);
CREATE INDEX IF NOT EXISTS idx_leads_property_type  ON leads(property_type);
CREATE INDEX IF NOT EXISTS idx_leads_author         ON leads(author);


-- ─── 3. USER PROFILES — new table ─────────────────────────
CREATE TABLE IF NOT EXISTS user_profiles (
    id                       BIGSERIAL PRIMARY KEY,
    author                   TEXT UNIQUE NOT NULL,           -- "Only-Sea-2741" (no u/ prefix)
    profile_url              TEXT,
    -- Account stats (from Reddit /user/<name>/about.json)
    account_age_days         INT,
    total_karma              INT,
    link_karma               INT,
    comment_karma            INT,
    -- Activity stats (computed from submitted + comments)
    posts_90d                INT DEFAULT 0,
    comments_90d             INT DEFAULT 0,
    subs_diversity           INT DEFAULT 0,                  -- # of distinct subs
    re_activity_pct          NUMERIC(5,2) DEFAULT 0,         -- % of activity in RE subs
    promo_hits               INT DEFAULT 0,                  -- count of promotional phrases
    -- Reddit data
    top_subreddits           JSONB,                          -- [{"sub": "...", "count": 10}]
    latest_post_titles       JSONB,                          -- ["title 1", ...]
    latest_comment_snippets  JSONB,                          -- ["snippet 1", ...]
    -- Claude classification (re-done here, with full author context)
    classification           TEXT,                           -- end_user / agent / unclear
    classification_confidence INT,
    reasoning                TEXT,
    red_flags                TEXT,
    supporting_signals       TEXT,
    -- Cache control
    enriched_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    enrich_status            TEXT NOT NULL DEFAULT 'ok'      -- ok / not_found / suspended / error
);

CREATE INDEX IF NOT EXISTS idx_profiles_author       ON user_profiles(author);
CREATE INDEX IF NOT EXISTS idx_profiles_enriched_at  ON user_profiles(enriched_at DESC);
CREATE INDEX IF NOT EXISTS idx_profiles_classification ON user_profiles(classification);


-- ─── 4. RLS — anyone authenticated can read/write ─────────
ALTER TABLE leads            ENABLE ROW LEVEL SECURITY;
ALTER TABLE hourly_batches   ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_profiles    ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY "auth read leads"   ON leads          FOR SELECT TO authenticated USING (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "auth update leads" ON leads          FOR UPDATE TO authenticated USING (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "auth read batches" ON hourly_batches FOR SELECT TO authenticated USING (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY "auth read profiles" ON user_profiles FOR SELECT TO authenticated USING (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- service_role bypasses RLS — so the Pi scraper can INSERT/UPDATE without policies.
