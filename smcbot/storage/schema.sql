-- SMC bot journal schema (idempotent: safe to run on every start)

CREATE TABLE IF NOT EXISTS trades (
    id              TEXT PRIMARY KEY,
    mode            TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    status          TEXT NOT NULL,
    setup_id        TEXT,
    score           INT,
    reasons         JSONB,
    poi             TEXT,
    swept           JSONB,
    planned_entry   DOUBLE PRECISION,
    fill_price      DOUBLE PRECISION,
    initial_sl      DOUBLE PRECISION,
    initial_tp      DOUBLE PRECISION,
    sl              DOUBLE PRECISION,
    tp              DOUBLE PRECISION,
    tp_r            DOUBLE PRECISION,
    qty             DOUBLE PRECISION,
    leverage        INT,
    risk_pct        DOUBLE PRECISION,
    risk_usd        DOUBLE PRECISION,
    created_at      TIMESTAMPTZ,
    filled_at       TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    exit_price      DOUBLE PRECISION,
    exit_reason     TEXT,
    pnl             DOUBLE PRECISION,
    fees            DOUBLE PRECISION,
    r_multiple      DOUBLE PRECISION,
    mfe_r           DOUBLE PRECISION,
    mae_r           DOUBLE PRECISION,
    hold_minutes    DOUBLE PRECISION,
    be_done         BOOLEAN,
    tp_extended     BOOLEAN,
    setup           JSONB,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS trades_mode_created ON trades (mode, created_at);
CREATE INDEX IF NOT EXISTS trades_symbol ON trades (symbol);
CREATE INDEX IF NOT EXISTS trades_status ON trades (status);

CREATE TABLE IF NOT EXISTS trade_events (
    id          BIGSERIAL PRIMARY KEY,
    trade_id    TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    event       TEXT NOT NULL,
    message     TEXT,
    price       DOUBLE PRECISION,
    data        JSONB
);
CREATE INDEX IF NOT EXISTS trade_events_trade ON trade_events (trade_id, ts);

-- every detected setup, taken or not (with the reason it was skipped)
CREATE TABLE IF NOT EXISTS setups (
    id              TEXT PRIMARY KEY,
    mode            TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    score           INT,
    taken           BOOLEAN NOT NULL DEFAULT FALSE,
    reject_reason   TEXT,
    entry           DOUBLE PRECISION,
    sl              DOUBLE PRECISION,
    tp              DOUBLE PRECISION,
    tp_r            DOUBLE PRECISION,
    poi             TEXT,
    swept           JSONB,
    reasons         JSONB,
    features        JSONB
);
CREATE INDEX IF NOT EXISTS setups_mode_created ON setups (mode, created_at);

CREATE TABLE IF NOT EXISTS scan_runs (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    mode            TEXT NOT NULL,
    duration_ms     INT,
    symbols         INT,
    setups_found    INT,
    orders_placed   INT,
    blocked_reason  TEXT,
    details         JSONB
);
CREATE INDEX IF NOT EXISTS scan_runs_ts ON scan_runs (ts);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    mode            TEXT NOT NULL,
    equity          DOUBLE PRECISION,
    free_balance    DOUBLE PRECISION,
    open_positions  INT,
    open_risk_usd   DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS equity_snapshots_ts ON equity_snapshots (mode, ts);

CREATE TABLE IF NOT EXISTS bot_logs (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    level       TEXT NOT NULL,
    logger      TEXT,
    message     TEXT,
    context     JSONB
);
CREATE INDEX IF NOT EXISTS bot_logs_ts ON bot_logs (ts);
CREATE INDEX IF NOT EXISTS bot_logs_level ON bot_logs (level, ts);

-- migrations (safe to re-run)
ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy TEXT NOT NULL DEFAULT 'smc';
ALTER TABLE setups ADD COLUMN IF NOT EXISTS strategy TEXT NOT NULL DEFAULT 'smc';
CREATE INDEX IF NOT EXISTS trades_strategy ON trades (mode, strategy, created_at);

-- ---------------------------------------------------------------------------
-- Analysis views (closed trades only). Dropped and recreated so columns can change.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_closed CASCADE;
DROP VIEW IF EXISTS v_rejected_setups CASCADE;

CREATE VIEW v_closed AS
SELECT * FROM trades WHERE status = 'closed' AND r_multiple IS NOT NULL;

CREATE VIEW v_summary AS
SELECT mode, strategy,
       count(*)                                           AS trades,
       round(100.0 * avg((r_multiple > 0)::int), 1)       AS win_rate_pct,
       round(avg(r_multiple)::numeric, 3)                 AS avg_r,
       round(sum(r_multiple)::numeric, 2)                 AS total_r,
       round(sum(pnl)::numeric, 2)                        AS total_pnl,
       round((sum(pnl) FILTER (WHERE pnl > 0) / NULLIF(-sum(pnl) FILTER (WHERE pnl < 0), 0))::numeric, 2) AS profit_factor,
       round(avg(hold_minutes)::numeric, 0)               AS avg_hold_min,
       round(avg(mfe_r)::numeric, 2)                      AS avg_mfe_r,
       round(avg(mae_r)::numeric, 2)                      AS avg_mae_r
FROM v_closed GROUP BY mode, strategy;

CREATE VIEW v_by_symbol AS
SELECT mode, strategy, symbol, count(*) AS trades,
       round(100.0 * avg((r_multiple > 0)::int), 1) AS win_rate_pct,
       round(avg(r_multiple)::numeric, 3) AS avg_r, round(sum(pnl)::numeric, 2) AS pnl
FROM v_closed GROUP BY mode, strategy, symbol ORDER BY mode, strategy, avg_r DESC;

CREATE VIEW v_by_score AS
SELECT mode, (score / 10) * 10 AS score_bucket, count(*) AS trades,
       round(100.0 * avg((r_multiple > 0)::int), 1) AS win_rate_pct,
       round(avg(r_multiple)::numeric, 3) AS avg_r
FROM v_closed GROUP BY mode, score_bucket ORDER BY mode, score_bucket;

CREATE VIEW v_by_exit_reason AS
SELECT mode, strategy, split_part(exit_reason, ' (', 1) AS exit_reason, count(*) AS trades,
       round(avg(r_multiple)::numeric, 3) AS avg_r, round(sum(pnl)::numeric, 2) AS pnl
FROM v_closed GROUP BY mode, strategy, 3 ORDER BY mode, strategy, trades DESC;

CREATE VIEW v_by_poi AS
SELECT mode, poi, side, count(*) AS trades,
       round(100.0 * avg((r_multiple > 0)::int), 1) AS win_rate_pct,
       round(avg(r_multiple)::numeric, 3) AS avg_r
FROM v_closed GROUP BY mode, poi, side ORDER BY mode, avg_r DESC;

CREATE VIEW v_by_hour AS
SELECT mode, strategy, extract(hour FROM created_at AT TIME ZONE 'UTC')::int AS hour_utc, count(*) AS trades,
       round(100.0 * avg((r_multiple > 0)::int), 1) AS win_rate_pct,
       round(avg(r_multiple)::numeric, 3) AS avg_r
FROM v_closed GROUP BY mode, strategy, hour_utc ORDER BY mode, strategy, hour_utc;

CREATE VIEW v_by_swept_level AS
SELECT t.mode, lvl AS swept_level, count(*) AS trades,
       round(100.0 * avg((t.r_multiple > 0)::int), 1) AS win_rate_pct,
       round(avg(t.r_multiple)::numeric, 3) AS avg_r
FROM v_closed t, jsonb_array_elements_text(t.swept) AS lvl
GROUP BY t.mode, lvl ORDER BY t.mode, avg_r DESC;

CREATE VIEW v_by_feature AS
SELECT mode, f.key AS feature, f.value AS value, count(*) AS trades,
       round(100.0 * avg((r_multiple > 0)::int), 1) AS win_rate_pct,
       round(avg(r_multiple)::numeric, 3) AS avg_r
FROM v_closed, jsonb_each_text(setup->'features') AS f
WHERE f.key IN ('killzone', 'smt', 'discount', 'ote', 'same_candle_reclaim', 'htf_trend', 'mtf_trend')
GROUP BY mode, f.key, f.value ORDER BY mode, feature, value;

CREATE VIEW v_optimizer_effect AS
SELECT mode, be_done, tp_extended, count(*) AS trades,
       round(avg(r_multiple)::numeric, 3) AS avg_r,
       round(avg(mfe_r)::numeric, 2) AS avg_mfe_r,
       round(avg(mfe_r - r_multiple)::numeric, 2) AS avg_r_left_on_table
FROM v_closed GROUP BY mode, be_done, tp_extended ORDER BY mode;

CREATE VIEW v_rejected_setups AS
SELECT mode, strategy, reject_reason, count(*) AS setups, round(avg(score)::numeric, 1) AS avg_score
FROM setups WHERE NOT taken GROUP BY mode, strategy, reject_reason ORDER BY mode, strategy, setups DESC;
