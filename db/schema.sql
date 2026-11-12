-- Desk Research: five tables.
-- sources     -> where we collect from
-- materials   -> what the collector brought (one row per link)
-- findings    -> what the worker extracted with the model
-- runs        -> every run of every block, with status
-- subscribers -> who pressed Start in the Telegram bot

CREATE TABLE IF NOT EXISTS sources (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,            -- europepmc | rss (legacy kinds retained)
    url         TEXT NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS runs (
    id           SERIAL PRIMARY KEY,
    service      TEXT NOT NULL,           -- collector | worker | bot
    status       TEXT NOT NULL,           -- started | running | finished | failed
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    items        INTEGER NOT NULL DEFAULT 0,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS materials (
    id            SERIAL PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources(id),
    run_id        INTEGER REFERENCES runs(id),
    url           TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    text          TEXT,
    published_at  TIMESTAMPTZ,
    collected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status        TEXT NOT NULL DEFAULT 'new'   -- collected | new | done | skipped | failed
);

-- Safe for existing T01 databases. Original text and statuses are preserved.
ALTER TABLE materials ADD COLUMN IF NOT EXISTS raw_payload JSONB;
ALTER TABLE materials ADD COLUMN IF NOT EXISTS content_scope TEXT NOT NULL DEFAULT 'legacy_unknown'
    CHECK (content_scope IN ('abstract', 'feed_excerpt', 'metadata_only', 'legacy_unknown'));

CREATE TABLE IF NOT EXISTS findings (
    id              SERIAL PRIMARY KEY,
    material_id     INTEGER NOT NULL UNIQUE REFERENCES materials(id),
    entity          TEXT,                 -- company the material is about
    summary         TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    publication_status TEXT NOT NULL DEFAULT 'draft'
        CHECK (publication_status IN ('draft', 'published')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- the bot marks a finding when it has been sent to subscribers
ALTER TABLE findings ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS publication_payload JSONB NOT NULL DEFAULT '{}'
    CHECK (jsonb_typeof(publication_payload) = 'object');
ALTER TABLE findings ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;
ALTER TABLE materials ADD COLUMN IF NOT EXISTS editorial_result JSONB;

-- A popular article and its research record must not publish the same evidence twice.
CREATE UNIQUE INDEX IF NOT EXISTS findings_published_evidence_key
    ON findings ((publication_payload->>'evidence_key'))
    WHERE publication_status = 'published' AND publication_payload ? 'evidence_key';

CREATE TABLE IF NOT EXISTS subscribers (
    chat_id     BIGINT PRIMARY KEY,
    name        TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Retire only the five original URLs; do not delete their records or materials.
UPDATE sources SET active = FALSE WHERE url IN (
    'https://hn.algolia.com/api/v1/search_by_date?query=AI&tags=story&hitsPerPage=15',
    'https://hn.algolia.com/api/v1/search_by_date?query=Anthropic&tags=story&hitsPerPage=15',
    'https://hn.algolia.com/api/v1/search_by_date?query=OpenAI&tags=story&hitsPerPage=15',
    'https://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&sortOrder=descending&max_results=10',
    'https://habr.com/ru/rss/hubs/artificial_intelligence/articles/'
);

INSERT INTO sources (name, kind, url) VALUES
    ('Europe PMC: Dogs pilot', 'europepmc', 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'),
    ('ScienceDaily: Dogs', 'rss', 'https://www.sciencedaily.com/rss/plants_animals/dogs.xml')
ON CONFLICT (name) DO NOTHING;
