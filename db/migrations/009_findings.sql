-- 009: findings + notification channels (Notifications Phase 1+2).
--
-- The ingester's threshold evaluator (ingestion/findings.py) runs at the end
-- of every ingest and emits STRUCTURED findings — one row per detected
-- condition (quota utilization, throttle rate, cost jump, model EOL). The
-- finding is the contract: every delivery channel (SNS today; Slack/JIRA/
-- EventBridge later) renders this one shape. recommended_action carries the
-- Phase-2 "prepared action": a ready-to-run CLI command + console deep link
-- the customer executes themselves — the dashboard never writes to monitored
-- accounts.
--
-- Lifecycle: finding_id is a stable dedup key. Re-detected → last_seen
-- refreshed. No longer detected → state='resolved'. Notifications fire on
-- state TRANSITIONS (new / resolved), never on every ingest, so a persistent
-- condition alerts once, not daily.

CREATE TABLE IF NOT EXISTS f_findings (
    finding_id       TEXT PRIMARY KEY,
    type             TEXT NOT NULL,      -- quota_utilization | throttle_rate | cost_jump | model_eol
    severity         TEXT NOT NULL,      -- critical | warning | info
    accountId        TEXT,
    model            TEXT,
    region           TEXT,
    title            TEXT NOT NULL,
    detail           TEXT NOT NULL DEFAULT '',
    metric           JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {value, threshold, unit, window}
    recommended_action JSONB NOT NULL DEFAULT '{}'::jsonb, -- {kind, summary, cli, console_url}
    state            TEXT NOT NULL DEFAULT 'active',       -- active | resolved
    acked            BOOLEAN NOT NULL DEFAULT FALSE,
    first_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen        TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at      TIMESTAMPTZ,
    notified_at      TIMESTAMPTZ                            -- last state-transition notification
);

CREATE INDEX IF NOT EXISTS ix_findings_state ON f_findings (state, severity, last_seen DESC);

-- Delivery channels. Phase 1 ships type='sns' only; the table shape is the
-- extension point (a Slack/JIRA connector = one new type + one adapter).
CREATE TABLE IF NOT EXISTS notification_channels (
    id               SERIAL PRIMARY KEY,
    type             TEXT NOT NULL,                 -- 'sns' (more later)
    config           JSONB NOT NULL DEFAULT '{}'::jsonb,  -- sns: {topic_arn}
    min_severity     TEXT NOT NULL DEFAULT 'warning',     -- info | warning | critical
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_delivery_at TIMESTAMPTZ,
    last_delivery_status TEXT NOT NULL DEFAULT ''   -- 'ok' | error text
);
