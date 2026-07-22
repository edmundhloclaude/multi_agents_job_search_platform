-- Job store schema (spec §3). Applied on first run.
-- dedup_key is the PRIMARY KEY: normalized company|title|location.
-- This is how re-scanning is prevented (upsert on collision).

CREATE TABLE IF NOT EXISTS jobs (
    dedup_key          TEXT PRIMARY KEY,

    source             TEXT NOT NULL,
    source_url         TEXT,
    first_seen         TEXT NOT NULL,
    last_seen          TEXT NOT NULL,

    raw                TEXT,               -- JSON blob of the extracted posting

    company            TEXT NOT NULL,
    title              TEXT NOT NULL,
    location           TEXT,
    comp_text          TEXT,
    requirements       TEXT,               -- JSON array

    application_method TEXT,               -- linkedin_easy_apply | external_ats | email

    screen_status      TEXT NOT NULL DEFAULT 'unscreened',
    screen_score       INTEGER,
    screen_rationale   TEXT,

    apply_status       TEXT NOT NULL DEFAULT 'none',
    resume_path        TEXT,
    cover_letter_path  TEXT,

    response_status    TEXT NOT NULL DEFAULT 'none'
);

CREATE INDEX IF NOT EXISTS idx_jobs_screen_status ON jobs(screen_status);
CREATE INDEX IF NOT EXISTS idx_jobs_apply_status  ON jobs(apply_status);
CREATE INDEX IF NOT EXISTS idx_jobs_source        ON jobs(source);

-- Run log (spec §5: orchestrator keeps a run log).
CREATE TABLE IF NOT EXISTS run_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    stage     TEXT NOT NULL,
    tier      TEXT,
    message   TEXT
);
