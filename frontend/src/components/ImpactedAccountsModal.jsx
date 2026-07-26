// "Accounts impacted" drill-down modal — opened from a chart bar's popover
// on the Errors and Latency tabs ("Drill accounts" link). Lists every
// (account, model, region) with traffic in the clicked hour/day bucket,
// with per-class error counts and average latency.
//
// Source: f_hourly_status (invocation logs) — the only account × hour grain
// in the store. When invocation logging is off the modal explains that
// instead of rendering an empty table.
import { useEffect, useState } from 'react';
import { Modal, Box, SpaceBetween, Alert } from '@cloudscape-design/components';
import { api, fmt, accountName, useAccountNames } from '../api.js';
import { ChartLoading } from './Common.jsx';
import PaginatedTable from './PaginatedTable.jsx';

function fmtMs(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  if (Number.isNaN(n)) return '—';
  return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${Math.round(n)}ms`;
}

export default function ImpactedAccountsModal({ ts, scope = 'hour', filters, onDismiss }) {
  useAccountNames();
  const [state, setState] = useState({ loading: true, rows: null, error: '' });

  useEffect(() => {
    if (!ts) return;
    let cancelled = false;
    setState({ loading: true, rows: null, error: '' });
    api('/impacted-accounts', { ...filters, ts, scope }, { useCache: false })
      .then(d => { if (!cancelled) setState({ loading: false, rows: d.rows || [], error: '' }); })
      .catch(e => { if (!cancelled) setState({ loading: false, rows: [], error: String(e.message || e) }); });
    return () => { cancelled = true; };
  }, [ts, scope, JSON.stringify(filters)]);

  if (!ts) return null;
  const when = new Date(ts);
  const label = scope === 'hour'
    ? `${when.toLocaleDateString(undefined, { month: 'numeric', day: 'numeric' })} ${String(when.getHours()).padStart(2, '0')}:00 (hour, UTC)`
    : when.toLocaleDateString(undefined, { month: 'numeric', day: 'numeric', year: 'numeric' });

  return (
    <Modal visible size="max" onDismiss={onDismiss}
           header={`Accounts impacted — ${label}`}>
      <SpaceBetween size="s">
        {state.loading ? <ChartLoading height={200} label="Loading impacted accounts…" /> :
         state.error ? <Alert type="error">{state.error}</Alert> :
         state.rows.length === 0 ? (
          <Alert type="info" header="No account-level detail for this bucket">
            Account-level impact comes from Bedrock Model Invocation Logs.
            Either logging isn't enabled, or no logged requests fall in this
            bucket under the current filters.
          </Alert>
         ) : (
          <PaginatedTable
            items={state.rows}
            pageSize={12}
            trackBy={(r) => `${r.accountid || r.accountId}|${r.modelid || r.modelId}|${r.region}`}
            downloadFileName={`impacted-accounts-${ts}.csv`}
            columnDefinitions={[
              { id: 'a', header: 'Account ID', cell: r => r.accountid || r.accountId, exportValue: r => r.accountid || r.accountId },
              { id: 'an', header: 'Account name', cell: r => accountName(r.accountid || r.accountId) || '—', exportValue: r => accountName(r.accountid || r.accountId) },
              { id: 'm',   header: 'Model',      cell: r => r.modelid || r.modelId, exportValue: r => r.modelid || r.modelId },
              { id: 'r',   header: 'Region',     cell: r => r.region, exportValue: r => r.region },
              { id: 'req', header: 'Requests',   cell: r => fmt(r.total_requests), exportValue: r => r.total_requests },
              { id: 'thr', header: 'Throttled (429)', cell: r => fmt(r.throttled), exportValue: r => r.throttled },
              { id: 'x4',  header: 'Other 4xx',  cell: r => fmt(r.other_4xx), exportValue: r => r.other_4xx },
              { id: 'x5',  header: '5xx',        cell: r => fmt(r.errors_5xx), exportValue: r => r.errors_5xx },
              { id: 'lat', header: 'Avg latency', cell: r => fmtMs(r.avg_latency_ms), exportValue: r => r.avg_latency_ms },
            ]}
            empty="No impacted accounts."
          />
        )}
        <Box variant="small" color="text-body-secondary">
          Derived from Bedrock Model Invocation Logs (per-request grain).
          Sorted by throttles + 5xx, worst first. Respects the top-bar filters.
        </Box>
      </SpaceBetween>
    </Modal>
  );
}
