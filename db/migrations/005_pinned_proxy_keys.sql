-- 005_pinned_proxy_keys.sql
-- Symmetric to pinned_tag_keys (invocation-log attribution): which PROXY
-- dimension keys an admin has chosen to surface as top-bar attribute filters.
-- Kept as a separate list so switching attribution source doesn't lose the
-- other source's selection.
ALTER TABLE user_preferences
    ADD COLUMN IF NOT EXISTS pinned_proxy_keys TEXT[] NOT NULL DEFAULT '{}';
