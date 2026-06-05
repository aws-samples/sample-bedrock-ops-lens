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

import { useMemo } from 'react';
import {
  SpaceBetween, Container, Header, Box, ColumnLayout,
  StatusIndicator, Button,
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

// Build a CSV including per-account drill-down rows. One header row,
// one row per (model, account) plus a "summary" row per model where
// the account fields are blank — so re-importing into a spreadsheet
// keeps both totals and detail visible.
function buildCsv(models) {
  const headers = [
    'severity', 'modelId', 'public_name', 'provider',
    'legacy_date', 'extended_access_date', 'eol_date',
    'recommended_upgrade', 'regions',
    'total_requests', 'unique_accounts', 'last_accessed',
    'detail_accountId', 'detail_requests', 'detail_regions', 'detail_last_accessed',
  ];
  const esc = (v) => {
    if (v === null || v === undefined) return '';
    const s = String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [headers.join(',')];
  for (const m of models) {
    const baseFields = [
      m.severity, m.modelId, m.public_name, m.provider,
      m.legacy_date, m.extended_access_date, m.eol_date,
      m.recommended_upgrade, (m.regions || []).join(';'),
      m.total_requests, m.unique_accounts, m.last_accessed,
    ];
    if (!m.accounts_detail || m.accounts_detail.length === 0) {
      lines.push([...baseFields, '', '', '', ''].map(esc).join(','));
      continue;
    }
    for (const d of m.accounts_detail) {
      lines.push([
        ...baseFields,
        d.accountId, d.total_requests,
        (d.regions || []).join(';'), d.last_accessed,
      ].map(esc).join(','));
    }
  }
  return lines.join('\n');
}

function downloadCsv(filename, text) {
  const blob = new Blob([text], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export default function ModelLifecycleTab({ filters, onInfo }) {
  const { data, loading, error } = useApi('/model-lifecycle', filters,
    [filters.start_date, filters.end_date,
     (filters.accounts || []).join(',')]);

  const models     = data?.models || [];
  const meta       = data?.meta || {};
  const inUse      = useMemo(() => models.filter(m => m.total_requests > 0), [models]);
  const pastEol    = useMemo(() => models.filter(m => m.severity === 'critical'), [models]);
  const top8       = useMemo(() => inUse.slice(0, 8), [inUse]);

  // Show all legacy models in the table — even ones with 0 requests in the
  // window, since "this model is dying and you might not know yet" is exactly
  // the kind of hint the tab is here to deliver.
  const tableItems = models;

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
            title={`Legacy models${models.length ? ` (${models.length})` : ''}`}
            description="Click a row to see which accounts are using each model."
            sectionId="lifecycle-table"
            onInfo={onInfo}
            actions={
              <Button
                iconName="download"
                disabled={models.length === 0}
                onClick={() => {
                  const csv = buildCsv(models);
                  downloadCsv(`model-lifecycle-${new Date().toISOString().slice(0,10)}.csv`, csv);
                }}
              >
                Download CSV
              </Button>
            }
          />
        }>
          {loading ? <ChartLoading height={200} />
            : <PaginatedTable
                items={tableItems}
                pageSize={25}
                trackBy="modelId"
                renderRowDetail={renderRowDetail}
                empty="No legacy models in your portfolio. Nothing to migrate."
                searchPlaceholder="Search by model id, name, provider…"
                columnDefinitions={columnDefinitions}
              />
          }
        </Container>
    </SpaceBetween>
  );
}
