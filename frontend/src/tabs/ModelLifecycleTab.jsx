// Model Lifecycle tab — shows which Bedrock models in the customer's
// portfolio are LEGACY, in extended access, or past EOL, plus the live
// usage on each so the customer knows where to focus migration work.
// ACTIVE models are deliberately absent: they need no migration work, and
// listing them buried the ones that do.
//
// Data is 100% live from AWS:
//   - lifecycle status + dates: bedrock:ListFoundationModels (refreshed
//     by the model_lifecycle ingester into dim_model_lifecycle)
//   - usage / drill-down:       this dashboard's f_daily fact table
// No bundled JSON, no scrape. The only product opinion is the
// recommended-upgrade map, kept in backend/app/routers/model_lifecycle.py.
//
// TWO LIFECYCLE POLICIES. Models launched on Bedrock before 2026-09-07 follow
// the legacy policy and may get a public-extended-access phase; models
// launched on or after it follow the current policy, which has NO
// extended-access phase and a Legacy period of either 6 months or 45 days.
// Hence the Policy and "Notice given" columns, and why Extended access reads
// "n/a" (not "—") for current-policy models — absent and not-applicable are
// different facts when you're planning a migration.
//
// Rows survive EOL. AWS removes a model from the API once it dies, so the
// ingester retains the row (api_visible = FALSE) and the EOL cell shows
// "removed from API": any traffic on such a row is failing requests.
//
// Three sections:
//   1. KPI ribbon — total tracked, in-use, critical count
//   2. Timeline — top 8 in-use models, with today's date marker
//   3. Table — every legacy/EOL model in the customer's portfolio with
//              expandable per-account drill-down + CSV download

import { useMemo, useState } from 'react';
import {
  SpaceBetween, Container, Header, Box, ColumnLayout,
  StatusIndicator, SegmentedControl,
} from '@cloudscape-design/components';
import { useApi, fmt } from '../api.js';
import { ChartLoading, KpiCard, SectionHeader } from '../components/Common.jsx';
import LifecycleTimeline from '../components/LifecycleTimeline.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';

const SEV_LABEL = {
  critical: 'Critical',
  warning:  'Warning',
  info:     'Info',
};

function SeverityBadge({ severity }) {
  const type = severity === 'critical' ? 'error'
             : severity === 'warning'  ? 'warning'
             : 'info';
  return <StatusIndicator type={type}>{SEV_LABEL[severity] || severity}</StatusIndicator>;
}

// Which lifecycle policy governs a model, derived by the ingester from its
// Bedrock launch date (before / on-or-after 2026-09-07). Rendered as plain
// text, not a StatusIndicator — neither policy is a problem in itself, and a
// coloured icon here would compete with the Severity column that IS the signal.
const POLICY_LABEL = {
  legacy:  'Legacy policy',
  current: 'Current policy',
};

function PolicyBadge({ policy }) {
  if (!policy) {
    // No startOfLifeTime from the API, so the regime is genuinely unknown.
    // Say so rather than defaulting to one — the notice period a customer
    // should expect differs between the two.
    return <Box color="text-body-secondary"><i>Unknown</i></Box>;
  }
  return (
    <Box>
      <Box>{POLICY_LABEL[policy] || policy}</Box>
      <Box color="text-body-secondary" fontSize="body-s">
        {policy === 'current' ? 'no extended access' : 'may have extended access'}
      </Box>
    </Box>
  );
}

export default function ModelLifecycleTab({ filters, onInfo }) {
  // Lifecycle is endpoint-agnostic: model status (Legacy / EOL / etc.) is a
  // property of the model identity, not how it's invoked. No runtime/mantle
  // switcher — it would only ever show identical numbers.
  const filtersAll = useMemo(() => ({ ...filters, endpoint: 'all' }), [filters]);
  return <ModelLifecycleBody filters={filtersAll} onInfo={onInfo} />;
}

function ModelLifecycleBody({ filters, onInfo }) {
  const { data, loading, error } = useApi('/model-lifecycle', filters,
    [filters.start_date, filters.end_date,
     (filters.accounts || []).join(',')]);

  const models     = data?.models || [];
  const meta       = data?.meta || {};
  const inUse      = useMemo(() => models.filter(m => m.total_requests > 0), [models]);
  const criticalCount = useMemo(
    () => models.filter(m => m.severity === 'critical').length, [models]);
  const top8       = useMemo(() => inUse.slice(0, 8), [inUse]);

  // Default the table to models the fleet is ACTUALLY using — otherwise most
  // rows expand to "no usage in window", which is noise. A toggle exposes the
  // full catalog (a model going legacy that you don't use yet can still be
  // worth knowing). Default 'in-use' per user feedback.
  const [scope, setScope] = useState('in-use');
  const tableItems = useMemo(
    () => (scope === 'in-use' ? inUse : models),
    [scope, inUse, models]);

  if (error) {
    return (
      <Container header={<Header variant="h2">Model Lifecycle</Header>}>
        <Box color="text-status-error">Failed to load: {String(error)}</Box>
      </Container>
    );
  }

  const lastRefresh = meta.refreshed_at
    ? new Date(meta.refreshed_at).toLocaleString(undefined,
        { dateStyle: 'medium', timeStyle: 'short' })
    : '—';

  const columnDefinitions = [
    {
      id: 'severity', header: 'Severity', minWidth: 110,
      cell: (item) => <SeverityBadge severity={item.severity} />,
    },
    {
      id: 'model', header: 'Model', minWidth: 260,
      cell: (item) => (
        <Box>
          <Box>{item.public_name || item.modelId}</Box>
          <Box color="text-body-secondary" fontSize="body-s">
            <code>{item.modelId}</code>
          </Box>
        </Box>
      ),
    },
    {
      id: 'provider', header: 'Provider', minWidth: 90,
      cell: (item) => item.provider || '—',
    },
    {
      id: 'lifecycle_policy', header: 'Policy', minWidth: 110,
      cell: (item) => <PolicyBadge policy={item.lifecycle_policy} />,
      exportValue: (item) => item.lifecycle_policy || 'unknown',
    },
    {
      id: 'notice_period_label', header: 'Notice given', minWidth: 110,
      // How much warning this model actually gave (EOL − Legacy). Under the
      // current policy this can be as little as 45 days, so flag that: it is
      // the difference between a comfortable migration and a scramble.
      cell: (item) => {
        if (!item.notice_period_label) return <Box color="text-body-secondary">—</Box>;
        return item.notice_period_days <= 60
          ? <StatusIndicator type="warning">{item.notice_period_label}</StatusIndicator>
          : item.notice_period_label;
      },
      exportValue: (item) => item.notice_period_label || '',
    },
    {
      id: 'legacy_date', header: 'Legacy date', minWidth: 110,
      cell: (item) => item.legacy_date || '—',
    },
    {
      id: 'extended_access_date', header: 'Extended access', minWidth: 130,
      // Absent and not-applicable are different facts. Current-policy models
      // have no extended-access phase at all, so '—' would wrongly imply AWS
      // simply hasn't published a date yet.
      cell: (item) => item.lifecycle_policy === 'current'
        ? <Box color="text-body-secondary" fontSize="body-s"><i>n/a</i></Box>
        : (item.extended_access_date || '—'),
      exportValue: (item) => item.lifecycle_policy === 'current'
        ? 'n/a' : (item.extended_access_date || ''),
    },
    {
      id: 'eol_date', header: 'EOL date', minWidth: 140,
      cell: (item) => (
        <Box>
          <Box>{item.eol_date || '—'}</Box>
          {item.removed_from_api && (
            <Box color="text-status-error" fontSize="body-s">
              removed from API
            </Box>
          )}
        </Box>
      ),
      exportValue: (item) => item.eol_date || '',
    },
    {
      id: 'unique_accounts', header: 'Accounts', minWidth: 80,
      cell: (item) => fmt(item.unique_accounts),
    },
    {
      id: 'total_requests', header: 'Requests (window)', minWidth: 130,
      cell: (item) => fmt(item.total_requests),
    },
    {
      id: 'legacy_invocations', header: 'Legacy calls', minWidth: 120,
      // A legacy model that is still being actively invoked is the highest-
      // urgency migration signal. Flag >0 with a warning; render 0 plainly.
      cell: (item) => Number(item.legacy_invocations) > 0
        ? <StatusIndicator type="warning">{fmt(item.legacy_invocations)}</StatusIndicator>
        : fmt(item.legacy_invocations || 0),
      exportValue: (item) => item.legacy_invocations || 0,
    },
    {
      id: 'last_accessed', header: 'Last accessed', minWidth: 110,
      cell: (item) => item.last_accessed || '—',
    },
    {
      id: 'recommended_upgrade', header: 'Recommended upgrade', minWidth: 280,
      cell: (item) => item.recommended_upgrade || (
        <Box color="text-body-secondary"><i>Consult model provider</i></Box>
      ),
    },
  ];

  const renderRowDetail = (item) => {
    if (!item.accounts_detail || item.accounts_detail.length === 0) {
      return (
        <Box color="text-body-secondary" padding={{ vertical: 's' }}>
          No usage of <code>{item.modelId}</code> in the selected window.
          Once usage starts, account-level breakdown will appear here.
        </Box>
      );
    }
    return (
      <Box padding={{ vertical: 's' }}>
        <Header variant="h3">Accounts using this model</Header>
        <PaginatedTable
          variant="embedded"
          pageSize={5}
          items={item.accounts_detail}
          empty="No accounts"
          searchPlaceholder="Search accounts…"
          columnDefinitions={[
            { id: 'accountId', header: 'Account ID', cell: r => <code>{r.accountId}</code> },
            { id: 'requests',  header: 'Requests',   cell: r => fmt(r.total_requests) },
            { id: 'regions',   header: 'Regions',    cell: r => (r.regions || []).join(', ') || '—' },
            { id: 'last',      header: 'Last accessed', cell: r => r.last_accessed || '—' },
          ]}
        />
      </Box>
    );
  };

  // No outer ContentLayout — the AppShell already wraps every tab in one.
  // Nesting ContentLayouts double-pads the top, which produced a visible
  // dead band between the FilterBar and this tab's KPI ribbon.
  return (
    <SpaceBetween size="m">
        {/* KPI ribbon ------------------------------------------------ */}
        {/* Three big KPI tiles + a smaller "data freshness" stamp.
            The freshness value is a long datetime string — rendering it
            with the display-l font that the count tiles use produced an
            absurd 4-line wrap. Drop it into its own slim tile. */}
        <ColumnLayout columns={4} variant="text-grid">
          <KpiCard title="Legacy or EOL models in your portfolio" value={fmt(models.length)} />
          <KpiCard title="Currently in use" value={fmt(inUse.length)} />
          {/* Was labelled "Past extended access" but has always counted every
              critical row — past EOL, EOL within 30 days, and past extended
              access. Label now matches what it computes. Current-policy
              models have no extended-access phase, so the old label was
              doubly wrong for them. */}
          <KpiCard title="Needs action now (critical)" value={fmt(criticalCount)} />
          <Container>
            <Box variant="awsui-key-label">Lifecycle data refreshed</Box>
            <Box variant="h3">{lastRefresh}</Box>
          </Container>
        </ColumnLayout>

        {/* Timeline -------------------------------------------------- */}
        <Container header={
          <SectionHeader
            title="Lifecycle timeline — top 8 in use"
            description="Each band runs from a model's Legacy date to its EOL date. The vertical line is today."
            sectionId="lifecycle-timeline"
            onInfo={onInfo}
          />
        }>
          {loading ? <ChartLoading height={300} />
            : top8.length === 0
              ? <Box color="text-body-secondary" textAlign="center" padding="l">
                  No legacy or EOL models are currently in use in this window. <br />
                  The table below still lists every model AWS has scheduled so
                  you can monitor proactively.
                </Box>
              : <LifecycleTimeline alerts={top8.map(m => ({
                  modelId: m.public_name || m.modelId,
                  severity: m.severity,
                  legacy_date: m.legacy_date,
                  // Never draw an extended-access marker for a current-policy
                  // model: that phase doesn't exist under the current policy,
                  // and a marker would imply a grace period it won't get.
                  extended_access_date: m.lifecycle_policy === 'current'
                    ? null : m.extended_access_date,
                  eol_date: m.eol_date,
                }))} />
          }
        </Container>

        {/* Table ----------------------------------------------------- */}
        <Container header={
          <SectionHeader
            title={`Legacy & EOL models (${tableItems.length})`}
            description="Click a row to see which accounts are using each model."
            sectionId="lifecycle-table"
            onInfo={onInfo}
            actions={
              <SegmentedControl
                selectedId={scope}
                onChange={({ detail }) => setScope(detail.selectedId)}
                label="Scope"
                options={[
                  { id: 'in-use', text: `In use (${inUse.length})` },
                  { id: 'all',    text: `All tracked (${models.length})` },
                ]}
              />
            }
          />
        }>
          {loading ? <ChartLoading height={200} />
            : <PaginatedTable
                items={tableItems}
                pageSize={25}
                downloadFileName="model-lifecycle.csv"
                trackBy="modelId"
                renderRowDetail={renderRowDetail}
                empty={scope === 'in-use'
                  ? 'No legacy or EOL models in active use in this window. Switch to "All tracked" to see every one AWS has scheduled.'
                  : 'No legacy or EOL models in your portfolio. Nothing to migrate.'}
                searchPlaceholder="Search by model id, name, provider…"
                columnDefinitions={columnDefinitions}
              />
          }
        </Container>
    </SpaceBetween>
  );
}
