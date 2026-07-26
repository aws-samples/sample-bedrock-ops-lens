-- 008: account-name dimension.
--
-- Human-friendly names for the 12-digit accountIds shown all over the UI.
-- Resolution chain (ingestion/accounts.py resolve_account_names):
--   1. config.yaml `account_names` map  (source='config' — always wins)
--   2. organizations:ListAccounts Name  (source='org' — discover-org mode)
--   3. account:GetAccountInformation    (source='account_api' — per-account
--      via the reader role; works without Organizations)
--   4. unresolved → no row (UI shows the bare ID)
-- Refreshed on every ingester run; UI resolves names client-side via
-- /api/accounts (no per-table SQL joins).

CREATE TABLE IF NOT EXISTS dim_account (
    accountId    TEXT PRIMARY KEY,
    account_name TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT '',   -- config | org | account_api
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
