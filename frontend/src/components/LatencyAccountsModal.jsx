// Per-account latency drill for one model (Latency tab → "Drill accounts").
// Which accounts experienced what latency on the clicked model, over the
// current filter window. Invocation-log derived (avg only — percentiles
// aren't additive across the hourly aggregate).
import { useEffect, useState } from 'react';
import { Modal, Box, SpaceBetween, Alert } from '@cloudscape-design/components';
import { api, fmt, fmtAccount, useAccountNames } from '../api.js';
import { ChartLoading } from './Common.jsx';
import PaginatedTable from './PaginatedTable.jsx';

function fmtMs(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  if (Number.isNaN(n)) return '—';
  return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${Math.round(n)}ms`;
}

export default function LatencyAccountsModal({ modelId, filters, onDismiss }) {
  useAccountNames();
  const [state, setState] = useState({ loading: true, rows: null, error: '' });

  useEffect(() => {
    if (!modelId) return;
    let cancelled = false;
    setState({ loading: true, rows: null, error: '' });
    api('/latency-impacted-accounts', { ...filters, model_id: modelId }, { useCache: false })
      .then(d => { if (!cancelled) setState({ loading: false, rows: d.rows || [], error: '' }); })
      .catch(e => { if (!cancelled) setState({ loading: false, rows: [], error: String(e.message || e) }); });
    return () => { cancelled = true; };
  }, [modelId, JSON.stringify(filters)]);

  if (!modelId) return null;
  return (
    <Modal visible size="large" onDismiss={onDismiss}
           header={`Latency by account — ${modelId}`}>
      <SpaceBetween size="s">
        {state.loading ? <ChartLoading height={200} label="Loading per-account latency…" /> :
         state.error ? <Alert type="error">{state.error}</Alert> :
         state.rows.length === 0 ? (
          <Alert type="info" header="No account-level latency for this model">
            Per-account latency comes from Bedrock Model Invocation Logs.
            Either logging isn't enabled, or no logged requests for this model
            fall in the current window/filters.
          </Alert>
         ) : (
          <PaginatedTable
            items={state.rows}
            pageSize={12}
            trackBy={(r) => `${r.accountid || r.accountId}|${r.region}`}
            downloadFileName={`latency-accounts-${modelId}.csv`}
            columnDefinitions={[
              { id: 'a',   header: 'Account ID',  cell: r => fmtAccount(r.accountid || r.accountId), exportValue: r => r.accountid || r.accountId },
              { id: 'r',   header: 'Region',      cell: r => r.region, exportValue: r => r.region },
              { id: 'lat', header: 'Avg latency', cell: r => fmtMs(r.avg_latency_ms), exportValue: r => r.avg_latency_ms },
              { id: 'n',   header: 'Samples',     cell: r => fmt(r.latency_samples), exportValue: r => r.latency_samples },
              { id: 'req', header: 'Requests',    cell: r => fmt(r.total_requests), exportValue: r => r.total_requests },
              { id: 'thr', header: 'Throttled',   cell: r => fmt(r.throttled), exportValue: r => r.throttled },
              { id: 'x5',  header: '5xx',         cell: r => fmt(r.errors_5xx), exportValue: r => r.errors_5xx },
            ]}
            empty="No rows."
          />
        )}
        <Box variant="small" color="text-body-secondary">
          Derived from Bedrock Model Invocation Logs; sorted slowest first.
          Respects the top-bar filters and the current date window.
        </Box>
      </SpaceBetween>
    </Modal>
  );
}
