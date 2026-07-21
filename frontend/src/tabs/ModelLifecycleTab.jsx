// Model Lifecycle tab — shows which Bedrock models in the customer's
// portfolio are LEGACY, in extended access, or past EOL, plus the live
// usage on each so the customer knows where to focus migration work.
//
// Data is 100% live from AWS:
//   - lifecycle status + dates: bedrock:ListFoundationModels (refreshed
//     by the model_lifecycle ingester into dim_model_lifecycle)
//   - usage / drill-down:       this dashboard's f_daily fact table
// No bundled JSON, no scrape. The only product opinion is the
// recommended-upgrade map, kept in backend/app/routers/model_lifecycle.py.
//
// Three sections:
//   1. KPI ribbon — total legacy, in-use legacy, past-EOL count
//   2. Timeline — top 8 in-use legacy models, with today's date marker
//   3. Table — every legacy model in the customer's portfolio with
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
  const pastEol    = useMemo(() => models.filter(m => m.severity === 'critical'), [models]);
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
      id: 'legacy_date', header: 'Legacy date', minWidth: 110,
      cell: (item) => item.legacy_date || '—',
    },
    {
      id: 'extended_access_date', header: 'Extended access', minWidth: 130,
      cell: (item) => item.extended_access_date || '—',
    },
    {
      id: 'eol_date', header: 'EOL date', minWidth: 110,
      cell: (item) => item.eol_date || '—',
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
          <KpiCard title="Legacy models in your portfolio" value={fmt(models.length)} />
          <KpiCard title="Currently in use" value={fmt(inUse.length)} />
          <KpiCard title="Past extended access (critical)" value={fmt(pastEol.length)} />
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
                  No legacy models are currently in use in this window. <br />
                  The table below still lists every legacy model so you can
                  monitor proactively.
                </Box>
              : <LifecycleTimeline alerts={top8.map(m => ({
                  modelId: m.public_name || m.modelId,
                  severity: m.severity,
                  legacy_date: m.legacy_date,
                  extended_access_date: m.extended_access_date,
                  eol_date: m.eol_date,
                }))} />
          }
        </Container>

        {/* Table ----------------------------------------------------- */}
        <Container header={
          <SectionHeader
            title={`Legacy models (${tableItems.length})`}
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
                  { id: 'all',    text: `All legacy (${models.length})` },
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
                  ? 'No legacy models in active use in this window. Switch to "All legacy" to see the full catalog.'
                  : 'No legacy models in your portfolio. Nothing to migrate.'}
                searchPlaceholder="Search by model id, name, provider…"
                columnDefinitions={columnDefinitions}
              />
          }
        </Container>
    </SpaceBetween>
  );
}
