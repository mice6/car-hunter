-- car-hunter schema (idempotent)

CREATE TABLE IF NOT EXISTS listings (
    id                BIGSERIAL PRIMARY KEY,
    source            TEXT        NOT NULL,              -- otomoto | olx
    source_id         TEXT        NOT NULL,
    kind              TEXT        NOT NULL,              -- candidate (uszkodzone) | market (zdrowe, do wyceny)
    url               TEXT,
    title             TEXT,
    short_description TEXT,
    make              TEXT,
    model             TEXT,
    year              INT,
    mileage_km        INT,
    power_hp          INT,
    engine_cc         INT,
    fuel              TEXT,
    gearbox           TEXT,
    body_type         TEXT,
    drive             TEXT,
    price_pln         NUMERIC(12,2),
    damaged           BOOLEAN,
    imported          BOOLEAN,
    has_registration  BOOLEAN,
    rhd               BOOLEAN,
    vin               TEXT,
    city              TEXT,
    region            TEXT,
    seller_type       TEXT,
    description       TEXT,
    photos            JSONB       NOT NULL DEFAULT '[]'::jsonb,
    raw               JSONB,
    listed_at         TIMESTAMPTZ,
    first_seen        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen         TIMESTAMPTZ NOT NULL DEFAULT now(),
    active            BOOLEAN     NOT NULL DEFAULT true,
    enriched_at       TIMESTAMPTZ,
    vision            JSONB,                              -- wynik analizy zdjęć (krok 3, RunPod)
    vision_at         TIMESTAMPTZ,
    UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS listings_kind_active_idx ON listings (kind, active);
CREATE INDEX IF NOT EXISTS listings_make_model_idx  ON listings (make, model, kind);
CREATE INDEX IF NOT EXISTS listings_vin_idx         ON listings (vin) WHERE vin IS NOT NULL;

CREATE TABLE IF NOT EXISTS price_history (
    listing_id BIGINT      NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    price_pln  NUMERIC(12,2) NOT NULL,
    seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS price_history_listing_idx ON price_history (listing_id, seen_at);

CREATE TABLE IF NOT EXISTS evaluations (
    listing_id      BIGINT PRIMARY KEY REFERENCES listings(id) ON DELETE CASCADE,
    market_value    NUMERIC(12,2),
    valuation_method TEXT,
    comparables_n   INT,
    repair_estimate NUMERIC(12,2),
    fees            NUMERIC(12,2),
    total_cost      NUMERIC(12,2),
    savings         NUMERIC(12,2),
    risk            NUMERIC(4,3),
    score           NUMERIC(12,2),
    passes_filters  BOOLEAN NOT NULL DEFAULT false,
    details         JSONB,
    evaluated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    alerted_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS evaluations_score_idx ON evaluations (passes_filters, score DESC);

-- kiedy ostatnio odświeżono rynek zdrowych aut dla danego modelu
CREATE TABLE IF NOT EXISTS market_refresh (
    source       TEXT NOT NULL,
    make         TEXT NOT NULL,
    model        TEXT NOT NULL,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    found        INT,
    PRIMARY KEY (source, make, model)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS olx_makes (
    category_id INT PRIMARY KEY,
    slug        TEXT NOT NULL
);

-- odciski aut, o których już był alert (to samo auto wystawione ponownie nie da drugiego alertu)
CREATE TABLE IF NOT EXISTS alerted (
    fp          TEXT PRIMARY KEY,
    listing_id  BIGINT,
    price_pln   NUMERIC(12,2),
    alerted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
