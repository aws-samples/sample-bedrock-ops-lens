// Admin editor for the output-token burndown rate catalog.
//
// Why this screen exists: the multipliers used to be hardcoded in Python, so a
// rate change from AWS meant editing code, rebuilding the image and
// redeploying. Until someone did that, every quota number that relied on the
// reconstruction path was wrong — by up to 10x on the output portion, which is
// the difference between "healthy" and "throttling".
//
// Two things this screen is careful about:
//   * It states plainly that AWS's own EstimatedTPMQuotaUsage wins wherever a
//     datapoint exists, so nobody thinks these rates drive every number.
//   * "Verified on" (the date WE checked the doc) and "Effective from" (the date
//     AWS's policy started) are separate columns. Conflating them would let an
//     edit made today silently re-rate last month's charts.
import { useEffect, useState } from 'react';
import {
  Container, Header, SpaceBetween, Box, Button, Alert, Table, Input,
  FormField, Link, StatusIndicator, Textarea, ExpandableSection, Badge,
} from '@cloudscape-design/components';
import { api, apiSend, clearCache } from '../api.js';

const BLANK = {
  id: '', label: '', all_of: [], rate: 1, endpoint: 'runtime',
  effective_from: null, verified_on: null, source_url: null,
};

export default function BurndownRateSettings({ isAdmin, onInfo }) {
  const [data, setData]       = useState(null);
  const [rows, setRows]       = useState([]);
  const [err, setErr]         = useState(null);
  const [ok, setOk]           = useState(null);
  const [busy, setBusy]       = useState(false);
  const [jsonMode, setJson]   = useState(false);
  const [jsonText, setText]   = useState('');
  const [reload, setReload]   = useState(0);

  useEffect(() => {
    let alive = true;
    api('/burndown-rates', {}, { useCache: false })
      .then(d => { if (!alive) return; setData(d); setRows(d.entries || []); setErr(null); })
      .catch(e => { if (alive) setErr(e.message || String(e)); });
    return () => { alive = false; };
  }, [reload]);

  const edit = (i, field, value) =>
    setRows(rows.map((r, n) => (n === i ? { ...r, [field]: value } : r)));

  const save = async (entries) => {
    setBusy(true); setErr(null); setOk(null);
    try {
      const res = await apiSend('/burndown-rates', { method: 'PUT', body: { entries } });
      // The dashboard's 60s response cache holds numbers computed with the OLD
      // multipliers. Without clearing it the admin would save successfully and
      // watch nothing change, then reasonably conclude the feature is broken.
      clearCache();
      setOk(`Saved — revision ${res.revision}. Charts recalculate on next load.`);
      setReload(n => n + 1);
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  const seed = async () => {
    setBusy(true); setErr(null); setOk(null);
    try {
      const res = await apiSend('/burndown-rates/seed', { method: 'POST', body: {} });
      clearCache();
      setOk(`Restored the bundled AWS-documented rates (revision ${res.revision}).`);
      setReload(n => n + 1);
    } catch (e) {
      setErr(e.message || String(e));
    } finally { setBusy(false); }
  };

  if (err && !data) return <Alert type="error" header="Could not load rates">{err}</Alert>;
  if (!data) return <Box padding="s">Loading rates…</Box>;

  const stale = data.stale;
  const unmapped = data.unmapped_models || [];

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="Output tokens consume more than one quota token on some models. Edit here — no redeploy needed."
          info={onInfo ? <Link variant="info" onFollow={onInfo}>Info</Link> : undefined}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => { setJson(!jsonMode); setText(JSON.stringify(rows, null, 2)); }}>
                {jsonMode ? 'Table view' : 'JSON import/export'}
              </Button>
              {isAdmin && <Button onClick={seed} loading={busy}>Restore AWS defaults</Button>}
              {isAdmin && (
                <Button variant="primary" loading={busy}
                        onClick={() => save(jsonMode ? safeParse(jsonText, setErr) : rows)}>
                  Save
                </Button>
              )}
            </SpaceBetween>
          }
        >
          Quota burndown rates
        </Header>
      }
    >
      <SpaceBetween size="m">
        {ok  && <Alert type="success" dismissible onDismiss={() => setOk(null)}>{ok}</Alert>}
        {err && <Alert type="error" dismissible onDismiss={() => setErr(null)}>{err}</Alert>}

        <Alert type="info" header="These rates are the fallback, not the primary source">
          {data.note}{' '}
          <Link external href={data.doc_url}>AWS token-burndown documentation</Link>
        </Alert>

        {stale && (
          <Alert type="warning" header="Showing the last known catalog">
            The stored catalog could not be read on the last refresh, so these are
            cached values. Reload to retry.
          </Alert>
        )}

        {data.seeded_from_bundled && (
          <Alert type="warning" header="Nothing saved yet">
            These are the bundled values compiled into this build. Save (or use
            “Restore AWS defaults”) to store them so they can be edited later.
          </Alert>
        )}

        {unmapped.length > 0 && (
          <Alert type="warning" header={`${unmapped.length} model(s) with traffic have no verified rate`}>
            <Box variant="p">
              These models are being charged a multiplier from the bundled default
              rather than a verified catalog entry. If AWS has published a rate for
              them, add it below.
            </Box>
            <Box variant="code">{unmapped.join(', ')}</Box>
          </Alert>
        )}

        <Box variant="small" color="text-body-secondary">
          Revision <b>{String(data.revision)}</b>
          {data.updated_at ? ` · last saved ${new Date(data.updated_at).toLocaleString()}` : ''}
          {' · '}changes apply to the backend and the scheduled findings job within 60 seconds.
        </Box>

        {jsonMode ? (
          <FormField label="Catalog JSON"
                     description="An array of entries. Paste to import; copy to export.">
            <Textarea rows={16} value={jsonText} onChange={e => setText(e.detail.value)}
                      disabled={!isAdmin} />
          </FormField>
        ) : (
          <Table
            variant="embedded"
            items={rows}
            empty={<Box padding="s">No entries. “Restore AWS defaults” seeds the documented rates.</Box>}
            columnDefinitions={[
              {
                id: 'label', header: 'Model', minWidth: 200,
                cell: (r) => isAdmin
                  ? <Input value={r.label || ''} placeholder="Anthropic Claude Opus 4.8"
                           onChange={e => edit(rows.indexOf(r), 'label', e.detail.value)} />
                  : (r.label || r.id),
              },
              {
                id: 'all_of', header: 'Match tokens', minWidth: 180,
                cell: (r) => isAdmin
                  ? <Input value={(r.all_of || []).join(', ')} placeholder="claude, opus5"
                           onChange={e => edit(rows.indexOf(r), 'all_of',
                             e.detail.value.split(',').map(s => s.trim()).filter(Boolean))} />
                  : (r.all_of || []).join(', '),
              },
              {
                id: 'rate', header: 'Output ×', width: 110,
                cell: (r) => isAdmin
                  ? <Input type="number" value={String(r.rate ?? '')}
                           onChange={e => edit(rows.indexOf(r), 'rate', Number(e.detail.value))} />
                  : `${r.rate}×`,
              },
              {
                id: 'effective_from', header: 'Effective from', width: 150,
                cell: (r) => isAdmin
                  ? <Input value={r.effective_from || ''} placeholder="YYYY-MM-DD (or blank)"
                           onChange={e => edit(rows.indexOf(r), 'effective_from', e.detail.value || null)} />
                  : (r.effective_from || 'always'),
              },
              {
                id: 'verified_on', header: 'Doc verified', width: 140,
                cell: (r) => isAdmin
                  ? <Input value={r.verified_on || ''} placeholder="YYYY-MM-DD"
                           onChange={e => edit(rows.indexOf(r), 'verified_on', e.detail.value || null)} />
                  : (r.verified_on
                      ? <StatusIndicator type="success">{r.verified_on}</StatusIndicator>
                      : <StatusIndicator type="warning">unverified</StatusIndicator>),
              },
              {
                id: 'endpoint', header: 'Endpoint', width: 110,
                cell: (r) => <Badge>{r.endpoint || 'runtime'}</Badge>,
              },
              // Needs a real width: with an empty header and width 90 the column
              // collapsed and Cloudscape wrapped "Remove" to one letter per line.
              ...(isAdmin ? [{
                id: 'rm', header: 'Actions', width: 130, minWidth: 120,
                cell: (r) => <Button variant="inline-link" iconName="remove"
                                     ariaLabel={`Remove ${r.label || r.id}`}
                                     onClick={() => setRows(rows.filter(x => x !== r))}>Remove</Button>,
              }] : []),
            ]}
            footer={isAdmin
              ? <Button onClick={() => setRows([...rows, { ...BLANK }])}>Add rate</Button>
              : undefined}
          />
        )}

        <ExpandableSection headerText="How a rate is chosen">
          <Box variant="p">
            For each hour of traffic: if AWS published an{' '}
            <b>EstimatedTPMQuotaUsage</b> datapoint, that value is used as-is —
            it already includes cache-write tokens and the output multiplier, so
            no rate is applied. Only when the datapoint is absent is consumption
            reconstructed as{' '}
            <Box variant="code" display="inline">
              uncached input + cache-write + output × rate
            </Box>
            . Cache reads never count. Per-workload attribution from proxy
            telemetry always reconstructs, because a model-level CloudWatch
            aggregate carries no workload dimension.
          </Box>
          <Box variant="p">
            Entries match on <b>all</b> of their tokens against a
            separator-stripped model id and name, so <Box variant="code" display="inline">gpt56sol</Box>{' '}
            matches both <Box variant="code" display="inline">openai.gpt-5-6-sol</Box> and
            “GPT-5.6 Sol”. Where several entries apply, the one with the latest
            effective date that is not in the future wins. Models with no entry
            fall back to the bundled table and are labelled unverified.
          </Box>
        </ExpandableSection>
      </SpaceBetween>
    </Container>
  );
}

function safeParse(text, setErr) {
  try {
    const v = JSON.parse(text);
    if (!Array.isArray(v)) throw new Error('JSON must be an array of entries');
    return v;
  } catch (e) {
    setErr(`Invalid JSON: ${e.message}`);
    return null;
  }
}
