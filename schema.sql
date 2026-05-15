-- ============================================================
--  GROWTHSTORIES Leads Dashboard — Database Schema
--  Run this in Supabase SQL Editor (one-time setup)
-- ============================================================

-- ─── 1. HOURLY BATCHES ─────────────────────────────────────
-- One row per scraper run. Groups leads by "the 2pm pull".
CREATE TABLE IF NOT EXISTS hourly_batches (
    id              BIGSERIAL PRIMARY KEY,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_posts     INT NOT NULL DEFAULT 0,
    source_counts   JSONB,                -- {"hyderabadrealestate": 5, ...}
    claude_tokens   INT DEFAULT 0,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_batches_run_at ON hourly_batches(run_at DESC);


-- ─── 2. LEADS ──────────────────────────────────────────────
-- One row per Reddit post. The heart of the dashboard.
CREATE TABLE IF NOT EXISTS leads (
    id                       BIGSERIAL PRIMARY KEY,
    batch_id                 BIGINT REFERENCES hourly_batches(id) ON DELETE SET NULL,

    -- Reddit metadata
    reddit_post_id           TEXT UNIQUE NOT NULL,   -- prevents duplicates
    source                   TEXT NOT NULL,          -- subreddit name
    post_url                 TEXT NOT NULL,
    title                    TEXT NOT NULL,
    body                     TEXT,
    author                   TEXT NOT NULL,
    post_score               INT DEFAULT 0,
    num_comments             INT DEFAULT 0,
    posted_at                TIMESTAMPTZ NOT NULL,
    fetched_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Claude classification (existing)
    classification           TEXT,                   -- end_user / agent / unclear
    classification_confidence INT,                   -- 0-100
    classification_reason    TEXT,

    -- Claude extracted fields (new)
    intent                   TEXT,                   -- buy / rent / invest / info / unclear
    budget_min               BIGINT,                 -- in INR
    budget_max               BIGINT,
    budget_text              TEXT,                   -- raw e.g. "25-35L"
    locality                 TEXT,                   -- e.g. "Kondapur"
    property_type            TEXT,                   -- apartment / villa / plot / commercial
    bhk                      TEXT,                   -- "2BHK", "3BHK", null

    -- Team workflow
    status                   TEXT NOT NULL DEFAULT 'new',
                                                     -- new / contacted / skip / done
    comment                  TEXT,
    comment_by               TEXT,                   -- user's display name
    comment_at               TIMESTAMPTZ,

    -- Hyderabad relevance flag (used to filter r/hyderabad + r/indianrealestate)
    is_hyderabad_re          BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_leads_posted_at      ON leads(posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_batch          ON leads(batch_id);
CREATE INDEX IF NOT EXISTS idx_leads_status         ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_source         ON leads(source);
CREATE INDEX IF NOT EXISTS idx_leads_classification ON leads(classification);
CREATE INDEX IF NOT EXISTS idx_leads_locality       ON leads(locality);
CREATE INDEX IF NOT EXISTS idx_leads_hyd_re         ON leads(is_hyderabad_re);


-- ─── 3. ROW-LEVEL SECURITY ─────────────────────────────────
-- Supabase requires this. For now: anyone logged in can read/write.
-- We can tighten later (e.g., only assigned user edits their leads).
ALTER TABLE leads            ENABLE ROW LEVEL SECURITY;
ALTER TABLE hourly_batches   ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Authenticated users can read leads"
    ON leads FOR SELECT
    TO authenticated
    USING (true);

CREATE POLICY "Authenticated users can update leads"
    ON leads FOR UPDATE
    TO authenticated
    USING (true);

CREATE POLICY "Authenticated users can read batches"
    ON hourly_batches FOR SELECT
    TO authenticated
    USING (true);

-- Service role (used by the Pi scraper) bypasses RLS automatically,
-- so the scraper can INSERT without these policies.


-- ─── 4. HELPFUL VIEW: today's leads grouped by batch ───────
CREATE OR REPLACE VIEW todays_batches AS
SELECT
    b.id                AS batch_id,
    b.run_at,
    b.total_posts,
    b.source_counts,
    COUNT(l.id)         AS leads_count,
    COUNT(*) FILTER (WHERE l.classification = 'end_user') AS end_user_count,
    COUNT(*) FILTER (WHERE l.classification = 'agent')    AS agent_count,
    COUNT(*) FILTER (WHERE l.status = 'new')              AS unactioned_count
FROM hourly_batches b
LEFT JOIN leads l ON l.batch_id = b.id
WHERE b.run_at >= CURRENT_DATE AT TIME ZONE 'Asia/Kolkata'
GROUP BY b.id
ORDER BY b.run_at DESC;
