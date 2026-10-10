-- Meme Radar schema. Every statement is idempotent; it runs on each start.
-- Timestamps are Unix seconds (UTC) as REAL.

-- One row per token, written the first time any source shows it to us.
-- Rows are never deleted: dead tokens must stay to avoid survivorship bias.
CREATE TABLE IF NOT EXISTS tokens (
    mint                TEXT PRIMARY KEY,
    name                TEXT,
    symbol              TEXT,
    creator             TEXT,
    created_at          REAL,      -- pumpportal: time we received the create event (it carries no timestamp);
                                   -- gecko/dexscreener: pool/pair creation time
    first_seen_at       REAL NOT NULL,
    source              TEXT NOT NULL,   -- pumpportal / gecko / dexscreener (first source to show it)
    metadata_uri        TEXT,
    twitter             TEXT,
    telegram            TEXT,
    website             TEXT,
    launchpad           TEXT,
    image_url           TEXT,
    description         TEXT,
    bonding_curve       TEXT,
    pool_address        TEXT,      -- current main pool/pair; used for charts
    dex_id              TEXT,
    initial_buy_tokens  REAL,
    initial_buy_sol     REAL,
    initial_mcap_sol    REAL,
    mayhem_mode         INTEGER,
    theme               TEXT       -- political / celebrity / ai_character / animal / other (config/themes.yaml)
);
CREATE INDEX IF NOT EXISTS idx_tokens_created ON tokens(created_at);
CREATE INDEX IF NOT EXISTS idx_tokens_creator ON tokens(creator);

-- Time series of market data. A row is skipped when nothing changed since the
-- previous one, so a gap means "unchanged", not "not checked".
CREATE TABLE IF NOT EXISTS snapshots (
    mint            TEXT NOT NULL,
    ts              REAL NOT NULL,
    source          TEXT NOT NULL,
    price_usd       REAL,
    mcap            REAL,
    fdv             REAL,
    liquidity_usd   REAL,          -- source=dexscreener: pool liquidity, NULL on a bonding curve;
                                   -- source=gecko: reserve_in_usd, which can include curve reserves
    vol_m5          REAL,
    vol_h1          REAL,
    vol_h6          REAL,
    vol_h24         REAL,
    buys_m5         INTEGER,
    sells_m5        INTEGER,
    buys_h1         INTEGER,
    sells_h1        INTEGER,
    buys_h24        INTEGER,
    sells_h24       INTEGER,
    price_chg_m5    REAL,
    price_chg_h1    REAL,
    price_chg_h24   REAL,
    boosted         INTEGER,       -- paid DexScreener promotion active at this time
    boosts_active   INTEGER,
    dex_id          TEXT,
    pair_address    TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_mint_ts ON snapshots(mint, ts);

-- Individual trades for watched tokens. Empty for now: PumpPortal's trade
-- stream needs a funded API key, which is outside the free-sources rule.
CREATE TABLE IF NOT EXISTS trades (
    mint            TEXT NOT NULL,
    ts              REAL NOT NULL,
    side            TEXT,
    sol_amount      REAL,
    token_amount    REAL,
    trader          TEXT,
    mcap_sol        REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_mint_ts ON trades(mint, ts);

CREATE TABLE IF NOT EXISTS migrations (
    mint            TEXT NOT NULL,
    ts              REAL NOT NULL,
    pool            TEXT,
    signature       TEXT
);
CREATE INDEX IF NOT EXISTS idx_migrations_mint ON migrations(mint);

CREATE TABLE IF NOT EXISTS safety (
    mint                        TEXT NOT NULL,
    ts                          REAL NOT NULL,
    mint_authority_enabled      INTEGER,
    freeze_authority_enabled    INTEGER,
    top10_pct                   REAL,   -- excludes liquidity pools / bonding curve accounts
    top_holder_pct              REAL,
    lp_locked_pct               REAL,
    risk_score                  REAL,
    risk_score_normalised       REAL,
    rugged                      INTEGER,
    total_holders               INTEGER,
    insiders_detected           INTEGER,
    creator                     TEXT,
    creator_balance             REAL,
    risks_json                  TEXT,
    top_holders_json            TEXT,
    authority_conflict          INTEGER    -- 1 = the report's fields and its risk list disagree on mint/freeze authority
);
CREATE INDEX IF NOT EXISTS idx_safety_mint_ts ON safety(mint, ts);

-- Raw payloads exactly as received (PumpPortal events, DexScreener profiles and boosts).
CREATE TABLE IF NOT EXISTS raw_events (
    id              INTEGER PRIMARY KEY,
    ts              REAL NOT NULL,
    source          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    mint            TEXT,
    payload         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_events_mint ON raw_events(mint);

-- Latest known values per token plus refresh scheduling. This is a cache for
-- the dashboard; the history lives in the tables above.
CREATE TABLE IF NOT EXISTS token_state (
    mint                        TEXT PRIMARY KEY,
    snapshot_ts                 REAL,
    snapshot_source             TEXT,
    snap_sig                    TEXT,
    price_usd                   REAL,
    mcap                        REAL,
    fdv                         REAL,
    liquidity_usd               REAL,   -- from DexScreener only; NULL means still on a bonding curve
    vol_m5                      REAL,
    vol_h1                      REAL,
    vol_h24                     REAL,
    buys_m5                     INTEGER,
    sells_m5                    INTEGER,
    buys_h1                     INTEGER,
    sells_h1                    INTEGER,
    price_chg_m5                REAL,
    price_chg_h1                REAL,
    boosts_active               INTEGER,
    score                       REAL,
    vol_accel                   REAL,
    buy_pressure                REAL,
    txn_growth                  REAL,
    migrated_at                 REAL,
    trending_rank               INTEGER,
    trending_seen_at            REAL,
    rising_seen_at              REAL,
    profile_seen_at             REAL,
    boost_seen_at               REAL,
    boost_total                 INTEGER,
    watched                     INTEGER NOT NULL DEFAULT 0,
    next_refresh_at             REAL,
    last_checked_at             REAL,
    miss_count                  INTEGER NOT NULL DEFAULT 0,
    safety_ts                   REAL,
    mint_authority_enabled      INTEGER,
    freeze_authority_enabled    INTEGER,
    top10_pct                   REAL,
    top_holder_pct              REAL,
    lp_locked_pct               REAL,
    risk_score                  REAL,
    risk_score_normalised       REAL,
    rugged                      INTEGER,
    total_holders               INTEGER,   -- from the latest RugCheck report
    -- RugCheck answered "no report" (HTTP 4xx) this many times in a row; 0 after a success.
    safety_miss_count           INTEGER NOT NULL DEFAULT 0,
    safety_miss_at              REAL,
    safety_retry_at             REAL,      -- when to ask again; NULL with misses > 0 means we stopped asking
    bot_volume                  INTEGER,   -- 1 = many trades but a flat price (see radar/scoring/bot_volume.py)
    followup_idx                INTEGER,   -- position of the next follow-up checkpoint (config order)
    followup_at                 REAL,      -- when the follow-up loop should make that check; NULL = none left
    -- Retention decision, made once the 7d follow-up is in: 'kept' = all snapshots stay,
    -- 'early' = only the first hours of snapshots stay, 'pruned' = snapshots deleted
    -- (token, outcome and follow-up rows always stay). NULL = not decided yet.
    retention                   TEXT,
    retention_at                REAL,
    peak_mcap                   REAL,      -- highest market cap in the token's snapshots when that was decided
    -- Fetching the description from the metadata link: NULL = not asked for, 'wanted' = queued,
    -- 'done' = saved, 'none' = the file has no description, 'skipped' = link is not somewhere
    -- we fetch from, 'failed' = could not be fetched.
    desc_state                  TEXT,
    desc_tries                  INTEGER NOT NULL DEFAULT 0,
    desc_retry_at               REAL,
    authority_conflict          INTEGER,   -- from the latest safety report; shown as "Unverified"
    -- Mint and freeze authority as read from the chain itself (public Solana RPC). When present
    -- these win over RugCheck's reading above. NULL values with a time = the account could not be read.
    chain_checked_at            REAL,
    chain_mint_authority        INTEGER,
    chain_freeze_authority      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_state_next_refresh ON token_state(next_refresh_at);
CREATE INDEX IF NOT EXISTS idx_state_score ON token_state(score);
CREATE INDEX IF NOT EXISTS idx_state_profile ON token_state(profile_seen_at);

-- Phase 2. One row per token per follow-up checkpoint (1m, 5m, ... 7d after first seen).
--   done        looked up on DexScreener in time; the figures are below
--   no_data     looked up in time, but DexScreener had no pair for the token
--   skipped     not looked up in time (collector off or behind), so the figures are unknown
--   backfilled  taken from snapshots recorded before follow-ups existed
CREATE TABLE IF NOT EXISTS followups (
    mint            TEXT NOT NULL,
    checkpoint      TEXT NOT NULL,
    due_at          REAL NOT NULL,
    done_at         REAL,
    status          TEXT NOT NULL,
    price_usd       REAL,
    mcap            REAL,
    liquidity_usd   REAL,
    vol_h24         REAL,
    -- The signal inputs at that moment. For tokens whose snapshots are later dropped
    -- (see retention in settings.yaml), these rows are what remains of their history.
    vol_m5          REAL,
    vol_h1          REAL,
    buys_m5         INTEGER,
    sells_m5        INTEGER,
    buys_h1         INTEGER,
    sells_h1        INTEGER,
    price_chg_m5    REAL,
    price_chg_h1    REAL,
    PRIMARY KEY (mint, checkpoint)
) WITHOUT ROWID;

-- Phase 2. What happened to each token after p0, the first price we saw for it.
-- Recomputed each time one of the token's follow-up checkpoints is recorded.
CREATE TABLE IF NOT EXISTS outcomes (
    mint                        TEXT PRIMARY KEY,
    p0_price                    REAL,
    p0_ts                       REAL,
    peak_multiple_24h           REAL,   -- highest price seen in the 24h after p0, as a multiple of p0
    time_to_peak_min            REAL,
    multiple_1h                 REAL,   -- price at the checkpoint / p0
    multiple_24h                REAL,
    multiple_72h                REAL,
    multiple_7d                 REAL,
    max_drawdown_before_peak    REAL,   -- deepest fall from a high on the way to the peak, 0 to 1
    rugged                      INTEGER,
    dead_at_7d                  INTEGER,
    peak                        TEXT,   -- moonshot / runner / flat, from the 24h peak; NULL = cannot tell yet
    fate                        TEXT,   -- rugged / dead / alive at day 7; NULL = cannot tell yet
    final                       INTEGER NOT NULL DEFAULT 0,  -- 1 = the 7d checkpoint has passed; nothing more is coming
    complete                    INTEGER NOT NULL DEFAULT 0,  -- 1 = final and nothing is missing
    missing                     TEXT,   -- comma-separated fields we do not have (yet)
    computed_at                 REAL
);

-- The first time each token appeared on the Rising list: what it looked like and which
-- driver pattern it showed. Joined with outcomes to say how similar tokens ended.
CREATE TABLE IF NOT EXISTS rising_entries (
    mint                        TEXT PRIMARY KEY,
    ts                          REAL NOT NULL,
    score                       REAL,
    price_usd                   REAL,
    mcap                        REAL,
    liquidity_usd               REAL,
    stage                       TEXT,      -- curve / just_graduated / graduated
    pattern                     TEXT,      -- stage/driver families/warnings, e.g. "curve/buying+volume/clean"
    checked                     INTEGER,   -- 1 = it had a safety report when the pattern was set
    paid_boost                  INTEGER,
    theme                       TEXT,
    drivers_json                TEXT,
    warnings_json               TEXT,
    peak_multiple_24h_after     REAL       -- highest price in the 24h after ts, as a multiple of price_usd
);
CREATE INDEX IF NOT EXISTS idx_rising_entries_pattern ON rising_entries(pattern);

-- Periods when a live stream delivered nothing, so whatever happened then is missing.
-- For pumpportal: tokens launched between started_at and ended_at were not seen being created.
CREATE TABLE IF NOT EXISTS outages (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,
    started_at      REAL NOT NULL,     -- the last event received before the silence
    ended_at        REAL NOT NULL,     -- the first event received after it
    reason          TEXT
);
CREATE INDEX IF NOT EXISTS idx_outages_source ON outages(source, started_at);

CREATE TABLE IF NOT EXISTS meta (
    key             TEXT PRIMARY KEY,
    value           TEXT
);
